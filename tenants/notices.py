"""Correction 15: the e-mails sent when a tenant is provisioned — the full one with the credentials to the tenant admin,
a notification without credentials to every platform user connected to the tenant (sales rep, key account manager,
resource manager, support engineer). The same text is shown for review (hide=True: secrets masked)."""

import ipaddress

from django.utils import timezone
from django.utils.translation import gettext as _

HIDDEN = "••••••••"


def _cluster(site, code):
    from inventory.models import Cluster

    for c in Cluster.objects.select_related("platform", "platform__site"):
        if (c.platform.site.code if c.platform.site_id else "") == site and code in (
            c.order_code,
            c.quota_code,
            c.name,
        ):
            return c
    return None


def _front_end(p):
    """The platform whose public URL serves `p` (a secondary zone: its primary's)."""
    from inventory.models import Platform

    if p is None:
        return None
    if p.public_url or not p.fe_primary:
        return p
    return Platform.objects.filter(key=p.fe_primary).first() or p


def pools(tenant):
    """[{site, cluster, platform, cpu, ram_gb, gpu, gpu_text, disk_gb, vms_gb, images_gb, files_gb, backups_gb,
    web_ui, endpoints}] — one per current quota record."""
    from .models import split_of
    from .ordering import gpu_short

    out = []
    for r in tenant.current_quota_records():
        c = _cluster(r.site, r.cluster)
        p = c.platform if c else None
        fe = _front_end(p)
        model = ""
        if c is not None and r.gpu:
            prof = next((h.profile for h in c.hosts.all() if h.profile and h.profile.gpu_count), None)
            model = gpu_short(prof.gpu_model) if prof else "GPU"
        vms, images, files = split_of(r)
        out.append(
            {
                "site": r.site,
                "cluster": c.name if c else r.cluster,
                "platform": p.key if p else "",
                "cpu": r.cpu or 0,
                "ram_gb": r.ram_gb or 0,
                "gpu": r.gpu or 0,
                "gpu_text": f"{r.gpu} × {model} ({r.gpu} bundle{'s' if r.gpu != 1 else ''})" if r.gpu else "",
                "disk_gb": r.disk_gb or 0,
                "vms_gb": vms,
                "images_gb": images,
                "files_gb": files,
                "backups_gb": r.backups_gb or 0,
                "web_ui": fe.fireedge_link if fe else "",
                "endpoints": [(k, v) for k, v in (fe.endpoints if fe else []) if k != "FireEdge"],
            }
        )
    return out


def _vm_ips(cidr):
    """Addresses of a main subnet left for the tenant's VMs: all host addresses less the egw and the igw."""
    net = ipaddress.ip_network(cidr)
    hosts = list(net) if net.prefixlen >= 31 else list(net.hosts())
    return max(len(hosts) - 2, 0), (hosts[2:] if len(hosts) > 2 else [])


def connectivity(tenant, hide=False):
    """[{site, kind, …}] the tenant's connections per site: internet (speed, main subnet, addresses, igw and its
    access), corporate network, L2 channels."""
    from .deploy import igw_type_of
    from .models import Connectivity, PrefixAssignment

    out = []
    prefixes = list(PrefixAssignment.active().filter(tenant=tenant))
    for c in Connectivity.current_for(tenant):
        kinds = (c.connection_type or "").split()
        if "inet" in kinds:
            main = next((p for p in prefixes if p.kind == "inet_main" and p.site == c.site), None)
            n, ips = _vm_ips(main.cidr) if main else (0, [])
            net = ipaddress.ip_network(main.cidr) if main else None
            hosts = (list(net) if net.prefixlen >= 31 else list(net.hosts())) if net else []
            egw, igw = (str(hosts[0]), str(hosts[1])) if len(hosts) >= 2 else ("", "")
            kind = c.igw_type or igw_type_of(tenant, c.site) or "vyos"
            keys = [k.name for k in tenant.ssh_keys.filter(on_igw=True)]
            row = {
                "site": c.site,
                "kind": "inet",
                "mbps": c.inet_mbps,
                "main": main.cidr if main else "",
                "vm_ips": n,
                "vm_range": f"{ips[0]} – {ips[-1]}" if ips else "",
                "egw": egw,
                "igw": igw,
                "extra": [p.cidr for p in prefixes if p.kind == "inet_extra" and p.site == c.site],
                "igw_type": kind,
                "igw_label": dict(Connectivity.Gateway.choices).get(kind, kind),
            }
            if kind == "vyos":
                row.update(
                    ssh_user=tenant.igw_username,
                    ssh_port=tenant.igw_ssh_port,
                    ssh_host=tenant.igw_host or igw,
                    ssh_keys=keys,
                    ssh_password="" if keys else (HIDDEN if hide else tenant.get_igw_password()),
                )
            out.append(row)
        if "corpnet" in kinds:
            out.append(
                {
                    "site": c.site,
                    "kind": "corpnet",
                    "subnets": [p.cidr for p in prefixes if p.kind == "corpnet" and p.site == c.site],
                }
            )
    for ch in tenant.l2_channels.exclude(state="removed").order_by("vlan", "pk"):
        out.append(
            {
                "site": (tenant.order or {}).get("site", ""),
                "kind": "l2",
                "name": getattr(ch, "name", "") or "",
                "vlan": ch.vlan,
                "vni": getattr(ch, "vni", None),
                "state": ch.get_state_display(),
            }
        )
    return out


def contacts(tenant):
    """[(role, user)] the platform users connected to the tenant, in the e-mails' order."""
    rows = [
        (_("Sales rep"), tenant.sales_rep),  # corrections-18: a corporate one may have its sales rep too
        (
            _("Key account manager"),
            tenant.organization.kam if tenant.organization_id and tenant.kind != "corp" else None,
        ),
        (_("Resource manager"), tenant.effective_resource_manager),
        (_("Support engineer"), tenant.effective_support_engineer),
    ]
    return [(r, u) for r, u in rows if u is not None]


def context(tenant, hide=False, provisioned_by=None, provisioned_at=None):
    from django.urls import reverse

    from accounts.utils import site_link

    dep = tenant.deployments.filter(state="done").order_by("-pk").first()
    by = (dep.created_by if dep else None) or provisioned_by  # who provisioned it, also when the e-mail is sent again
    at = provisioned_at or (dep.finished_at if dep else None) or timezone.now()
    platforms = sorted({p["platform"] for p in pools(tenant) if p["platform"]}) or [
        (tenant.order or {}).get("platform", "")
    ]
    return {
        "tenant": tenant,
        "org": tenant.organization,
        "platforms": ", ".join(x for x in platforms if x),
        "pools": pools(tenant),
        "conns": connectivity(tenant, hide=hide),
        "contacts": contacts(tenant),
        "admin_person": tenant.tenant_admin,
        "admin_user": tenant.one_admin_name,
        "admin_password": HIDDEN if hide else tenant.get_admin_password(),
        "by": by,
        "at": timezone.localtime(at),
        "link": site_link(reverse("tenant_detail", args=[tenant.pk])),
    }


def _project_admins(tenant):
    """Active project admins of a tenant, each only once, its tenant admin first: the ``tenant_admin`` plus any other
    users holding the project-admin role on it (owner 2026-11-03: credentials go to these and no one else)."""
    from .models import Membership

    out, seen = [], set()
    a = tenant.tenant_admin
    if a is not None and a.is_active and a.email:
        out.append(a)
        seen.add(a.pk)
    for m in tenant.memberships.filter(role=Membership.Role.TENANT_ADMIN, user__is_active=True).select_related("user"):
        if m.user.pk not in seen and m.user.email:
            out.append(m.user)
            seen.add(m.user.pk)
    return out


def send_ready(
    tenant,
    by=None,
    hide=False,
    to_admin=True,
    to_staff=True,
    override_to=None,
    subject_prefix="",
    allow_client=False,
    to_org_admins=True,
):
    """Credentials to the project admins only (owner 2026-11-03; no copy to the provisioner or to org admins who are
    not a project admin) and one notification per connected platform user (several roles: one e-mail). override_to:
    send every message to these addresses instead (review). Returns the number of messages sent. ``to_org_admins`` is
    kept for callers but no longer adds org-admin copies."""
    from accounts.models import audit
    from accounts.utils import notify

    ctx = context(tenant, hide=hide, provisioned_by=by)
    name = tenant.name or tenant.title  # owner 2026-10-29: no group name in e-mails
    sent = 0
    # owner 2026-11-03: the credentials go only to the project admins (the project's tenant admin and any other
    # project-admin members of it), each their own e-mail — never to org admins who are not a project admin, and no
    # copy to the platform user who provisioned (owner 2026-10-30 21:02).
    project_admins = _project_admins(tenant)
    if to_admin:
        if override_to:  # review: one representative message to the review address
            if project_admins:
                sent += notify(
                    override_to,
                    "tenant_ready_admin",
                    {**ctx, "you": project_admins[0]},
                    subject_prefix + _("Project %(t)s is ready: access details") % {"t": name},
                    allow_client=allow_client,
                )
        elif project_admins:
            for u in project_admins:
                sent += notify(
                    [u.email],
                    "tenant_ready_admin",
                    {**ctx, "you": u},
                    subject_prefix + _("Project %(t)s is ready: access details") % {"t": name},
                    allow_client=allow_client,
                )
            audit(
                by,
                "tenant_credentials_sent" if sent else "tenant_credentials_not_sent",
                tenant,
                to=", ".join(u.email for u in project_admins),
            )
            if not sent:  # owner 2026-10-29: the platform users only after the project admins' went out
                audit(by, "tenant_ready_staff_skipped", tenant, why="the project admin's e-mail was not sent")
                return sent
        else:  # no project admin: nobody got the credentials — the platform users are not told
            audit(by, "tenant_ready_staff_skipped", tenant, why="no project admin")
            return sent
    if to_staff:
        people = {}
        for role, u in ctx["contacts"]:
            people.setdefault(u.pk, (u, []))[1].append(str(role).lower())
        for u, roles in people.values():
            role = " " + _("and") + " "
            # by address: a platform user without a portal login (sales reps as directory entries) gets it too
            sent += notify(
                override_to or [u.email],
                "tenant_ready_staff",
                {**ctx, "you": u, "role": role.join(roles)},
                subject_prefix + _("Project %(t)s provisioned") % {"t": name},
            )
        if not override_to and people:
            audit(by, "tenant_ready_notified", tenant, to=", ".join(u.email for u, _r in people.values()))
    return sent
