"""Clients' projects and everything the portal holds about them, plus `PortalSettings` — the installation's own
settings.

A *tenant* here is a **project** in the product's own words. Two rules to know before changing anything:
`PortalSettings` is a single row read with `PortalSettings.get()`, and a setting an edition may pin must be read
through `accounts/edition.effective()` **where it is used**, never with `getattr` at the point of use. The second
is what makes a stored value harmless on a Standard installation. Model by model, with each one's rule:
`worklog/internals.md` section 5.
"""
import re
from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Max
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from accounts.models import Role



def parse_cidrs(text):
    """CIDRs from text (comma, space or newline separated), normalised; raises ValueError on a bad one."""
    import ipaddress
    out = []
    for part in re.split(r"[\s,;]+", text or ""):
        if part:
            n = str(ipaddress.ip_network(part, strict=False))
            if n not in out:
                out.append(n)
    return out

class TaxIdType(models.TextChoices):
    IIN = "iin", _("IIN (individual / individual entrepreneur)")
    BIN = "bin", _("BIN (legal entity)")


class TenantKind(models.TextChoices):
    """Who the tenant is for. Decides which staff approve it."""
    CLOUD = "cloud", _("Cloud")
    CORP = "corp", _("Corporate")


class TenantStage(models.TextChoices):
    """Commercial state: client (production) or poc (test). Tenants start as poc; the type (cloud / corp) is apart."""
    POC = "poc", _("POC")
    CLIENT = "client", _("Client")


class OrgKind(models.TextChoices):
    """Who the organization is. Only corporate organizations may order corporate tenants and the corporate network."""
    CLOUD = "cloud", _("Cloud client")
    CORP = "corp", _("Corporate")


class Approval(models.TextChoices):
    PENDING = "pending", _("Waiting for approval")
    APPROVED = "approved", _("Approved")
    REJECTED = "rejected", _("Rejected")
    CANCELLED = "cancelled", _("Cancelled")


class Lifecycle(models.TextChoices):
    """Status of a tenant (quota.md): ok → paused (2 weeks) → suspended (1 month) → saved → archived (1 month later).

        ok         account active, VMs running, connectivity working, resources kept
        paused     account blocked, VMs running, connectivity blocked, resources kept
        suspended  account blocked, VMs powered off, connectivity blocked, resources kept
        saved      account and VMs deleted; one full backup of every VM is kept
        archived   nothing left on the platform; the records stay (tenants are never deleted from the portal)
        deleting   the portal is removing it from the platform (then archived)

    Changed by hand only; the one exception: saved becomes archived a month later (date_review)."""
    OK = "ok", _("OK")
    PAUSED = "paused", _("Paused")
    SUSPENDED = "suspended", _("Suspended")
    SAVED = "saved", _("Saved")
    ARCHIVED = "archived", _("Archived")
    DELETING = "deleting", _("Being deleted")


# hand changes allowed from each status, and the review period of the new one (days; None: no review date)
STATUS_NEXT = {"ok": ("paused",), "paused": ("ok", "suspended"), "suspended": ("ok", "saved"), "saved": ("archived",)}
STATUS_PERIOD = {"paused": 14, "suspended": 30, "saved": 30}
GONE = ("saved", "archived", "deleting")  # nothing (or only backups) left on the platform


class Origin(models.TextChoices):
    PORTAL = "portal", _("Registered in the portal")
    ADOPTED = "adopted", _("Adopted from the old system")
    TEST = "test", _("Test project")


tenant_name_validator = RegexValidator(  # legacy names (tenant-<name>) and new short names both pass
    r"^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$",
    _("2-32 characters: lowercase latin letters, digits and hyphens."),
)
org_short_name_validator = RegexValidator(
    r"^[a-z][a-z0-9]{2,14}$", _("3-15 characters: lowercase latin letters and digits, starting with a letter."))
tenant_short_name_validator = RegexValidator(
    r"^[a-z][a-z0-9]{3,14}$", _("4-15 characters: lowercase latin letters and digits, starting with a letter."))


def _role_field(role):
    from accounts.models import STAFF_ROLE_FIELDS
    return STAFF_ROLE_FIELDS[role]


class PortalSettings(models.Model):
    """Single row of switches that change how the portal behaves."""

    rm_approval_required = models.BooleanField(
        _("resource manager approval required"), default=True,
        help_text=_("Off: the super admin approves (names, places and confirms resources) instead."),
    )
    sales_approval_required = models.BooleanField(
        _("sales rep approval required"), default=False,
        help_text=_("Off: sales reps are directory entries without login; RM approves alone."),
    )
    support_approval_required = models.BooleanField(
        _("support engineer approval required"), default=False,
        help_text=_("Off: corporate projects need only the resource manager."),
    )
    default_support_engineer = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        limit_choices_to={"is_support": True}, verbose_name=_("default support engineer"),
        help_text=_("Assigned to corporate organizations without one when data is imported."),
    )
    health_interval_minutes = models.PositiveIntegerField(
        _("platform health check period, minutes"), default=30,
        help_text=_("How often OpenNebula endpoints and the network function image of each platform are checked."),
    )
    portal_site = models.CharField(
        _("portal site"), max_length=16, blank=True, default="dc1",
        help_text=_("The site the portal runs on. Endpoints of other sites are reached through their jump host."),
    )
    password_policy = models.JSONField(
        _("password policy"), default=dict, blank=True,
        help_text=_("Rules for every password the portal takes (see accounts/policy.py for the defaults)."))
    list_rows = models.PositiveSmallIntegerField(
        _("list items per page"), default=10, help_text=_("How many rows a list shows per page until a user picks another number."))
    platform_refresh_minutes = models.PositiveSmallIntegerField(
        _("projects refresh from the platforms, minutes"), default=30,
        help_text=_("How often the platforms are read for the projects (groups, quotas, users, VMs); 0: only with Refresh. "
                    "Needed while the old management system still changes them."))
    task_days = models.PositiveIntegerField(_("task history, days"), default=10,
                                            help_text=_("Finished tasks older than this are removed."))
    log_days = models.PositiveIntegerField(_("log history, days"), default=500,
                                           help_text=_("Log entries older than this are removed."))
    corporate_domains = models.TextField(
        _("corporate email domains"), blank=True,
        help_text=_("One per line, e.g. corp.example. Organizations registered with these domains are corporate."),
    )
    tenant_history_days = models.PositiveIntegerField(
        _("project history, days"), default=730,
        help_text=_("Replaced versions of projects' resource and connectivity records are kept this long."))
    client_mail = models.BooleanField(
        _("e-mail to clients"), default=False,
        help_text=_("Off: the portal mails only platform (staff) users; messages to anybody else are held and logged."))
    notify_clients = models.BooleanField(
        _("email to clients"), default=False,
        help_text=_("Off: notices to organization admins (contract and POC ends) are only logged."))

    # the portal appliance: what would otherwise be set on the VM (everything here, nothing by hand on the OS)
    public_url = models.URLField(_("public URL"), blank=True, help_text=_("Links in emails. Empty: the installer's."))
    test_url = models.URLField(_("test URL"), blank=True,
                               help_text=_("Where a new portal version under test is published (super admins only)."))
    mail_mode = models.CharField(_("mail"), max_length=16, default="internal",
                                 choices=[("internal", _("internal relay (this VM)")), ("direct", _("direct delivery (this VM)")),
                                          ("external", _("external SMTP server"))],
                                 help_text=_("internal: the VM's postfix (loopback only) relays through the SMTP server below "
                                             "and signs with DKIM; direct: the VM's postfix delivers to the recipients' "
                                             "mail servers itself and signs with DKIM; external: the portal sends to the "
                                             "SMTP server itself."))
    mail_from = models.CharField(_("sender address"), max_length=200, blank=True)
    mail_host = models.CharField(_("SMTP server"), max_length=200, blank=True)
    mail_port = models.PositiveIntegerField(_("SMTP port"), default=587)
    mail_tls = models.BooleanField(_("STARTTLS"), default=True)
    mail_user = models.CharField(_("SMTP user"), max_length=200, blank=True)
    mail_password_encrypted = models.TextField(blank=True)
    mail_dkim_domain = models.CharField(_("DKIM domain"), max_length=200, blank=True)
    mail_dkim_selector = models.CharField(_("DKIM selector"), max_length=63, blank=True)
    mail_dkim_key_encrypted = models.TextField(blank=True)
    # u06: two backup blocks — the portal and the platforms' front ends. Each has its own password and two destinations,
    # a local folder and an S3 bucket, kept independent. One schedule per block (start + every N hours) makes the backup
    # and sends it to whichever destinations are set. The local folder keeps N days; the S3 bucket keeps its own (its
    # lifecycle) unless an explicit keep is set. accounts/appliance.py (portal) and accounts/fe_backup.py (platforms).
    # --- the portal ---
    backup_password_encrypted = models.TextField(blank=True)
    backup_local = models.BooleanField(_("to a local folder"), default=True)  # u08: each destination has its own switch
    backup_path = models.CharField(_("local folder"), max_length=300, blank=True, default="/var/backups/claugine")
    backup_owner = models.CharField(_("local backups owner (user:group)"), max_length=64, blank=True, default="",
                                    help_text=_("The local backup files are set to this owner, e.g. portal:portal "
                                                "(the portal runs as root; NFS creates them as nobody otherwise). "
                                                "Empty: the owner of the local folder."))
    backup_start = models.TimeField(_("first backup of the day"), default="00:45")
    backup_period_hours = models.PositiveSmallIntegerField(_("every, hours"), default=6)
    backup_keep_days = models.PositiveSmallIntegerField(_("keep local backups, days"), default=10)
    backup_s3 = models.BooleanField(_("to an S3 bucket"), default=False)
    backup_s3_endpoint = models.CharField(_("S3 endpoint"), max_length=300, blank=True)
    backup_s3_region = models.CharField(_("S3 region"), max_length=64, default="us-east-1")
    backup_s3_bucket = models.CharField(_("S3 bucket"), max_length=200, blank=True)
    backup_s3_prefix = models.CharField(_("S3 prefix"), max_length=200, default="", blank=True)  # u08: empty = one dir per backup
    backup_s3_access_key = models.CharField(_("S3 access key"), max_length=200, blank=True)
    backup_s3_secret_encrypted = models.TextField(blank=True)
    backup_s3_style = models.CharField(_("S3 addressing"), max_length=8, default="path",
                                       choices=[("path", _("path style")), ("virtual", _("virtual-hosted style"))])
    backup_s3_keep_days = models.PositiveSmallIntegerField(_("S3: keep, days"), null=True, blank=True,
                                                           help_text=_("Empty: the bucket's own lifecycle keeps them."))
    # u07: the S3 copy can run on its own schedule; empty falls back to the local schedule above
    backup_s3_start = models.TimeField(_("S3: start at"), null=True, blank=True)
    backup_s3_period_hours = models.PositiveSmallIntegerField(_("S3: every, hours"), null=True, blank=True)
    backup_requested_at = models.DateTimeField(null=True, blank=True)
    backup_last_at = models.DateTimeField(null=True, blank=True)
    backup_last_result = models.TextField(blank=True)
    backup_local_last_at = models.DateTimeField(null=True, blank=True)  # u07: per-destination schedule tracking
    backup_s3_last_at = models.DateTimeField(null=True, blank=True)
    # --- the platforms' front ends (accounts/fe_backup.py), replacing claugine-cli jobs/fe-backup.sh ---
    fe_backup = models.BooleanField(_("back up the platforms' front ends"), default=False)  # u08: derived = local or S3
    fe_backup_local = models.BooleanField(_("to a local folder"), default=False)  # u08: the platforms' own switches
    fe_backup_platforms = models.JSONField(_("platforms to include"), default=list, blank=True,
                                           help_text=_("Empty: every platform with a front-end secret."))
    fe_backup_password_encrypted = models.TextField(blank=True)
    fe_backup_dirs = models.TextField(_("folders of each front-end node"), blank=True,
                                      default="/etc/one\n/var/lib/one/.ssh\n/var/lib/one/.one\n/var/lib/one/remotes")
    fe_backup_path = models.CharField(_("local folder"), max_length=300, blank=True)
    fe_backup_time = models.TimeField(_("first backup of the day"), default="05:25")
    fe_backup_period_hours = models.PositiveSmallIntegerField(_("every, hours"), default=24)
    fe_backup_keep_days = models.PositiveSmallIntegerField(_("keep local backups, days"), default=10)
    fe_backup_s3 = models.BooleanField(_("to an S3 bucket"), default=False)
    fe_backup_s3_use_portal = models.BooleanField(_("use the Portal's S3 settings"), default=True)  # u07
    fe_backup_s3_endpoint = models.CharField(_("S3 endpoint"), max_length=300, blank=True)
    fe_backup_s3_region = models.CharField(_("S3 region"), max_length=64, default="us-east-1")
    fe_backup_s3_bucket = models.CharField(_("S3 bucket"), max_length=200, blank=True)
    fe_backup_s3_prefix = models.CharField(_("S3 prefix"), max_length=200, default="", blank=True)  # u08: empty = one dir per backup
    fe_backup_s3_access_key = models.CharField(_("S3 access key"), max_length=200, blank=True)
    fe_backup_s3_secret_encrypted = models.TextField(blank=True)
    fe_backup_s3_style = models.CharField(_("S3 addressing"), max_length=8, default="path",
                                          choices=[("path", _("path style")), ("virtual", _("virtual-hosted style"))])
    fe_backup_s3_keep_days = models.PositiveSmallIntegerField(_("S3: keep, days"), null=True, blank=True,
                                                             help_text=_("Empty: the bucket's own lifecycle keeps them."))
    # u07: the platforms' S3 copy can run on its own schedule; empty falls back to the local schedule above
    fe_backup_s3_start = models.TimeField(_("S3: start at"), null=True, blank=True)
    fe_backup_s3_period_hours = models.PositiveSmallIntegerField(_("S3: every, hours"), null=True, blank=True)
    fe_backup_requested_at = models.DateTimeField(null=True, blank=True)
    fe_backup_last_at = models.DateTimeField(null=True, blank=True)
    fe_backup_last_result = models.TextField(blank=True)
    fe_backup_local_last_at = models.DateTimeField(null=True, blank=True)  # u07: per-destination schedule tracking
    fe_backup_s3_last_at = models.DateTimeField(null=True, blank=True)

    def fe_backup_dir_list(self):
        """The front-end folders to back up (u06: shown as a +/- list; stored one per line)."""
        return [d.strip() for d in (self.fe_backup_dirs or "").splitlines() if d.strip().startswith("/")]

    def fe_backup_s3_conf_source(self):
        """u06: the platforms' S3 connection, or the portal's if the platform one is left empty ("try to get it")."""
        return self if (self.fe_backup_s3_endpoint and self.fe_backup_s3_bucket) else None

    # the old management system is no longer read (2026-10-28: the portal is the only system managing the platforms)
    service_secret = models.CharField(max_length=40, blank=True,
                                      help_text="the portal's own ssh key on project routers (a user secret, made on first use)")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)

    def set_secret(self, field, raw):
        from inventory.crypto import encrypt
        setattr(self, f"{field}_encrypted", encrypt(raw) if raw else "")

    def get_secret(self, field):
        from inventory.crypto import decrypt
        v = getattr(self, f"{field}_encrypted")
        return decrypt(v) if v else ""

    def has_secret(self, field):
        return bool(getattr(self, f"{field}_encrypted"))

    @property
    def corporate_domain_list(self):
        return sorted({d.strip().lower().lstrip("@").lstrip(".") for d in self.corporate_domains.split() if d.strip()})

    def is_corporate_email(self, email):
        domain = (email or "").rsplit("@", 1)[-1].strip().lower()
        return bool(domain) and any(domain == d or domain.endswith("." + d) for d in self.corporate_domain_list)

    # corrections-18: the timers (Settings → Data): {key: {minutes, last_at, state, detail, started_at}}
    timers = models.JSONField(default=dict, blank=True)
    # owner 2026-10-31: HAProxy on the portal VMs (names, front ends, public ports): accounts/haproxy.py
    haproxy = models.JSONField(default=dict, blank=True)
    # owner 2026-10-31: the range the ssh port of a new internet gateway is chosen from (10000-59999)
    igw_port_min = models.PositiveIntegerField(_("igw ssh ports from"), default=50000)
    igw_port_max = models.PositiveIntegerField(_("igw ssh ports to"), default=59999)
    # owner 2026-10-31: new releases from GitHub installed by themselves on both nodes (accounts/autoupdate.py); off
    auto_update = models.BooleanField(_("automatic updates"), default=False)
    auto_update_state = models.JSONField(default=dict, blank=True)
    # owner 2026-11-04: which releases licensed updates offer when the control plane drives them (cp.example.com).
    update_stream = models.CharField(_("update stream"), max_length=8, default="stable",
                                     choices=[("stable", _("stable only")), ("all", _("all releases (incl. preview)"))],
                                     help_text=_("Stable only, or also preview releases. Used when updates come from "
                                                 "the licence control plane."))
    # u01: Settings → Reports (accounts/reports.py): redirect_to, items {key: on, period, weekday, time, redirect}, sent
    reports = models.JSONField(default=dict, blank=True)
    # u03: users may be added (a record without sign-in, invited later) besides being invited; off: only invited
    users_add = models.BooleanField(_("users can be added before they are invited"), default=True)
    # the provider (owner 2026-10-29 16:00): its name in the portal and the e-mails, and its logo (PNG, kept here)
    provider_name = models.CharField(_("provider name"), max_length=100, default="Example Cloud")
    logo = models.BinaryField(null=True, blank=True, editable=False)
    logo_updated_at = models.DateTimeField(null=True, blank=True)

    @classmethod
    def get(cls):
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj


class Organization(models.Model):
    """Legal entity. Not represented in OpenNebula."""

    tax_id = models.CharField(_("IIN/BIN"), max_length=12, unique=True, null=True, blank=True)
    tax_id_type = models.CharField(_("tax ID type"), max_length=3, choices=TaxIdType.choices, blank=True, default=TaxIdType.BIN)
    name = models.CharField(_("legal name"), max_length=300)
    kind = models.CharField(
        _("type"), max_length=16, choices=OrgKind.choices, default=OrgKind.CLOUD,
        help_text=_("Corporate: set when the admin registers with a corporate email domain, or by staff invitation."),
    )
    short_name = models.CharField(
        _("short name"), max_length=15, unique=True, null=True, blank=True, validators=[org_short_name_validator],
        help_text=_("Used in project IDs: _<short name>_<project>. Fixed once a project group exists."),
    )
    sales_rep = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="sales_organizations", limit_choices_to={"is_sales": True}, verbose_name=_("sales rep"),
    )
    kam = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="kam_organizations", limit_choices_to={"is_kam": True}, verbose_name=_("key account manager"),
    )
    resource_manager = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="rm_organizations", limit_choices_to={"is_resource_manager": True},
        verbose_name=_("resource manager"),
        help_text=_("Approves this organization's projects. Empty: any resource manager."),
    )
    support_engineer = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="support_organizations", limit_choices_to={"is_support": True},
        verbose_name=_("support engineer"),
        help_text=_("Empty: any support engineer."),
    )
    contact_email = models.EmailField(_("contact email"), blank=True)
    contact_phone = models.CharField(_("contact phone"), max_length=32, blank=True)
    default_ssh_key = models.TextField(_("default ssh public key"), blank=True,
                                       help_text=_("Offered for the igw of every new project of the organization."))
    notes = models.TextField(_("notes"), blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    archived_at = models.DateTimeField(_("archived"), null=True, blank=True,
                                       help_text=_("Organizations are archived, never deleted; they can be brought back."))
    archived_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # correction 16: an organization registered on the organizations' portal waits for approval (its users cannot sign
    # in until then): the resource manager first (assigns the platform users), then the sales rep when sales approval
    # is on in Settings. Organizations made by staff or by invitation are approved from the start.
    registration = models.CharField(_("registration"), max_length=16, default="approved",
                                    choices=[("pending", _("Waiting for approval")), ("approved", _("Approved"))])
    city = models.CharField(_("city"), max_length=100, blank=True)
    reg_rm_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reg_rm_at = models.DateTimeField(null=True, blank=True)
    reg_sales_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reg_sales_at = models.DateTimeField(null=True, blank=True)
    # owner 2026-10-29 20:30: what the check of the registration found (accounts.regcheck): score, flags, address
    reg_check = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def is_pending(self):
        return self.registration == "pending"

    @property
    def kind_locked(self):
        """corrections-18: the type cannot change once the organization has a tenant."""
        return bool(self.pk) and self.tenants.exists()

    @property
    def is_archived(self):
        return self.archived_at is not None

    def archive_blockers(self):
        """Why this organization cannot be archived: tenants that are not archived."""
        n = self.tenants.exclude(status=Lifecycle.ARCHIVED).exclude(approval__in=[Approval.REJECTED, Approval.CANCELLED]).count()
        return [_("%(n)s projects are not archived") % {"n": n}] if n else []

    @property
    def admins(self):
        return self.users.filter(is_org_admin=True, is_active=True)

    @property
    def days_on_platform(self):  # u05: shown in the organizations list
        return (timezone.localdate() - timezone.localtime(self.created_at).date()).days if self.created_at else None

    @property
    def is_corp(self):
        return self.kind == OrgKind.CORP

    @property
    def effective_support_engineer(self):
        return self.support_engineer

    def save(self, *args, **kwargs):
        if self.kind == OrgKind.CORP:
            # Corporate organizations: a resource manager, a support engineer and optionally a sales rep (corrections-18;
            # one with a corporate e-mail domain); no KAM.
            self.kam = None
        if not self.short_name and self.name:
            from .naming import suggest_org_short_name
            self.short_name = suggest_org_short_name(self.name, exclude_pk=self.pk)
        super().save(*args, **kwargs)

    @property
    def short_name_locked(self):
        """Fixed once a group _<short name>_... (or _t_<short name>_...) exists, or a tenant is deployed (its gateways' names carry the short name,
        correction 12)."""
        if not self.short_name:
            return False
        live = self.tenants.exclude(status=Lifecycle.ARCHIVED)
        return (self.tenants.filter(group_name__startswith=f"_t_{self.short_name}_").exists()
                or self.tenants.filter(group_name__startswith=f"_{self.short_name}_").exists()
                or live.filter(one_group_id__isnull=False).exists() or live.filter(deployments__state="done").exists())

    @property
    def short_name_editable(self):
        """u02: the short name can be changed only while the organization has no projects (requests that were
        rejected or cancelled do not count) and nothing on the platforms uses it."""
        if self.short_name_locked:
            return False
        return not (self.pk and self.tenants.exclude(approval__in=[Approval.REJECTED, Approval.CANCELLED]).exists())

    def deletion_blockers(self):
        """Why this organization cannot be deleted; empty if it has no history."""
        from accounts.models import Invitation, User
        reasons = []
        if self.tenants.exists():
            reasons.append(_("it has projects"))
        if User.objects.filter(organization=self).exists():
            reasons.append(_("it has users"))
        if self.join_requests.exists():
            reasons.append(_("it has join requests"))
        if Invitation.objects.filter(organization=self).exists():
            reasons.append(_("invitations were sent for it"))
        return reasons

    def visible_to(self, user):
        if not user.is_authenticated:
            return False
        if user.is_staff_member:
            if user.is_super_admin or user.has_role(Role.RESOURCE_MANAGER) or user.has_role(Role.SUPPORT):
                return True
            return (user.is_sales and self.sales_rep_id == user.pk) or (user.is_kam and self.kam_id == user.pk)
        return user.organization_id == self.pk


class Tenant(models.Model):
    """A project with its own cost center: the OpenNebula group tenant-<name>."""

    organization = models.ForeignKey(
        Organization, null=True, blank=True, on_delete=models.PROTECT, related_name="tenants",
        verbose_name=_("organization"),
    )
    name = models.CharField(
        _("short name"), max_length=32, null=True, blank=True, validators=[tenant_name_validator],
        help_text=_("Unique within the organization. Project ID (OpenNebula group): _<organization>-<short name>."),
    )
    group_name = models.CharField(
        _("OpenNebula group"), max_length=32, unique=True, null=True, blank=True,
        help_text=_("Fixed on approval. Adopted projects keep their old group project-<name>."),
    )
    title = models.CharField(_("project title"), max_length=200, blank=True)
    kind = models.CharField(_("type"), max_length=16, choices=TenantKind.choices, default=TenantKind.CLOUD)
    stage = models.CharField(_("stage"), max_length=16, choices=TenantStage.choices, default=TenantStage.POC)
    order = models.JSONField(_("order"), default=dict, blank=True,
                             help_text="What the customer asked for, and the site/cluster staff chose.")
    origin = models.CharField(_("origin"), max_length=16, choices=Origin.choices, default=Origin.PORTAL)
    approval = models.CharField(_("approval"), max_length=16, choices=Approval.choices, default=Approval.PENDING)
    status = models.CharField(_("status"), max_length=16, choices=Lifecycle.choices, default=Lifecycle.OK)
    date_review = models.DateField(_("review date"), null=True, blank=True,
                                   help_text=_("The day the current status (or the POC) ends and is reviewed."))
    status_changed_at = models.DateTimeField(null=True, blank=True)

    # cost center (u05: contract number and date, and the balance feature, removed)
    contract_end = models.DateField(_("contract end"), null=True, blank=True,
                                    help_text=_("Production contract expiration: the organization's admins are told before it."))
    notes = models.TextField(_("notes"), blank=True)  # u05: staff notes on the project

    # approvals of the tenant itself
    rm_approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    rm_approved_at = models.DateTimeField(null=True, blank=True)
    support_approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    support_approved_at = models.DateTimeField(null=True, blank=True)
    sales_approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    sales_approved_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)

    # OpenNebula
    one_group_id = models.IntegerField(_("OpenNebula group ID"), null=True, blank=True)
    one_admin_user = models.CharField(_("OpenNebula group admin"), max_length=128, blank=True,
                                      help_text=_("The OpenNebula user associated with the project: the admin of its group."))
    one_admin_id = models.IntegerField(null=True, blank=True)

    # access chosen with the order: the OpenNebula admin's password (stored encrypted, shown to tenant admins)
    # and the igw's administrator (user name and ssh key; password login to the igw is disabled)
    admin_password_encrypted = models.TextField(blank=True)
    igw_username = models.CharField(_("igw user name"), max_length=32, blank=True)
    igw_ssh_key = models.TextField(_("igw ssh public key"), blank=True)
    igw_ssh_port = models.PositiveIntegerField(_("igw ssh port"), null=True, blank=True,
                                               help_text=_("Random, from the range in Settings → Data, chosen with the order."))
    igw_password_encrypted = models.TextField(blank=True, help_text="igw user's password (console; ssh where allowed)")
    igw_host = models.CharField(_("igw address"), max_length=100, blank=True,
                                help_text=_("The address the igw is reached at over ssh. Empty: its main address."))
    # an imported tenant's igw (the old system's VyOS): its configuration uploaded and checked, then a new igw made
    # from it (adopted) or left as it is to be done with the tenant's staff (not adopted)
    igw_adoption = models.CharField(_("igw adoption"), max_length=16, blank=True,
                                    choices=[("", _("not checked")), ("ready", _("configuration transferable")),
                                             ("adopted", _("adopted: new igw made")),
                                             ("not_adopted", _("imported, not adopted"))])
    igw_adoption_note = models.TextField(blank=True)
    igw_config_encrypted = models.TextField(blank=True, help_text="the old igw's configuration (set commands), encrypted")
    igw_ssh_from = models.TextField(_("igw ssh allowed from"), blank=True, default="0.0.0.0/0",
                                    help_text=_("Networks allowed to reach the igw over ssh, one per line or comma separated."))

    # correction 15: the person responsible for the tenant on the client's side — a user of its organization; gets the
    # credentials when the tenant is provisioned
    tenant_admin = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="admin_tenants",
        verbose_name=_("project admin"), help_text=_("A user of the project's organization: gets the credentials."))

    # corporate tenants: their own resource manager and support engineer (else the organization's / the default)
    resource_manager = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="rm_tenants",
        limit_choices_to={"is_resource_manager": True}, verbose_name=_("resource manager"),
        help_text=_("Corporate projects: the manager responsible for it. Empty: the organization's."),
    )
    support_engineer = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="support_tenants",
        limit_choices_to={"is_support": True}, verbose_name=_("support engineer"),
        help_text=_("Empty: the organization's."),
    )

    # test tenants (adoption mode)
    test_expires_at = models.DateTimeField(null=True, blank=True)

    igw_cpu = models.PositiveSmallIntegerField(_("igw vCPU"), default=1)  # owner 2026-10-29 18:58: 1 / 1 GB by default
    igw_ram_gb = models.PositiveSmallIntegerField(_("igw RAM, GB"), default=1)
    igw_vm = models.JSONField(default=dict, blank=True,
                              help_text="the igw VM found on the platform (tenants.igwdetect): the VM holding the address "
                                        "after the egw's on the main subnet — id, name, image, type, status, at")
    gw_status = models.JSONField(default=dict, blank=True,
                                 help_text="the last check of the gateways (correction 11): {role: {at, ok, detail}}")

    @property
    def igw_address(self):
        """The igw's ssh address: the one set, else the one found on the platform (tenants.igwdetect)."""
        return (self.igw_host or (self.igw_vm or {}).get("ip") or "").strip()

    @property
    def igw_ssh_line(self):
        """A ready ssh command for the igw when its address and user are known."""
        ip = self.igw_address
        # -l user / host (not user@host) so an email masker (browser add-on, proxy) can't mangle the command
        return f"ssh -p {self.igw_ssh_port or 22} -l {self.igw_username} {ip}" if ip and self.igw_username else ""
    # adoption: what the old system says, kept as hints for staff
    source_present = models.BooleanField(default=True, help_text="Present in the last import from the old system.")
    hint_legal_name = models.CharField(max_length=500, blank=True)
    hint_status = models.CharField(max_length=32, blank=True)
    hint_contacts = models.TextField(blank=True)
    hint_manager_email = models.CharField(max_length=254, blank=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(_("last modification"), auto_now=True, null=True)
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name", "pk"]

    def __str__(self):
        return self.name or self.title or f"#{self.pk}"

    @property
    def gateways_deployed(self):
        """An egw, cgw or L2 gateway of the tenant was made (a finished deployment, or L2 channels): its name is fixed
        (correction 12) — the gateways' names carry it."""
        return self.deployments.filter(state="done").exists() or self.l2_channels.exists()

    @property
    def one_group_name(self):
        """The group's name in OpenNebula: fixed, legacy, or what approval would give it."""
        from .naming import group_name
        if self.group_name:
            return self.group_name
        if self.origin == Origin.ADOPTED and self.name:
            return f"tenant-{self.name}"
        org = self.organization if self.organization_id else None
        if self.name and org and org.short_name:
            return group_name(org.short_name, self.name)
        return ""

    @property
    def follows_naming(self):
        from .naming import GROUP_RE
        return bool(GROUP_RE.match(self.one_group_name or ""))

    @property
    def sales_rep(self):
        return self.organization.sales_rep if self.organization_id else None

    @property
    def is_test(self):
        return self.origin == Origin.TEST

    # --- approvals ---------------------------------------------------------

    def required_approvals(self):
        """Roles whose approval a new tenant (or a change of it) needs."""
        from accounts.models import User
        conf = PortalSettings.get()
        roles = [Role.RESOURCE_MANAGER]
        if self.kind == TenantKind.CORP:
            return roles  # correction 13: the support engineer provisions, he does not approve
        # Sales signs off only when the switch is on and the organization actually has a rep;
        # with nobody to ask, the resource manager's approval is enough.
        rep = self.sales_rep
        if conf.sales_approval_required and rep is not None and rep.is_active and rep.login_enabled:
            roles.append(Role.SALES_MANAGER)
        return roles

    def approved_by_role(self, role):
        return bool({
            Role.RESOURCE_MANAGER: self.rm_approved_at,
            Role.SUPPORT: self.support_approved_at,
            Role.SALES_MANAGER: self.sales_approved_at,
        }[role])

    def rm_step_by_super_admin(self):
        """The resource manager step is done by the super admin: switched off, or no resource manager can sign in."""
        from accounts.models import User
        if not PortalSettings.get().rm_approval_required:
            return True
        return not User.objects.filter(kind="staff", is_resource_manager=True, is_active=True, login_enabled=True).exists()

    def step_label(self, role):
        if role == Role.RESOURCE_MANAGER and self.rm_step_by_super_admin():
            return _("super admin")
        return {Role.RESOURCE_MANAGER: _("resource manager"), Role.SUPPORT: _("support engineer"),
                Role.SALES_MANAGER: _("sales rep")}[role]

    def missing_labels(self):
        return [self.step_label(r) for r in self.missing_approvals()]

    def missing_approvals(self):
        return [r for r in self.required_approvals() if not self.approved_by_role(r)]

    def finalize_if_complete(self):
        if self.approval == Approval.PENDING and not self.missing_approvals():
            self.approval = Approval.APPROVED
            self.approved_at = timezone.now()
            return True
        return False

    @property
    def is_closed_request(self):
        """A request that never became a tenant: it can be deleted outright."""
        return self.approval in (Approval.REJECTED, Approval.CANCELLED) and not self.one_group_id

    def _customer_admin(self, user):
        return self.membership_role(user) == Membership.Role.ORG_ADMIN

    @property
    def effective_resource_manager(self):
        return self.resource_manager or (self.organization.resource_manager if self.organization_id else None)

    @property
    def effective_support_engineer(self):
        if self.support_engineer_id:
            return self.support_engineer
        return self.organization.support_engineer if self.organization_id else None

    def _assigned(self, role):
        return self.effective_resource_manager if role == Role.RESOURCE_MANAGER else self.effective_support_engineer

    def can_approve_as(self, user, role):
        """The organization's own resource manager / support engineer approves when one is assigned
        and can sign in; otherwise anyone with the role. The super admin always can."""
        if not (user.is_authenticated and user.is_staff_member):
            return False
        if user.is_super_admin:  # correction 16: the super admin has every right (asked only in the roles held)
            return True
        if role == Role.SALES_MANAGER:
            return bool(self.sales_rep) and self.sales_rep.pk == user.pk
        if role == Role.RESOURCE_MANAGER and not PortalSettings.get().rm_approval_required:
            return False  # switched off: the super admin approves
        if not user.has_role(role):
            return False
        assigned = self._assigned(role)
        if assigned is None or not (assigned.is_active and assigned.login_enabled):
            return True
        return assigned.pk == user.pk

    def approvers(self, role):
        """Who is asked to approve: the assigned person, or everybody with the role."""
        from accounts.models import User
        if role == Role.SALES_MANAGER:
            return [self.sales_rep] if self.sales_rep else []
        if role == Role.RESOURCE_MANAGER and not PortalSettings.get().rm_approval_required:
            supers = User.objects.filter(is_super_admin=True, is_active=True, login_enabled=True)
            return list(supers.filter(is_resource_manager=True)) or list(supers)  # correction 16: those with the role
        assigned = self._assigned(role)
        if assigned is not None and assigned.is_active and assigned.login_enabled:
            return [assigned]
        return list(User.objects.with_role(role).filter(login_enabled=True))

    def asks(self, user, role):
        """Correction 16: this approval waits for `user` — they are asked (the list "waiting for me", the counts,
        the e-mails): a super admin only in the roles ticked for them (a role nobody active holds: every super admin)."""
        if not self.can_approve_as(user, role):
            return False
        if not user.is_super_admin:
            return True
        if role == Role.SALES_MANAGER:
            return bool(self.sales_rep) and self.sales_rep.pk == user.pk
        if role == Role.RESOURCE_MANAGER and not PortalSettings.get().rm_approval_required:
            return user in self.approvers(role)
        from accounts.models import User
        if not user.holds(role):
            return not User.objects.filter(kind="staff", is_active=True, login_enabled=True,
                                           **{_role_field(role): True}).exists()
        assigned = self._assigned(role)
        if assigned is None or not (assigned.is_active and assigned.login_enabled):
            return True
        return assigned.pk == user.pk

    def _approving_staff(self, user):
        return user.is_authenticated and user.is_staff_member and (
            user.is_super_admin or user.has_role(Role.RESOURCE_MANAGER))

    def can_cancel(self, user):
        return self.approval == Approval.PENDING and (self._customer_admin(user) or self._approving_staff(user))

    def can_delete(self, user):
        return self.is_closed_request and (self._customer_admin(user) or self._approving_staff(user))

    # --- deleting a tenant ----------------------------------------------------

    @property
    def is_deleted(self):
        """Nothing (or only the backups of a saved tenant) is left on the platform."""
        return self.status in GONE

    @property
    def is_archived(self):
        return self.status == Lifecycle.ARCHIVED

    @property
    def on_platform_since(self):
        """When the project went onto the platform: its first finished deployment, else when it was created."""
        dep = self.deployments.filter(state="done").order_by("started_at").first()
        start = dep.started_at if (dep and dep.started_at) else self.created_at
        return timezone.localtime(start).date() if start else None

    @property
    def days_on_platform(self):
        since = self.on_platform_since
        return (timezone.localdate() - since).days if since else None

    @property
    def review_due(self):
        """The review date has come (a POC's end, or the end of paused / suspended)."""
        return bool(self.date_review and self.date_review <= timezone.localdate()
                    and self.status in (Lifecycle.OK, Lifecycle.PAUSED, Lifecycle.SUSPENDED))

    REVIEW_SOON_DAYS = {"poc": 7, "client": 92}  # u01: highlighted this close to the review date (POC a week, client 3 months)

    @property
    def review_alert(self):
        """"due" (the review date has come), "soon" (within a week for a POC, 3 months for a client) or ""."""
        if not self.date_review or self.status in GONE:
            return ""
        left = (self.date_review - timezone.localdate()).days
        if left <= 0:
            return "due"
        return "soon" if left <= self.REVIEW_SOON_DAYS.get(self.stage, 0) else ""

    POC_DAYS, POC_MAX_DAYS = 30, 92  # a POC is reviewed a month after creation; sales may extend it up to 3 months

    def poc_review_limit(self):
        from datetime import timedelta
        start = (self.created_at or timezone.now()).date()
        return start + timedelta(days=self.POC_MAX_DAYS)

    def status_choices(self):
        """The statuses this tenant can be given by hand now."""
        return [(v, Lifecycle(v).label) for v in STATUS_NEXT.get(self.status, ())]

    def can_change_status(self, user):
        return (self.approval == Approval.APPROVED and self.status != Lifecycle.DELETING and bool(STATUS_NEXT.get(self.status))
                and user.is_authenticated and (user.is_super_admin or user.has_role(Role.RESOURCE_MANAGER)))

    def can_change_review(self, user):
        """Resource managers and the super admin: any date; the sales rep: a POC's, up to 3 months after creation."""
        if not (user.is_authenticated and user.is_staff_member) or self.is_archived:
            return False
        if user.is_super_admin or user.has_role(Role.RESOURCE_MANAGER):
            return True
        return (self.stage == TenantStage.POC and user.is_sales and self.organization_id
                and self.organization.sales_rep_id == user.pk)

    def can_request_deletion(self, user):
        """u12: an org admin, the org's manager, a resource manager or the super admin may delete a project — not a
        KAM and not a project admin (they manage resources, not the project itself)."""
        if self.approval != Approval.APPROVED or self.is_deleted:
            return False
        if self.membership_role(user) == Membership.Role.ORG_ADMIN:
            return True
        from .permissions import manages_org  # super admin, resource manager, or the org's manager
        org = self.organization if self.organization_id else None
        return manages_org(user, org)

    def can_delete_now(self, user):
        """Resource managers and the super admin delete a tenant at once (admin panel)."""
        return self.approval == Approval.APPROVED and not self.is_deleted and self._approving_staff(user)

    def can_change_stage(self, user):
        if not (user.is_authenticated and user.is_staff_member):
            return False
        if user.is_super_admin or user.has_role(Role.RESOURCE_MANAGER) or user.has_role(Role.SUPPORT):
            return True
        return user.is_sales and self.organization_id and self.organization.sales_rep_id == user.pk

    # --- access ------------------------------------------------------------

    def membership_role(self, user):
        if user.is_authenticated and user.is_staff_member:
            # Support engineers and super admins can be users of a corporate tenant.
            if self.kind != TenantKind.CORP or not (user.is_support or user.is_super_admin):
                return None
            m = self.memberships.filter(user=user).first()
            return m.role if m else None
        if not user.is_authenticated or not user.is_customer or user.organization_id != self.organization_id:
            return None
        if user.is_org_admin:
            return Membership.Role.ORG_ADMIN
        m = self.memberships.filter(user=user).first()
        if m:
            return m.role
        return Membership.Role.TENANT_USER if user.is_org_readonly else None

    def visible_to(self, user):
        if not user.is_authenticated:
            return False
        if user.is_staff_member:
            if user.is_super_admin or user.has_role(Role.RESOURCE_MANAGER) or user.has_role(Role.SUPPORT):
                return True
            return bool(self.organization_id) and (
                (user.is_sales and self.organization.sales_rep_id == user.pk)
                or (user.is_kam and self.organization.kam_id == user.pk))
        return self.membership_role(user) is not None

    IGW_PORT_LIMITS = (10000, 59999)  # the range in Settings → Data stays within these (owner 2026-10-31)

    @staticmethod
    def igw_ports():
        """(first, last) ssh port of a new igw: Settings → Data."""
        s = PortalSettings.get()
        lo, hi = Tenant.IGW_PORT_LIMITS
        a, b = max(lo, min(s.igw_port_min, hi)), max(lo, min(s.igw_port_max, hi))
        return (a, b) if a <= b else (b, a)

    @classmethod
    def random_igw_port(cls):
        import secrets
        a, b = cls.igw_ports()
        return a + secrets.randbelow(b - a + 1)

    def set_igw_password(self, raw, by=None):
        """Keep the igw password (encrypted); the one it replaces goes to the history (correction 14)."""
        from inventory.crypto import encrypt
        if self.pk and self.igw_password_encrypted and raw:
            IgwPasswordHistory.objects.create(tenant=self, password_encrypted=self.igw_password_encrypted, created_by=by)
        self.igw_password_encrypted = encrypt(raw) if raw else ""

    def igw_ssh_from_list(self):
        return parse_cidrs(self.igw_ssh_from) or ["0.0.0.0/0"]

    def set_igw_config(self, text):
        from inventory.crypto import encrypt
        self.igw_config_encrypted = encrypt(text) if text else ""

    def get_igw_config(self):
        from inventory.crypto import decrypt
        return decrypt(self.igw_config_encrypted) if self.igw_config_encrypted else ""

    def get_igw_password(self):
        from inventory.crypto import decrypt
        return decrypt(self.igw_password_encrypted) if self.igw_password_encrypted else ""

    def set_admin_password(self, raw):
        """Store the password (encrypted, viewable by the tenant's admins) and keep its hash in the history."""
        from django.contrib.auth.hashers import make_password
        from inventory.crypto import encrypt
        self.admin_password_encrypted = encrypt(raw) if raw else ""
        if raw and self.pk:
            PasswordHistory.objects.create(tenant=self, password_hash=make_password(raw))

    def password_history(self):
        from accounts.policy import policy
        return list(self.password_hashes.values_list("password_hash", flat=True)[:policy()["history"]])

    def get_admin_password(self):
        from inventory.crypto import decrypt
        return decrypt(self.admin_password_encrypted) if self.admin_password_encrypted else ""

    @property
    def fireedge_public_url(self):
        """Where the tenant opens OpenNebula: its platform's public FireEdge URL (a secondary zone: its primary's)."""
        if not hasattr(self, "_fe_url"):
            from inventory.models import Platform
            key = (self.order or {}).get("platform")
            p = Platform.objects.filter(key=key).only("public_url", "fireedge_port", "fe_primary").first() if key else None
            url = p.fireedge_link if p else ""
            if p and not url and p.fe_primary:
                q = Platform.objects.filter(key=p.fe_primary).only("public_url", "fireedge_port").first()
                url = q.fireedge_link if q else ""
            self._fe_url = url
        return self._fe_url

    @property
    def one_admin_name(self):
        """The tenant's OpenNebula admin: as recorded (chosen at provisioning, or read from the platform); else
        <org>-<tenant name> (correction 13), <org>-<tenant>-admin for the portal's _t_ groups."""
        from .naming import GROUP_RE, LEGACY_PREFIX, admin_name
        if self.one_admin_user:
            return self.one_admin_user
        g = self.one_group_name or ""
        m = GROUP_RE.match(g)
        if m and g.startswith(LEGACY_PREFIX):
            return f"{m.group(1)}-{m.group(2)}-admin"  # the portal's tenants before correction 13
        if m:
            return admin_name(m.group(1), self.name or m.group(2))  # <org>-<project>-adm (owner 2026-10-31)
        return ""

    @property
    def last_deployment(self):
        return self.deployments.exclude(state="plan").first()

    def can_order(self, user):
        return self.membership_role(user) in (Membership.Role.ORG_ADMIN, Membership.Role.TENANT_ADMIN)

    # --- resources ---------------------------------------------------------

    def current_quota_records(self):
        return QuotaRecord.current_for(self)

    def current_connectivity(self):
        return Connectivity.current_for(self)


class Membership(models.Model):
    class Role(models.TextChoices):
        ORG_ADMIN = "org_admin", _("Organization admin")
        TENANT_ADMIN = "tenant_admin", _("Project admin")
        TENANT_USER = "tenant_user", _("Project user")

    TENANT_ROLES = [(Role.TENANT_ADMIN.value, Role.TENANT_ADMIN.label), (Role.TENANT_USER.value, Role.TENANT_USER.label)]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=16, choices=TENANT_ROLES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("user", "tenant")]


class Registration(models.Model):
    """Organization data kept until the applicant confirms the email."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="registration")
    tax_id = models.CharField(max_length=12)
    tax_id_type = models.CharField(max_length=3, choices=TaxIdType.choices)
    organization_name = models.CharField(max_length=300)
    reg_check = models.JSONField(default=dict, blank=True)  # owner 2026-10-29 20:30 (accounts.regcheck)
    created_at = models.DateTimeField(auto_now_add=True)


class JoinRequest(models.Model):
    """Someone registered with an IIN/BIN that already belongs to an organization."""

    class State(models.TextChoices):
        PENDING = "pending", _("Pending")
        ACCEPTED = "accepted", _("Accepted")
        REJECTED = "rejected", _("Rejected")

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="join_requests")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="join_requests")
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class Source(models.TextChoices):
    PORTAL = "portal", _("Portal")
    IMPORT = "import", _("Imported from the old system")
    MANUAL = "manual", _("Entered by staff")


VMS_SHARE_DEFAULT = 80  # % of the storage quota for VM disks; the rest is for images


def images_share(disk_gb, vms_pct=VMS_SHARE_DEFAULT):
    """(Before the single storage quota) GB for images when VM disks get vms_pct %. Kept for old records."""
    return int(round((disk_gb or 0) * (100 - vms_pct) / 100))


def default_split(total):
    """(images, files) of a storage quota when none is set: none (owner 2026-11-11: the free images and files tier is
    gone; a pool gets images or files only when they are asked for). VMs get it all."""
    return 0, 0


def fit_split(total, images, files):
    """Keep a split when it still fits the total, else the default one."""
    if images is not None and files is not None and images + files <= (total or 0):
        return images, files
    return default_split(total)


def split_of(rec):
    """(vms, images, files) GB of a quota record (or resource change): one storage number, split three ways."""
    images, files = fit_split(rec.disk_gb, rec.images_gb, rec.files_gb)
    return max((rec.disk_gb or 0) - images - files, 0), images, files


class QuotaRecord(models.Model):
    """One version of a tenant's allocation on one cluster (quota.md record). Append-only."""

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="quota_records")
    site = models.CharField(max_length=16)
    cluster = models.CharField(_("quota code"), max_length=16)
    version = models.PositiveIntegerField()
    removed = models.BooleanField(default=False, help_text="This version removes the record.")
    cpu = models.PositiveIntegerField(default=0)
    ram_gb = models.PositiveIntegerField(default=0)
    gpu = models.PositiveIntegerField(default=0)
    disk_gb = models.PositiveIntegerField(default=0)
    images_gb = models.PositiveIntegerField(null=True, blank=True)
    files_gb = models.PositiveIntegerField(null=True, blank=True)
    backups_gb = models.PositiveIntegerField(null=True, blank=True)
    effective_from = models.DateTimeField(default=timezone.now)
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.PORTAL)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    FIELDS = ("cpu", "ram_gb", "gpu", "disk_gb", "images_gb", "files_gb", "backups_gb")

    class Meta:
        unique_together = [("tenant", "site", "cluster", "version")]
        ordering = ["site", "cluster", "-version"]

    @property
    def split(self):
        """(vms, images, files) GB: disk_gb is the one storage number; images and files are parts of it."""
        return split_of(self)

    def values(self):
        return {f: getattr(self, f) for f in self.FIELDS}

    @classmethod
    def current_for(cls, tenant, at=None):
        at = at or timezone.now()
        rows = cls.objects.filter(tenant=tenant, effective_from__lte=at).order_by("site", "cluster", "-version")
        seen, out = set(), []
        for r in rows:
            key = (r.site, r.cluster)
            if key in seen:
                continue
            seen.add(key)
            if not r.removed:
                out.append(r)
        return out

    @classmethod
    def next_version(cls, tenant, site, cluster):
        v = cls.objects.filter(tenant=tenant, site=site, cluster=cluster).aggregate(m=Max("version"))["m"]
        return (v or 0) + 1


class Connectivity(models.Model):
    """One version of a tenant's connectivity on one site (quota.md site-level fields). Append-only.

    connection_type holds the routed connections, space-separated: "inet", "corpnet", "corpnet inet",
    or "none" when the tenant has only external L2 channels.
    """

    class Type(models.TextChoices):
        INET = "inet", _("Internet")
        CORPNET = "corpnet", _("Corporate network")
        NONE = "none", _("None (L2 only)")

    LABELS = {"inet": _("Internet"), "corpnet": _("Corporate network"), "none": _("None (L2 only)")}

    @staticmethod
    def type_value(conns):
        routed = sorted(c for c in conns if c in ("inet", "corpnet"))
        return " ".join(routed) or "none"

    @property
    def types(self):
        return (self.connection_type or "").split()

    @property
    def connection_display(self):
        return " + ".join(str(self.LABELS.get(t, t)) for t in self.types) or "—"

    class Gateway(models.TextChoices):
        VYOS = "vyos", _("advanced (VyOS)")
        ONEVR = "onevr", _("native (Service Virtual Router)")
        CUSTOM = "custom", _("custom (your own VM)")

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="connectivity")
    site = models.CharField(max_length=16)
    version = models.PositiveIntegerField()
    connection_type = models.CharField(max_length=32, blank=True)
    igw_type = models.CharField(max_length=16, choices=Gateway.choices, blank=True)
    inet_mbps = models.PositiveIntegerField(null=True, blank=True)
    inet_main_prefix = models.PositiveSmallIntegerField(null=True, blank=True)
    inet_public_prefixes = models.CharField(max_length=200, blank=True, help_text="Lengths, space-separated.")
    corpnet_prefixes = models.CharField(max_length=200, blank=True, help_text="Lengths, space-separated.")
    l2_vlans = models.CharField(max_length=200, blank=True)
    l2_vnets = models.CharField(max_length=200, blank=True)
    effective_from = models.DateTimeField(default=timezone.now)
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.PORTAL)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    FIELDS = ("connection_type", "igw_type", "inet_mbps", "inet_main_prefix", "inet_public_prefixes",
              "corpnet_prefixes", "l2_vlans", "l2_vnets")

    class Meta:
        unique_together = [("tenant", "site", "version")]
        ordering = ["site", "-version"]

    def values(self):
        return {f: getattr(self, f) for f in self.FIELDS}

    @classmethod
    def current_for(cls, tenant, at=None):
        at = at or timezone.now()
        rows = cls.objects.filter(tenant=tenant, effective_from__lte=at).order_by("site", "-version")
        seen, out = set(), []
        for r in rows:
            if r.site not in seen:
                seen.add(r.site)
                out.append(r)
        return out

    @classmethod
    def next_version(cls, tenant, site):
        v = cls.objects.filter(tenant=tenant, site=site).aggregate(m=Max("version"))["m"]
        return (v or 0) + 1


class PrefixAssignment(models.Model):
    """A subnet assigned to a tenant from a delegated pool (address registry)."""

    class Kind(models.TextChoices):
        INET_MAIN = "inet_main", _("Internet main prefix")
        INET_EXTRA = "inet_extra", _("Additional internet prefix")
        INET_NEXT = "inet_next", _("Next main internet prefix (replacement approved)")
        CORPNET = "corpnet", _("Corporate prefix")
        SERVICES = "services", _("Platform services (management cluster)")
        RESERVED = "reserved", _("Reserved")

    cidr = models.CharField(max_length=43)
    site = models.CharField(max_length=16)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    tenant = models.ForeignKey(Tenant, null=True, blank=True, on_delete=models.PROTECT, related_name="prefixes")
    note = models.CharField(max_length=200, blank=True)
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.PORTAL)
    released_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["site", "cidr"]

    def __str__(self):
        return self.cidr

    @classmethod
    def active(cls):
        return cls.objects.filter(released_at__isnull=True)


class Deployment(models.Model):
    """One run of the provisioning of a tenant on its platform: the steps and what was created."""

    class State(models.TextChoices):
        PLAN = "plan", _("preview")
        RUNNING = "running", _("running")
        DONE = "done", _("deployed")
        FAILED = "failed", _("failed")

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="deployments")
    platform_key = models.CharField(max_length=100)
    state = models.CharField(max_length=16, choices=State.choices, default=State.RUNNING)
    steps = models.JSONField(default=list, blank=True)
    resources = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    notify = models.JSONField(default=list, blank=True,
                              help_text="correction 13: the users (ids) who get the credentials when it is done")

    class Meta:
        ordering = ["-started_at"]

    STALE_MINUTES = 15

    @property
    def is_stale(self):
        """Still "running" long after it started: the process that ran it was stopped."""
        from datetime import timedelta
        return self.state == self.State.RUNNING and self.started_at < timezone.now() - timedelta(minutes=self.STALE_MINUTES)


class PasswordHistory(models.Model):
    """Hashes of the tenant admin's former passwords: the last ones may not be used again."""
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="password_hashes")
    password_hash = models.CharField(max_length=256)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]


class ResourceChange(models.Model):
    """A tenant's request to change its resources on one cluster; applied when a resource manager approves it."""

    class State(models.TextChoices):
        PENDING = "pending", _("waiting for approval")
        APPROVED = "approved", _("approved")
        REJECTED = "rejected", _("rejected")
        CANCELLED = "cancelled", _("cancelled")

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="resource_changes")
    site = models.CharField(max_length=16)
    cluster = models.CharField(_("quota code"), max_length=16)
    cpu = models.PositiveIntegerField(default=0)
    ram_gb = models.PositiveIntegerField(default=0)
    gpu = models.PositiveIntegerField(default=0)
    disk_gb = models.PositiveIntegerField(default=0, help_text="storage: VM disks, images and files together")
    images_gb = models.PositiveIntegerField(default=0, help_text="the part of disk_gb for images")
    files_gb = models.PositiveIntegerField(default=0, help_text="the part of disk_gb for files")
    backups_gb = models.PositiveIntegerField(default=0)
    note = models.TextField(blank=True)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(blank=True)
    applied = models.CharField(max_length=300, blank=True, help_text="what happened in OpenNebula on approval")
    requested = models.JSONField(default=dict, blank=True,
                                 help_text="what the project asked for, when the approver changed the values")
    pool = models.JSONField(default=dict, blank=True,
                            help_text="correction 15: {kind: add | remove | new_site, connectivity: {...}} — a resource "
                                      "pool added on another cluster or site, or removed; empty: a change of a pool")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    VALUES = ("cpu", "ram_gb", "gpu", "disk_gb", "images_gb", "files_gb", "backups_gb")

    @property
    def vms_gb(self):
        return split_of(self)[0]

    @property
    def changed_on_approval(self):
        """[(field, asked, approved)] where the approver changed the values."""
        return [(f, self.requested[f], getattr(self, f)) for f in self.VALUES
                if f in (self.requested or {}) and self.requested[f] != getattr(self, f)]


class NetworkRequest(models.Model):
    """A tenant asks for one more network: a public prefix, a corporate prefix or an L2 channel.

    A support engineer (or the super admin) approves it; for an L2 channel the approver names the external
    VLAN (the OpenNebula network on the external side). Approved prefixes are assigned from the delegated
    subnets; approved items are created in OpenNebula by the next deployment run.
    """

    class Kind(models.TextChoices):
        INET = "inet", _("Internet")
        CORPNET = "corpnet", _("Corporate")
        L2 = "l2", _("L2")
        IGW = "igw", _("igw type")

    class Action(models.TextChoices):
        ADD = "add", _("add")
        DELETE = "delete", _("delete")
        REPLACE = "replace", _("replace")
        BANDWIDTH = "bandwidth", _("bandwidth")
        NETWORK = "network", _("project network")  # L2: another tenant network (no approval)

    class Run(models.TextChoices):
        NONE = "", _("not started")
        RUNNING = "running", _("running")
        DONE = "done", _("done")
        FAILED = "failed", _("failed")

    class State(models.TextChoices):
        PENDING = "pending", _("waiting for approval")
        APPROVED = "approved", _("approved")
        REJECTED = "rejected", _("rejected")
        CANCELLED = "cancelled", _("cancelled")

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="network_requests")
    site = models.CharField(max_length=16)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    action = models.CharField(max_length=16, choices=Action.choices, default=Action.ADD)
    target = models.CharField(_("network"), max_length=128, blank=True,
                              help_text="delete/replace: the prefix (or L2 channel VNI) it applies to")
    prefix_len = models.PositiveSmallIntegerField(null=True, blank=True)
    pool = models.CharField(max_length=64, blank=True, default="",
                            help_text="inet/corpnet: the base IX subnet the requester chose; blank = auto")
    mbps = models.PositiveIntegerField(_("bandwidth, Mbps"), null=True, blank=True, help_text="inet: the new bandwidth")
    vlan = models.PositiveIntegerField(_("VLAN"), null=True, blank=True, help_text="L2: VLAN number, set at approval")
    tenant_network = models.CharField(_("project network"), max_length=128, blank=True)
    vlan_name = models.CharField(_("external VLAN"), max_length=128, blank=True)
    l2_name = models.CharField(_("name"), max_length=64, blank=True, help_text="L2: the channel's name")
    note = models.TextField(blank=True)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    result = models.CharField(max_length=200, blank=True, help_text="assigned prefix or channel")
    run = models.CharField(max_length=16, choices=Run.choices, default=Run.NONE, blank=True)
    steps = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def what(self):
        if self.kind == self.Kind.L2:
            if self.action == self.Action.DELETE:
                return f"− VNI {self.target}"
            if self.action == self.Action.NETWORK:
                return f"{self.l2_name or 'L2'}: {self.target or '—'} ⇒ {self.tenant_network}"
            return f"{self.l2_name or 'L2'}: {self.tenant_network or '—'} ↔ VLAN {self.vlan or '?'}"
        if self.action == self.Action.BANDWIDTH:
            return f"{self.target} ⇒ {self.mbps} Mbps"
        if self.kind == self.Kind.IGW:
            return f"igw {self.target} ⇒ {self.result}"
        if self.action == self.Action.DELETE:
            return f"− {self.target}"
        if self.action == self.Action.REPLACE:
            return f"{self.target} ⇒ /{self.prefix_len}" + (f" {self.result}" if self.result else "")
        return f"/{self.prefix_len}" + (f" → {self.result}" if self.result else "")


class L2Channel(models.Model):
    """An L2 channel of a tenant. owner 2026-11-03 (u11): two kinds — EXTERNAL bridges a project network and an outside
    VLAN over VXLAN; INTERNAL bridges two of the project's own networks on two different sites (one resource pool each)
    over VXLAN. External needs a resource manager's approval and a VLAN from support; internal needs neither."""

    class Kind(models.TextChoices):
        EXTERNAL = "external", _("external")
        INTERNAL = "internal", _("internal")

    class State(models.TextChoices):
        PLANNED = "planned", _("planned")
        ACTIVE = "active", _("active")
        REMOVED = "removed", _("removed")

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="l2_channels")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.EXTERNAL)
    name = models.CharField(_("name"), max_length=64, default="external", help_text="a free name to tell the channels apart")
    site = models.CharField(max_length=16)
    tenant_network = models.CharField(max_length=128, blank=True, help_text="the project's network it extends (the org subnet)")
    # internal: the second endpoint — the other site and the project network there
    site_b = models.CharField(max_length=16, blank=True)
    tenant_network_b = models.CharField(max_length=128, blank=True)
    vlan_name = models.CharField(max_length=128, blank=True)
    vlan = models.PositiveIntegerField(null=True, blank=True, help_text="external VLAN number (set at approval)")
    mgmt_platform = models.CharField(max_length=100, blank=True, help_text="where the external side (src) runs")
    mode = models.CharField(max_length=8, default="tunnel",
                            help_text="tunnel: src gateway on the edge platform, VXLAN to the dst gateway; bridge: one VM "
                                      "on the tenant's platform bridging the VLAN and the tenant network")
    edge_cluster = models.CharField(max_length=100, blank=True, help_text="the edge cluster the external side runs on")
    vlan_net_id = models.IntegerField(null=True, blank=True, help_text="the VLAN network on the management platform")
    ether_ar = models.IntegerField(null=True, blank=True, help_text="the ethernet address range on the project network")
    vni = models.PositiveIntegerField(unique=True)
    ip_a = models.CharField(max_length=64, blank=True)
    ip_b = models.CharField(max_length=64, blank=True)
    vm_a = models.IntegerField(null=True, blank=True)
    vm_b = models.IntegerField(null=True, blank=True)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PLANNED)
    request = models.OneToOneField(NetworkRequest, null=True, blank=True, on_delete=models.SET_NULL, related_name="channel")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["vni"]

    VNI_BASE = 1000

    @classmethod
    def next_vni(cls):
        top = cls.objects.aggregate(m=models.Max("vni"))["m"]
        return max(top or 0, cls.VNI_BASE - 1) + 1

    def addresses(self):
        return self.ip_a, self.ip_b

    def endpoint_1(self):
        """owner 2026-11-03 (u11): external — the outside VLAN; internal — the project network on the first site."""
        if self.kind == self.Kind.INTERNAL:
            return f"{self.site} · {self.tenant_network or '—'}"
        return f"VLAN {self.vlan}" if self.vlan else _("VLAN not set")

    def endpoint_2(self):
        """external — the project network it extends; internal — the project network on the second site."""
        if self.kind == self.Kind.INTERNAL:
            return f"{self.site_b} · {self.tenant_network_b or '—'}"
        return self.tenant_network or "—"


class TenantDeletion(models.Model):
    """Deleting a tenant: asked by its admins and approved by a resource manager, or done by staff directly.

    The run removes everything of the tenant in OpenNebula (VMs, networks, images, templates, security groups,
    virtual routers, ACLs, users, the group), releases its addresses and marks the tenant deleted.
    """

    class State(models.TextChoices):
        PENDING = "pending", _("waiting for approval")
        REJECTED = "rejected", _("rejected")
        CANCELLED = "cancelled", _("cancelled")
        RUNNING = "running", _("deleting")
        DONE = "done", _("deleted")
        FAILED = "failed", _("failed")

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="deletions")
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    note = models.TextField(blank=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(blank=True)
    steps = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True)
    plan = models.JSONField(default=dict, blank=True, help_text="the resources to delete, listed when requested")
    plan_error = models.TextField(blank=True)
    planned_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    PLAN_LABELS = (("vms", "VMs"), ("backupjobs", "backup jobs"), ("vrouters", "virtual routers"),
                   ("templates", "templates"), ("images", "images and backups"), ("secgroups", "security groups"),
                   ("vmgroups", "VM groups"), ("networks", "networks"), ("acls", "ACL rules"), ("users", "users"),
                   ("group", "group"))

    @property
    def plan_rows(self):
        p = self.plan or {}
        rows = []
        for key, label in self.PLAN_LABELS:
            items = p.get(key) or []
            if isinstance(items, dict):
                items = [items]
            rows.append({"key": key, "label": label, "items": items})
        return rows

    @property
    def plan_total(self):
        return sum(len(r["items"]) for r in self.plan_rows)


class TenantSshKey(models.Model):
    """An ssh public key of a tenant. Keys assigned to the igw can log in to it (ssh is key-only)."""

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="ssh_keys")
    name = models.CharField(max_length=64)
    key = models.TextField()
    on_igw = models.BooleanField(_("assigned to the igw"), default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]
        unique_together = [("tenant", "name")]

    def __str__(self):
        return self.name

    @property
    def parts(self):
        bits = self.key.split()
        return (bits + ["", "", ""])[:3]

    @property
    def key_type(self):
        return self.parts[0]

    @property
    def comment(self):
        return " ".join(self.key.split()[2:])

    @property
    def fingerprint(self):
        import base64
        import hashlib
        try:
            raw = base64.b64decode(self.parts[1])
        except ValueError:
            return ""
        return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")

    def sync_igw_key(self):
        """The order's igw key (the one the igw is created with) is the first key assigned to the igw."""
        t = self.tenant
        first = t.ssh_keys.filter(on_igw=True).first()
        t.igw_ssh_key = first.key if first else ""
        t.save(update_fields=["igw_ssh_key"])


class ConfigCheck(models.Model):
    """A check of the tenant's routers: their running configuration against Infrastructure → Templates.

    results: {role: {"status": ok|drift|unreachable|error, "host", "missing": [...], "extra": [...], "error"}};
    lines are stored with secrets masked.
    """

    class State(models.TextChoices):
        RUNNING = "running", _("running")
        DONE = "done", _("done")
        FAILED = "failed", _("failed")

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="config_checks")
    state = models.CharField(max_length=16, choices=State.choices, default=State.RUNNING)
    results = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    @property
    def ok(self):
        return self.state == self.State.DONE and all(r.get("status") == "ok" for r in (self.results or {}).values())


class IgwConfig(models.Model):
    """A saved configuration of a tenant's igw (VyOS set commands), encrypted: read from the igw, edited by the
    tenant's admins, uploaded, or kept automatically before another one was applied."""

    class Source(models.TextChoices):
        READ = "read", _("read from the igw")
        EDITED = "edited", _("edited")
        UPLOADED = "uploaded", _("uploaded")
        BEFORE = "before", _("kept before an apply")

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="igw_configs")
    name = models.CharField(max_length=120)
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.EDITED)
    based_on = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    text_encrypted = models.TextField(blank=True)
    lines = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    applied_at = models.DateTimeField(null=True, blank=True, help_text="correction 14: when it was last applied (kept)")
    current = models.BooleanField(default=False, help_text="correction 14: the running configuration as last read "
                                                             "(one per tenant, not counted among the 10 stored)")

    class Meta:
        ordering = ["-created_at", "-pk"]

    MAX_STORED = 10

    def __str__(self):
        return self.name

    def set_text(self, text):
        from inventory.crypto import encrypt
        text = "\n".join(x.rstrip() for x in (text or "").replace("\r\n", "\n").splitlines() if x.strip()) + "\n"
        self.text_encrypted = encrypt(text)
        self.lines = sum(1 for x in text.splitlines() if x.startswith("set "))

    def get_text(self):
        from inventory.crypto import decrypt
        return decrypt(self.text_encrypted) if self.text_encrypted else ""


class IgwApply(models.Model):
    """Applying a saved configuration to the igw: over ssh with an automatic rollback unless confirmed in time
    (VyOS commit-confirm), or by making the igw again with it."""

    class Method(models.TextChoices):
        SSH = "ssh", _("over ssh, rolled back unless confirmed")
        REDEPLOY = "redeploy", _("by making the igw again")
        COMMANDS = "commands", _("commands")

    class State(models.TextChoices):
        RUNNING = "running", _("applying")
        WAITING = "waiting", _("waiting for confirmation")
        CONFIRMED = "confirmed", _("confirmed")
        ROLLED_BACK = "rolled_back", _("rolled back: not confirmed in time")
        DONE = "done", _("done")
        FAILED = "failed", _("failed")

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="igw_applies")
    config = models.ForeignKey(IgwConfig, null=True, on_delete=models.SET_NULL, related_name="applies")
    before = models.ForeignKey(IgwConfig, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
                               help_text="the running configuration kept just before")
    method = models.CharField(max_length=16, choices=Method.choices, default=Method.SSH)
    timeout_s = models.PositiveIntegerField(default=60)
    state = models.CharField(max_length=16, choices=State.choices, default=State.RUNNING)
    deadline = models.DateTimeField(null=True, blank=True)
    result = models.TextField(blank=True)
    deployment = models.ForeignKey("Deployment", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    commands_encrypted = models.TextField(blank=True, help_text="correction 14: the commands run (method commands)")

    def set_commands(self, text):
        from inventory.crypto import encrypt
        self.commands_encrypted = encrypt(text) if text else ""

    def get_commands(self):
        from inventory.crypto import decrypt
        return decrypt(self.commands_encrypted) if self.commands_encrypted else ""

    class Meta:
        ordering = ["-created_at", "-pk"]

    @property
    def seconds_left(self):
        if self.state != self.State.WAITING or not self.deadline:
            return 0
        return max(int((self.deadline - timezone.now()).total_seconds()), 0)



class IgwPasswordHistory(models.Model):
    """Correction 14: the igw user's former passwords (encrypted), newest first; shown to the tenant's admins (logged)."""
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="igw_passwords")
    password_encrypted = models.TextField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def get_password(self):
        from inventory.crypto import decrypt
        return decrypt(self.password_encrypted)


class TenantMove(models.Model):
    """corrections-18: moving a tenant to another organization — asked by staff, approved by the resource manager and
    the target organization's sales rep (when it has one); a person holding both roles approves once."""

    class State(models.TextChoices):
        PENDING = "pending", _("waiting for approval")
        DONE = "done", _("moved")
        REJECTED = "rejected", _("rejected")
        CANCELLED = "cancelled", _("cancelled")

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="moves")
    from_org = models.ForeignKey(Organization, null=True, on_delete=models.SET_NULL, related_name="+")
    to_org = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+")
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    rm_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    rm_at = models.DateTimeField(null=True, blank=True)
    sales_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    sales_at = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"move {self.tenant_id} → {self.to_org_id}"
