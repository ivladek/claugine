"""T36 B5d - time (owner 2026-11-26 11:29, QB5-2: "a new global settings. within site all system syncing with local
ntp (on fe nodes of mgmt or simple platfrom), all fe nodes (mgmt or simple platfrom) on each site syncing with the same
set of ewxternal ntp sources").

- the installation's external NTP servers: Settings → Customer (PortalSettings.ntp_external, up to 4);
- the site's time servers: its hosts whose host service has the function ``time`` (kvm-mgmt, kvm-simple-fe - the FE
  hosts of its mgmt or simple platform), in service (deployed or non-compliant) and confirmed to serve time
  (Host.time_served_at - commit-577: set only by a check over ssh, by Deploy's OS baseline or Check time servers;
  never a planned host or one built before B5d until checked), by name - chrony there, from the external list,
  serving the site;
- every other system of the site (its other hosts, their iLOs, the VMs, the VyOS routers) syncs with them.

The site's typed NTP list (Site.ntp_servers) is no longer read: it is kept as it was (migration 0133 names a site whose
list differs from its time hosts)."""

import ipaddress

TIME = "time"  # the function (Profiles → Functions) a time server's host service has
LIMIT = 4  # timesyncd, chrony and the routers take up to four


def external():
    """The installation's external NTP servers (Settings → Customer)."""
    from tenants.models import PortalSettings

    return [x for x in (PortalSettings.get().ntp_external or []) if x][:LIMIT]


def time_hosts(site):
    """The site's hosts whose host service has the time function, in service and confirmed to serve time
    (commit-577), with a core address - by name."""
    from .models import IN_SERVICE, Host

    keep, fns = [], {}
    clients = None
    for h in (
        Host.objects.filter(site=site, service__isnull=False, state__in=IN_SERVICE, time_served_at__isnull=False)
        .exclude(ip="")
        .select_related("service")
        .order_by("name")
    ):
        if h.service_id not in fns:
            fns[h.service_id] = TIME in h.service.functions()
        if not fns[h.service_id]:
            continue
        if h.time_confirmed_by != PEER:  # commit-577: confirmed by itself - only while it is the site's only host
            if clients is None:
                clients = set(site_hosts(site).values_list("pk", flat=True))
            if clients - {h.pk}:
                continue
        keep.append(h)
    return keep


SELF, PEER = "self", "peer"  # commit-577: who got the answer that confirmed a time server (Host.time_confirmed_by)


def site_hosts(site):
    """The site's hosts that can be its clients and ask: in service, in a cluster, with a core address."""
    from .models import IN_SERVICE, Host

    return Host.objects.filter(site=site, state__in=IN_SERVICE, clusters__isnull=False).exclude(ip="").distinct()


def servers(site):
    """The addresses the site's systems sync with: its time hosts' core addresses (up to 4); [] while it has none."""
    if site is None:
        return []
    return [h.ip for h in time_hosts(site)][:LIMIT]


def allowed(site, extra=()):
    """Who may ask a time server of the site: the site's internal networks (Site.management hosts, as fail2ban's and
    the routers' management list) and ``extra`` (the host's own networks), as prefixes, each once, in order."""
    out = []
    for x in [*((site.management or {}).get("hosts") or []), *extra]:
        try:
            n = str(ipaddress.ip_network(x if "/" in str(x) else f"{x}/32", strict=False))
        except ValueError:
            continue
        if n not in out:
            out.append(n)
    return out


def chrony_conf(upstream, allow):
    """/etc/chrony/chrony.conf of a time server: the external servers only (no Ubuntu pool, no sources.d), serving
    ``allow``."""
    lines = ["# claugine (T36 B5d): the site's time server - the installation's external NTP servers, serving the site"]
    lines += [f"server {s} iburst" for s in upstream]
    lines += [
        "driftfile /var/lib/chrony/chrony.drift",
        "ntsdumpdir /var/lib/chrony",
        "logdir /var/log/chrony",
        "maxupdateskew 100.0",
        "rtcsync",
        "makestep 1 3",
        "leapsectz right/UTC",
    ]
    lines += [f"allow {a}" for a in allow]
    return "\n".join(lines) + "\n"


# commit-577: what the host says - chrony running, something on udp/123, and a selected source (waited for up to 2
# minutes after a restart); then PROBE run by the site's other hosts (confirm): an NTP request to the server's core
# address from the client's own core address, so it crosses the network and the server's bridge and firewall as the
# site's systems' requests do; commit-582: over a connected socket (any other endpoint's datagram is dropped), the
# answer must carry the request's random transmit time as its originate time
CHECK = r"""set -e
systemctl is-active --quiet chrony || { echo "chrony is not running" >&2; exit 1; }
ss -Hlun | grep -q ':123 ' || { echo "nothing listens on udp/123" >&2; exit 1; }
i=0
until chronyc -n sources | grep -q '^\^\*'; do
  i=$((i + 1)); [ "$i" -ge 12 ] && { echo "chrony has no selected source" >&2; exit 1; }
  sleep 10
done
"""
PROBE = r"""import os, socket, struct, sys
ip, port, src = sys.argv[1], int(sys.argv[2]), sys.argv[3]
tx = os.urandom(8)
so = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
so.settimeout(3)
so.bind((src, 0))
so.connect((ip, port))
def said(ok, text):
    print(("NTP_OK " if ok else "NTP_FAIL ") + text)
    sys.exit(0)
try:
    so.send(b"\x23" + 39 * b"\0" + tx)
    data = so.recv(512)
except OSError as exc:
    said(False, f"no NTP answer from {ip} to {src}: {exc}")
if len(data) < 48 or data[24:32] != tx:
    said(False, f"{ip} gave an answer that is not to this request")
leap, mode, stratum = data[0] >> 6, data[0] & 7, data[1]
if mode != 4 or not 1 <= stratum <= 15 or leap == 3:
    said(False, f"{ip} answers but serves no synchronised time (mode {mode}, stratum {stratum}, leap {leap})")
said(True, f"serving time on {ip} (stratum {stratum})")
"""


def probe_script(ip, src, port=123):
    """PROBE as a shell line: an NTP request to ``ip`` sent from the address ``src`` of the machine running it."""
    import shlex

    return f"python3 - {shlex.quote(ip)} {int(port)} {shlex.quote(src)} <<'CLAUGINE_PY'\n{PROBE}CLAUGINE_PY\n"


DEADLINE = 180  # commit-585: seconds the peers are tried in, one after another


def peers(h):
    """commit-577/585: every other host of the site in service, in a cluster (its platform's SA logs in), with a core
    address - real clients of the site, their request crosses the network and the server's bridge and firewall; the
    server's own platform's first, then by name."""
    q = site_hosts(h.site).exclude(pk=h.pk)
    mine = set(h.clusters.values_list("platform_id", flat=True)) if h.pk else set()
    return sorted(q, key=lambda x: (not (set(x.clusters.values_list("platform_id", flat=True)) & mine), x.name))


PEER_TIMEOUT = 30  # commit-586: seconds one peer is given, so a stalled one leaves time for the next


def _peer_ssh(peer, script, timeout=PEER_TIMEOUT, deadline=None):
    """commit-587: the attempt, the jump host's included, ends by ``deadline`` (time.monotonic(), set by confirm when
    the peer is chosen - the lookups here count against it)."""
    from .hostsfile import _ssh

    c = peer.clusters.select_related("platform").order_by("pk").first()
    return _ssh(peer, c.platform, script, timeout=timeout, deadline=deadline)


class TimeCheckError(Exception):
    """The server does not serve time: its own check failed, or a client of the site that ran the request got no
    synchronised answer."""


class Inconclusive(Exception):
    """commit-585: no host of the site could run the request (ssh or sudo refused, no answer from it): nothing is
    known about the server - its mark stays as it was."""


def outcome(out):
    """(ok, text) from PROBE's output; None when it did not run (no NTP_OK / NTP_FAIL line)."""
    for line in reversed(str(out or "").splitlines()):
        if line.startswith("NTP_OK "):
            return True, line[7:]
        if line.startswith("NTP_FAIL "):
            return False, line[9:]
    return None


def confirm(h, ip, ssh_self, ssh_peer=None, clock=None):
    """commit-577/585: CHECK on the server itself, then the request from the site's clients - the peers in order until
    one runs it (deadline DEADLINE s): its synchronised answer confirms (PEER), its failure fails the server; a peer
    that cannot run it is skipped. Every peer skipped: Inconclusive. No other host of the site in service yet (its
    first FE hosts): the server asks itself (SELF - counted only while it stays the site's only host). (how, by)."""
    import time

    clock = clock or time.monotonic  # the peers' deadlines are on this clock (time.monotonic, as sshexec reads)
    ssh_self(CHECK)
    skipped = []
    end = clock() + DEADLINE
    for p in peers(h):
        now = clock()  # commit-587: one reading for what is left and for this attempt's deadline
        left = end - now
        if left < 1:
            skipped.append("the deadline passed")
            break
        give = min(PEER_TIMEOUT, int(left))
        script = probe_script(ip, p.ip)
        try:  # commit-586/587: each attempt ends by an absolute deadline - never after the search's own end
            res = outcome((ssh_peer or _peer_ssh)(p, script, timeout=give, deadline=min(now + give, end)))
        except Exception as exc:  # noqa: BLE001 - it could not run there
            res, why = None, str(exc)
        else:
            why = "it did not run the request"
        if clock() >= end:  # commit-586/587: read again when an attempt returns - nothing after it is used
            skipped.append(f"{p.name}: its result came after the deadline" if res is not None else f"{p.name}: {why}")
            skipped.append("the deadline passed")
            break
        if res is None:
            skipped.append(f"{p.name}: {why}")
            continue
        if not res[0]:
            raise TimeCheckError(f"{p.name} gets no time from it: {res[1]}")
        return f"answered {p.name}", PEER
    if skipped:
        raise Inconclusive("no host of the site could run the check - " + "; ".join(skipped))
    res = outcome(ssh_self(probe_script(ip, ip)))
    if res is None or not res[0]:
        raise TimeCheckError(res[1] if res else "the request did not run on the server")
    return "answered itself (no other host of the site in service yet)", SELF


def address_of(h):
    """(ip, ip_rev) of the host as stored now - what a check confirms (commit-580); None when it is gone (commit-590)."""
    from .models import Host

    return Host.objects.filter(pk=h.pk).values_list("ip", "ip_rev").first()


def mark(h, ok, addr, by="", only_by=None):
    """Host.time_served_at now (ok) or cleared - commit-580: only while the host's address and its revision are still
    ``addr`` = (ip, ip_rev), as read before the check (any address edit meanwhile, even back to the same address, raised
    the revision and cleared the mark)."""
    from django.utils import timezone

    from .models import Host

    if addr is None:  # commit-590: the host is gone
        return False
    ip, rev = addr
    q = Host.objects.filter(pk=h.pk, ip=ip, ip_rev=rev)
    if only_by is not None:  # commit-588: only while it is still confirmed as the caller saw it
        q = q.filter(time_confirmed_by=only_by)
    n = q.update(time_served_at=timezone.now() if ok else None, time_confirmed_by=by if ok else "")
    return bool(n)  # commit-584: False - the address changed during the check, its result is not kept


STALE = "not kept: the address changed during the check - check again"


def time_candidates(platform):
    """The platform's hosts in service whose host service has the time function (Check time servers), by name."""
    from .models import IN_SERVICE, Host

    out = []
    for h in (
        Host.objects.filter(clusters__platform=platform, state__in=IN_SERVICE, service__isnull=False)
        .exclude(ip="")
        .distinct()
        .select_related("service", "site")
        .order_by("name")
    ):
        if TIME in h.service.functions():
            out.append(h)
    return out


def check_platform(platform, ssh=None, ssh_peer=None):
    """commit-577: each time host of the platform checked (confirm: itself over ssh with the platform's SA, then its
    site's hosts as clients); {host: "serving time, answered …" | the error} - a host passing is marked a time server,
    one failing is no longer one. One host never stops the others."""
    if ssh is None:
        from .hostsfile import _ssh as ssh
    out = {}
    for h in time_candidates(platform):
        addr = address_of(h)
        if addr is None:  # commit-590: removed since the list was read
            out[h.name] = "FAILED: removed during the check"
            continue
        try:
            how, by = confirm(h, addr[0], lambda script, h=h: ssh(h, platform, script), ssh_peer=ssh_peer)
        except Inconclusive as exc:  # commit-585: nothing known - its mark stays
            out[h.name] = f"INCONCLUSIVE: {exc}"
        except Exception as exc:  # noqa: BLE001 - said per host
            mark(h, False, addr)
            out[h.name] = f"FAILED: {exc}"
        else:
            out[h.name] = f"serving time, {how}" if mark(h, True, addr, by) else f"FAILED: {STALE}"
    return out


class CheckFailed(Exception):
    pass


def check_task(platform, task=None):
    out = check_platform(platform)
    bad = sorted(k for k, v in out.items() if v.startswith(("FAILED", "INCONCLUSIVE")))
    if bad:
        if task is not None:
            task.result = {"hosts": out}
        raise CheckFailed(f"{len(bad)} of {len(out)} time server(s) of {platform.key} not confirmed: {', '.join(bad)}")
    return {"hosts": out}


def client_check(site, src, ssh):
    """commit-577: a new host of the site (``src``: its core address, ``ssh``: a script run on it) asks each of the
    site's time servers before it is given them - (the addresses that answered it, the reasons of those that did
    not). A self-confirmed server that answers is confirmed by it (PEER); one that does not is no longer a time
    server (it never answered a client). A peer-confirmed server that does not is left out for this host only."""
    ok, bad = [], []
    chosen = []  # commit-584: (host, its (ip, ip_rev) as asked) of each that answered

    def keep_fresh():
        """commit-584/591: the chosen ones read again together - one whose address moved (or that is gone) since it
        was asked is dropped, said in ``bad``; its place is free for the next candidate."""
        from .models import Host

        now = {
            pk: (ip, rev)
            for pk, ip, rev in Host.objects.filter(pk__in=[h.pk for h, _a in chosen]).values_list("pk", "ip", "ip_rev")
        }
        for h, addr in list(chosen):
            if now.get(h.pk) != addr:
                chosen.remove((h, addr))
                gone = h.pk not in now
                bad.append(
                    f"{h.name}: " + ("removed during the check" if gone else "its address changed during the check")
                )

    candidates = iter(time_hosts(site))
    while True:
        if len(chosen) >= LIMIT:  # commit-589: the first four that answer, in discovery order
            keep_fresh()  # commit-591: this read decides and is the last - a stale one's place goes to the next
            if len(chosen) >= LIMIT:
                break
        h = next(candidates, None)
        if h is None:  # every candidate asked: the last read of what was chosen
            keep_fresh()
            break
        addr = address_of(h)
        if addr is None:  # commit-590
            bad.append(f"{h.name}: removed during the check")
            continue
        res = outcome(ssh(probe_script(addr[0], src)))
        if res is None:
            raise TimeCheckError("the time request did not run on this host")
        now = address_of(h)
        if now != addr:  # commit-584/590: edited or removed during the probe - not what was asked any more
            bad.append(
                f"{h.name}: " + ("removed during the check" if now is None else "its address changed during the check")
            )
            continue
        if res[0]:
            if h.time_confirmed_by != PEER and not mark(h, True, addr, PEER):
                bad.append(f"{h.name}: {STALE}")  # commit-584
                continue
            chosen.append((h, addr))
        else:
            bad.append(f"{h.name}: {res[1]}")
            if h.time_confirmed_by == SELF:  # commit-588: unless a peer confirmed it meanwhile
                mark(h, False, addr, only_by=SELF)
    ok = [addr[0] for _h, addr in chosen]
    return ok, bad
