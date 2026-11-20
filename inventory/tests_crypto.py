"""Stored secrets under several keys (MultiFernet, owner 2026-11-11): the first key encrypts, every key decrypts,
and rotate_keys moves every stored secret to the first key."""

from io import StringIO

from cryptography.fernet import Fernet
from django.core.management import call_command
from django.test import TestCase, override_settings

OLD, NEW = Fernet.generate_key().decode(), Fernet.generate_key().decode()


class Keys(TestCase):
    def test_new_key_first_reads_old_and_rotate_moves_everything(self):
        from inventory import crypto
        from tenants.models import PortalSettings

        with override_settings(FIELD_ENCRYPTION_KEY=OLD):
            s = PortalSettings.get()
            s.backup_password_encrypted = crypto.encrypt("archive-pw")
            s.save()
        with override_settings(FIELD_ENCRYPTION_KEY=f"{NEW}, {OLD}"):
            self.assertEqual(crypto.decrypt(PortalSettings.get().backup_password_encrypted), "archive-pw")
            out = StringIO()
            call_command("rotate_keys", stdout=out)
            self.assertIn("tenants.PortalSettings.backup_password_encrypted: 1", out.getvalue())
            call_command("rotate_keys", stdout=(again := StringIO()))
            self.assertIn("nothing to rotate", again.getvalue())  # already under the first key
        with override_settings(FIELD_ENCRYPTION_KEY=NEW):  # the old key can go now
            self.assertEqual(crypto.decrypt(PortalSettings.get().backup_password_encrypted), "archive-pw")

    def test_one_key_as_before(self):
        from inventory import crypto

        with override_settings(FIELD_ENCRYPTION_KEY=OLD):
            self.assertEqual(crypto.decrypt(crypto.encrypt("x")), "x")


class RotateWhileSaved(TestCase):
    """core-014 (third full review): a secret saved while the rotation runs is kept, never replaced by the
    re-encrypted old value."""

    def _rotate_with_a_save_in_between(self, saved_under):
        from unittest import mock

        from cryptography.fernet import MultiFernet

        from inventory import crypto
        from tenants.models import PortalSettings

        with override_settings(FIELD_ENCRYPTION_KEY=OLD):
            s = PortalSettings.get()
            s.backup_password_encrypted = crypto.encrypt("old-pw")
            s.save()
        real, calls = MultiFernet.rotate, []

        def rotate_then_owner_saves(self_, token):
            out = real(self_, token)
            if not calls:  # the owner saves a new password between the read and the update
                calls.append(1)
                with override_settings(FIELD_ENCRYPTION_KEY=saved_under):
                    PortalSettings.objects.update(backup_password_encrypted=crypto.encrypt("new-pw"))
            return out

        with override_settings(FIELD_ENCRYPTION_KEY=f"{NEW},{OLD}"):
            with mock.patch.object(MultiFernet, "rotate", rotate_then_owner_saves):
                crypto.rotate()
        with override_settings(FIELD_ENCRYPTION_KEY=NEW):  # under the first key, and the owner's value
            self.assertEqual(crypto.decrypt(PortalSettings.get().backup_password_encrypted), "new-pw")

    def test_saved_under_the_new_key_is_kept(self):
        self._rotate_with_a_save_in_between(f"{NEW},{OLD}")

    def test_saved_under_the_old_key_is_read_again_and_rotated(self):
        self._rotate_with_a_save_in_between(OLD)
