"""Numbers for the staff dashboard. Everything here is read-only and cheap enough to run per page view."""

import ipaddress
from collections import Counter

from django.db.models import Count, Q

from tenants.models import Approval, Organization, PrefixAssignment


def _pct(used, total):
    return round(100 * used / total) if total else None


def address_pools():
    """Use of the internet and corporate pools on the exchange networks of shared platforms."""
    from inventory.models import Platform

    active = [(ipaddress.ip_network(p.cidr, strict=False), p.site) for p in PrefixAssignment.active()]
    out = []
    for plat in Platform.objects.filter(kind="shared").select_related("site").order_by("key"):
        for d in plat.delegated_subnets().order_by("purpose", "cidr"):
            pool = ipaddress.ip_network(d.cidr, strict=False)
            used = sum(
                n.num_addresses
                for n, site in active
                if site == plat.site.code and n.version == pool.version and n.subnet_of(pool)
            )
            out.append(
                {
                    "platform": plat.key,
                    "segment": d.segment,
                    "cidr": d.cidr,
                    "purpose": d.purpose,
                    "used": used,
                    "total": pool.num_addresses,
                    "pct": _pct(used, pool.num_addresses),
                }
            )
    return out


# --- dashboards per role -----------------------------------------------------------------------------------

DASHBOARDS = [  # (key, label) in the order of the switch
    ("super", "Super admin"),
    ("support", "Support engineer"),
    ("rm", "Resource manager"),
    ("sales", "Sales manager"),
    ("kam", "Key account manager"),
]


def dashboards_of(user):
    """The dashboards a platform user may open: one per role they hold."""
    out = []
    if user.is_super_admin:
        out.append("super")
    if user.is_support:
        out.append("support")
    if user.is_resource_manager:
        out.append("rm")
    if user.is_sales:
        out.append("sales")
    if user.is_kam:
        out.append("kam")
    return out


def _tb(gb):
    return round(gb / 1024, 1)


def build(user, board):
    """The widgets of one dashboard, seen as `user` (whose approvals, organizations and tenants count)."""
    from adoption.models import PlatformStats
    from inventory import capacity
    from inventory.models import Cluster, Platform, PlatformDatastore
    from tenants.views import _visible_tenants, pending_for, waiting_items

    items = waiting_items(user)
    ctx = {"board": board, "approvals": {"total": len(items), "mine": pending_for(user)}}
    tenants = _visible_tenants(user).exclude(approval__in=[Approval.REJECTED, Approval.CANCELLED])
    scoped = board in ("sales", "kam")  # u12: a manager/KAM gets the RM layout, scoped to their own orgs
    scoped_orgs = None
    if scoped:
        if board == "sales":
            scoped_orgs = Organization.objects.filter(sales_rep=user)
            tenants = tenants.filter(organization__sales_rep=user)
        else:
            scoped_orgs = Organization.objects.filter(kam=user)
            tenants = tenants.filter(organization__kam=user)
    shared = board in ("rm", "sales", "kam")  # the shared-cluster, RM-style capacity view (read-only for manager/KAM)
    stats = list(PlatformStats.objects.all())
    vm = Counter()
    for s in stats:
        for k in ("total", "running"):
            vm[k] += int(s.vms.get(k) or 0)
    clusters = Cluster.objects.exclude(state="retired")
    dss = PlatformDatastore.objects.select_related("volume", "platform")
    if shared:
        clusters = clusters.filter(kind="shared")
        dss = dss.filter(platform__kind="shared")
    ctx.update(
        platforms=Platform.objects.count(),
        platforms_shared=Platform.objects.filter(kind="shared").count(),
        clusters=clusters.count(),
        clusters_shared=clusters.filter(kind="shared").count(),
        datastores_tb=_tb(sum(d.volume.size_gb for d in dss if d.volume)),
        datastores=dss.count(),
        vms=dict(vm),
        collected=bool(stats),
        organizations=(scoped_orgs.count() if scoped else Organization.objects.count()),
        tenants=tenants.filter(approval=Approval.APPROVED).count(),
        tenants_pending=tenants.filter(approval=Approval.PENDING).count(),
        capacity=capacity.clusters(kinds=["shared"] if shared else None),
        storage=capacity.datastores(shared_only=shared),
        pools=address_pools(),
        views=["logical"] if shared else ["logical", "physical"],
        refreshed=_refreshed(),
    )
    if scoped:  # u12: the manager's own orgs / the KAM's own projects, alongside the RM-style capacity widgets
        orgs = scoped_orgs.select_related("sales_rep", "kam", "resource_manager", "support_engineer").annotate(
            n_tenants=Count("tenants", filter=~Q(tenants__status="archived"), distinct=True),
            n_users=Count("users", filter=Q(users__is_active=True), distinct=True),
        )
        if board == "sales":
            from tenants.views import with_pending

            ctx["org_list"] = with_pending(list(orgs), user)
        else:
            ctx["tenant_list"] = list(tenants.select_related("organization").order_by("organization__name", "name"))
    return ctx


def _refreshed():
    """Owner 2026-10-29 21:10: the numbers here come from the Platforms timer (Settings → Data → Timers)."""
    from django.utils.dateparse import parse_datetime

    from . import timers

    st = timers.state("platform")
    return {
        "minutes": st["minutes"],
        "at": parse_datetime(st.get("last_at") or "") if st.get("last_at") else None,
        "state": "running" if timers.is_running(st) else st.get("state", ""),
    }
