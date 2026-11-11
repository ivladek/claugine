"""Networks of a tenant: public networks (the main one first), corporate networks and L2 channels.

Tenant admins ask to add, delete or (the main network) replace them; a resource manager (or the super admin)
approves (correction 13; the igw type and an L2 channel's subnet change without approval). An L2 channel: the resource
manager approves it, the support engineer sets its VLAN, and it is made as soon as its VLAN and subnet are set.
An approved change is carried out at once (tenants.netops) when the tenant is deployed.
"""
import json

from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts.models import Role, audit
from accounts.utils import notify, site_link, users_with_role

from .views import back_to
from . import ordering
from .models import Connectivity, Approval, L2Channel, Membership, NetworkRequest, OrgKind, PrefixAssignment, Tenant

PREFIX_CHOICES = (32, 31, 30, 29, 28, 27, 26, 25, 24)


def can_request(tenant, user):
    from .permissions import can_manage_project  # u12 §1: manager/KAM/RM have full resource management
    return tenant.approval == Approval.APPROVED and not tenant.is_deleted and (
        user.is_super_admin or can_manage_project(user, tenant)
        or tenant.membership_role(user) in (Membership.Role.ORG_ADMIN, Membership.Role.TENANT_ADMIN))


def can_decide(tenant, user):
    """Correction 13: the resource manager approves network changes."""
    return user.is_super_admin or tenant.can_approve_as(user, Role.RESOURCE_MANAGER)


def can_set_vlan(tenant, user):
    """The support engineer (or the super admin) gives an approved L2 channel its VLAN."""
    return user.is_authenticated and (user.is_super_admin or tenant.can_approve_as(user, Role.SUPPORT)
                                      or user.has_role(Role.SUPPORT)) and not tenant.is_deleted


def corporate_allowed(tenant):
    return bool(tenant.organization_id and tenant.organization.kind == OrgKind.CORP)


def _platform_keys_for_site(tenant, site):
    """Platform keys serving the tenant's resource pools on one site (from its current quota records). u11: an L2
    endpoint is a resource pool, so a project's networks are listed per site, not only on the order's platform."""
    codes = {r.cluster for r in tenant.current_quota_records() if r.site == site and r.cluster}
    if not codes:
        return []
    from inventory.models import Cluster
    keys = []
    for c in Cluster.objects.select_related("platform", "platform__site").filter(platform__site__code=site):
        if (c.order_code in codes or c.name in codes) and c.platform.key not in keys:
            keys.append(c.platform.key)
    return keys


def tenant_networks(tenant, site=None):
    """Networks of the tenant's group in OpenNebula (id, name, address): cached for 5 minutes. Main excluded.

    ``site=None`` uses the order's primary platform (back-compat); a ``site`` lists the networks on the platforms of
    the tenant's resource pools there, so an L2 endpoint can be chosen on any site the project has resources on."""
    order = tenant.order or {}
    the_site = site or order.get("site") or ""
    key = f"tenant-nets-{tenant.pk}-{the_site or '-'}"
    got = cache.get(key)
    if got is not None:
        return got
    out = []
    try:
        from inventory.models import PlatformControl
        from .deploy import One
        if site is None:
            pkeys = [order.get("platform")] if order.get("platform") else []
        else:
            pkeys = _platform_keys_for_site(tenant, site)
        seen = set()
        for pkey in pkeys:
            c = PlatformControl.objects.filter(key=pkey).first()
            if c is None or not c.configured or not tenant.group_name:
                continue
            one = One(c)
            for v in one.xml("one.vnpool.info", -2, -1, -1).findall("VNET"):
                name = v.findtext("NAME") or ""
                if v.findtext("GNAME") != tenant.group_name or name.endswith(f"-main_{the_site}") or name in seen:
                    continue
                seen.add(name)
                ar = v.find("AR_POOL/AR")
                out.append({"id": int(v.findtext("ID")), "name": name,
                            "address": (ar.findtext("IP") if ar is not None else "") or ""})
    except Exception:  # noqa: BLE001 - the dialog then takes a typed name
        out = []
    cache.set(key, out, 300)
    return out


def connection_kinds(tenant):
    """{"inet", "corpnet", "l2"} the tenant has: its order, its current connectivity and its assigned prefixes (adopted
    tenants have no order). Correction 11: inet or corpnet, never both; L2 goes with either."""
    from .ordering import connections as _conns
    have = set(_conns(tenant.order or {}))
    for c in tenant.current_connectivity():
        have |= set((c.connection_type or "").split())
    for kind in PrefixAssignment.active().filter(tenant=tenant).values_list("kind", flat=True):
        have.add("corpnet" if kind == "corpnet" else "inet" if kind.startswith("inet") else "")
    have.discard("")
    return have


def connectivity_context(tenant, user):
    """What the Connectivity card shows: public, corporate and L2 lists with their pending requests."""
    site = (tenant.order or {}).get("site")
    prefixes = list(PrefixAssignment.active().filter(tenant=tenant))
    main = [p for p in prefixes if p.kind == "inet_main"]
    reqs = list(tenant.network_requests.select_related("requested_by", "decided_by"))
    pending = [r for r in reqs if r.state == NetworkRequest.State.PENDING]
    busy = {r.target for r in pending if r.target}
    running = [r for r in reqs if r.state == NetworkRequest.State.APPROVED and r.run == NetworkRequest.Run.RUNNING]
    for p in prefixes:
        p.busy = p.cidr in busy or any(r.target == p.cidr for r in running)
        p.plen = p.cidr.split("/")[-1]
    chans = list(tenant.l2_channels.exclude(state=L2Channel.State.REMOVED))
    for ch in chans:
        ch.busy = str(ch.vni) in busy
        ch.running = any(r.kind == "l2" and (r.pk == ch.request_id or r.target == str(ch.vni)) for r in running)
    deployed = bool(tenant.one_group_id)
    corp = [p for p in prefixes if p.kind == "corpnet"]
    # correction 14: the Internet table — one row per site
    from .igwdetect import addresses
    inet_rows = []
    for c in tenant.current_connectivity():
        if "inet" not in c.types and not any(p.site == c.site and p.kind.startswith("inet") for p in prefixes):
            continue
        mine = [p for p in prefixes if p.site == c.site]
        ips = addresses(tenant, c.site)
        vm = tenant.igw_vm or {}
        inet_rows.append({
            "c": c, "site": c.site,
            "main": [p for p in mine if p.kind == "inet_main"], "extra": [p for p in mine if p.kind == "inet_extra"],
            "next": [p for p in mine if p.kind == "inet_next"],
            "ip": ips[1] if ips else "", "egw_ip": ips[0] if ips else "",
            "vm_name": vm.get("name", ""), "vm_id": vm.get("id"), "vm_status": vm.get("status", ""),
            "vm_images": vm.get("images") or [], "vm_cpu": vm.get("cpu"), "vm_ram": vm.get("ram_gb"),
            "vm_disks": vm.get("disks_gb") or [], "vm_at": vm.get("at", ""),
            "pending_add": [r for r in pending if r.kind == "inet" and r.action == "add" and r.site == c.site],
            "pending_mbps": next((r for r in pending if r.kind == "inet" and r.action == "bandwidth" and r.site == c.site), None),
            "pending_replace": next((r for r in pending if r.kind == "inet" and r.action == "replace" and r.site == c.site), None),
        })
    _l2_sites = sorted({r.site for r in tenant.current_quota_records() if r.site})
    _can_req = can_request(tenant, user)
    _l2_pools = ([{"site": s, "networks": tenant_networks(tenant, s)} for s in _l2_sites]
                 if (_can_req and deployed) else [])
    return {
        "inet_rows": inet_rows,
        "can_size_igw": bool(user and user.is_staff_member and (user.is_super_admin or user.has_role(Role.SUPPORT)
                                                                or user.has_role(Role.RESOURCE_MANAGER))),
        "net_public": main + [p for p in prefixes if p.kind == "inet_extra"],
        "net_next": [p for p in prefixes if p.kind == "inet_next"],
        "net_corp": corp, "net_corp_deletable": len(corp) > 1,
        "net_l2": chans,
        "net_pending": {k: [r for r in pending if r.kind == k] for k in ("inet", "corpnet", "l2")},
        "net_pending_add": {k: [r for r in pending if r.kind == k and r.action == "add"] for k in ("inet", "corpnet")},
        "net_pending_other": {k: [r for r in pending if r.kind == k and r.action != "add"] for k in ("inet", "corpnet")},
        "net_inet_pools": _pool_cidrs(tenant, "inet"), "net_corpnet_pools": _pool_cidrs(tenant, "corpnet"),
        "net_base_subnets": _pool_choices(tenant, "inet"),  # u11: base IX subnets the requester can choose, "auto" default
        "net_l2_sites": _l2_sites,  # u11: the project's resource-pool sites (an L2 endpoint is a pool)
        "net_l2_pools": _l2_pools, "net_l2_pools_json": json.dumps(_l2_pools),  # u11: per-site subnets for the add dialog
        "igw_types": Connectivity.Gateway.choices,
        "net_running": running,
        "net_failed": [r for r in reqs if r.state == NetworkRequest.State.APPROVED and r.run == NetworkRequest.Run.FAILED][:5],
        "net_can_request": can_request(tenant, user), "net_can_decide": can_decide(tenant, user),
        "net_can_set_vlan": can_set_vlan(tenant, user),
        "net_corp_allowed": corporate_allowed(tenant), "net_prefixes": PREFIX_CHOICES, "net_site": site,
        "net_kinds": connection_kinds(tenant),
        "net_tenant_networks": tenant_networks(tenant) if (can_request(tenant, user) and deployed) else [],
    }


def _back(request, tenant):
    default = reverse("tenant_detail", args=[tenant.pk]) + "#connectivity"
    from django.utils.http import url_has_allowed_host_and_scheme
    nxt = request.POST.get("next") or request.GET.get("next") or ""  # u11: return to the projects list at the row
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(nxt)
    return redirect(default)


def _prefix_len(request):
    try:
        n = int(request.POST.get("prefix_len") or 0)
    except ValueError:
        n = 0
    return n if n in PREFIX_CHOICES else None


@require_POST
def l2_add_view(request, pk):
    """owner 2026-11-04 (u11): add an L2 channel from one "Add L2 channel" dialog.

    Endpoint 1 is always one of the project's subnets, chosen as site -> subnet. Endpoint 2 is either:
      - an external VLAN  -> EXTERNAL channel: opens a request for a resource manager, support sets the VLAN; or
      - a project subnet on a different site -> INTERNAL channel: no approval, one per site-pair.
    A subnet<->subnet channel always spans two resource pools on two different sites (so it can only be added once the
    project has resources on two sites, i.e. after it is created)."""
    tenant = get_object_or_404(Tenant, pk=pk)
    user = request.user
    if not user.is_authenticated or not can_request(tenant, user):
        raise PermissionDenied
    sites = sorted({r.site for r in tenant.current_quota_records() if r.site})
    name = (request.POST.get("name") or "").strip()[:64]
    site_a = (request.POST.get("site_a") or "").strip()
    if sites and site_a not in sites:
        messages.error(request, _("Choose a site the project has resources on."))
        return _back(request, tenant)
    net_a = (request.POST.get("network_a") or request.POST.get("network_a_text") or "").strip()[:128]
    if net_a and site_a and net_a.endswith(f"-main_{site_a}"):
        messages.error(request, _("The main network cannot be extended over L2."))
        return _back(request, tenant)

    if (request.POST.get("ep2") or "vlan").strip() == "subnet":  # endpoint 2 is another project subnet: internal
        if len(sites) < 2:
            messages.error(request, _("A subnet-to-subnet L2 channel needs project resources on two different sites."))
            return _back(request, tenant)
        site_b = (request.POST.get("site_b") or "").strip()
        if site_b == site_a or site_a not in sites or site_b not in sites:
            messages.error(request, _("Choose two different sites the project has resources on."))
            return _back(request, tenant)
        pair = {site_a, site_b}
        for ch in tenant.l2_channels.filter(kind=L2Channel.Kind.INTERNAL).exclude(state=L2Channel.State.REMOVED):
            if {ch.site, ch.site_b} == pair:
                messages.error(request, _("There is already an internal L2 channel between these two sites."))
                return _back(request, tenant)
        net_b = (request.POST.get("network_b") or request.POST.get("network_b_text") or "").strip()[:128]
        if net_b and net_b.endswith(f"-main_{site_b}"):
            messages.error(request, _("The main network cannot be extended over L2."))
            return _back(request, tenant)
        ch = L2Channel.objects.create(
            tenant=tenant, kind=L2Channel.Kind.INTERNAL, name=name or "internal",
            site=site_a, tenant_network=net_a, site_b=site_b, tenant_network_b=net_b,
            vni=L2Channel.next_vni(), state=L2Channel.State.PLANNED)
        audit(user, "l2_internal_added", tenant, vni=ch.vni, sites=f"{site_a}+{site_b}")
        messages.success(request, _("Internal L2 channel added (no approval needed); it is made once both networks are set."))
        return _back(request, tenant)

    # endpoint 2 is an outside VLAN: external channel, resource-manager approval, support sets the VLAN
    req = NetworkRequest(tenant=tenant, site=site_a or (tenant.order or {}).get("site") or "",
                         kind="l2", action=NetworkRequest.Action.ADD, requested_by=user,
                         note=(request.POST.get("note") or "")[:2000])
    req.l2_name = name or "external"
    req.tenant_network = net_a
    req.save()
    audit(user, "network_requested", tenant, kind="l2", change="add", what=req.what, note=req.note[:200])
    approvers = tenant.approvers(Role.RESOURCE_MANAGER)
    notify(approvers, "network_requested", {"tenant": tenant, "req": req,
                                            "link": site_link(reverse("tenant_detail", args=[tenant.pk]))},
           _("Network request for %(t)s") % {"t": tenant.name or tenant.title})
    messages.success(request, _("L2 channel sent for approval."))
    return _back(request, tenant)


# back-compat: the old internal-only endpoint name still resolves to the unified view
l2_internal_add_view = l2_add_view


@require_POST
def network_request_view(request, pk):
    tenant = get_object_or_404(Tenant, pk=pk)
    user = request.user
    if not user.is_authenticated or not can_request(tenant, user):
        raise PermissionDenied
    kind = request.POST.get("kind")
    action = request.POST.get("action") or NetworkRequest.Action.ADD
    if kind == NetworkRequest.Kind.IGW:
        return _igw_change(request, tenant, user)
    if action not in NetworkRequest.Action.values or kind not in NetworkRequest.Kind.values:
        raise PermissionDenied
    site = (tenant.order or {}).get("site") or ""
    posted = (request.POST.get("site") or "").strip()  # owner 2026-10-29 20:20: one connection per site, a row each
    if posted and posted != site and posted in {c.site for c in tenant.current_connectivity()}:
        site = posted
    req = NetworkRequest(tenant=tenant, site=site, kind=kind, action=action, requested_by=user,
                         note=(request.POST.get("note") or "")[:2000])
    if kind == "corpnet" and not corporate_allowed(tenant):
        messages.error(request, _("Only corporate organizations can order the corporate network."))
        return _back(request, tenant)
    have = connection_kinds(tenant)
    if action == NetworkRequest.Action.ADD and ((kind == "corpnet" and "inet" in have) or (kind == "inet" and "corpnet" in have)):
        messages.error(request, _("The internet and the corporate network cannot be combined in one project."))
        return _back(request, tenant)
    target = (request.POST.get("target") or "").strip()
    active = PrefixAssignment.active().filter(tenant=tenant)
    if action == NetworkRequest.Action.ADD:
        if kind in ("inet", "corpnet"):
            req.prefix_len = _prefix_len(request)
            if req.prefix_len is None:
                messages.error(request, _("Choose a prefix length."))
                return _back(request, tenant)
            chosen = (request.POST.get("pool") or "").strip()  # u11: the requester's base IX subnet; blank/auto = portal picks
            req.pool = chosen if any(chosen == c for c in _pool_cidrs(tenant, kind)) else ""
        else:  # L2: a name; the subnet now or later (correction 13)
            req.l2_name = (request.POST.get("l2_name") or "external").strip()[:64] or "external"
            req.tenant_network = (request.POST.get("tenant_network") or request.POST.get("tenant_network_text") or "").strip()[:128]
            if req.tenant_network and req.tenant_network.endswith(f"-main_{site}"):
                messages.error(request, _("The main network cannot be extended over L2."))
                return _back(request, tenant)
    elif action == NetworkRequest.Action.BANDWIDTH:
        from .ordering import MBPS_MAX, MBPS_MIN
        cur = next((c for c in tenant.current_connectivity() if c.site == site), None)
        if kind != "inet" or cur is None or "inet" not in cur.types:
            raise PermissionDenied
        try:
            req.mbps = int(request.POST.get("mbps") or 0)
        except ValueError:
            req.mbps = 0
        if not MBPS_MIN <= req.mbps <= MBPS_MAX:
            messages.error(request, _("Bandwidth: from %(lo)s to %(hi)s Mbps, in steps of 1.") % {"lo": MBPS_MIN, "hi": MBPS_MAX})
            return _back(request, tenant)
        old = cur.inet_mbps or (tenant.order or {}).get("inet_mbps") or 100
        if req.mbps == old:
            messages.info(request, _("Nothing changed."))
            return _back(request, tenant)
        req.target = f"{old} Mbps"
        if tenant.network_requests.filter(state=NetworkRequest.State.PENDING, kind="inet", action="bandwidth").exists():
            messages.error(request, _("A bandwidth change is already waiting for approval."))
            return _back(request, tenant)
    elif action == NetworkRequest.Action.DELETE:
        if kind == "l2":
            if not tenant.l2_channels.exclude(state=L2Channel.State.REMOVED).filter(vni=int(target or 0) if target.isdigit() else -1).exists():
                raise PermissionDenied
        else:
            want = "inet_extra" if kind == "inet" else "corpnet"
            if not active.filter(kind=want, cidr=target).exists():
                messages.error(request, _("Only an additional public network or a corporate network can be deleted."))
                return _back(request, tenant)
            if kind == "corpnet" and active.filter(kind="corpnet").count() < 2:
                messages.error(request, _("The last corporate network cannot be deleted: the cgw needs one."))
                return _back(request, tenant)
        req.target = target
    else:  # replace: the main public network only
        if kind != "inet" or not active.filter(kind="inet_main", cidr=target).exists():
            raise PermissionDenied
        req.prefix_len = _prefix_len(request)
        if req.prefix_len is None or req.prefix_len > 31:
            messages.error(request, _("Choose a prefix length: the main network needs room for the egw and the igw."))
            return _back(request, tenant)
        chosen = (request.POST.get("pool") or "").strip()  # u11: base IX subnet for the new main network
        req.pool = chosen if any(chosen == c for c in _pool_cidrs(tenant, "inet")) else ""
        req.target = target
    if req.target and tenant.network_requests.filter(state=NetworkRequest.State.PENDING, target=req.target).exists():
        messages.error(request, _("There is already a request for %(t)s.") % {"t": req.target})
        return _back(request, tenant)
    req.save()
    audit(user, "network_requested", tenant, kind=kind, change=action, what=req.what, note=req.note[:200])
    approvers = tenant.approvers(Role.RESOURCE_MANAGER)
    notify(approvers, "network_requested", {"tenant": tenant, "req": req,
                                            "link": site_link(reverse("tenant_detail", args=[tenant.pk]))},
           _("Network request for %(t)s") % {"t": tenant.name or tenant.title})
    messages.success(request, _("Network request sent for approval."))
    return _back(request, tenant)


def _igw_change(request, tenant, user):
    """Another igw type, at once (the tenant's own router: no approval). The igw's configuration is not kept."""
    from .deploy import igw_type_of
    new = request.POST.get("igw_type") or ""
    if new not in Connectivity.Gateway.values:
        raise PermissionDenied
    site = (tenant.order or {}).get("site") or ""
    old = igw_type_of(tenant, site)
    if not old:
        messages.error(request, _("The project has no internet connection: it has no igw."))
        return _back(request, tenant)
    if new == old:
        messages.info(request, _("Nothing changed."))
        return _back(request, tenant)
    if tenant.network_requests.filter(kind="igw", run=NetworkRequest.Run.RUNNING).exists():
        messages.error(request, _("An igw change is already running."))
        return _back(request, tenant)
    if not tenant.one_group_id:  # not deployed yet: only the record
        cur = next((c for c in Connectivity.current_for(tenant) if c.site == site), None)
        if cur is not None:
            vals = cur.values()
            vals["igw_type"] = new
            Connectivity.objects.create(tenant=tenant, site=site, version=Connectivity.next_version(tenant, site),
                                        created_by=user, **vals)
        tenant.order = {**(tenant.order or {}), "igw_type": new}
        tenant.save(update_fields=["order"])
        audit(user, "igw_type_changed", tenant, was=old, now=new)
        messages.success(request, _("The igw will be %(t)s.") % {"t": Connectivity.Gateway(new).label})
        return _back(request, tenant)
    n_cfg = tenant.igw_configs.count()  # correction 14: the stored configurations go with the old igw
    if n_cfg:
        tenant.igw_configs.all().delete()
        audit(user, "igw_configs_dropped", tenant, count=n_cfg, why="igw type change")
    now = timezone.now()
    req = NetworkRequest.objects.create(tenant=tenant, site=site, kind="igw", action="replace", target=old, result=new,
                                        requested_by=user, state=NetworkRequest.State.APPROVED, decided_by=user, decided_at=now,
                                        note=(request.POST.get("note") or "")[:2000])
    audit(user, "igw_type_change", tenant, was=old, now=new)
    from . import netops
    why = netops.blocked_reason(tenant)
    if why:
        req.run, req.error = NetworkRequest.Run.FAILED, why
        req.save(update_fields=["run", "error"])
        messages.warning(request, _("Not carried out: %(why)s.") % {"why": why})
    else:
        netops.start(req, user)
        messages.info(request, _("The igw is being replaced: its steps are on the project page."))
    return _back(request, tenant)


def _pool_cidrs(tenant, purpose):
    """owner 2026-11-03 (u11): the delegated ranges (BGP-connected subnets) of the tenant's platform, cloud first —
    offered when approving an extra prefix; the first is the default."""
    from inventory.models import DelegatedSubnet
    from .ordering import pools
    pkey = (tenant.order or {}).get("platform")
    if not pkey:
        return []
    pk = DelegatedSubnet.Purpose.INET if purpose == "inet" else DelegatedSubnet.Purpose.CORPNET
    return [d.cidr for d in pools(pkey, pk)]


def _pool_choices(tenant, purpose):
    """owner 2026-11-03 (u11): [(cidr, label)] the requester can pick a base subnet from; "auto" lets the portal pick."""
    from inventory.models import DelegatedSubnet
    from .ordering import pools
    pkey = (tenant.order or {}).get("platform")
    if not pkey:
        return []
    pk = DelegatedSubnet.Purpose.INET if purpose == "inet" else DelegatedSubnet.Purpose.CORPNET
    return [(d.cidr, f"{d.segment} · {d.cidr}") for d in pools(pkey, pk)]


def _approve(request, tenant, req, user):
    """Assign what the request needs. Returns an error text or ''."""
    A = NetworkRequest.Action
    pool = request.POST.get("inet_pool") or req.pool or None  # u11: RM's choice, else the base subnet the requester asked for
    if req.action == A.ADD and req.kind in ("inet", "corpnet"):
        p = ordering.allocate_one(tenant, req.kind, req.prefix_len, user, note=f"network request #{req.pk}", pool=pool)
        req.result = p.cidr
    elif req.action == A.REPLACE:
        p = ordering.allocate_one(tenant, "inet", req.prefix_len, user, note=f"replaces {req.target} (request #{req.pk})", pool=pool)
        p.kind = PrefixAssignment.Kind.INET_NEXT
        p.save(update_fields=["kind"])
        req.result = p.cidr
    elif req.action == A.ADD and req.kind == "l2":  # correction 13: planned; the VLAN comes from the support engineer
        ch = L2Channel.objects.create(tenant=tenant, site=req.site, name=req.l2_name or "external",
                                      tenant_network=req.tenant_network, vlan_name="", vlan=None,
                                      vni=L2Channel.next_vni(), request=req)
        req.result = f"VNI {ch.vni}, VLAN to be set"
    else:
        req.result = req.target
    return ""


@require_POST
def network_decide_view(request, pk, rid, decision):
    tenant = get_object_or_404(Tenant, pk=pk)
    req = get_object_or_404(NetworkRequest, pk=rid, tenant=tenant, state=NetworkRequest.State.PENDING)
    user = request.user
    if not user.is_authenticated:
        raise PermissionDenied
    if decision == "cancel":
        if not (can_request(tenant, user) or user.is_super_admin):
            raise PermissionDenied
        req.state = NetworkRequest.State.CANCELLED
    elif decision == "reject":
        if not can_decide(tenant, user):
            raise PermissionDenied
        req.state, req.reason = NetworkRequest.State.REJECTED, (request.POST.get("reason") or "")[:2000]
    elif decision == "approve":
        if not can_decide(tenant, user):
            raise PermissionDenied
        try:
            with transaction.atomic():
                err = _approve(request, tenant, req, user)
                if err:
                    messages.error(request, err)
                    return _back(request, tenant)
                req.state = NetworkRequest.State.APPROVED
                req.decided_by, req.decided_at = user, timezone.now()
                req.save()
        except ordering.AllocationError as exc:
            messages.error(request, str(exc))
            return _back(request, tenant)
    else:
        raise PermissionDenied
    req.decided_by, req.decided_at = user, timezone.now()
    req.save()
    audit(user, f"network_{req.state}", tenant, kind=req.kind, change=req.action, what=req.what, reason=req.reason[:200])
    if req.state == NetworkRequest.State.APPROVED and req.kind == "l2" and req.action == NetworkRequest.Action.ADD:
        messages.success(request, _("Approved: %(w)s. The support engineer sets its VLAN.") % {"w": req.what})
        _ask_for_vlan(tenant, req)
        l2_maybe_start(req.channel, user, request)
    elif req.state == NetworkRequest.State.APPROVED:
        messages.success(request, _("Approved: %(w)s.") % {"w": req.what})
        from . import netops
        why = netops.blocked_reason(tenant)
        if not why:
            netops.start(req, user)
            messages.info(request, _("The change is being made: its steps are on the project page."))
        elif tenant.one_group_id:
            messages.warning(request, _("Not carried out: %(why)s.") % {"why": why})
    else:
        messages.success(request, _("Network request %(s)s.") % {"s": req.get_state_display()})
    return back_to(request, reverse("tenant_detail", args=[tenant.pk]) + "#connectivity")


@require_POST
def network_retry_view(request, pk, rid):
    """Run a failed approved change again (its steps continue where they can)."""
    tenant = get_object_or_404(Tenant, pk=pk)
    req = get_object_or_404(NetworkRequest, pk=rid, tenant=tenant, state=NetworkRequest.State.APPROVED,
                            run=NetworkRequest.Run.FAILED)
    if not (request.user.is_authenticated and can_decide(tenant, request.user)):
        raise PermissionDenied
    from . import netops
    why = netops.blocked_reason(tenant)
    if why:
        messages.error(request, _("Not carried out: %(why)s.") % {"why": why})
    else:
        netops.start(req, request.user)
        messages.info(request, _("The change is being made again."))
    return _back(request, tenant)


# --- L2 channels (correction 13) ---------------------------------------------------------------------

def _ask_for_vlan(tenant, req):
    se = tenant.effective_support_engineer
    people = [se] if se else list(users_with_role(Role.SUPPORT))
    notify(people, "network_requested", {"tenant": tenant, "req": req,
                                         "link": site_link(reverse("tenant_detail", args=[tenant.pk]) + "#connectivity")},
           _("L2 channel of %(t)s: set its VLAN") % {"t": tenant.name or tenant.title})


def l2_maybe_start(ch, user=None, request=None):
    """Make an approved, planned channel once it has its VLAN and its subnet (and the tenant is deployed)."""
    from . import netops
    if ch is None or ch.state != L2Channel.State.PLANNED or not ch.vlan or not ch.tenant_network:
        return False
    req = ch.request
    if req is None or req.state != NetworkRequest.State.APPROVED or req.run in (NetworkRequest.Run.RUNNING,
                                                                                NetworkRequest.Run.DONE):
        return False
    why = netops.blocked_reason(ch.tenant)
    if why:
        if request is not None:
            messages.warning(request, _("The L2 channel is made later: %(why)s.") % {"why": why})
        return False
    req.vlan, req.tenant_network = ch.vlan, ch.tenant_network
    req.save(update_fields=["vlan", "tenant_network"])
    netops.start(req, user)
    if request is not None:
        messages.info(request, _("The L2 channel is being made: its steps are in Tasks."))
    return True


@require_POST
def l2_vlan_view(request, pk, cid):
    tenant = get_object_or_404(Tenant, pk=pk)
    ch = get_object_or_404(L2Channel, pk=cid, tenant=tenant, state=L2Channel.State.PLANNED)
    user = request.user
    if not can_set_vlan(tenant, user):
        raise PermissionDenied
    try:
        vlan = int(request.POST.get("vlan") or 0)
    except ValueError:
        vlan = 0
    if not 1 <= vlan <= 4094:
        messages.error(request, _("Give the VLAN number of the L2 channel (1-4094)."))
        return _back(request, tenant)
    if L2Channel.objects.filter(site=ch.site, vlan=vlan).exclude(state=L2Channel.State.REMOVED).exclude(pk=ch.pk).exists():
        messages.error(request, _("VLAN %(v)s is already used by another L2 channel on %(s)s.") % {"v": vlan, "s": ch.site})
        return _back(request, tenant)
    was = ch.vlan
    ch.vlan, ch.vlan_name = vlan, f"VLAN {vlan}"
    if not L2Channel.objects.filter(vni=vlan).exclude(pk=ch.pk).exists():
        ch.vni = vlan  # the VLAN number is the VNI too when it is free
    ch.save(update_fields=["vlan", "vlan_name", "vni"])
    if ch.request_id:
        NetworkRequest.objects.filter(pk=ch.request_id).update(vlan=vlan, result=f"VLAN {vlan}, VNI {ch.vni}")
    audit(user, "l2_vlan_set", tenant, channel=ch.name, vlan=vlan, was=was or "", vni=ch.vni)
    messages.success(request, _("VLAN %(v)s set.") % {"v": vlan})
    if not ch.tenant_network:
        messages.info(request, _("The channel is made once its subnet is chosen."))
    l2_maybe_start(ch, user, request)
    return _back(request, tenant)


@require_POST
def l2_network_view(request, pk, cid):
    """Another subnet (tenant network) for a channel: no approval. A planned channel just records it; an active one gets
    its tenant-side gateway made again on that network."""
    tenant = get_object_or_404(Tenant, pk=pk)
    ch = get_object_or_404(L2Channel, pk=cid, tenant=tenant)
    user = request.user
    if not can_request(tenant, user) or ch.state == L2Channel.State.REMOVED:
        raise PermissionDenied
    site = ch.site
    net = (request.POST.get("tenant_network") or "").strip()[:128]
    if not net:
        messages.error(request, _("Choose the subnet."))
        return _back(request, tenant)
    if net.endswith(f"-main_{site}"):
        messages.error(request, _("The main network cannot be extended over L2."))
        return _back(request, tenant)
    if net == ch.tenant_network:
        messages.info(request, _("Nothing changed."))
        return _back(request, tenant)
    if tenant.network_requests.filter(kind="l2", run=NetworkRequest.Run.RUNNING).exists():
        messages.error(request, _("An L2 change is already running."))
        return _back(request, tenant)
    old = ch.tenant_network
    if ch.state == L2Channel.State.PLANNED:
        ch.tenant_network = net
        ch.save(update_fields=["tenant_network"])
        audit(user, "l2_network_set", tenant, channel=ch.name, was=old, now=net)
        messages.success(request, _("Subnet set."))
        if not ch.vlan:
            messages.info(request, _("The channel is made once the support engineer sets its VLAN."))
        l2_maybe_start(ch, user, request)
        return _back(request, tenant)
    now = timezone.now()
    req = NetworkRequest.objects.create(tenant=tenant, site=site, kind="l2", action=NetworkRequest.Action.NETWORK,
                                        target=str(ch.vni), tenant_network=net, l2_name=ch.name, vlan=ch.vlan,
                                        result=f"from {old}", requested_by=user, state=NetworkRequest.State.APPROVED,
                                        decided_by=user, decided_at=now)
    audit(user, "l2_network_change", tenant, channel=ch.name, was=old, now=net)
    from . import netops
    why = netops.blocked_reason(tenant)
    if why:
        req.run, req.error = NetworkRequest.Run.FAILED, why
        req.save(update_fields=["run", "error"])
        messages.warning(request, _("Not carried out: %(why)s.") % {"why": why})
    else:
        netops.start(req, user)
        messages.info(request, _("The channel's gateway is being made again on %(n)s: its steps are in Tasks.") % {"n": net})
    return _back(request, tenant)
