"""The authentication backend: Django's, narrowed so an archived or locked user cannot sign in."""

from django.contrib.auth.backends import ModelBackend


class PortalBackend(ModelBackend):
    """Directory entries (login_enabled off) can never sign in."""

    def user_can_authenticate(self, user):
        return super().user_can_authenticate(user) and getattr(user, "login_enabled", True)
