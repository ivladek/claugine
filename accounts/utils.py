"""The small things every app uses: the role gates on views, sending an e-mail, and two-factor.

`staff_required` / `customer_required` are the decorators that keep a view honest about who may open it;
`notify()` is the one way an e-mail leaves the portal (it applies the template, the frame and the client-mail
switch). Anything here is imported widely, so it must stay free of app imports at module level.
"""

from functools import wraps

import segno
from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from .models import User


def staff_required(*roles, super_only=False):
    """Allow signed-in staff holding any of `roles` (the super admin passes for SUPPORT and RM)."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            user = request.user
            if not user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if not user.is_staff_member:
                raise PermissionDenied
            if super_only and not user.is_super_admin:
                raise PermissionDenied
            if roles and not user.is_super_admin and not any(user.has_role(r) for r in roles):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


def customer_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not user.is_customer:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapper


def site_link(path: str) -> str:
    from .appliance import public_url

    return f"{public_url()}{path}"


ALLOW_CLIENT_HEADER = "X-Portal-Allow-Client"  # one message the owner allowed to a client while client mail is off


def notify(recipients, template, context, subject, bcc=None, allow_client=False, cc=None, attachments=()):
    """Send an e-mail rendered from the template with the id `template` (mailtemplates.T; owner 2026-11-17: by id, not
    by name), its plain-text part made from it. cc: copies; bcc: hidden copies (the
    credentials e-mail goes to the client only, no copies: owner 2026-10-30). allow_client: this message may go to a
    client even while e-mail to clients is off (only on the owner's explicit word). attachments: (file name, bytes,
    MIME type) - the Analysis export (2026-11-13)."""
    # Directory-only entries (sales reps without login) get no portal email.
    emails = sorted(
        {
            (r.email if isinstance(r, User) else r)
            for r in recipients
            if r and not (isinstance(r, User) and not r.login_enabled)
        }
    )
    if not emails:
        return 0
    ctx = {"site_url": settings.SITE_URL, **context}
    from .mailhtml import linkify, text_of

    # owner 2026-11-17: one HTML template per e-mail; the plain-text part is made from it ("portal can convert html to
    # text byself")
    from .mailtemplates import file_of
    from .mailtemplates import render as render_mail  # corrections-18: an edited template (Settings → Mail) first

    html = linkify(render_mail(file_of(template), {**ctx, "subject": subject}))
    body = text_of(html)
    from django.core.mail import EmailMultiAlternatives

    m = EmailMultiAlternatives(
        f"[Example Cloud] {subject}",
        body,
        settings.DEFAULT_FROM_EMAIL,
        emails,
        cc=sorted({c for c in (cc or []) if c and c not in emails}),
        bcc=sorted({b for b in (bcc or []) if b and b not in emails and b not in (cc or [])}),
        headers={ALLOW_CLIENT_HEADER: "1"} if allow_client else None,
    )
    m.attach_alternative(html, "text/html")
    for name, data, mime in attachments:
        m.attach(name, data, mime)
    n = m.send(fail_silently=False)
    return 0 if getattr(m, "portal_kept", False) else n  # kept in files when the relay failed: not sent


def users_with_role(*roles):
    """Active staff who can act in any of `roles` and can sign in to do it."""
    qs = User.objects.none()
    for r in roles:
        qs = qs | User.objects.with_role(r)
    return qs.filter(login_enabled=True).distinct()


# --- TOTP ---------------------------------------------------------------

ISSUER = "Example Cloud"


def totp_device(user, confirmed=True):
    """The user's authenticator (django-otp's TOTPDevice, owner 2026-11-11: the same as claugine-web's), or None."""
    from django_otp.plugins.otp_totp.models import TOTPDevice

    return TOTPDevice.objects.filter(user=user, confirmed=confirmed).order_by("-pk").first()


def new_totp_device(user):
    """A new, unconfirmed authenticator for the set-up page; an earlier unconfirmed one goes."""
    from django_otp.plugins.otp_totp.models import TOTPDevice

    TOTPDevice.objects.filter(user=user, confirmed=False).delete()
    return TOTPDevice.objects.create(user=user, name="authenticator", confirmed=False, tolerance=1)


def totp_secret(device):
    """The key as the base32 text an authenticator app is given by hand."""
    import base64

    return base64.b32encode(device.bin_key).decode().rstrip("=")


def totp_uri(user, device):
    from urllib.parse import quote, urlencode

    from .branding import provider

    issuer = provider() or ISSUER
    query = urlencode({"secret": totp_secret(device), "issuer": issuer, "digits": device.digits, "period": device.step})
    return f"otpauth://totp/{quote(issuer)}:{quote(user.email)}?{query}"


def totp_code(device, offset=0):
    """The device's code now (offset: in steps) - for the tests."""
    from django_otp.oath import TOTP

    t = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    t.time = timezone.now().timestamp() + offset * device.step
    return f"{t.token():0{device.digits}d}"


def totp_qr_svg(uri: str) -> str:
    """The QR code of the authenticator link as an SVG data URI (segno, the same as claugine-web's)."""
    return segno.make(uri, error="m").svg_data_uri(scale=8, border=2)


def verify_totp(user, code: str, device=None) -> bool:
    """Verify a 6-digit code against the user's authenticator (django-otp: the time window, no reuse of an accepted
    code, throttling after wrong codes). `device`: the one being set up, when not the confirmed one."""
    code = (code or "").replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return False
    device = device or totp_device(user)
    if device is None:
        return False
    return device.verify_token(code)
