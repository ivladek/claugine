"""Check the configuration of a tenant's routers (egw, igw, cgw) against Infrastructure → Templates.

The portal logs in over ssh — to the egw/cgw as the platform's super admin on the exchange network
(the site's VyOS management port), to the igw as its service user `claugine` on the igw's ssh port; endpoints of another site than
the portal's are reached through that site's jump host (inventory.sshexec) — reads `show configuration
commands` and compares it with the configuration built from the template:

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
FIRST_BOOT = re.compile(r"^set (interfaces ethernet eth\d+ (address|ipv6|mtu|hw-id)|interfaces loopback"
                        r"|protocols static route 0\.0\.0\.0/0 |service ssh (client-keepalive-interval|disable-host-validation"
                        r"|disable-password-authentication)|system login user \S+ authentication encrypted-password"
                        r"|system login user vyos |system (host-name|time-zone|config-management|option|syslog))")


def compare(expected, actual):
    """(missing, extra) between the template's set-lines and the router's lines."""
    exp = [_norm(x) for x in expected.splitlines() if x.startswith("set ")]
    every = [_norm(x) for x in actual.splitlines() if x.startswith("set ")]
    act = [x for x in every if not FIRST_BOOT.match(x)]
    act_set, exp_set = set(every), set(exp)
    managed = {_prefix(x) for x in exp}
    missing = [x for x in exp if x not in act_set]
    extra = [x for x in act if x not in exp_set and _prefix(x) in managed]
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


def mask(lines, secrets):
    out = []
    for line in lines:
        if "encrypted-password" in line or "plaintext-password" in line:
            line = " ".join(line.split()[:-1] + ["«hash»"])
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


def targets(tenant):
    """[(role, host, port, user, private key)] of the tenant's routers, from its last deployment."""
    from inventory import nfconfig
    from inventory.credentials import account, service_key
    from inventory.models import Platform
    dep = tenant.deployments.exclude(state="plan").filter(state="done").first() or tenant.deployments.exclude(state="plan").first()
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
    if res.get("igw") and res.get("igw_ip") and igw_type_of(tenant) == "vyos":
        svc = service_key()
        out.append(("igw", res["igw_ip"], tenant.igw_ssh_port or 22, nfconfig.SERVICE_USER, svc.private_key))
    return out


def run(check):
    """Check every router of the tenant; save the results. Never raises."""
    from inventory import nfconfig
    t = check.tenant
    secrets = _secrets()
    results = {}
    try:
        tgts = targets(t)
        if not tgts:
            raise DeployError("the project has no deployed routers")
        for role, host, port, user, key in tgts:
            r = {"host": f"{host}:{port}", "user": user}
            try:
                if not user or not key:
                    raise DeployError("no login (user or key) for this router")
                expected = nfconfig.build(role, tenant=t, reveal=True)
                actual = fetch(host, port, user, key, site=(t.order or {}).get("site"))
                missing, extra = compare(expected, actual)
                r.update(status="ok" if not missing and not extra else "drift", missing=mask(missing, secrets),
                         extra=mask(extra, secrets), lines=len([x for x in actual.splitlines() if x.startswith("set ")]),
                         expected=len([x for x in expected.splitlines() if x.startswith("set ")]))
            except nfconfig.BuildError as exc:
                r.update(status="error", error=str(exc))
            except DeployError as exc:
                r.update(status="unreachable", error=mask([str(exc)], secrets)[0])
            results[role] = r
        check.state = check.State.DONE
    except DeployError as exc:
        check.state, check.error = check.State.FAILED, str(exc)
    check.results, check.finished_at = results, timezone.now()
    check.save()
    from accounts.models import audit
    audit(check.requested_by, "config_checked", t, **{k: v["status"] for k, v in results.items()}, error=check.error[:200])
    return check


def start(tenant, user=None):
    import threading
    from django.conf import settings
    from django.db import connection
    from .models import ConfigCheck
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
