"""Passkeys as the second step of the sign-in (owner 2026-11-12). The browser's part cannot run here: the WebAuthn
verification itself is the webauthn library's (mocked where a real authenticator would answer); the flow around it -
challenges in the session, the credential kept, the sign-in finished, the attempts counted - is ours."""

import json
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import AuditLog, Kind, Passkey, User
from accounts.testkit import enrol


@override_settings(SITE_URL="https://portal.example.com")
class Passkeys(TestCase):
    def setUp(self):
        self.u = User.objects.create_user(
            "pk@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, email_verified=True, is_super_admin=True
        )
        enrol(self.u)

    def post_json(self, name, body=None, **kw):
        return self.client.post(reverse(name, **kw), json.dumps(body or {}), content_type="application/json")

    def test_register_a_passkey(self):
        self.client.force_login(self.u)
        r = self.post_json("passkey_register_options")
        o = r.json()
        self.assertEqual(o["rp"]["id"], "portal.example.com")  # bound to the portal's own domain
        self.assertEqual(o["user"]["name"], "pk@corp.kz")
        self.assertEqual(o["authenticatorSelection"]["userVerification"], "required")
        verified = SimpleNamespace(credential_id=b"\x01\x02\x03", credential_public_key=b"pub", sign_count=0)
        with mock.patch("webauthn.verify_registration_response", return_value=verified) as v:
            r = self.post_json(
                "passkey_register_verify",
                {"name": "MacBook", "credential": {"id": "AQID", "response": {"transports": ["internal"]}}},
            )
        self.assertTrue(r.json()["ok"])
        self.assertEqual(v.call_args.kwargs["expected_origin"], "https://portal.example.com")
        k = Passkey.objects.get(user=self.u)
        self.assertEqual(
            (k.name, k.credential_id, bytes(k.public_key), k.transports), ("MacBook", "AQID", b"pub", ["internal"])
        )
        self.assertTrue(AuditLog.objects.filter(action="passkey_added").exists())
        self.assertContains(self.client.get(reverse("profile")), "MacBook")
        # the challenge is used once
        self.assertEqual(self.post_json("passkey_register_verify", {"credential": {}}).status_code, 400)

    def test_a_garbage_registration_is_refused_by_the_library(self):
        self.client.force_login(self.u)
        self.post_json("passkey_register_options")
        r = self.post_json(
            "passkey_register_verify", {"credential": {"id": "x", "rawId": "x", "type": "public-key", "response": {}}}
        )
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Passkey.objects.exists())

    def _password_step(self):
        r = self.client.post(reverse("login"), {"username": "pk@corp.kz", "password": "Str0ng-PASS-123"})
        self.assertRedirects(r, reverse("login_otp"), fetch_redirect_response=False)

    def test_sign_in_with_a_passkey(self):
        Passkey.objects.create(user=self.u, name="Phone", credential_id="AQID", public_key=b"pub", sign_count=3)
        self._password_step()
        self.assertContains(self.client.get(reverse("login_otp")), "Use a passkey")
        o = self.post_json("passkey_auth_options").json()
        self.assertEqual([c["id"] for c in o["allowCredentials"]], ["AQID"])
        with mock.patch("webauthn.verify_authentication_response", return_value=SimpleNamespace(new_sign_count=4)) as v:
            r = self.post_json("passkey_auth_verify", {"credential": {"id": "AQID"}})
        self.assertTrue(r.json()["ok"])
        self.assertEqual(v.call_args.kwargs["credential_current_sign_count"], 3)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)  # signed in
        k = Passkey.objects.get()
        self.assertEqual(k.sign_count, 4)
        self.assertIsNotNone(k.last_used_at)

    def test_a_wrong_passkey_counts_as_a_wrong_attempt(self):
        Passkey.objects.create(user=self.u, name="Phone", credential_id="AQID", public_key=b"pub")
        self._password_step()
        for _ in range(4):
            self.post_json("passkey_auth_options")
            with mock.patch("webauthn.verify_authentication_response", side_effect=ValueError("bad signature")):
                self.assertEqual(self.post_json("passkey_auth_verify", {"credential": {"id": "AQID"}}).status_code, 400)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 302)  # not signed in
        self.post_json("passkey_auth_options")
        r = self.post_json("passkey_auth_verify", {"credential": {"id": "AQID"}})
        self.assertEqual(r.json()["next"], reverse("login"))  # the fifth: the sign-in starts again
        self.assertEqual(self.post_json("passkey_auth_options").status_code, 400)

    def test_no_passkey_step_without_the_password(self):
        Passkey.objects.create(user=self.u, name="Phone", credential_id="AQID", public_key=b"pub")
        self.assertEqual(self.post_json("passkey_auth_options").status_code, 400)
        self.assertEqual(self.post_json("passkey_auth_verify", {"credential": {"id": "AQID"}}).status_code, 400)

    def test_remove_only_your_own(self):
        other = User.objects.create_user("o@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, email_verified=True)
        mine = Passkey.objects.create(user=self.u, name="Mine", credential_id="AQ", public_key=b"p")
        theirs = Passkey.objects.create(user=other, name="Theirs", credential_id="Ag", public_key=b"p")
        self.client.force_login(self.u)
        self.assertEqual(self.client.post(reverse("passkey_remove", args=[theirs.pk])).status_code, 404)
        self.client.post(reverse("passkey_remove", args=[mine.pk]))
        self.assertEqual(list(Passkey.objects.values_list("name", flat=True)), ["Theirs"])


class SoftAuthenticator:
    """A software passkey (ES256, attestation "none"): what a phone or Touch ID sends, made here, so the real webauthn
    verification runs end to end with no mock."""

    def __init__(self, rp_id, origin):
        import os

        from cryptography.hazmat.primitives.asymmetric import ec

        self.rp_id, self.origin = rp_id, origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.count = 0

    @staticmethod
    def b64(b):
        from webauthn.helpers import bytes_to_base64url

        return bytes_to_base64url(b)

    def _auth_data(self, flags, extra=b""):
        import hashlib
        import struct

        return hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", self.count) + extra

    def _client_data(self, kind, challenge):
        return json.dumps({"type": kind, "challenge": challenge, "origin": self.origin, "crossOrigin": False}).encode()

    def create(self, options):
        import struct

        import cbor2

        nums = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})
        att = bytes(16) + struct.pack(">H", len(self.cred_id)) + self.cred_id + cose
        auth = self._auth_data(0x45, att)  # user present, user verified, attested data
        return {
            "id": self.b64(self.cred_id),
            "rawId": self.b64(self.cred_id),
            "type": "public-key",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": self.b64(self._client_data("webauthn.create", options["challenge"])),
                "attestationObject": self.b64(cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth})),
                "transports": ["internal"],
            },
        }

    def get(self, options):
        import hashlib

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec

        self.count += 1
        auth = self._auth_data(0x05)  # user present, user verified
        cdata = self._client_data("webauthn.get", options["challenge"])
        sig = self.key.sign(auth + hashlib.sha256(cdata).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            "id": self.b64(self.cred_id),
            "rawId": self.b64(self.cred_id),
            "type": "public-key",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": self.b64(cdata),
                "authenticatorData": self.b64(auth),
                "signature": self.b64(sig),
                "userHandle": None,
            },
        }


@override_settings(SITE_URL="https://portal.example.com")
class PasskeysEndToEnd(TestCase):
    def test_register_then_sign_in_with_real_signatures(self):
        u = User.objects.create_user("e2e@corp.kz", "Str0ng-PASS-123", kind=Kind.STAFF, email_verified=True)
        enrol(u)
        dev = SoftAuthenticator("portal.example.com", "https://portal.example.com")
        self.client.force_login(u)

        def post(name, body=None):
            return self.client.post(reverse(name), json.dumps(body or {}), content_type="application/json")

        o = post("passkey_register_options").json()
        r = post("passkey_register_verify", {"name": "Soft", "credential": dev.create(o)})
        self.assertTrue(r.json()["ok"], r.content)
        self.client.logout()
        self.client.post(reverse("login"), {"username": "e2e@corp.kz", "password": "Str0ng-PASS-123"})
        o = post("passkey_auth_options").json()
        r = post("passkey_auth_verify", {"credential": dev.get(o)})
        self.assertTrue(r.json().get("ok"), r.content)
        self.assertEqual(Passkey.objects.get(user=u).sign_count, 1)
        # a signature from another site's page (a look-alike origin) is refused
        self.client.logout()
        self.client.post(reverse("login"), {"username": "e2e@corp.kz", "password": "Str0ng-PASS-123"})
        dev.origin = "https://portal-example.com"
        o = post("passkey_auth_options").json()
        self.assertEqual(post("passkey_auth_verify", {"credential": dev.get(o)}).status_code, 400)
