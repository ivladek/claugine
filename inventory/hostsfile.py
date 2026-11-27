"""T36 B4c (owner 2026-11-24 rules v2 B6; 2026-11-25 19:25 QB4-3 "short + fqdn of all hosts and one fe vip too for mgmt
or simple platfroms"): the hosts of a mgmt or a simple platform know each other and their front end by name - a block
of /etc/hosts the portal owns, between its BEGIN and END lines, on every host of that platform:

    <core address> <host>.<internal domain> <host>     every host of the platform's clusters (not retired)
    <fe VIP> <fe name>.<internal domain> <fe name>      the platform's front end (its VIP)

Never on a shared or ext-managed platform's hosts. The block is written on the host Deploy makes (its `joined` step)
and rewritten on the platform's other hosts in service whenever its hosts change (a host joins or is retired) or by
hand (Sites → Clusters → Write /etc/hosts); the rest of the file is kept as it is. Written under a lock on the host,
checked after (getent), the previous file kept as /etc/hosts.claugine-prev."""

import shlex

from django.db import IntegrityError, transaction

from accounts.models import audit

from .models import IN_SERVICE, Host

KINDS = ("mgmt", "simple")
BEGIN = "# BEGIN claugine: the platform's hosts and front end (managed by the portal, do not edit)"
END = "# END claugine"


class HostsFileError(Exception):
    pass


def applies(platform):
    return platform.kind in KINDS


def members(platform):
    """commit-540: the hosts of the platform's clusters that are not retired - a host that left a retired cluster for
    another platform is never this platform's."""
    from .models import Cluster

    live = Cluster.objects.filter(platform=platform).exclude(state="retired")
    return Host.objects.filter(clusters__in=live).distinct()


def fe_name(platform):
    return ((platform.fe or {}).get("hostname") or platform.fe_name or "").strip().split(".")[0]


def entries(platform):
    """[(address, fqdn, short)] of the block, and what keeps it from being complete."""
    from . import dns

    domain = dns.domain()
    why = [] if domain else ["Settings → Customer: the internal domain is not set"]
    rows = []
    hosts = members(platform).exclude(state="retired").exclude(ip="").order_by("name")
    for h in hosts:
        rows.append((h.ip, f"{h.name}.{domain}" if domain else h.name, h.name))
    name = fe_name(platform)
    if platform.fe_vip and platform.fe_vip != "0.0.0.0" and name:
        rows.append((platform.fe_vip, f"{name}.{domain}" if domain else name, name))
    return rows, why


def missing_fe(platform):
    """Why the front end's line is left out (its VIP or name not set yet: a green field's mgmt platform before its
    front end), or ""."""
    if platform.fe_vip and platform.fe_vip != "0.0.0.0" and fe_name(platform):
        return ""
    return f"{platform.key}: the front end's VIP or name is not set yet - its line comes when they are"


def block(platform):
    """The block as the records say (rendered under the platform's lock: nothing it reads changes meanwhile)."""
    rows, why = entries(platform)
    if why:
        raise HostsFileError("; ".join(why))
    lines = [BEGIN] + [f"{a} {f} {s}" for a, f, s in rows] + [END]
    return "\n".join(lines) + "\n"


def rows_of(text):
    """[(address, fqdn, short)] of a block's text."""
    return [tuple(ln.split()[:3]) for ln in text.splitlines() if ln and not ln.startswith("#")]


def script(text):
    """The block in place of the old one (or appended), the rest of /etc/hosts kept; then every name of every row must
    resolve to its own address, else the previous file is put back and the script fails (commit-544). Written only
    under the platform's lock (lock()), rendered from the records under it (commit-540/541)."""
    checks = "".join(f"check {shlex.quote(n)} {shlex.quote(a)}\n" for a, f, sh in rows_of(text) for n in (f, sh))
    return (
        "set -e\n"
        "exec 9>/run/claugine-hosts.lock; flock -w 60 9\n"
        "f=/etc/hosts\n"
        "t=$(mktemp); trap 'rm -f \"$t\"' EXIT\n"  # commit-546: removed on every exit
        "cp -p $f /etc/hosts.claugine-prev\n"
        f"awk -v b={shlex.quote(BEGIN)} -v e={shlex.quote(END)} "
        "'$0==b{skip=1;next} skip&&$0==e{skip=0;next} !skip' $f > $t\n"
        "cat >> $t <<'CLAUGINE_HOSTS'\n" + text + "CLAUGINE_HOSTS\n"
        "cat $t > $f\n"  # the same file (its owner, mode, inode)
        "bad=''\n"
        'check() { got=$(getent hosts "$1" | awk \'NR==1{print $1}\'); [ "$got" = "$2" ] || bad="$bad $1=${got:-none}(not $2)"; }\n'
        + checks
        + 'if [ -n "$bad" ]; then cat /etc/hosts.claugine-prev > $f; echo "names resolve wrong:$bad - the previous /etc/hosts is back" >&2; exit 1; fi\n'
        "echo ok\n"
    )


def write(h, platform, ssh=None):
    """The block on one host (``ssh``: a Run's, else the platform's SA over sshexec), rendered from the records now:
    the platform's revision and the rows in one snapshot (commit-541). Returns its row count."""
    from .models import Platform

    with transaction.atomic():  # commit-540/541: rendered and installed under the platform's lock
        lock([platform], site=False)  # commit-552: a writer - never the site
        platform = Platform.objects.get(pk=platform.pk)
        text = block(platform)
        run = ssh or (lambda s: _ssh(h, platform, s))
        run(script(text))
    return len(rows_of(text))


def lock(platforms, site=True):
    """commit-540/541: the platforms' locks: every /etc/hosts write and every edit of what the block holds takes them,
    so neither runs while the other is at work. One order everywhere (commit-550, commit-552): the platforms'
    /etc/hosts locks (by id), then - for an edit - their site rows, then (by the caller) the platform, cluster and host
    rows. An edit waiting for a write to its platform holds no site row meanwhile, so the site's other platforms are
    edited freely; a writer (``site=False``) takes its platform's lock alone and reads the rest without locks."""
    from .models import HostsFileLock, Site

    wanted = {p.pk: p.site_id for p in platforms if p is not None and p.pk and applies(p)}
    for pk in sorted(wanted):
        if not HostsFileLock.objects.filter(platform_id=pk).exists():
            try:
                with transaction.atomic():
                    HostsFileLock.objects.create(platform_id=pk)
            except IntegrityError:  # made meanwhile by another request
                pass
        HostsFileLock.objects.select_for_update().get(platform_id=pk)
    for site_id in sorted(set(wanted.values())) if site else ():
        Site.objects.select_for_update().filter(pk=site_id).first()


class NotATarget(Exception):
    pass


def write_target(host_id, platform):
    """commit-540: one refresh write, the host checked again at the moment of the write and kept locked through it -
    still in a live cluster of this platform, in service, with its address - so a membership or state change (whose
    edits lock the same row) waits for the write, and a host moved meanwhile is skipped."""
    from .models import Platform

    with transaction.atomic():
        # commit-540/541: the platform's lock first - no membership, host or front end edit meanwhile; commit-552: a
        # writer takes no site lock
        lock([platform], site=False)
        # commit-550: the host's own row only (select_related would lock its site too)
        h = Host.objects.select_for_update(of=("self",)).select_related("site").filter(pk=host_id).first()
        if h is None or h.state not in IN_SERVICE or not h.ip or not members(platform).filter(pk=h.pk).exists():
            raise NotATarget("skipped: no longer a host of this platform in service")
        platform = Platform.objects.get(pk=platform.pk)
        text = block(platform)
        _ssh(h, platform, script(text))
    return len(rows_of(text))


def _ssh(h, platform, text, timeout=180, deadline=None):
    from . import sshexec
    from .hostdeploy import logins

    user, key, *_r = logins(platform)
    return sshexec.run(
        h.ip,
        22,
        user,
        key,
        "sudo -n bash -s",
        site=h.site.code,
        stdin=text,
        timeout=timeout,
        stdout_reason=True,
        deadline=deadline,
    )


def targets(platform, exclude=None):
    """The platform's hosts in service (deployed or non-compliant) the block is rewritten on."""
    q = members(platform).filter(state__in=IN_SERVICE).exclude(ip="")  # commit-540
    if exclude is not None:
        q = q.exclude(pk=exclude.pk)
    return list(q.select_related("site").order_by("name"))


def refresh_all(platform, exclude=None):
    """Write the block on each target; {host: "ok, N lines" | the error} - one host failing never stops the others."""
    out = {}
    for h in targets(platform, exclude=exclude):
        try:
            out[h.name] = f"ok, {write_target(h.pk, platform)} lines"
        except NotATarget as exc:
            out[h.name] = str(exc)
        except Exception as exc:  # noqa: BLE001 - said per host in the task's result
            out[h.name] = f"FAILED: {exc}"
    return out


class RefreshFailed(Exception):
    """commit-545: the task fails when a host failed - every host was still tried; the error names each result."""


def refresh_task(platform, exclude=None, task=None):
    out = refresh_all(platform, exclude=exclude)
    bad = sorted(k for k, v in out.items() if v.startswith("FAILED"))
    if bad:
        if task is not None:  # commit-547: every host's result kept on the task, whatever the error's length
            task.result = {"hosts": out}
        raise RefreshFailed(f"/etc/hosts not written on {len(bad)} of {len(out)} host(s): {', '.join(bad)}")
    return {"hosts": out}


def refresh(user, platform, reason, exclude=None):
    """A task rewriting the block on the platform's hosts in service (after the commit). None when it does not apply."""
    if not applies(platform):
        return None
    from accounts import tasks

    def go():
        tasks.start(
            "hosts file",
            f"/etc/hosts on {platform.key}: {reason}",
            lambda t: refresh_task(platform, exclude=exclude, task=t),
            user=user,
            target=platform,
        )

    transaction.on_commit(go)
    audit(user, "hosts_file_refresh", platform, platform=platform.key, reason=reason)
    return True


def changed(user, platforms, reason):
    """commit-542: the block's rows may have changed (a host added, removed, retired or renamed, a cluster retired):
    each mgmt or simple platform concerned is refreshed after the commit."""
    seen = set()
    for p in platforms:
        if p is not None and p.pk not in seen:
            seen.add(p.pk)
            refresh(user, p, reason)


def platforms_of(h):
    """The platforms of the host's clusters that are not retired (commit-540)."""
    return [c.platform for c in h.clusters.exclude(state="retired").select_related("platform")]
