"""Three things that must happen on every request: two-factor for staff, acting as another user, and returning to a list.

- `StaffTwoFactorMiddleware`: a staff member without two-factor set up gets no further than setting it up.
- `ActAsMiddleware`: a super admin may act as another user (u12) — the whole interface then renders as them.
- `ListReturnMiddleware`: remembers where a list was, so closing a dialog comes back to the same page and filter.
"""
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _


class StaffTwoFactorMiddleware:
    """Every account must enrol in two-factor authentication before doing anything else (correction 11: for any
    user, not only staff; the class keeps its name for the settings)."""

    ALLOWED_PREFIXES = ("/account/2fa/setup/", "/logout/", "/static/", "/i18n/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and user.requires_2fa
            and not user.totp_enabled
            and not request.path_info.startswith(self.ALLOWED_PREFIXES)
        ):
            return redirect(reverse("totp_setup"))
        return self.get_response(request)


class ActAsMiddleware:
    """owner u12: a super admin may act as another (non-archived) user — the whole interface renders as if that user
    were logged in, for testing. ``request.impersonator`` is the real super admin; the session key ``act_as`` holds
    the target's pk. It is re-checked every request, so losing the super-admin role (or archiving the target) ends it.
    Runs after the two-factor check, so the real super admin's 2FA is still enforced before the swap."""

    SAFE_METHODS = ("GET", "HEAD", "OPTIONS", "TRACE")
    # owner u12: acting as a user is read-only — only ending/switching it and signing out still work.
    ALLOW_WRITE = ("/act-as/", "/logout/", "/i18n/setlang/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.impersonator = None
        pk = request.session.get("act_as")
        user = getattr(request, "user", None)
        if pk and user is not None and user.is_authenticated and user.is_super_admin:
            from .models import User
            target = User.objects.filter(pk=pk, archived_at__isnull=True).first()
            if target is not None and target.pk != user.pk:
                request.impersonator = user
                request.user = target
                # owner u12: while acting as someone, every state-changing request is a no-op — the action can be
                # opened and submitted, but nothing is executed or saved. The view is never reached.
                if request.method not in self.SAFE_METHODS and not request.path.startswith(self.ALLOW_WRITE):
                    from django.contrib import messages
                    messages.info(request, _("Acting as a user is read-only — nothing was changed."))
                    ref = request.META.get("HTTP_REFERER") or ""
                    if ref and url_has_allowed_host_and_scheme(ref, allowed_hosts={request.get_host()},
                                                               require_https=request.is_secure()):
                        return redirect(ref)
                    return redirect("dashboard")
            else:
                request.session.pop("act_as", None)
        return self.get_response(request)


class ListReturnMiddleware:
    """A form posted from a detail shown under a list carries ``back`` (the list with that row selected): after the
    action the person returns there instead of to the object's own page."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.method == "POST" and 300 <= response.status_code < 400 and request.headers.get("X-Partial") == "1":
            # a form sent in the background (the igw dialogs): the page it returns to, as a partial (with the messages)
            response["Location"] = with_partial(response.get("Location", ""))
            return response
        if request.method == "POST" and 300 <= response.status_code < 400:
            back = request.POST.get("back", "")
            if back.startswith("/") and not back.startswith("//") and "\\" not in back:
                response["Location"] = keep_dialog(back, response.get("Location", ""))
        return response


DIALOG_PARAMS = ("igw", "provision")  # a dialog the action goes back to (the tenant page's igw dialogs, Provision)


def keep_dialog(back, location):
    """The list URL with the dialog the view wanted open (owner 2026-10-29: Read config must stay in the dialog);
    olist.js passes it on to the selected row's details."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    want = [(k, v) for k, v in parse_qsl(urlsplit(location).query) if k in DIALOG_PARAMS]
    if not want:
        return back
    u = urlsplit(back)
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k not in DIALOG_PARAMS] + want
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(q), u.fragment))


def with_partial(location):
    """The address with partial=1 (no fragment): the page's content without the frame, its messages first."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    u = urlsplit(location)
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k != "partial"] + [("partial", "1")]
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(q), ""))
