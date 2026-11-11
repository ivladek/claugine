"""The tenant's gateways on its page (correction 11): status for everyone who sees the tenant; a check and a redeploy for
the super admin and support engineers.

    status    from the platform's last read of its VMs (Infrastructure → the site's core services refresh): VM name and
              id, state, management address (VRF MGMT, network cloud-vyos), exchange address; whether the name follows
              the standard (_<org>_<tenant>_<role>_<site>)
    check     as a route reflector's (2026-10-28): over ssh on the management address (VRF MGMT) — it answers, its BGP
              peers are established, the platform's addresses are on it; stored on the tenant (gw_status) — by Check,
              and by every redeploy once the new VM is ready
    redeploy  tenants.takeover.Takeover.replace — a new VM from the templates, the old one removed once the new one passes
"""
from django.utils import timezone

ROLES = ("egw", "cgw")
CHECKS = {"bgp": "ipv4"}  # as the route reflectors' (inventory.core_services)


def run_checks(ip, port, user, key, site, nics=None, checks=None):
    """The checks of a gateway, as a route reflector's: ([{check, ok, detail}], facts). Raises SshError.
    checks: CHECKS (BGP) by default; {} for an L2 gateway (no BGP)."""
    from inventory.core_services import READ_SCRIPT, check_script, evaluate, parse
    from inventory.sshexec import run
    checks = CHECKS if checks is None else checks
    script = check_script(checks)
    out = run(ip, port, user, key, READ_SCRIPT + ("; " + script if script else ""), site=site, timeout=40)
    facts = parse(out)
    if nics:
        facts["vm"] = {"nics": [list(n) for n in nics]}
    res = [{"check": "answers ssh on the management address (VRF MGMT)", "ok": True, "detail": f"{ip}:{port}"}]
    return res + evaluate(checks, out, facts, ip), facts


def result(checks, facts=None, error=""):
    """What is stored for a gateway (Tenant.gw_status[role]): all checks ok, a one-line summary, the checks."""
    ok = bool(checks) and all(c["ok"] for c in checks) and not error
    bad = [f"{c['check']}: {c['detail']}" for c in checks if not c["ok"]]
    bgp = next((c["detail"] for c in checks if c["check"].startswith("BGP")), "")
    detail = error or ("; ".join(bad) if bad else f"all {len(checks)} checks ok" + (f"; BGP {bgp}" if bgp else ""))
    return {"at": timezone.now().isoformat(), "ok": ok, "detail": detail[:400], "checks": checks,
            "hostname": (facts or {}).get("hostname", ""), "os": (facts or {}).get("os", "")}


def ready(checks):
    """A new gateway is ready: it answers, the platform's addresses are on it, at least one BGP peer is established."""
    bgp = next((c for c in checks if c["check"].startswith("BGP")), None)
    addrs = next((c for c in checks if c["check"].startswith("the platform's addresses")), None)
    detail = (bgp or {}).get("detail", "")
    up = int(detail.split("/")[0]) if "/" in detail and detail.split("/")[0].isdigit() else 0
    return bool(checks) and checks[0]["ok"] and up > 0 and (addrs is None or addrs["ok"])


def store(tenant, role, res):
    from .models import Tenant
    t = Tenant.objects.get(pk=tenant.pk)
    st = dict(t.gw_status or {})
    st[role] = res
    Tenant.objects.filter(pk=tenant.pk).update(gw_status=st)
    tenant.gw_status = st


def roles_of(tenant):
    """The gateways the tenant has: egw with the internet, cgw with the corporate network."""
    from .models import PrefixAssignment
    from .network_views import connection_kinds
    kinds = connection_kinds(tenant)
    out = []
    if "inet" in kinds:
        out.append("egw")
    if "corpnet" in kinds or PrefixAssignment.active().filter(tenant=tenant, kind="corpnet").exists():
        out.append("cgw")
    return out


def names(tenant, role):
    """[standard name, the name it had before] of a gateway VM."""
    from .deploy import object_base
    from .naming import vyos_names
    site = (tenant.order or {}).get("site") or ""
    return [vyos_names(tenant, role, site)[0], f"{object_base(tenant)}-{role}_{site}"]


def entries(tenant):
    """[{key, role, kind, label, platform, names}] of the tenant's gateways (correction 12): egw / cgw (kind router),
    each L2 gateway (kind l2: src on the management platform and dst, or the bridge) and the igw (kind igw)."""
    from .deploy import igw_type_of, object_base
    from .models import L2Channel
    from .naming import vyos_names
    o = tenant.order or {}
    site, pkey, base = o.get("site") or "", o.get("platform") or "", object_base(tenant)
    out = []
    for role in roles_of(tenant):
        out.append({"key": role, "role": role, "kind": "router", "label": role, "platform": pkey,
                    "names": names(tenant, role)})
    old = tenant.group_name[len("tenant-"):] if tenant.origin == "adopted" and (tenant.group_name or "").startswith("tenant-") else ""
    for ch in tenant.l2_channels.exclude(state=L2Channel.State.REMOVED).order_by("vlan", "pk"):
        v = ch.vlan or 0
        sides = [("br", pkey)] if ch.mode == "bridge" else [("src", ch.mgmt_platform or f"{site}.mgmt"), ("dst", pkey)]
        for side, pk in sides:
            key = f"l2{side}{v:04d}"
            legacy = [f"{base}-l2br{v}_{site}"] if side == "br" else [f"{base}-s2s{v}{side}_{site}"]
            if old and side != "br":
                legacy.append(f"tenant_s2s-{v:04d}-{side}_{old}_{site}")
            out.append({"key": key, "role": "l2", "kind": "l2", "label": f"L2 {side} · VLAN {v}", "platform": pk,
                        "names": [vyos_names(tenant, key, site)[0]] + legacy})
    kind = igw_type_of(tenant, site)
    if kind:
        out.append({"key": "igw", "role": "igw", "kind": "igw", "label": "igw", "platform": pkey, "igw_type": kind,
                    "names": [vyos_names(tenant, "igw", site)[0], f"{base}-igw_{site}"]})
    return out


def rows(tenant):
    """The rows of the Gateways card: each entry with its VM as the platforms showed it at their last read."""
    from inventory.models import Platform, PlatformControl
    from inventory.nfconfig import management
    from .naming import vyos_names
    o = tenant.order or {}
    site = o.get("site") or ""
    try:
        port = management(site)[1]["vyos"]
    except Exception:  # noqa: BLE001 - a site without settings
        port = 22
    busy = tenant.deployments.filter(state="running").exists() if tenant.pk else False
    controls, out = {}, []
    for e in entries(tenant):
        pk = e["platform"]
        if pk not in controls:
            c = PlatformControl.objects.filter(key=pk).first()
            controls[pk] = (c, {v["name"]: v for v in ((c.vms or []) if c else [])}, Platform.objects.filter(key=pk).first())
        c, vms, p = controls[pk]
        std = e["names"][0]
        vm = next((vms[n] for n in e["names"] if n in vms), None)
        if vm is None and e["kind"] == "igw" and (tenant.igw_vm or {}).get("id"):
            iv = tenant.igw_vm  # the tenant's own VM: found by its address (tenants.igwdetect)
            vm = {**iv, "nics": iv.get("nics") or [], "vcpu": iv.get("cpu"), "ram_gb": iv.get("ram_gb"), "disks_gb": iv.get("disks_gb") or []}
        nics = (vm or {}).get("nics") or []
        mgmt = next((ip for net, ip in nics if net.startswith("cloud-vyos") and ip), "")
        if e["kind"] == "igw":  # owner 2026-10-29: the igw is managed on its own address (main subnet)
            mgmt = (vm or {}).get("ip") or tenant.igw_host or ""
        ix = next((ip for net, ip in nics if net.split("_")[0].endswith("-ix") and ip), "")
        chk = _with_time((tenant.gw_status or {}).get(e["key"]) or {})
        running = bool(vm) and (vm.get("status") or "") == "running"
        out.append({**e, "name": (vm or {}).get("name") or std, "standard": std, "found": vm is not None,
                    "is_standard": bool(vm) and vm.get("name") == std, "id": (vm or {}).get("id"),
                    "status": (vm or {}).get("status") or "", "host": (vm or {}).get("host") or "", "mgmt_ip": mgmt,
                    "ix_ip": ix, "read_at": c.vms_at if c else None, "vm": vm or {},
                    "hostname": vyos_names(tenant, e["key"], site)[1],
                    "old": vms.get(f"{std}-old"), "port": port, "busy": busy, "login": (p.sa if p else ""),
                    "check": chk,
                    # what a tenant user sees: ok or not (the last check when there is one, else the VM running)
                    "healthy": (chk.get("ok") if chk.get("at") else running)})
    return out


def _with_time(chk):
    from datetime import datetime
    out = dict(chk)
    try:
        out["when"] = timezone.localtime(datetime.fromisoformat(chk["at"])) if chk.get("at") else None
    except (TypeError, ValueError):
        out["when"] = None
    return out


def refresh(tenant):
    """Read the tenant's platform's VMs again (read only), so the gateways' rows are current."""
    from inventory.core_services import read_platform_vms
    from inventory.models import PlatformControl
    c = PlatformControl.objects.filter(key=(tenant.order or {}).get("platform")).first()
    if c is None:
        return
    try:
        read_platform_vms(c)
    except Exception:  # noqa: BLE001 - the rows then show the last read
        pass


def check(tenant, user=None, key=None):
    """The tenant's egw / cgw (as route reflectors: ssh in VRF MGMT, BGP, the platform's addresses) and L2 gateways (ssh,
    addresses) — all, or the one of `key`. Stored on the tenant. Never raises."""
    from accounts.models import audit
    from inventory.credentials import account
    from inventory.models import Platform
    from inventory.sshexec import SshError
    o = tenant.order or {}
    refresh(tenant)
    status = dict(tenant.gw_status or {})
    accounts = {}
    done = {}
    for r in rows(tenant):
        if r["kind"] == "igw" or (key and r["key"] != key):
            continue
        if r["platform"] not in accounts:
            p = Platform.objects.filter(key=r["platform"]).first()
            accounts[r["platform"]] = account(p.sa) if p is not None and p.sa else (None, {})
        user_name, acc = accounts[r["platform"]]
        if not r["found"]:
            res = result([], error="no VM on the platform (last read)")
        elif not r["mgmt_ip"]:
            res = result([], error="no management address (made before the management VRF): redeploy it")
        elif not user_name or not acc.get("key"):
            res = result([], error="the platform's super admin secret has no ssh key")
        else:
            try:
                checks, facts = run_checks(r["mgmt_ip"], r["port"], user_name, acc["key"], o.get("site"), r["vm"].get("nics"),
                                           CHECKS if r["kind"] == "router" else {})
                res = result(checks, facts)
            except (SshError, ValueError) as exc:
                res = result([{"check": "answers ssh on the management address (VRF MGMT)", "ok": False,
                               "detail": f"{r['mgmt_ip']}:{r['port']}: {str(exc)[:200]}"}])
        status[r["key"]] = done[r["key"]] = res
    tenant.gw_status = status
    tenant.save(update_fields=["gw_status"])
    audit(user, "tenant_gateways_checked", tenant, **{k: ("ok" if v.get("ok") else v.get("detail", ""))[:120] for k, v in done.items()})
    return done
