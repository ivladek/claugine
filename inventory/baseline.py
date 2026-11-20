"""The platform baseline (owner 2026-11-18, T25): the base OS, the OpenNebula version and the FRR version in Settings →
Platforms are used for every decision; the base OS and the OpenNebula version are every platform's cornerstone.

    08:50 "base os - Ubuntu 26.04 / OpenNebula version - 7.4 / FRR Version - 10.6 / thiese data will use for any
          decisions"
    08:56 "base os and opennebula version is corner stone of platfrom / if any of them changed it means that task to
          upgrade must be started as soon as possible and no any host addition and new platfroms can be done"

The check reads every active platform: its OpenNebula version (XML-RPC one.system.version) and each front-end node's
OS (ssh), and each host of its host pool: the OpenNebula version the host reports (TEMPLATE/VERSION) and its OS and FRR
(ssh as the platform's SA). A node on another Ubuntu release or another OpenNebula release (major.minor) is out of
baseline; FRR is shown, never counted. A node that could not be read is "not read": shown, not counted either - the
check proves a difference, it does not guess one. The result is kept on the platform (Platform.baseline) and read by
the deployment of a host and the creation of a platform, which are refused while any platform is out of baseline
(`blocking()`). The timer "baseline" runs the check once a day; Settings → Platforms has Check now.
"""

import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

from django.utils import timezone

OS_RE = re.compile(r"^\d{2}\.04$")  # an Ubuntu LTS release
VER_RE = re.compile(r"^\d{1,3}\.\d{1,3}$")  # commit-175: fits the 8-character fields

READ_SCRIPT = (
    '. /etc/os-release 2>/dev/null; echo "os=$ID $VERSION_ID"; '
    "echo \"frr=$(dpkg-query -W -f='${Version}' frr 2>/dev/null)\"; "
    "echo \"one=$(dpkg-query -W -f='${Version}' opennebula-node-kvm 2>/dev/null)\"; "
    # commit-173: a front end's own OpenNebula (the server package), never the endpoint's answer
    "echo \"onefe=$(dpkg-query -W -f='${Version}' opennebula 2>/dev/null)\""
)


def key(base):
    """commit-174/177: what decides whether a result still applies - the OS and OpenNebula releases and the baseline's
    revision (FRR is only shown)."""
    return {"os": (base or {}).get("os"), "one": (base or {}).get("one"), "rev": (base or {}).get("rev")}  # commit-177


def values(s=None):
    """{"os": "26.04", "one": "7.4", "frr": "10.6", "rev": 1} from Settings → Platforms."""
    if s is None:
        from tenants.models import PortalSettings

        s = PortalSettings.get()
    return {"os": s.baseline_os, "one": s.baseline_one, "frr": s.baseline_frr, "rev": s.baseline_rev}


def _major_minor(v):
    m = re.match(r"^\s*(\d+)\.(\d+)", str(v or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def same(found, wanted):
    """found (any version string) has the release wanted ("7.4.1-1" vs "7.4": yes; "26.04" vs "26.4": yes)."""
    a, b = _major_minor(found), _major_minor(wanted)
    return a is not None and a == b


def _parse(out):
    got = {}
    for line in (out or "").splitlines():
        k, sep, v = line.partition("=")
        if sep and k.strip() in ("os", "frr", "one", "onefe"):
            got[k.strip()] = v.strip()
    distro, _sp, ver = got.get("os", "").partition(" ")
    return {
        "distro": distro,
        "os": ver,
        "frr": got.get("frr", ""),
        "one": got.get("one", ""),
        "onefe": got.get("onefe", ""),
    }


def _login(p):
    """(user, private key) of the platform's SA secret, else its site's (inventory.sshexec.default_account)."""
    from .credentials import account
    from .sshexec import default_account

    ref = p.sa or default_account(p.site.code)
    user, acc = account(ref) if ref else (None, {})
    return (user or "", acc.get("key") or "") if user and acc.get("key") else ("", "")


def _ssh_read(addr, p, user, key):
    from . import sshexec

    return _parse(sshexec.run(addr, 22, user, key, READ_SCRIPT, site=p.site.code, timeout=30))


def _row(kind, name, addr, got, one, base, error=""):
    row = {"kind": kind, "name": name, "addr": addr, "os": "", "distro": "", "one": one or "", "frr": "", "error": ""}
    if got:
        row.update({k: got.get(k) or row[k] for k in ("os", "distro", "frr")})
        if kind == "host" and not row["one"]:
            row["one"] = got.get("one") or ""
        if kind == "front end":  # commit-173
            row["one"] = got.get("onefe") or ""
    row["error"] = str(error)[:200]
    wrong = []
    if row["os"]:
        if row["distro"] not in ("", "ubuntu") or not same(row["os"], base["os"]):
            wrong.append("os")
    if row["one"] and not same(row["one"], base["one"]):
        wrong.append("one")
    row["wrong"] = wrong
    row["state"] = "out" if wrong else ("ok" if row["os"] and row["one"] else "unread")
    return row


def read_platform(p, base=None):
    """The baseline check of one platform: {"at", "base", "nodes": [...], "out", "unread", "error"}. Nothing is saved."""
    from adoption.collector import OneError, client_for

    from .models import Host, PlatformControl

    base = base or values()
    out = {"at": timezone.now().isoformat(), "base": base, "nodes": [], "out": 0, "unread": 0, "error": ""}
    c = PlatformControl.objects.filter(key=p.key).first()
    if c is None:
        out["error"] = "no endpoint"
        return out
    try:
        cl = client_for(c)
        fes = cl.frontends()
        hosts = [
            (h.findtext("NAME") or "", h.findtext("TEMPLATE/VERSION") or "")
            for h in ET.fromstring(cl._call("one.hostpool.info")).findall("HOST")
        ]
    except (OneError, ET.ParseError, OSError) as exc:
        out["error"] = str(exc)[:200]
        return out
    user, key = _login(p)
    known = {h.name: h for h in Host.objects.filter(site=p.site)}
    jobs = []
    for f in fes:
        addr = f["addr"] or (p.fe_vip if len(fes) == 1 else "")
        jobs.append(("front end", f["name"] or addr or p.key, addr, ""))  # commit-173: read on the node
    for name, ver in hosts:
        h = known.get(name)
        jobs.append(("host", name, (h.ip if h and h.ip else name), ver))

    def one_job(j):
        kind, name, addr, ver = j
        if not addr:
            return _row(kind, name, addr, None, ver, base, "no address")
        if not user:
            return _row(kind, name, addr, None, ver, base, "no SA secret with a private key")
        try:
            return _row(kind, name, addr, _ssh_read(addr, p, user, key), ver, base)
        except Exception as exc:  # noqa: BLE001 - shown on the row
            return _row(kind, name, addr, None, ver, base, exc)

    with ThreadPoolExecutor(max_workers=8) as ex:
        out["nodes"] = list(ex.map(one_job, jobs))
    out["out"] = sum(1 for n in out["nodes"] if n["state"] == "out")
    out["unread"] = sum(1 for n in out["nodes"] if n["state"] == "unread")
    return out


def platforms():
    from .models import Platform

    return [p for p in Platform.objects.select_related("site").order_by("site__code", "name") if p.is_active]


def checked_of(r):
    """commit-171: the OS and OpenNebula values of the platform's last complete check (the endpoint read, every node
    read), or None."""
    r = r or {}
    if "checked" in r:
        return r["checked"]
    return key(r.get("base")) if r.get("at") and not r.get("error") and not r.get("unread") else None


def merge(prev, r):
    """commit-171: a node confirmed out of baseline stays so until a read shows it in baseline or OpenNebula no longer
    lists it; an endpoint error keeps the previous nodes (with the error); a node not read now keeps its "out". The
    result keeps "checked": the values of the last complete check - an incomplete one keeps the one before it."""
    prev = prev or {}
    complete = not r.get("error") and not any(n.get("state") == "unread" for n in r["nodes"])
    if key(prev.get("base")) == key(r.get("base")):
        was = {(n["kind"], n["name"]): n for n in prev.get("nodes") or [] if n.get("state") == "out"}
        if r.get("error"):
            r["nodes"] = [dict(n, kept=True) for n in prev.get("nodes") or []]
        else:
            nodes = []
            for n in r["nodes"]:
                old = was.get((n["kind"], n["name"]))
                if old is not None and n["state"] == "unread":
                    n = dict(
                        n,
                        wrong=old.get("wrong") or [],
                        state="out",
                        kept=True,
                        **{k: n.get(k) or old.get(k) or "" for k in ("os", "distro", "one")},
                    )
                nodes.append(n)
            r["nodes"] = nodes
    r["out"] = sum(1 for n in r["nodes"] if n.get("state") == "out")
    r["unread"] = sum(1 for n in r["nodes"] if n.get("state") == "unread")
    r["checked"] = key(r.get("base")) if complete else checked_of(prev)
    return r


def check(p, base=None):
    """Read one platform and keep the result on it (merged with the confirmed results before it, commit-171)."""
    from .models import Platform

    r = merge(Platform.objects.filter(pk=p.pk).values_list("baseline", flat=True).first(), read_platform(p, base))
    Platform.objects.filter(pk=p.pk).update(baseline=r)
    p.baseline = r
    return r


def check_all(base=None):
    """Every active platform (the timer "baseline", Check now). Returns (ok, detail)."""
    base = base or values()
    res = {p.key: check(p, base) for p in platforms()}
    out = [k for k, r in res.items() if r["out"]]
    errs = [k for k, r in res.items() if r["error"]]
    unread = sum(r["unread"] for r in res.values())
    parts = []
    if out:
        parts.append("upgrade required: " + ", ".join(out))
    if errs:
        parts.append("not read: " + ", ".join(errs))
    if unread:
        parts.append(f"{unread} node(s) not read")
    return not out and not errs, "; ".join(parts) or f"{len(res)} platform(s) in baseline"


def state(p, base=None):
    """ "ok", "out" (upgrade required), "stale" (not completely checked against these OS and OpenNebula values yet -
    it blocks, commit-171/172), "unread" (an error or a node not read, between changes of values), or "" (never
    checked)."""
    r = p.baseline or {}
    if not r.get("at"):
        return ""
    cur = key(base or values())
    if key(r.get("base")) == cur and r.get("out"):
        return "out"
    if checked_of(r) != cur:
        return "stale"
    if r.get("error") or r.get("unread"):
        return "unread"
    return "ok"


def blocking(base=None):
    """The platforms that block (no host is deployed and no platform is created): [(key, kind, [what])], kind "out"
    (upgrade required) or "stale" (not checked against these values yet, commit-176)."""
    base = base or values()
    out = []
    for p in platforms():
        st = state(p, base)
        r = p.baseline or {}
        if st == "out":
            what = sorted({f"{n['name']}: " + ", ".join(n["wrong"]) for n in r.get("nodes", []) if n.get("wrong")})
            out.append((p.key, "out", what))
        elif st == "stale":
            why = (
                [f"not all read: {r['error'] or str(r.get('unread')) + ' node(s)'}"]
                if r.get("error") or r.get("unread")
                else []
            )
            out.append((p.key, "stale", why))
    return out


def problem(base=None):
    """A sentence for a refused action, or "" when no platform blocks."""
    b = blocking(base)
    if not b:
        return ""
    base = base or values()
    out = [k for k, kind, _w in b if kind == "out"]
    stale = [k for k, kind, _w in b if kind == "stale"]
    parts = []
    if out:
        parts.append(f"upgrade required: {', '.join(out)} not on the baseline")
    if stale:
        parts.append(f"not checked yet against this baseline: {', '.join(stale)} (Settings → Platforms → Check now)")
    text = "; ".join(parts)
    return (
        text[:1].upper() + text[1:] + f" (Ubuntu {base['os']}, OpenNebula {base['one']}); "
        "no host is deployed and no platform is created until then."
    )
