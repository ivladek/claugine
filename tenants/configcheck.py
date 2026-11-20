"""Check the configuration of the provider's VyOS routers against Infrastructure → Templates.

owner 2026-11-17: "dont count and dont cotrol any igw - they under client resposnability after deployment / control
only rrs, egw, cgw and l2gs"; Settings → Portal, the VyOS timer: "for all vyos routers - rrs, egw, l2gw, cgw, cloud
egw/igw". So: a project's egw, cgw and L2 gateways (l2-a, l2-b, l2-br of its active external channels) — never its
igw — and the sites' core routers (route reflectors, the site's egw and igw: check_core).

The portal logs in over ssh as the platform's super admin on the router's management address (the site's VyOS
management port); endpoints of another site than the portal's are reached through that site's jump host
(inventory.sshexec) — reads `show configuration commands` and compares it with the configuration built from the
template:

    missing   lines of the template the router does not have
    extra     lines the router has under the paths the template manages (firewall, BGP, NAT, QoS, ssh, …)
              that the template does not give

Quotes are ignored in the comparison. Secrets never leave the check: stored lines have them masked.
"""

import re

from django.utils import timezone

from .deploy import DeployError

SHOW = "/opt/vyatta/bin/vyatta-op-cmd-wrapper show configuration commands"
TIMEOUT = 40


def _norm(line):
    return " ".join(line.replace("'", "").replace('"', "").split())


def _prefix(line, n=3):
    return " ".join(line.split()[:n])


# what the first boot sets from the OpenNebula context (left out of the templates): never "extra"
FIRST_BOOT = re.compile(
    r"^set (interfaces ethernet eth\d+ (address|ipv6|mtu|hw-id)|interfaces loopback"
    r"|protocols static route 0\.0\.0\.0/0 |service ssh (client-keepalive-interval|disable-host-validation"
    r"|disable-password-authentication)|system login user \S+ authentication encrypted-password"
    r"|system login user vyos |system (host-name|time-zone|config-management|option|syslog))"
)


# an L2 gateway's tunnel keys are made with the gateways and kept nowhere: never compared
TUNNEL_KEYS = re.compile(r"^set interfaces wireguard \S+ (private-key|peer \S+ (preshared-key|public-key)) ")


def compare(expected, actual, ignore=None):
    """(missing, extra) between the template's set-lines and the router's lines (`ignore`: lines on neither side)."""
    exp = [_norm(x) for x in expected.splitlines() if x.startswith("set ")]
    every = [_norm(x) for x in actual.splitlines() if x.startswith("set ")]
    if ignore is not None:
        exp = [x for x in exp if not ignore.match(x)]
        every = [x for x in every if not ignore.match(x)]
    act = [x for x in every if not FIRST_BOOT.match(x)]
    act_set, exp_set = set(every), set(exp)
    managed = {_prefix(x) for x in exp}
    missing = [x for x in exp if x not in act_set]
    extra = [x for x in act if x not in exp_set and _prefix(x) in managed]
    # commit-052: what the template deletes ("delete protocols static route 0.0.0.0/0") must not be on the router,
    # even where the first boot would otherwise set it
    gone = [_norm(x)[len("delete ") :] for x in expected.splitlines() if x.startswith("delete ")]
    for x in every:
        if x not in exp_set and x not in extra and any(x == "set " + g or x.startswith("set " + g + " ") for g in gone):
            extra.append(x)
    return missing, extra


def _secrets():
    from inventory.models import Secret

    out = []
    for sec in Secret.objects.exclude(kind=Secret.Kind.KEY):
        for f in Secret.SECRET_FIELDS:
            if f in ("private_key", "os_private_key") or not sec.has(f):
                continue
            v = sec.get(f)
            if len(v) >= 6:
                out.append(v)
    return sorted(set(out), key=len, reverse=True)


# commit-050: a credential is hidden by where it stands in the command, whether or not the portal knows it
# (the value is the last element of a VyOS command, and may hold spaces once its quotes are gone: all of it goes,
# whatever it begins with)
CREDENTIAL = re.compile(r"\b(password|preshared-key|private-key|secret|key-string|community)( +)\S.*$")
# a login's hash, by its command path only (a password that merely holds these words is a credential like any other)
LOGIN_HASH = re.compile(r"^(set system login user \S+ authentication (?:encrypted|plaintext)-password )\S.*$")


def mask(lines, secrets):
    out = []
    for line in lines:
        if LOGIN_HASH.match(line):
            line = LOGIN_HASH.sub(r"\1«hash»", line)
        else:
            line = CREDENTIAL.sub(r"\1\2«secret»", line)
        for s in secrets:
            if s in line:
                line = line.replace(s, "«secret»")
        out.append(line)
    return out


def fetch(host, port, user, private_key, site=None):
    """The router's configuration as set-lines (ssh, key only; through the site's jump host when needed)."""
    from inventory.sshexec import SshError, run

    try:
        return run(host, port, user, private_key, SHOW, site=site, timeout=TIMEOUT)
    except SshError as exc:
        raise DeployError(str(exc)) from exc


def targets(tenant, igw=True):
    """[(role, host, port, user, private key)] of the tenant's routers, from its last deployment (`igw=False`: without
    the igw, whose login is not even looked up — commit-054)."""
    from inventory import nfconfig
    from inventory.credentials import account, service_key
    from inventory.models import Platform

    dep = (
        tenant.deployments.exclude(state="plan").filter(state="done").first()
        or tenant.deployments.exclude(state="plan").first()
    )
    res = (dep.resources if dep else {}) or {}
    out = []
    p = Platform.objects.filter(key=(tenant.order or {}).get("platform")).select_related("site").first()
    sa_name, sa = None, {}
    if p is not None and p.sa:
        sa_name, sa = account(p.sa)
    from inventory.nfconfig import management

    vyos_port = management((tenant.order or {}).get("site"))[1]["vyos"]
    for role, ip_key in (("egw", "egw_ix_ip"), ("cgw", "cgw_ix_ip")):
        if res.get(role) and res.get(f"{role}_mgmt_ip"):  # the management VRF (eth0)
            out.append((role, res[f"{role}_mgmt_ip"], vyos_port, sa_name, sa.get("key") or ""))
        elif res.get(role) and res.get(ip_key):  # routers made before the management VRF
            out.append((role, res[ip_key], vyos_port, sa_name, sa.get("key") or ""))
    from .deploy import igw_type_of

    if igw and res.get("igw") and res.get("igw_ip") and igw_type_of(tenant) == "vyos":
        svc = service_key()
        out.append(("igw", res["igw_ip"], tenant.igw_ssh_port or 22, nfconfig.SERVICE_USER, svc.private_key))
    return out


def l2_targets(tenant):
    """[(name, role, host, port, user, private key, site, channel)] of the project's L2 gateways: of every active external
    channel, the bridge (l2-br) or the two tunnel ends (l2-a on the project's platform, l2-b on the edge platform). The
    management address is read from the VM on its platform."""
    from inventory.credentials import account
    from inventory.models import Platform, PlatformControl
    from inventory.nfconfig import MGMT_NETWORK, management

    from .deploy import One
    from .models import L2Channel

    site = (tenant.order or {}).get("site")
    out = []
    chans = tenant.l2_channels.filter(state=L2Channel.State.ACTIVE, kind=L2Channel.Kind.EXTERNAL)
    if not chans.exists():
        return out
    port = management(site)[1]["vyos"]
    home = (tenant.order or {}).get("platform")
    for ch in chans:
        sides = (
            [("l2-br", home, ch.vm_a)]
            if ch.mode == "bridge"
            else [("l2-a", home, ch.vm_a), ("l2-b", ch.mgmt_platform or home, ch.vm_b)]
        )
        for role, pkey, vm_id in sides:
            name = f"{role} VNI {ch.vni}"
            host, user, key = "", None, ""
            # commit-051: the login the gateway was made with — the edge platform's super admin, else the project
            # platform's (tenants.netops _sa_of)
            for k in dict.fromkeys([pkey, home]):
                p = Platform.objects.filter(key=k).first()
                if p is not None and p.sa:
                    user, acc = account(p.sa)
                    key = acc.get("key") or ""
                if user:
                    break
            c = PlatformControl.objects.filter(key=pkey).first()
            if vm_id is not None and c is not None:
                try:
                    vm = One(c).xml("one.vm.info", vm_id, False)
                    host = next(
                        (
                            n.findtext("IP")
                            for n in vm.findall("TEMPLATE/NIC")
                            if n.findtext("NETWORK") == f"{MGMT_NETWORK}_{site}" and n.findtext("IP")
                        ),
                        "",
                    )
                except DeployError:
                    host = ""
            out.append((name, role, host, port, user, key, site, ch))
    return out


def check_targets(tenant):
    """What the check compares: [(name, role, host, port, user, private key, site, channel)] — egw, cgw and the L2
    gateways; never the igw (the project's own after its deployment)."""
    site = (tenant.order or {}).get("site")
    out = [
        (role, role, host, port, user, key, site, None) for role, host, port, user, key in targets(tenant, igw=False)
    ]
    return out + l2_targets(tenant)


def _check_one(role, host, port, user, key, site, build, secrets):
    """The result of one router: status (ok, drift, unreachable, error), missing, extra (masked), line counts."""
    from inventory import nfconfig

    r = {"host": f"{host}:{port}", "user": user or ""}
    try:
        if not host:
            raise DeployError("no management address for this router")
        if not user or not key:
            raise DeployError("no login (user or key) for this router")
        expected = build()
        actual = fetch(host, port, user, key, site=site)
        missing, extra = compare(expected, actual, ignore=TUNNEL_KEYS if role.startswith("l2-") else None)
        r.update(
            status="ok" if not missing and not extra else "drift",
            missing=mask(missing, secrets),
            extra=mask(extra, secrets),
            lines=len([x for x in actual.splitlines() if x.startswith("set ")]),
            expected=len([x for x in expected.splitlines() if x.startswith("set ")]),
        )
    except nfconfig.BuildError as exc:
        r.update(status="error", error=str(exc))
    except DeployError as exc:
        r.update(status="unreachable", error=mask([str(exc)], secrets)[0])
    return r


def run(check):
    """Check every router of the tenant (egw, cgw, L2 gateways); save the results. Never raises."""
    from inventory import nfconfig

    t = check.tenant
    secrets = _secrets()
    results = {}
    try:
        tgts = check_targets(t)
        if not tgts:
            raise DeployError("the project has no deployed routers")
        for name, role, host, port, user, key, site, ch in tgts:

            def build(role=role, ch=ch):
                return nfconfig.build(role, tenant=t, reveal=True, **({"channel": ch} if ch is not None else {}))

            results[name] = _check_one(role, host, port, user, key, site, build, secrets)
        check.state = check.State.DONE
    except DeployError as exc:
        check.state, check.error = check.State.FAILED, str(exc)
    check.results, check.finished_at = results, timezone.now()
    check.save()
    from accounts.models import audit

    audit(
        check.requested_by, "config_checked", t, **{k: v["status"] for k, v in results.items()}, error=check.error[:200]
    )
    return check


def core_nodes():
    """The sites' VyOS core routers: the route reflectors and the site's own egw and igw (cloud-gw, portal-gw)."""
    from inventory.core_deploy import ROLES
    from inventory.models import CoreServiceNode
    from inventory.nfconfig import GATEWAY_ROLES

    nodes = CoreServiceNode.objects.select_related("service", "service__site", "service__template").order_by(
        "service__site__code", "service__name", "name"
    )
    return [n for n in nodes if n.role in ROLES or n.role in GATEWAY_ROLES]


def check_core(node):
    """Compare a core router with its template (inventory.core_deploy.config_of); kept in node.facts["config"]."""
    from inventory import core_deploy, nfconfig
    from inventory.credentials import account
    from inventory.sshexec import SshError
    from inventory.sshexec import run as ssh_run

    user, acc = account(node.secret)
    site = node.service.site.code if node.service.site_id else None
    secrets = _secrets()
    r = {"host": f"{node.address}:{node.ssh_port}", "user": user or ""}
    try:
        try:
            expected = core_deploy.config_of(node, reveal=True, mgmt_ip=node.address)
        except core_deploy.RedeployError as exc:
            raise nfconfig.BuildError(str(exc)) from exc
        if not user or not (acc.get("key") or acc.get("password")):
            raise DeployError("no login (user or key) for this router")
        try:
            actual = ssh_run(
                node.address,
                node.ssh_port,
                user,
                acc.get("key"),
                SHOW,
                site=site,
                timeout=TIMEOUT,
                password=acc.get("password"),
            )
        except SshError as exc:
            raise DeployError(str(exc)) from exc
        missing, extra = compare(expected, actual)
        r.update(
            status="ok" if not missing and not extra else "drift",
            missing=mask(missing, secrets),
            extra=mask(extra, secrets),
            lines=len([x for x in actual.splitlines() if x.startswith("set ")]),
            expected=len([x for x in expected.splitlines() if x.startswith("set ")]),
        )
    except nfconfig.BuildError as exc:
        r.update(status="error", error=str(exc))
    except DeployError as exc:
        r.update(status="unreachable", error=mask([str(exc)], secrets)[0])
    r["at"] = timezone.now().isoformat()
    from django.db import transaction

    from inventory.models import CoreServiceNode

    with transaction.atomic():  # commit-053: merged into what is stored now (a node read may save meanwhile)
        cur = CoreServiceNode.objects.select_for_update().get(pk=node.pk)
        cur.facts = {**(cur.facts or {}), "config": r}
        cur.save(update_fields=["facts"])
    node.facts = cur.facts
    return r


def start(tenant, user=None):
    import threading

    from django.conf import settings
    from django.db import connection

    from accounts import maintenance

    from .models import ConfigCheck

    with maintenance.admitting():  # core-046: admitted only while no maintenance runs (accounts.maintenance)
        check = ConfigCheck.objects.create(tenant=tenant, requested_by=user if getattr(user, "pk", None) else None)
    if not getattr(settings, "DEPLOY_ASYNC", True):
        return run(check)

    def worker():
        try:
            run(check)
        finally:
            connection.close()

    threading.Thread(target=worker, name=f"check-{tenant.pk}", daemon=True).start()
    return check
