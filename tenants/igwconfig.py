"""The configurations of a tenant's igw (VyOS), kept by the portal: read, edited, applied, rolled back.

A configuration is the igw's `show configuration commands` (set lines), kept encrypted (IgwConfig). Applying one:

    over ssh    the portal keeps the running configuration first (to roll back to), then turns the igw into the
                chosen one: it sets what is missing and deletes what the chosen one does not have (never the hw-id
                lines, nor the account the portal logs in with), and commits with `commit-confirm <minutes>`.
                Unless it is confirmed in time (Confirm on the page: `confirm` and `save`), the igw goes back to its
                previous configuration by itself. VyOS counts whole minutes: the timeout in seconds is rounded up
                (60 s = 1 minute).
    redeploy    the igw is made again from the platform's VyOS image with the chosen configuration on its first
                boot, on the same networks and addresses (the takeover's "new igw" steps).

Login: the portal's service account (its key) on an igw the portal made; otherwise the igw login the tenant's
admins keep on the tenant page (address, ssh port, user, password).
"""
import math
import re
import threading
from datetime import timedelta

from django.conf import settings
from django.db import connection
from django.utils import timezone

from .configcheck import SHOW
from .deploy import DeployError
from .routercfg import HEAD, plan

TIMEOUT = 120
MIN_TIMEOUT_S, MAX_TIMEOUT_S = 60, 3600


def login(tenant):
    """{host, port, user, key, password, site, via} to reach the tenant's igw."""
    from .configcheck import targets
    site = (tenant.order or {}).get("site")
    for role, host, port, user, key in targets(tenant):
        if role == "igw" and user and key:
            return {"host": tenant.igw_host or host, "port": port, "user": user, "key": key, "password": None,
                    "site": site, "via": "portal"}
    pw = tenant.get_igw_password()
    host = tenant.igw_host or _main_ip(tenant)
    if host and tenant.igw_username and pw:
        return {"host": host, "port": tenant.igw_ssh_port or 22, "user": tenant.igw_username, "key": None, "password": pw,
                "site": site, "via": "tenant"}
    raise DeployError("no login to the igw: keep its address, ssh port, user and password in Access (igw login)")


def _main_ip(tenant):
    dep = tenant.deployments.filter(state="done").first()
    return ((dep.resources if dep else {}) or {}).get("igw_ip") or ""


def ssh(lg, remote, stdin=None, timeout=60):
    from inventory.sshexec import SshError, run
    try:
        if lg.get("password"):
            return run(lg["host"], lg["port"], lg["user"], None, remote, site=lg["site"], stdin=stdin, timeout=timeout,
                       password=lg["password"])
        return run(lg["host"], lg["port"], lg["user"], lg["key"], remote, site=lg["site"], stdin=stdin, timeout=timeout)
    except SshError as exc:
        raise DeployError(str(exc)) from exc


def fetch(tenant):
    """The igw's running configuration (set lines)."""
    lg = login(tenant)
    text = ssh(lg, SHOW)
    if not any(x.startswith("set ") for x in text.splitlines()):
        raise DeployError("the igw returned no configuration")
    return text


def save_running(tenant, user, name="", source=None):
    from .models import IgwConfig
    c = IgwConfig(tenant=tenant, name=name or f"running {timezone.localtime().strftime('%d.%m.%Y %H:%M')}",
                  source=source or IgwConfig.Source.READ, created_by=user)
    c.set_text(fetch(tenant))
    c.save()
    return c


def keep_pattern(lg):
    """What applying never deletes: hw-id lines and the account the portal logs in with."""
    user = re.escape(lg["user"])
    return re.compile(rf"^set (interfaces ethernet eth\d+ hw-id|system login user {user}( |$))")


def minutes(timeout_s):
    return max(1, math.ceil(int(timeout_s or 60) / 60))


def apply_script(deletes, sets, mins):
    """One vbash session: the changes, then commit-confirm (no save: an unconfirmed change goes back)."""
    return (HEAD + "".join(d + "\n" for d in deletes) + "".join(x + "\n" for x in sets)
            + f'if ! commit-confirm {mins}; then echo "CLAUGINE-COMMIT-FAILED"; discard; exit 1; fi\n'
              "exit\n")


CONFIRM = HEAD + 'if ! confirm; then echo "CLAUGINE-CONFIRM-FAILED"; exit 1; fi\nsave >/dev/null\nexit\n'


def _run_ssh(a):
    """Apply over ssh with an automatic rollback (the IgwApply `a`)."""
    from .models import IgwConfig
    t = a.tenant
    lg = login(t)
    running = ssh(lg, SHOW)
    before = IgwConfig(tenant=t, name=f"before applying {a.config.name}"[:120], source=IgwConfig.Source.BEFORE,
                       created_by=a.created_by)
    before.set_text(running)
    before.save()
    a.before = before
    target = a.config.get_text()
    deletes, sets = plan(running, target, running, keep=keep_pattern(lg))
    if not deletes and not sets:
        a.state, a.result = a.State.DONE, "nothing to change: the igw already has this configuration"
        return
    mins = minutes(a.timeout_s)
    out = ssh(lg, "vbash -s", stdin=apply_script(deletes, sets, mins), timeout=TIMEOUT)
    if "CLAUGINE-COMMIT-FAILED" in out:
        a.state = a.State.FAILED
        a.result = f"commit failed, nothing changed (deleted {len(deletes)}, set {len(sets)} were discarded)"
        return
    a.state, a.deadline = a.State.WAITING, timezone.now() + timedelta(minutes=mins)
    a.result = (f"deleted {len(deletes)}, set {len(sets)}; committed with commit-confirm {mins} min: confirm before "
                f"{timezone.localtime(a.deadline).strftime('%H:%M:%S')}, or the igw goes back to the configuration before")


def _run_redeploy(a):
    from . import takeover
    t = a.tenant
    t.set_igw_config(a.config.get_text())
    t.save(update_fields=["igw_config_encrypted"])
    dep = takeover.start(t, "igw", a.created_by)
    a.deployment = dep
    a.result = "the igw is being made again with this configuration: see the deployment"
    a.state = a.State.RUNNING


def start(tenant, config, method, timeout_s, user):
    """Start applying a saved configuration. Returns the IgwApply."""
    from accounts.models import audit
    from .models import IgwApply
    if tenant.igw_applies.filter(state__in=[IgwApply.State.RUNNING, IgwApply.State.WAITING]).exists():
        expire(tenant)
        if tenant.igw_applies.filter(state__in=[IgwApply.State.RUNNING, IgwApply.State.WAITING]).exists():
            raise DeployError("another apply is running or waiting for confirmation")
    if method == IgwApply.Method.SSH:
        room(tenant)  # correction 14: the running configuration is kept as a stored one first
    a = IgwApply.objects.create(tenant=tenant, config=config, method=method, created_by=user,
                                timeout_s=min(max(int(timeout_s or 60), MIN_TIMEOUT_S), MAX_TIMEOUT_S))
    audit(user, "igw_config_apply_started", tenant, config=config.name, method=method, timeout=a.timeout_s)

    def work():
        try:
            (_run_redeploy if method == IgwApply.Method.REDEPLOY else _run_ssh)(a)
        except DeployError as exc:
            a.state, a.result = IgwApply.State.FAILED, str(exc)[:2000]
        if a.state in (IgwApply.State.DONE, IgwApply.State.FAILED):
            a.finished_at = timezone.now()
        a.save()
        mark_applied(a)
        audit(user, f"igw_config_apply_{a.state}", tenant, config=config.name, result=a.result[:300])

    if not getattr(settings, "DEPLOY_ASYNC", True):
        work()
        return a

    def worker():
        try:
            work()
        except Exception as exc:  # noqa: BLE001 - recorded on the apply
            IgwApply.objects.filter(pk=a.pk).update(state=IgwApply.State.FAILED, result=f"{type(exc).__name__}: {exc}"[:2000],
                                                    finished_at=timezone.now())
        finally:
            connection.close()
    threading.Thread(target=worker, name=f"igw-apply-{a.pk}", daemon=True).start()
    return a


def confirm(a, user):
    """Keep an applied configuration: `confirm` and `save` on the igw."""
    from accounts.models import audit
    expire(a.tenant)
    a.refresh_from_db()
    if a.state != a.State.WAITING:
        raise DeployError("nothing waits for confirmation")
    out = ssh(login(a.tenant), "vbash -s", stdin=CONFIRM, timeout=TIMEOUT)
    if "CLAUGINE-CONFIRM-FAILED" in out:
        raise DeployError("the igw did not take the confirmation")
    a.state, a.finished_at = a.State.CONFIRMED, timezone.now()
    a.result += "; confirmed and saved"
    a.save(update_fields=["state", "finished_at", "result"])
    mark_applied(a)
    audit(user, "igw_config_confirmed", a.tenant, config=a.config.name if a.config else "")


def expire(tenant):
    """Applies not confirmed in time: the igw went back by itself. Redeploys follow their deployment."""
    from .models import IgwApply
    now = timezone.now()
    for a in tenant.igw_applies.filter(state__in=[IgwApply.State.WAITING, IgwApply.State.RUNNING]):
        if a.state == IgwApply.State.WAITING and a.deadline and a.deadline <= now:
            a.state, a.finished_at = IgwApply.State.ROLLED_BACK, a.deadline
            a.result += "; not confirmed in time: the igw went back to the configuration before"
            a.save(update_fields=["state", "finished_at", "result"])
        elif a.method == IgwApply.Method.REDEPLOY and a.deployment_id:
            st = a.deployment.state
            if st in ("done", "failed"):
                a.state = IgwApply.State.DONE if st == "done" else IgwApply.State.FAILED
                a.finished_at = a.deployment.finished_at or now
                a.result = "the igw was made again with this configuration" if st == "done" else \
                    f"making the igw again failed: {a.deployment.error}"[:2000]
                a.save(update_fields=["state", "finished_at", "result"])
                mark_applied(a)


def available(tenant):
    """Whether the tenant has an igw the portal can manage (VyOS)."""
    from .deploy import igw_type_of
    return tenant.approval == "approved" and igw_type_of(tenant) == "vyos"


# --- correction 14: the Configs dialog — current, stored (10 at most), commands, password -----------------------

def stored(tenant):
    return tenant.igw_configs.filter(current=False)


def room(tenant, need=1):
    """Raise when the stored list has no room (10 at most; the current one is not counted)."""
    from .models import IgwConfig
    if stored(tenant).count() + need > IgwConfig.MAX_STORED:
        raise DeployError(f"{IgwConfig.MAX_STORED} configurations are stored already: delete one of them first")


def read_current(tenant, user):
    """Read the igw's running configuration now and keep it as the current one (replacing the last read)."""
    from .models import IgwConfig
    text = fetch(tenant)
    c = tenant.igw_configs.filter(current=True).first() or IgwConfig(tenant=tenant, current=True, source=IgwConfig.Source.READ)
    c.name, c.created_by, c.created_at = "current", user, timezone.now()
    c.set_text(text)
    c.save()
    IgwConfig.objects.filter(pk=c.pk).update(created_at=timezone.now())
    return c


def save_stored(tenant, text, name, user, based_on=None):
    from .models import IgwConfig
    room(tenant)
    c = IgwConfig(tenant=tenant, name=(name or f"saved {timezone.localtime().strftime('%d.%m.%Y %H:%M')}")[:120],
                  based_on=based_on, created_by=user,
                  source=IgwConfig.Source.EDITED if based_on else IgwConfig.Source.UPLOADED)
    c.set_text(text)
    c.save()
    return c


def mark_applied(a):
    """The configuration an apply made the igw's (confirmed, or done): keeps when."""
    if a.config_id and a.state in (a.State.CONFIRMED, a.State.DONE):
        type(a.config).objects.filter(pk=a.config_id).update(applied_at=a.finished_at or timezone.now())


def commands_script(lines, mins=None):
    body = "".join(x + "\n" for x in lines)
    if mins:
        return (HEAD + body + f'if ! commit-confirm {mins}; then echo "CLAUGINE-COMMIT-FAILED"; discard; exit 1; fi\n'
                "exit\n")
    return (HEAD + body + 'if ! commit; then echo "CLAUGINE-COMMIT-FAILED"; discard; exit 1; fi\n'
            "save >/dev/null\nexit\n")


def run_commands(tenant, text, rollback_s, user):
    """Run configuration commands on the igw: with commit-confirm (rolled back unless confirmed in time) when
    rollback_s, else commit and save. Returns the IgwApply."""
    from accounts.models import audit
    from .models import IgwApply
    lines = [x.strip() for x in (text or "").replace("\r\n", "\n").splitlines() if x.strip() and not x.strip().startswith("#")]
    bad = [x for x in lines if not x.startswith(("set ", "delete ", "comment "))]
    if not lines:
        raise DeployError("no commands")
    if bad:
        raise DeployError(f"only set / delete / comment commands: {bad[0][:80]}")
    expire(tenant)
    if tenant.igw_applies.filter(state__in=[IgwApply.State.RUNNING, IgwApply.State.WAITING]).exists():
        raise DeployError("another change is running or waiting for confirmation")
    mins = minutes(rollback_s) if rollback_s else None
    a = IgwApply(tenant=tenant, method=IgwApply.Method.COMMANDS, created_by=user,
                 timeout_s=min(max(int(rollback_s or 60), MIN_TIMEOUT_S), MAX_TIMEOUT_S))
    a.set_commands("\n".join(lines))
    a.save()
    try:
        out = ssh(login(tenant), "vbash -s", stdin=commands_script(lines, mins), timeout=TIMEOUT)
        if "CLAUGINE-COMMIT-FAILED" in out:
            a.state, a.result = a.State.FAILED, "commit failed: nothing changed"
        elif mins:
            a.state, a.deadline = a.State.WAITING, timezone.now() + timedelta(minutes=mins)
            a.result = (f"{len(lines)} command(s) committed with commit-confirm {mins} min: confirm before "
                        f"{timezone.localtime(a.deadline).strftime('%H:%M:%S')}, or the igw goes back by itself")
        else:
            a.state, a.result = a.State.DONE, f"{len(lines)} command(s) committed and saved"
    except DeployError as exc:
        a.state, a.result = a.State.FAILED, str(exc)[:2000]
    if a.state != a.State.WAITING:
        a.finished_at = timezone.now()
    a.save()
    audit(user, f"igw_commands_{a.state}", tenant, lines=len(lines), rollback=bool(mins), result=a.result[:300])
    return a


def set_password(tenant, password, user):
    """Set the igw user's password on the igw (commit and save) — the portal keeps it afterwards."""
    if not tenant.igw_username:
        raise DeployError("the igw has no user")
    q = password.replace("'", "'\\''")
    out = ssh(login(tenant), "vbash -s", stdin=commands_script(
        [f"set system login user {tenant.igw_username} authentication plaintext-password '{q}'"]), timeout=TIMEOUT)
    if "CLAUGINE-COMMIT-FAILED" in out:
        raise DeployError("the igw did not take the password")
