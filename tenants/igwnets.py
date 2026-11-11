"""The networks of a tenant's VyOS igw (correction 12): the tenant's subnets on the platform, each connected to the igw or
not; connecting one hot-plugs a NIC (with the network's gateway address when it is free), disconnecting removes it. The
igw's first NIC (the main network) is never touched. The igw's configuration for a new interface is the tenant's (its
configurations page). Reads and changes the platform live."""
import time

from .deploy import DeployError, Deployer, One, template

WAIT_S = 120


def _deployer(tenant):
    d = Deployer(tenant)
    d.one = One(d.control)
    return d


def state(tenant):
    """{"vm": {id, name, state}, "networks": [{id, name, cidr, connected, ip, nic_id, main}]} — live."""
    d = _deployer(tenant)
    name = d.gw("igw")
    vm_id = d._vm_exists(name)
    if vm_id is None:
        raise DeployError(f"no igw VM ({name}) on {d.platform.key}")
    x = d.one.xml("one.vm.info", vm_id, False)
    nics = sorted(x.findall("TEMPLATE/NIC"), key=lambda n: int(n.findtext("NIC_ID")))
    main_id = nics[0].findtext("NETWORK_ID") if nics else None
    by_net = {n.findtext("NETWORK_ID"): n for n in nics}
    out = []
    for v in d.one.xml("one.vnpool.info", -2, -1, -1).findall("VNET"):
        if v.findtext("GNAME") != d.group and v.findtext("ID") != main_id:
            continue
        full = d.one.xml("one.vn.info", int(v.findtext("ID")), False)
        ar = full.find("AR_POOL/AR")
        cidr = ""
        if full.findtext("TEMPLATE/NETWORK_ADDRESS"):
            cidr = f"{full.findtext('TEMPLATE/NETWORK_ADDRESS')}/{full.findtext('TEMPLATE/NETWORK_MASK') or ''}".rstrip("/")
        elif ar is not None and ar.findtext("IP"):
            cidr = f"{ar.findtext('IP')} + {ar.findtext('SIZE')}"
        nic = by_net.get(v.findtext("ID"))
        out.append({"id": int(v.findtext("ID")), "name": v.findtext("NAME"), "cidr": cidr, "connected": nic is not None,
                    "ip": nic.findtext("IP") if nic is not None else "", "nic_id": int(nic.findtext("NIC_ID")) if nic is not None else None,
                    "main": v.findtext("ID") == main_id, "gateway": full.findtext("TEMPLATE/GATEWAY") or ""})
    out.sort(key=lambda n: (not n["main"], n["name"]))
    return {"vm": {"id": vm_id, "name": x.findtext("NAME"), "state": x.findtext("STATE")}, "networks": out}


def _wait(one, vm_id):
    end = time.monotonic() + WAIT_S
    while True:
        x = one.xml("one.vm.info", vm_id, False)
        if x.findtext("STATE") in ("3", "8") and x.findtext("LCM_STATE") in ("3", "0"):
            return
        if time.monotonic() > end:
            raise DeployError(f"igw #{vm_id} not ready after {WAIT_S} s (state {x.findtext('STATE')}/{x.findtext('LCM_STATE')})")
        time.sleep(2)


def apply(tenant, wanted, user=None):
    """Connect the igw to the networks of `wanted` (ids) and disconnect it from the others (but the main network).
    Returns a short report."""
    from accounts.models import audit
    st = state(tenant)
    d = _deployer(tenant)
    vm_id, done = st["vm"]["id"], []
    wanted = {int(w) for w in wanted}
    for n in st["networks"]:
        if n["main"]:
            continue
        if n["id"] in wanted and not n["connected"]:
            leased = {l.findtext("IP") for l in d.one.xml("one.vn.info", n["id"], False).findall("AR_POOL/AR/LEASES/LEASE")}
            nic = {"NETWORK_ID": n["id"]}
            if n["gateway"] and n["gateway"] not in leased:
                nic["IP"] = n["gateway"]
            d.one.call("one.vm.attachnic", vm_id, template({"NIC": nic}))
            _wait(d.one, vm_id)
            done.append(f"{n['name']} connected" + (f" ({nic['IP']})" if nic.get("IP") else ""))
        elif n["id"] not in wanted and n["connected"]:
            d.one.call("one.vm.detachnic", vm_id, n["nic_id"])
            _wait(d.one, vm_id)
            done.append(f"{n['name']} disconnected")
    if done:
        audit(user, "igw_networks_changed", tenant, vm=vm_id, changes="; ".join(done)[:300])
    return "; ".join(done) or "nothing changed"
