"""Resource tracking of an organization: every change of its tenants' resources, newest first (correction 08).

A change is a moment when a quota record (per cluster) or a connectivity version (per site) of a tenant took effect,
or the tenant was deleted. Each row shows the tenant's totals right after the change: vCPU and RAM (with the GPU slots
of GPU clusters), storage, backups, public addresses, internet bandwidth and L2 channels.
"""

from django.utils import timezone

from .models import Connectivity, QuotaRecord, TenantDeletion


def _day(at):
    return timezone.localtime(at).date() if timezone.is_aware(at) else at.date()


def _public_ips(c):
    if "inet" not in (c.connection_type or "").split():
        return 0
    lengths = [c.inet_main_prefix] if c.inet_main_prefix else []
    lengths += [int(x) for x in (c.inet_public_prefixes or "").split() if x.isdigit()]
    return sum(2 ** (32 - n) for n in lengths if 0 < n <= 32)


def _l2(c):
    return max(len((c.l2_vlans or "").split()), len((c.l2_vnets or "").split()))


def totals(tenant, at):
    recs = QuotaRecord.current_for(tenant, at=at)
    conns = Connectivity.current_for(tenant, at=at)
    return {
        "cpu": sum(r.cpu or 0 for r in recs),
        "ram_gb": sum(r.ram_gb or 0 for r in recs),
        "gpu": sum(r.gpu or 0 for r in recs),
        "disk_gb": sum(r.disk_gb or 0 for r in recs),
        "backups_gb": sum(r.backups_gb or 0 for r in recs),
        "public_ips": sum(_public_ips(c) for c in conns),
        "inet_mbps": sum(c.inet_mbps or 0 for c in conns if "inet" in (c.connection_type or "").split()),
        "l2": sum(_l2(c) for c in conns),
        "empty": not recs and not conns,
    }


def history(org=None, tenants=None):
    """[{tenant, action, date, days, cpu, ram_gb, gpu, disk_gb, backups_gb, public_ips, inet_mbps, l2}], newest first:
    of an organization's tenants, or of the given tenants (a tenant's own page, correction 11)."""
    rows = []
    for t in tenants if tenants is not None else org.tenants.all():
        moments = sorted(
            {r.effective_from.replace(microsecond=0) for r in t.quota_records.all()}
            | {c.effective_from.replace(microsecond=0) for c in t.connectivity.all()}
        )
        deleted = t.deletions.filter(state=TenantDeletion.State.DONE).order_by("decided_at", "created_at").last()
        prev, out = None, []
        for at in moments:
            # everything of the same second together (a version of several clusters / sites at once)
            v = totals(t, at.replace(microsecond=999999))
            action = "create" if prev is None else ("delete" if v["empty"] else "update")
            if prev is not None and {k: v[k] for k in v if k != "empty"} == {
                k: prev["v"][k] for k in v if k != "empty"
            }:
                continue  # a version that changed nothing tracked here
            out.append({"tenant": t, "action": action, "date": at, "days": None, **v})
            prev = {"date": at, "v": v}
        if deleted is not None:
            at = deleted.decided_at or deleted.created_at
            out.append(
                {
                    "tenant": t,
                    "action": "delete",
                    "date": at,
                    "days": None,
                    **{
                        k: 0 for k in ("cpu", "ram_gb", "gpu", "disk_gb", "backups_gb", "public_ips", "inet_mbps", "l2")
                    },
                }
            )
        # days alive (owner 2026-10-29): how long each state lasted — until the next change, the latest until today
        today = timezone.localdate()
        for i, r in enumerate(out):
            if r["action"] == "delete":
                continue
            end = _day(out[i + 1]["date"]) if i + 1 < len(out) else today
            r["days"] = max((end - _day(r["date"])).days, 0)
            r["current"] = i + 1 == len(out)  # u09: the state in effect now (until today)
        rows += out
    return sorted(rows, key=lambda r: r["date"], reverse=True)
