"""The project's own VMs on one resource pool (owner 2026-11-12): "resource pool removal - can be allowed even vms
exists / if any vm exists warn again and ask to confirm vms deletion".

A pool is a cluster of a site's platform. Its VMs are the VMs of the project's group whose last placement is on that
cluster. The project's gateways (the names the portal gives them, tenants/undeploy.names_of) are never among them:
they belong to the project, not to the pool.
"""

from .deploy import DeployError, One


def _platforms(site):
    from inventory.models import Platform, PlatformControl

    for p in Platform.objects.filter(site__code=site).order_by("key"):
        c = PlatformControl.objects.filter(key=p.key).first()
        if c is not None and c.configured:
            yield p, c


def pool_vms(tenant, site, cluster_code):
    """[{"platform", "id", "name"}] of the project's VMs on the pool. Raises DeployError when no platform answers."""
    from inventory.models import Cluster

    from .undeploy import names_of

    gateways = names_of(tenant)
    out, answered = [], False
    for platform, control in _platforms(site):
        names = set(
            Cluster.objects.filter(platform=platform, quota_code=cluster_code)
            .exclude(state="retired")
            .values_list("name", flat=True)
        )
        if not names:
            continue
        try:
            one = One(control)
            ids = {c.findtext("NAME"): c.findtext("ID") for c in one.xml("one.clusterpool.info").findall("CLUSTER")}
            g = one.find("one.grouppool.info", "GROUP", tenant.group_name)
            answered = True
            if g is None:
                continue
            gid, cids = g.findtext("ID"), {ids[n] for n in names if n in ids}
            for v in one.xml("one.vmpool.info", -2, -1, -1, -1).findall("VM"):
                hist = v.findall("HISTORY_RECORDS/HISTORY")
                cid = hist[-1].findtext("CID") if hist else None
                name = v.findtext("NAME") or ""
                if v.findtext("GID") == gid and cid in cids and not gateways.match(name):
                    out.append({"platform": platform.key, "id": int(v.findtext("ID")), "name": name})
        except DeployError:
            continue
    if not answered:
        raise DeployError(f"no platform of {site} answered")
    return out


def delete_pool_vms(tenant, site, cluster_code, user=None):
    """Terminate the project's VMs on the pool (they go away within a minute or two). Returns their names."""
    from accounts.models import audit
    from inventory.models import PlatformControl

    vms = pool_vms(tenant, site, cluster_code)
    for v in vms:
        One(PlatformControl.objects.get(key=v["platform"])).call("one.vm.action", "terminate-hard", v["id"])
    if vms:
        audit(user, "pool_vms_deleted", tenant, site=site, pool=cluster_code, vms=", ".join(v["name"] for v in vms))
    return [v["name"] for v in vms]
