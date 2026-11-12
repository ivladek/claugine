"""The portal's own records: people, invitations, the log, tasks, super-admin approvals and the e-mail templates.

`User` is `AUTH_USER_MODEL` and covers staff and clients alike (`kind` separates them); a user is never deleted,
only archived. `AuditLog` is append-only — `cleanup_history` trims it by age, nothing edits it. The rules behind
these models are in `worklog/decisions.md`; the map of the whole project is `worklog/internals.md`.
"""

import hashlib
import os
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .kz import CITIES, format_phone


class Role(models.TextChoices):
    """Staff roles. The super admin is a flag on the user, not a role."""

    SUPPORT = "support", _("Support engineer")
    RESOURCE_MANAGER = "resource_manager", _("Resource manager")
    SALES_MANAGER = "sales_manager", _("Sales manager")
    KAM = "kam", _("Key account manager")


STAFF_ROLE_FIELDS = {
    Role.SUPPORT: "is_support",
    Role.RESOURCE_MANAGER: "is_resource_manager",
    Role.SALES_MANAGER: "is_sales",
    Role.KAM: "is_kam",
}
# The super admin holds these roles implicitly; never the sales role.
SUPER_ADMIN_ROLES = {Role.SUPPORT, Role.RESOURCE_MANAGER}


class Kind(models.TextChoices):
    STAFF = "staff", _("Staff")
    CUSTOMER = "customer", _("Customer")


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email, password=None, **extra):
        if not email:
            raise ValueError("email is required")
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra):
        extra.update(kind=Kind.STAFF, is_super_admin=True, is_staff=True, is_superuser=True, email_verified=True)
        return self.create_user(email, password, **extra)

    def sales_reps(self):
        return self.filter(kind=Kind.STAFF, is_sales=True, is_active=True)

    def kams(self):
        return self.filter(kind=Kind.STAFF, is_kam=True, is_active=True)

    def support_engineers(self):
        """Staff explicitly given the support role (the super admin's implicit role does not count)."""
        return self.filter(kind=Kind.STAFF, is_support=True, is_active=True, login_enabled=True)

    def with_role(self, role):
        """Who holds the role (explicitly; Administration → Users → Roles) — they are asked to approve and told.
        Nobody who can sign in (should not happen: every role keeps an active user): the super admins."""
        qs = self.filter(kind=Kind.STAFF, is_active=True)
        field = STAFF_ROLE_FIELDS[role]
        holders = qs.filter(**{field: True})
        if role in SUPER_ADMIN_ROLES and not holders.filter(login_enabled=True).exists():
            return qs.filter(is_super_admin=True)
        return holders


def photo_upload_path(instance, filename):
    """Kept for old migrations (photos were files before 0009; they are in the database now)."""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    return f"photos/{secrets.token_hex(16)}{ext}"


PHOTO_SIDE = 512  # photos are kept at most this many pixels on the longer side


def photo_bytes(upload):
    """(bytes, content type) of an uploaded photo, scaled down to PHOTO_SIDE: what the database keeps."""
    import io

    from PIL import Image, ImageOps

    upload.seek(0)
    img = ImageOps.exif_transpose(Image.open(upload))
    img.thumbnail((PHOTO_SIDE, PHOTO_SIDE))
    out = io.BytesIO()
    if img.mode in ("RGBA", "LA", "P"):
        img.convert("RGBA").save(out, "PNG", optimize=True)
        return out.getvalue(), "image/png"
    img.convert("RGB").save(out, "JPEG", quality=85, optimize=True)
    return out.getvalue(), "image/jpeg"


class User(AbstractBaseUser, PermissionsMixin):
    """A portal user; the email is the user ID.

    Staff hold a set of roles. Customers belong to one organization and get
    per-tenant roles through memberships. A sales rep can exist as a
    directory entry without login (``login_enabled`` off) while sales
    approval is switched off.
    """

    email = models.EmailField(_("email"), unique=True)
    kind = models.CharField(_("account type"), max_length=16, choices=Kind.choices, default=Kind.CUSTOMER)

    # staff
    is_super_admin = models.BooleanField(_("super admin"), default=False)
    is_support = models.BooleanField(_("support engineer"), default=False)
    is_resource_manager = models.BooleanField(_("resource manager"), default=False)
    is_sales = models.BooleanField(_("sales manager"), default=False)
    is_kam = models.BooleanField(_("key account manager"), default=False)
    # owner 2026-11-03: eligibility for a platform user to be assigned an org role. Only a platform user with the
    # flag may be chosen as an organization admin / a project admin (the actual org and projects are assigned in the
    # organization edit and the project team); not used for customer users, who are eligible by membership.
    may_org_admin = models.BooleanField(_("may be an organization admin"), default=False)
    may_tenant_admin = models.BooleanField(_("may be a project admin"), default=False)
    login_enabled = models.BooleanField(
        _("can sign in"),
        default=True,
        help_text=_("Off for sales reps kept as directory entries while sales approval is off."),
    )

    # customer
    organization = models.ForeignKey(
        "tenants.Organization",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="users",
        verbose_name=_("organization"),
    )
    is_org_admin = models.BooleanField(_("organization admin"), default=False)
    is_org_readonly = models.BooleanField(
        _("read only"), default=False, help_text=_("Sees every project of the organization; changes nothing.")
    )
    archived_at = models.DateTimeField(
        _("archived"), null=True, blank=True, help_text=_("Archived users are kept for the record and never deleted.")
    )

    # profile
    full_name = models.CharField(_("full name"), max_length=200, blank=True)
    phone = models.CharField(_("mobile number"), max_length=16, blank=True)
    city = models.CharField(
        _("city"), max_length=32, choices=CITIES, default="almaty"
    )  # owner 2026-10-29 20:24: required
    photo_data = models.BinaryField(_("photo"), blank=True, default=b"", editable=False)
    photo_type = models.CharField(max_length=32, blank=True, editable=False)

    email_verified = models.BooleanField(default=False)
    consent_at = models.DateTimeField(null=True, blank=True)
    totp_secret = models.CharField(max_length=64, blank=True)
    totp_enabled = models.BooleanField(default=False)
    totp_last_counter = models.BigIntegerField(default=0)
    password_history = models.JSONField(
        default=list, blank=True, help_text="hashes of the last passwords, newest first"
    )

    is_active = models.BooleanField(_("active"), default=True)
    is_staff = models.BooleanField(default=False, help_text="Access to the technical admin site.")
    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    def set_password(self, raw_password):
        """Keep the hash of the password being replaced: the policy refuses the last ones."""
        if self.password and self.has_usable_password():
            keep = self.password_history or []
            if self.password not in keep:
                self.password_history = ([self.password] + keep)[:1000]
        super().set_password(raw_password)

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        ordering = ["email"]

    def __str__(self):
        return self.full_name or self.email

    # --- roles -------------------------------------------------------------

    @property
    def is_staff_member(self):
        return self.kind == Kind.STAFF

    @property
    def is_customer(self):
        return self.kind == Kind.CUSTOMER

    def has_role(self, role):
        if not self.is_authenticated or self.kind != Kind.STAFF:
            return False
        if self.is_super_admin:  # correction 16: the super admin has every right whatever roles are ticked
            return True
        return bool(getattr(self, STAFF_ROLE_FIELDS[role]))

    def holds(self, role):
        """The role is assigned to this person (not just allowed because they are the super admin)."""
        return bool(self.is_authenticated and self.kind == Kind.STAFF and getattr(self, STAFF_ROLE_FIELDS[role]))

    @property
    def staff_role_labels(self):
        labels = []
        if self.is_super_admin:
            labels.append(str(_("Super admin")))
        for role, field in STAFF_ROLE_FIELDS.items():
            if getattr(self, field):
                labels.append(str(role.label))
        return labels

    @property
    def role_display(self):
        if self.is_staff_member:
            return ", ".join(self.staff_role_labels) or str(_("Staff"))
        if self.is_org_admin:
            return str(_("Organization admin"))
        return str(_("Customer"))

    @property
    def requires_2fa(self):
        """Every user (correction 11, 2026-10-28: "2fa must be on always for any user")."""
        return True

    @property
    def photo(self):
        """True when the person has a photo (kept in the database)."""
        return bool(self.photo_data)

    @photo.setter
    def photo(self, upload):
        """Set from an uploaded image (None or empty: no change)."""
        if upload:
            self.photo_data, self.photo_type = photo_bytes(upload)

    @property
    def phone_display(self):
        return format_phone(self.phone)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class InvitationQuerySet(models.QuerySet):
    def by_token(self, token):
        return self.filter(token_hash=_hash_token(token or "")).first()

    def open(self):
        return self.filter(accepted_at__isnull=True, revoked_at__isnull=True, expires_at__gt=timezone.now())


class Invitation(models.Model):
    """One-time link that creates an account, or activates a directory entry.

    ``grants`` describes what the account gets:
      staff:    {"super_admin": bool, "roles": ["support", ...]}
      customer: {"org_admin": bool, "read_only": bool, "tenants": {"<tenant id>": "tenant_admin" | "tenant_user"}}
      organization: {"org_kind": "cloud" | "corp", "sales_rep": <user id> | None} - the person registers
                    a new organization and becomes its admin
    """

    class Purpose(models.TextChoices):
        STAFF = "staff", _("Staff account")
        CUSTOMER = "customer", _("Customer account")
        ACTIVATE = "activate", _("Activate existing entry")
        ORGANIZATION = "organization", _("New customer organization")

    purpose = models.CharField(max_length=16, choices=Purpose.choices, default=Purpose.STAFF)
    email = models.EmailField(blank=True)
    organization = models.ForeignKey("tenants.Organization", null=True, blank=True, on_delete=models.CASCADE)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="activation_invites",
        help_text="Existing directory entry to activate.",
    )
    grants = models.JSONField(default=dict, blank=True)
    token_hash = models.CharField(max_length=64, unique=True)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    objects = InvitationQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Invitation #{self.pk} {self.email}"

    @classmethod
    def issue(cls, *, purpose, email="", organization=None, user=None, grants=None, invited_by=None, ttl_hours=None):
        token = secrets.token_urlsafe(32)
        inv = cls.objects.create(
            purpose=purpose,
            email=(email or (user.email if user else "")).lower(),
            organization=organization,
            user=user,
            grants=grants or {},
            invited_by=invited_by,
            token_hash=_hash_token(token),
            expires_at=timezone.now() + timedelta(hours=ttl_hours or settings.INVITATION_TTL_HOURS),
        )
        return inv, token

    @property
    def state(self):
        if self.accepted_at:
            return "accepted"
        if self.revoked_at:
            return "revoked"
        if self.expires_at <= timezone.now():
            return "expired"
        return "open"

    @property
    def is_usable(self):
        return self.state == "open"

    @property
    def summary(self):
        g = self.grants or {}
        if self.purpose == self.Purpose.STAFF:
            parts = [str(_("Super admin"))] if g.get("super_admin") else []
            parts += [str(Role(r).label) for r in g.get("roles", [])]
            return ", ".join(parts)
        if self.purpose == self.Purpose.ACTIVATE:
            return str(_("Activate sales rep entry"))
        if self.purpose == self.Purpose.ORGANIZATION:
            return (
                str(_("New organization: corporate"))
                if g.get("org_kind") == "corp"
                else str(_("New organization: cloud"))
            )
        parts = [str(_("Organization admin"))] if g.get("org_admin") else []
        if g.get("read_only"):
            parts.append(str(_("Read only")))
        if g.get("tenants"):
            parts.append(str(_("%(n)s project role(s)") % {"n": len(g["tenants"])}))
        return ", ".join(parts) or str(_("Customer"))


class AuditLog(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=64)
    target_type = models.CharField(max_length=64, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action} {self.target_type}:{self.target_id}"


class Task(models.Model):
    """Long work the portal does in the background (server profile checks, applying site settings to servers, …).

    Tenant work keeps its own records (deployments, network changes, deletions, configuration checks); the Tasks
    page shows those together with these."""

    class State(models.TextChoices):
        QUEUED = "queued", _("queued")
        RUNNING = "running", _("running")
        DONE = "done", _("done")
        FAILED = "failed", _("failed")

    kind = models.CharField(max_length=32)
    title = models.CharField(max_length=200)
    target_type = models.CharField(max_length=64, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    state = models.CharField(max_length=16, choices=State.choices, default=State.QUEUED)
    progress = models.CharField(max_length=200, blank=True)
    steps = models.JSONField(default=list, blank=True)
    result = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    STALE_MINUTES = 60

    def __str__(self):
        return f"Task #{self.pk} {self.title}"

    @property
    def is_stale(self):
        from datetime import timedelta

        return self.state in (self.State.QUEUED, self.State.RUNNING) and self.created_at < timezone.now() - timedelta(
            minutes=self.STALE_MINUTES
        )

    def step(self, name, status, detail=""):
        self.steps = [s for s in self.steps if s.get("name") != name] + [
            {"name": name, "status": status, "detail": str(detail)[:500]}
        ]
        self.progress = f"{name}: {status}"
        self.save(update_fields=["steps", "progress"])


def audit(actor, action, target=None, **details):
    from .testmount import read_only

    if read_only():  # the version under test on the standby: its database follows the active node
        return
    AuditLog.objects.create(
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        action=action,
        target_type=target._meta.model_name if target is not None else "",
        target_id=str(target.pk) if target is not None else "",
        details={k: (v if isinstance(v, (int, float, bool, type(None))) else str(v)) for k, v in details.items()},
    )


class SuperAdminRemoval(models.Model):
    """Correction 16 / owner u12: granting or taking the super admin role. Another super admin asks; the number of
    super admins who must agree (the one asking counts as one) is fixed when the request is made, from the current
    super-admin count: 1→1, 2→2, 3→2, 4 or more→3. A confirmation that reaches that number applies the change;
    one refusal ends it, and so does the 48-hour deadline. The one being removed cannot confirm their own removal."""

    class State(models.TextChoices):
        PENDING = "pending", _("waiting for confirmations")
        DONE = "done", _("done")
        DECLINED = "declined", _("declined")
        EXPIRED = "expired", _("expired")

    class Op(models.TextChoices):
        ADD = "add", _("grant")
        REMOVE = "remove", _("remove")

    HOURS = 48
    op = models.CharField(max_length=8, choices=Op.choices, default=Op.REMOVE)
    required = models.PositiveSmallIntegerField(default=1, help_text="super admins who must agree, including the asker")
    target = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    confirmed = models.JSONField(default=list, blank=True)  # user ids
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    finished_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"super admin removal: {self.target_id}"

    @property
    def deadline(self):
        from datetime import timedelta

        return self.created_at + timedelta(hours=self.HOURS)


class MailTemplate(models.Model):
    """corrections-18: an e-mail template edited in Settings → Mail; it replaces the file templates/emails/<name>
    (the portal's own text) until it is reset."""

    name = models.CharField(max_length=100, unique=True)  # e.g. tenant_ready_admin.txt
    body = models.TextField()
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name
