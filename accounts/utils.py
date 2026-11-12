"""The small things every app uses: the role gates on views, sending an e-mail, and two-factor.

`staff_required` / `customer_required` are the decorators that keep a view honest about who may open it;
`notify()` is the one way an e-mail leaves the portal (it applies the template, the frame and the client-mail
switch). Anything here is imported widely, so it must stay free of app imports at module level.
"""

from datetime import datetime
from functools import wraps

import pyotp
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


def notify(recipients, template, context, subject, bcc=None, allow_client=False, cc=None):
    """Send an e-mail rendered from templates/emails/<template>.txt (and .html). cc: copies; bcc: hidden copies (the
    credentials e-mail goes to the client only, no copies: owner 2026-10-30). allow_client: this message may go to a
    client even while e-mail to clients is off (only on the owner's explicit word)."""
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
    from django.template import TemplateDoesNotExist

    from .mailtemplates import render as render_mail  # corrections-18: an edited template (Settings → Mail) first

    body = render_mail(f"{template}.txt", ctx)
    try:  # owner 2026-10-29: an HTML e-mail (sections and lists) where the template has one; the text stays for the rest
        html = render_mail(f"{template}.html", {**ctx, "subject": subject})
    except TemplateDoesNotExist:  # the text in the HTML frame (accounts.mailhtml)
        from .mailhtml import framed

        html = framed(body, subject)
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


def new_totp_secret():
    return pyotp.random_base32()


def totp_uri(user, secret):
    from .branding import provider

    return pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name=provider() or ISSUER)


def totp_qr_svg(uri: str) -> str:
    """The QR code of the authenticator link as an SVG data URI (segno, the same as claugine-web's)."""
    return segno.make(uri, error="m").svg_data_uri(scale=8, border=2)


def verify_totp(user, code: str, secret: str | None = None) -> bool:
    """Verify a 6-digit code; reject reuse of an already accepted code."""
    code = (code or "").replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return False
    totp = pyotp.TOTP(secret or user.totp_secret)
    now = timezone.now().timestamp()
    for offset in (-1, 0, 1):
        counter = totp.timecode(datetime.fromtimestamp(now + offset * totp.interval))
        if totp.generate_otp(counter) == code:
            from .testmount import read_only

            if read_only():  # the version under test on the standby: the used code is kept in memory there
                from django.core.cache import cache

                key = f"totp-last-{user.pk}"
                if counter <= max(user.totp_last_counter, cache.get(key, 0)):
                    return False
                cache.set(key, counter, 300)
                return True
            if counter <= user.totp_last_counter:
                return False
            user.totp_last_counter = counter
            user.save(update_fields=["totp_last_counter"])
            return True
    return False
