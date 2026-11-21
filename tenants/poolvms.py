"""The project's own VMs on one resource pool (owner 2026-11-12): "resource pool removal - can be allowed even vms
exists / if any vm exists warn again and ask to confirm vms deletion".

A pool is a cluster of a site's platform. Its VMs are the VMs of the project's group whose last placement is on that
cluster. The project's gateways (the names the portal gives them, tenants/undeploy.names_of) are never among them:
they belong to the project, not to the pool.
"""

import time

from .deploy import DeployError, One

WAIT_SECONDS, POLL_SECONDS = 90, 3  # core-010: how long a removal waits for its VMs to be gone


def _platforms(site, cluster_code):
    """(platform, control, its cluster names) of every platform of the site that has, or had, the pool's cluster -
    retired clusters included (commit-024: a retired cluster's VMs may still be there). One without credentials
    raises: its VMs cannot be listed, so the pool cannot be taken for empty."""
    from inventory.models import Cluster, Platform, PlatformControl

    for p in Platform.objects.filter(site__code=site).order_by("key"):
        names = {c.name for c in Cluster.objects.filter(platform=p) if c.order_code == cluster_code}
        if not names:
            continue
        c = PlatformControl.objects.filter(key=p.key).first()
        if c is None or not c.configured:
            raise DeployError(
                f"{p.key}: the pool is there, but the platform has no credentials: its VMs cannot be listed"
            )
        yield p, c, names


def pool_vms(tenant, site, cluster_code):
    """[{"platform", "id", "name"}] of the project's VMs on the pool. Raises DeployError when a platform that has the
    pool does not answer, or cannot list its VMs (core-010: that was an empty pool, and the removal went on); none
    when no platform of the site has, or had, the pool's cluster (it was never placed)."""
    from .undeploy import names_of

    gateways = names_of(tenant)
    out = []
    for platform, control, names in _platforms(site, cluster_code):
        try:
            one = One(control)
            ids = {c.findtext("NAME"): c.findtext("ID") for c in one.xml("one.clusterpool.info").findall("CLUSTER")}
            g = one.find("one.grouppool.info", "GROUP", tenant.group_name)
            if g is None:
                continue
            gid, cids = g.findtext("ID"), {ids[n] for n in names if n in ids}
            for v in one.xml("one.vmpool.info", -2, -1, -1, -1).findall("VM"):
                hist = v.findall("HISTORY_RECORDS/HISTORY")
                cid = hist[-1].findtext("CID") if hist else None
                name = v.findtext("NAME") or ""
                if v.findtext("GID") == gid and cid in cids and not gateways.match(name):
                    out.append({"platform": platform.key, "id": int(v.findtext("ID")), "name": name})
        except DeployError as exc:
            raise DeployError(f"{platform.key}: the pool's VMs cannot be listed ({exc})") from exc
    return out  # no configured platform has the pool's cluster: the portal has no VMs of it to delete


def delete_pool_vms(tenant, site, cluster_code, user=None):
    """Terminate the project's VMs on the pool and wait until they are gone (core-010: the removal was finished while
    they still ran). Returns their names; raises DeployError when one cannot be terminated or is still there after
    WAIT_SECONDS - the removal then waits, and is approved again."""
    from accounts.models import audit
    from inventory.models import PlatformControl

    vms = pool_vms(tenant, site, cluster_code)
    for v in vms:
        One(PlatformControl.objects.get(key=v["platform"])).call("one.vm.action", "terminate-hard", v["id"])
    if vms:
        audit(user, "pool_vms_deleted", tenant, site=site, pool=cluster_code, vms=", ".join(v["name"] for v in vms))
    ids = {(v["platform"], v["id"]) for v in vms}
    deadline = time.monotonic() + WAIT_SECONDS
    while ids:
        ids &= {(v["platform"], v["id"]) for v in pool_vms(tenant, site, cluster_code)}
        if not ids:
            break
        if time.monotonic() > deadline:
            raise DeployError(f"{len(ids)} VM(s) still there after {WAIT_SECONDS} s: approve the removal again")
        time.sleep(POLL_SECONDS)
    return [v["name"] for v in vms]
