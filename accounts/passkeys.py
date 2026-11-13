"""Passkeys (WebAuthn / FIDO2) as the second step of the sign-in (owner 2026-11-12, agreed with claugine-web).

A person registers a passkey on their profile page (Touch ID, Face ID, Windows Hello, an Android phone or a security
key; a passkey synced by the platform's keychain works on the person's other devices too). After the password, the
second step takes either the authenticator code or a passkey. The authenticator app stays: it is mandatory for every
user (correction 11) and it is the way back when a device is lost.

A passkey is bound to the portal's domain - the host of SITE_URL - so a look-alike site cannot use it. The portal
keeps only the public key and the signature counter (accounts.models.Passkey).

The browser side is static/passkeys.js; the JSON endpoints here take and give what the WebAuthn API needs.
"""

import json
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from .models import Passkey, User, audit

REG_KEY = "passkey_reg_challenge"
AUTH_KEY = "passkey_auth_challenge"


def rp():
    """(id, origin, name) of the relying party: the portal's public address."""
    url = urlsplit(settings.SITE_URL)
    from .branding import provider

    return url.hostname or "localhost", f"{url.scheme}://{url.netloc}", provider() or "Claugine"


def _descriptors(user):
    from webauthn.helpers import base64url_to_bytes
    from webauthn.helpers.structs import AuthenticatorTransport, PublicKeyCredentialDescriptor

    known = {t.value for t in AuthenticatorTransport}
    return [
        PublicKeyCredentialDescriptor(
            id=base64url_to_bytes(p.credential_id),
            transports=[AuthenticatorTransport(t) for t in (p.transports or []) if t in known] or None,
        )
        for p in user.passkeys.all()
    ]


def _read_only():
    from .testmount import read_only

    return read_only()


# --- registering, on the profile page ------------------------------------------------------------------------------


@login_required
@require_POST
def register_options_view(request):
    from webauthn import generate_registration_options, options_to_json
    from webauthn.helpers.structs import (
        AuthenticatorSelectionCriteria,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )

    if _read_only():
        return JsonResponse({"error": _("Not on this node: its database is read only.")}, status=403)
    user = request.user
    rp_id, _origin, rp_name = rp()
    opts = generate_registration_options(
        rp_id=rp_id,
        rp_name=rp_name,
        user_id=f"claugine-user-{user.pk}".encode(),
        user_name=user.email,
        user_display_name=user.full_name or user.email,
        exclude_credentials=_descriptors(user),
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED, user_verification=UserVerificationRequirement.REQUIRED
        ),
    )
    from webauthn.helpers import bytes_to_base64url

    request.session[REG_KEY] = bytes_to_base64url(opts.challenge)
    return JsonResponse(json.loads(options_to_json(opts)))


@login_required
@require_POST
def register_verify_view(request):
    from webauthn import verify_registration_response
    from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

    challenge = request.session.pop(REG_KEY, None)
    if not challenge or _read_only():
        return JsonResponse({"error": _("Start again.")}, status=400)
    try:
        body = json.loads(request.body or b"{}")
        rp_id, origin, _name = rp()
        v = verify_registration_response(
            credential=body.get("credential") or {},
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=rp_id,
            expected_origin=origin,
            require_user_verification=True,
        )
    except Exception as exc:  # noqa: BLE001 - whatever the browser or the key sent, the answer is the same
        return JsonResponse({"error": _("The passkey was not accepted: %(e)s") % {"e": str(exc)[:200]}}, status=400)
    name = (str(body.get("name") or "").strip() or _("Passkey"))[:64]
    transports = [t for t in ((body.get("credential") or {}).get("response") or {}).get("transports") or [] if t]
    key = Passkey.objects.create(
        user=request.user,
        name=name,
        credential_id=bytes_to_base64url(v.credential_id),
        public_key=v.credential_public_key,
        sign_count=v.sign_count,
        transports=transports[:8],
    )
    audit(request.user, "passkey_added", request.user, passkey=key.name)
    messages.success(request, _("Passkey %(n)s added.") % {"n": key.name})
    return JsonResponse({"ok": True, "next": reverse("profile")})


@login_required
@require_POST
def remove_view(request, pid):
    key = get_object_or_404(Passkey, pk=pid, user=request.user)
    key.delete()
    audit(request.user, "passkey_removed", request.user, passkey=key.name)
    messages.success(request, _("Passkey %(n)s removed.") % {"n": key.name})
    return redirect("profile")


# --- signing in: the second step -------------------------------------------------------------------------------------


def _pending_user(request):
    uid = request.session.get("pre2fa_uid")
    if not uid:
        return None
    return User.objects.filter(pk=uid, is_active=True, login_enabled=True).first()


@require_POST
def auth_options_view(request):
    from webauthn import generate_authentication_options, options_to_json
    from webauthn.helpers import bytes_to_base64url
    from webauthn.helpers.structs import UserVerificationRequirement

    user = _pending_user(request)
    if user is None or not user.passkeys.exists():
        return JsonResponse({"error": _("Sign in again.")}, status=400)
    rp_id, _origin, _name = rp()
    opts = generate_authentication_options(
        rp_id=rp_id, allow_credentials=_descriptors(user), user_verification=UserVerificationRequirement.REQUIRED
    )
    request.session[AUTH_KEY] = bytes_to_base64url(opts.challenge)
    return JsonResponse(json.loads(options_to_json(opts)))


@require_POST
def auth_verify_view(request):
    from webauthn import verify_authentication_response
    from webauthn.helpers import base64url_to_bytes

    user = _pending_user(request)
    challenge = request.session.pop(AUTH_KEY, None)
    if user is None or not challenge:
        return JsonResponse({"error": _("Sign in again."), "next": reverse("login")}, status=400)
    try:
        body = json.loads(request.body or b"{}")
        cred = body.get("credential") or {}
        key = user.passkeys.get(credential_id=cred.get("id") or "")
        rp_id, origin, _name = rp()
        v = verify_authentication_response(
            credential=cred,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=rp_id,
            expected_origin=origin,
            credential_public_key=bytes(key.public_key),
            credential_current_sign_count=key.sign_count,
            require_user_verification=True,
        )
    except Exception:  # noqa: BLE001 - an unknown key, a wrong signature or a replayed counter: one answer
        attempts = request.session.get("pre2fa_attempts", 0) + 1
        request.session["pre2fa_attempts"] = attempts
        if attempts >= 5:
            request.session.flush()
            return JsonResponse({"error": _("Too many attempts. Sign in again."), "next": reverse("login")}, status=400)
        return JsonResponse({"error": _("The passkey was not accepted.")}, status=400)
    if not _read_only():  # the version under test on the standby keeps nothing
        key.sign_count, key.last_used_at = v.new_sign_count, timezone.now()
        key.save(update_fields=["sign_count", "last_used_at"])
    backend = request.session.get("pre2fa_backend")
    nxt = request.session.get("pre2fa_next") or reverse("dashboard")
    for k in ("pre2fa_uid", "pre2fa_backend", "pre2fa_next", "pre2fa_attempts"):
        request.session.pop(k, None)
    login(request, user, backend=backend)
    audit(user, "login", user, second_factor="passkey", passkey=key.name)
    return JsonResponse({"ok": True, "next": nxt})
