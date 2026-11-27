"""The machines' logging from Profiles → Logging (owner 2026-11-24 08:54 - 09:36, T33; before: 2026-11-21, from the
services' log sources): the logged services a machine's profile picks and the rsyslog and logrotate files they make.

Owner's rules (worklog/decisions.md, 2026-11-20 22:22 .. 2026-11-24 09:36):
- a machine has the logged services of its profile (logged_of; T34, owner 13:10: "portal i the single source of
  truth" - nothing is looked for on a machine). A Base OS machine always has ``common``; a KVM host that also runs a
  core service (the management nodes: the front end) has that service's too (2026-11-24 09:33); ``nvidia`` only on a
  host with GPUs (09:33);
- every logged service's rsyslog.conf is plain rsyslog text written as it is (09:06 "dont use templates"); it sets
  $.log_type and calls send_logs - the forwarding (00-claugine.conf) is generated: the site's own log collector
  (Sites → the site → Log collectors, "Core services"), one queue (MB on disk while the collector is down) and one retry
  interval for every machine (09:06, Profiles → Logging above the list);
- rotation once a day: Days is how old the kept logs may be; over Size the oldest are deleted first, even younger
  (09:06; claugine-logcap after each rotation); every file its text reads or writes and its "Also rotated" files are
  rotated (09:34 "need to all logs on each systsm will rotated");
- rsyslog's --mark-- lines are dropped everywhere (2026-11-21 07:32).

Every file written starts with HEADER; files of other origin in /etc/rsyslog.d and /etc/logrotate.d are moved to
/etc/claugine/dist/ (kept, not deleted).
"""

import fnmatch
import json
import re

HEADER = "# written by claugine (Profiles → Logging); a change here is overwritten by the next deployment\n"
DIST = "/etc/claugine/dist"
STAGE = "/etc/claugine/logging.new"
PREV = "/etc/claugine/logging.prev"  # commit-258
LOCK = "/run/claugine-logging.lock"  # commit-263: one logging install at a time on a host
BUSY = 75  # commit-263: the exit code of an install refused because another one runs
REOPEN = "/usr/lib/rsyslog/rsyslog-rotate"
# commit-257: an action() ignores the legacy $FileCreateMode, so every file action names its mode
FILE_OPTS = 'fileCreateMode = "0640" fileOwner = "syslog" fileGroup = "adm"'
LOGCAP = "/usr/local/sbin/claugine-logcap"
# commit-447: the Days and Size of every logged service, applied daily after logrotate whether it rotated or not
LOGCAP_CONF = "/etc/claugine/logcap.conf"
LOGCAP_DROPIN = "/etc/systemd/system/logrotate.service.d/claugine-logcap.conf"
LOGCAP_DROPIN_TEXT = f"""[Service]
ExecStartPost=-{LOGCAP} --all {LOGCAP_CONF}
"""
FORWARD = "/etc/rsyslog.d/00-claugine.conf"


class LogConfError(Exception):
    pass


def _q(v):
    """A string inside rsyslog's double quotes."""
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def expand(keys, by_key):
    """The services named, each followed by those it includes (recursively), once each, in order; unknown keys are
    left out (owner 13:15: Graylog with its MongoDB, the front end with its MySQL and Prometheus …)."""
    out, seen = [], set()

    def add(k):
        if k in seen or k not in by_key:
            return
        seen.add(k)
        out.append(k)
        for i in by_key[k].includes or []:
            add(i)

    for k in keys or []:
        add(k)
    return out


def _profiles_of(m):
    """The service profiles a machine is made from: a host - the KVM host profile, then the profile of each core
    service it runs (a management node's front end, owner 2026-11-24 09:33); a base-OS VM - its core service's."""
    from .models import Host, ServiceProfile

    def of(service):
        if service.profile_id:  # owner 2026-11-23 (U4): the profile the service is linked to
            return service.profile
        return ServiceProfile.of_type(service.stype)  # a service saved before the link

    if isinstance(m, Host):  # T36 B4a: its own host service (kvm-mgmt ... kvm-shared-gpu), else the type's
        out = [m.service if m.service_id else ServiceProfile.of_type(ServiceProfile.HOST)]
        out += [of(n.service) for n in m.core_nodes.select_related("service__profile").order_by("pk")]
    else:
        out = [of(m.service)]
    return [p for p in out if p is not None]


def profile_keys(m):
    """The services (HostService: their units are checked) a machine's profiles give it; NVIDIA on a host whose server
    profile has GPUs (owner 13:44)."""
    from .models import Host

    keys = []
    for p in _profiles_of(m):
        keys += [k for k in p.services or [] if k not in keys]
    if isinstance(m, Host) and m.profile_id and m.profile.gpu_count and "nvidia" not in keys:
        keys.append("nvidia")
    return keys or ["base-os"]


def services_of(m, services):
    """The keys of a machine's services: its profile's, expanded with what they include."""
    return expand(profile_keys(m), {s.key: s for s in services})


def has_gpus(m):
    from .models import Host

    return isinstance(m, Host) and bool(m.profile_id and m.profile.gpu_count)


def logged_of(m):
    """The logged services of a machine (owner 2026-11-24): its profiles' (a VyOS profile gives nothing here: its VMs'
    logging is in their VyOS configuration), common always, nvidia only on a host with GPUs (09:33); by name."""
    from .models import LoggedService

    out = {}
    for p in _profiles_of(m):
        if p.is_vyos:
            continue
        for s in p.logged.all():
            if not s.is_vyos:
                out[s.name] = s
    if LoggedService.COMMON not in out:
        common = LoggedService.objects.filter(name=LoggedService.COMMON).first()
        if common is not None:
            out[common.name] = common
    if not has_gpus(m):
        out.pop("nvidia", None)
    return [out[k] for k in sorted(out)]


def collector(site):
    """The site's collector of the core services (correction 10: Sites → Log collectors, purpose "core")."""
    c = (site.log_collectors or {}).get("core") or {}
    if not c.get("host") or not c.get("port"):
        raise LogConfError(
            f"site {site.code} has no log collector for the core services (Sites → {site.code} → Log collectors)"
        )
    proto = (c.get("proto") or "tcp").lower()
    if proto not in ("tcp", "udp"):
        raise LogConfError(f"site {site.code}: the core services' log collector is {proto}, not tcp or udp")
    return {"host": str(c["host"]), "port": int(c["port"]), "proto": proto}


# --- rsyslog -------------------------------------------------------------------------------------------------------

# owner 2026-11-21 10:04 (analysis 5): $RepeatedMsgReduction off - every line kept with its own time, none folded
# into "last message repeated N times"
RSYSLOG_CONF = """module( load = "imuxsock" )
module( load = "imklog" permitnonkernelfacility = "on" )
module( load = "imfile" )
$RepeatedMsgReduction off
$FileOwner syslog
$FileGroup adm
$FileCreateMode 0640
$DirCreateMode 0755
$Umask 0022
$WorkDirectory /var/spool/rsyslog
$IncludeConfig {dir}/*.conf
"""


def _forward(name, template, queue_file, col, rules):
    tcp = col["proto"] == "tcp"
    return (
        f'ruleset ( name = "{name}" ) {{\n'
        "  action (\n"
        f'    type = "omfwd"\n    name = "{name}"\n'
        f'    target = {_q(col["host"])}\n    port = "{col["port"]}"\n    protocol = "{col["proto"]}"\n'
        f'    template = "{template}"\n'
        + ('    tcp_framing = "octet-counted"\n    keepalive = "on"\n' if tcp else "")
        + '    queue.type = "LinkedList"\n    queue.size = "10000"\n'
        f'    queue.filename = "{queue_file}"\n    queue.maxDiskSpace = "{int(rules["queue_mb"])}m"\n'
        '    queue.saveOnShutdown = "on"\n'
        f'    action.resumeRetryCount = "-1"\n    action.resumeInterval = "{int(rules["retry_seconds"])}"\n'
        "  )\n}\n"
    )


MARK = 'if ( $msg contains "-- MARK --" or $msg contains_i "--mark--" ) then { stop }\n'


def common_conf(col, rules):
    """00-claugine.conf: the forwarding, one for the machine (owner 08:54 "common rsyslog.conf part described how to
    logs forwarded to collector can be generated automaticaly")."""
    return (
        HEADER
        # owner 2026-11-21 07:32 "drop mark"; commit-260: first, before anything is written or sent (00-claugine is the
        # first file of the default ruleset)
        + "\n"
        + MARK
        + "\n# the message sent to the site's log collector: RFC 5424 with log_type (and vm_id) in front of the text\n"
        'template ( name = "log_standard" type = "string"\n'
        '  string = "<%pri%>1 %timestamp:::date-rfc3339% %hostname% %syslogtag% - - log_type=%$.log_type% %msg%\\n" )\n'
        'template ( name = "log_onevm" type = "string"\n'
        '  string = "<%pri%>1 %timestamp:::date-rfc3339% %hostname% %syslogtag% - - log_type=%$.log_type% vm_id=%$.vmid% %msg%\\n" )\n\n'
        f"# the site's collector; the queue: in memory, spilled to disk when the collector is down ({int(rules['queue_mb'])} MB,\n"
        "# kept over a restart, the oldest dropped when full), retried forever\n"
        + _forward("send_logs", "log_standard", "fwd_logs", col, rules)
        + "\n"
        + _forward("send_logs_onevm", "log_onevm", "fwd_logs_vm", col, rules)
    )


def _blocks(text, word):
    """The parameter text of each ``<word> ( ... )`` in rsyslog text (quotes and comments read as rsyslog reads them)."""
    out, i, n = [], 0, len(text)
    pat = re.compile(r"\b" + word + r"\s*\(")
    while i < n:
        c = text[i]
        if c == "#":
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if c in "\"'":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        m = pat.match(text, i)
        if m and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] in "_.")):
            depth, j, quote = 1, m.end(), ""
            while j < n and depth:
                ch = text[j]
                if quote:
                    if ch == "\\":
                        j += 1
                    elif ch == quote:
                        quote = ""
                elif ch in "\"'":
                    quote = ch
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                j += 1
            out.append(text[m.end() : j - 1])
            i = j
            continue
        i += 1
    return out


def _param(block, name):
    m = re.search(r"(?i)\b" + re.escape(name) + r'\s*=\s*"((?:[^"\\]|\\.)*)"', block)
    return m.group(1).replace('\\"', '"').replace("\\\\", "\\") if m else None


def conf_files(text):
    """[(path, "imfile" | "omfile")] of the files rsyslog text reads (imfile inputs) or writes (omfile actions)."""
    out = []
    for b in _blocks(text or "", "input"):
        if (_param(b, "type") or "").lower() == "imfile" and _param(b, "file"):
            out.append((_param(b, "file"), "imfile"))
    for b in _blocks(text or "", "action"):
        if (_param(b, "type") or "").lower() == "omfile" and _param(b, "file"):
            out.append((_param(b, "file"), "omfile"))
    seen, uniq = set(), []
    for p, kind in sorted(out, key=lambda x: x[1] != "omfile"):  # a file written and read is rotated as written
        if p not in seen:
            seen.add(p)
            uniq.append((p, kind))
    return uniq


LOG_TYPE_SET = re.compile(r'set\s+\$\.log_type\s*=\s*"([^"]*)"\s*;')


def log_types(text):
    """The log_type values rsyslog text sets (common's, owner 09:36: "set by its rules"), in order, once each."""
    out = []
    for v in LOG_TYPE_SET.findall(text or ""):
        if v not in out:
            out.append(v)
    return out


def log_type_of(s):
    return s.log_type or s.name


def conf_name(s):
    """The service's rsyslog file: common last (its catch-all stops), the others before it."""
    return "/etc/rsyslog.d/90-common.conf" if s.name == "common" else f"/etc/rsyslog.d/40-{s.name}.conf"


def rotate_name(s):
    return "/etc/logrotate.d/90-common.conf" if s.name == "common" else f"/etc/logrotate.d/40-{s.name}.conf"


def service_conf(s):
    """The rsyslog file of a logged service; None when its text is empty."""
    text = (s.rsyslog_conf or "").strip("\n")
    if not text.strip():
        return None
    lt = ", ".join(log_types(text)) if s.name == "common" else log_type_of(s)
    return HEADER + f"# {s.name} - log_type {lt}\n\n" + text + "\n"


def check_text(s):
    """LogConfError for a logged service a machine could not use: its log_type set by its text (owner 09:32), the
    files it reads or writes and those also rotated given as full paths."""
    if s.is_vyos:
        return
    if not s.name or not re.match(r"^[a-z0-9][a-z0-9-]{0,39}$", s.name):
        raise LogConfError("the name is lowercase letters, digits and dashes, at most 40")
    text = s.rsyslog_conf or ""
    if text.strip() and s.name != "common":
        want = log_type_of(s)
        got = log_types(text)
        if want not in got:
            raise LogConfError(f'{s.name}: its rsyslog.conf must set $.log_type = "{want}";')
        other = [v for v in got if v != want]
        if other:
            raise LogConfError(f"{s.name}: its rsyslog.conf sets another log_type: {', '.join(other)}")
    if "CLAUGINE_LOGCONF_EOF" in text:
        raise LogConfError(f"{s.name}: the text holds the portal's end marker")
    for p, _k in conf_files(text):
        if not p.startswith("/"):
            raise LogConfError(f"{s.name}: {p} is not a full path")
    for p in s.also_rotated or []:
        if not str(p).startswith("/var/log/") or any(ch.isspace() or ch in "\"'" for ch in str(p)):
            raise LogConfError(f"{s.name}: also rotated: {p} - a path under /var/log/ without spaces or quotes")
    cmd = s.after_rotation or ""
    if "\n" in cmd:
        raise LogConfError(f"{s.name}: after rotation: one line")
    if int(s.days or 0) < 1 or int(s.size_mb or 0) < 1:
        raise LogConfError(f"{s.name}: Days and Size are at least 1")


# --- logrotate -----------------------------------------------------------------------------------------------------

LOGROTATE_CONF = """# run rotations as root and use adm group for log files under /var/log
su root adm
daily
# keep the last {days} + the current one
rotate {days}
notifempty
missingok
compress
delaycompress
create 0640 root adm
include /etc/logrotate.d
"""

# the files of the system and its packages every machine rotates (the reference machines' 90-packages, 90-system):
# [(file, [(paths, own lines)])]; commit-448: under common's Days and Size, one rule per file as before
FIXED = [
    (
        "90-packages.conf",
        [
            (
                ["/var/log/dpkg.log", "/var/log/alternatives.log", "/var/log/ubuntu-advantage*.log"],
                "  create 0644 root root\n",
            ),
            (["/var/log/apt/*.log"], ""),
            (["/var/log/cloud-init*.log"], ""),
            (["/var/log/unattended-upgrades/*.log"], ""),
        ],
    ),
    (
        "90-system.conf",
        [
            (["/var/log/dmesg"], ""),
            (["/var/log/apport.log"], ""),
            (["/var/log/btmp"], "  create 0660 root utmp\n"),
            (["/var/log/wtmp"], "  create 0664 root utmp\n"),
        ],
    ),
]


def fixed_paths():
    return [(name, p) for name, stanzas in FIXED for paths, _own in stanzas for p in paths]


# --- matching as logrotate does (commit-446): one path part at a time, "*" never crosses "/", a leading dot only by a
# dot ---------------------------------------------------------------------------------------------------------------

GLOB_CHARS = set("*?[")


def _part_match(name, pat):
    if name.startswith(".") and not pat.startswith("."):
        return False
    return fnmatch.fnmatchcase(name, pat)


def path_match(path, pattern):
    """A file name ``path`` that logrotate's glob ``pattern`` takes."""
    a, b = str(path).split("/"), str(pattern).split("/")
    return len(a) == len(b) and all(_part_match(x, y) for x, y in zip(a, b, strict=True))


def _part_covers(kept, cand):
    """Every name the part ``cand`` can match, the part ``kept`` matches too (only cases read for certain)."""
    if kept == cand:
        return True
    if not GLOB_CHARS & set(cand):  # a plain name
        return _part_match(cand, kept)
    if kept.startswith("*") and not GLOB_CHARS & set(kept[1:]) and not cand.startswith("."):
        suffix = kept[1:]  # "*<literal end>" over a pattern that ends with that literal text
        tail = cand[len(cand) - len(suffix) :] if suffix else ""
        return tail == suffix and not (GLOB_CHARS | {"]"}) & set(tail)
    return False


def covers(kept, cand):
    """The pattern ``kept`` rotates every file the pattern (or name) ``cand`` can match."""
    a, b = str(cand).split("/"), str(kept).split("/")
    return len(a) == len(b) and all(_part_covers(y, x) for x, y in zip(a, b, strict=True))


def _covered(path, globs):
    """A file (or a pattern) one of the globs already rotates whole."""
    return any(covers(g, path) for g in globs)


def rotated(s):
    """(written, others): the files of a logged service logrotate rotates - those its text writes (rsyslog reopens
    them) and the others (read by it, or also rotated); a file another of its patterns, or the system's, already
    rotates whole is left to that one."""
    if s.is_vyos:
        return [], []
    files = conf_files(s.rsyslog_conf)
    also = [str(p) for p in s.also_rotated or [] if str(p).strip()]
    fixed = [p for _n, p in fixed_paths()]
    written = [p for p, k in files if k == "omfile" and not _covered(p, also + fixed)]
    read = [p for p, k in files if k == "imfile" and not _covered(p, also + fixed + written)]
    others = read + [p for p in also if p not in read]
    return written, others


def _stanza(paths, days, size, body, cap):
    quoted = " ".join(f"'{p}'" for p in cap)  # claugine-logcap reads the patterns itself
    return (
        "\n".join(paths)
        + "\n{\n"
        + f"  daily\n  rotate {days}\n  maxage {days}\n  compress\n  delaycompress\n  missingok\n  notifempty\n"
        + body
        # owner 09:06: over Size the oldest are deleted first, even younger than Days; commit-447: and any copy older
        + (f"  lastaction\n    {LOGCAP} {size} {days} {quoted}\n  endscript\n" if size else "")
        + "}\n"
    )


def _cap(s):
    """The files a service's Size counts: its own; common's with the system's (commit-448: one budget)."""
    written, others = rotated(s)
    return written + others + ([p for _n, p in fixed_paths()] if s.name == "common" else [])


def service_rotate(s):
    """The logrotate file of a logged service; None when it rotates nothing."""
    written, others = rotated(s)
    if not written and not others:
        return None
    cap, days, size = _cap(s), int(s.days), int(s.size_mb)
    out = [HEADER, f"# {s.name}: daily, kept {days} days, at most {size} MB\n"]
    if written:
        out.append(_stanza(written, days, size, f"  sharedscripts\n  postrotate\n    {REOPEN}\n  endscript\n", cap))
    if others:
        cmd = (s.after_rotation or "").strip()
        body = f"  sharedscripts\n  postrotate\n    {cmd}\n  endscript\n" if cmd else "  copytruncate\n"
        out.append(_stanza(others, days, size, body, cap))
    return "".join(out)


def fixed_rotate(common):
    """{file name: text} of the system's rotation, under common's Days and Size (commit-448); 5 days, no Size
    without common."""
    days, size = (int(common.days), int(common.size_mb)) if common is not None else (5, 0)
    cap = _cap(common) if common is not None else []
    return {
        name: HEADER + "".join(_stanza(paths, days, size, own, cap) for paths, own in stanzas)
        for name, stanzas in FIXED
    }


# claugine-logcap SIZE_MB DAYS PATTERN...: after a rotation, the rotated copies of the files (name.1, name.2.gz,
# name-20261124…) older than DAYS are deleted (commit-447: also copies of an earlier scheme, e.g. the dated ones of the
# 2026-11-21 hourly rotation), then the oldest first until the files and their copies are at most SIZE_MB together
LOGCAP_SCRIPT = r"""#!/usr/bin/python3
# written by claugine (Profiles → Logging): the Days and Size of a logged service, run by logrotate after its rotation
# (claugine-logcap SIZE_MB DAYS PATTERN...) and daily for every service after logrotate (claugine-logcap --all FILE:
# one line "SIZE_MB DAYS PATTERN..." each; commit-447)
import glob, json, os, re, sys, time

def size(f):
    try:
        return os.path.getsize(f)
    except OSError:
        return 0

def mtime(f):
    try:
        return os.path.getmtime(f)
    except OSError:
        return 0

def cap(mb, days, patterns):
    limit = mb * 1024 * 1024
    current, copies = set(), set()
    for pat in patterns:
        for f in glob.glob(pat):
            if os.path.isfile(f):
                current.add(f)
        for f in glob.glob(pat + "[.-]*"):
            if os.path.isfile(f) and re.search(r"(\.[0-9]+|-[0-9]{8,10})(\.gz|\.xz|\.bz2|\.zst)?$", f):
                copies.add(f)
    copies -= current
    # a day more than Days: logrotate's own copies are a little older than their rotation
    old = time.time() - (days + 1) * 86400
    for f in sorted(copies):
        if mtime(f) < old:
            try:
                os.remove(f)
            except OSError:
                pass
    copies = {f for f in copies if os.path.exists(f)}
    total = sum(size(f) for f in current | copies)
    for f in sorted(copies, key=mtime):
        if total <= limit:
            break
        n = size(f)
        try:
            os.remove(f)
            total -= n
        except OSError:
            pass

def main():
    if sys.argv[1:2] == ["--all"]:
        try:
            with open(sys.argv[2], encoding="utf-8") as src:
                lines = src.read().split("\n")  # commit-452: records end at a newline only
        except (IndexError, OSError):
            return 0
        for line in lines:
            if not line.strip() or line.startswith("#"):
                continue
            try:  # commit-452: [Size MB, Days, [files]], each pattern exactly
                mb, days, patterns = json.loads(line)
                cap(int(mb), int(days), [str(p) for p in patterns])
            except (ValueError, TypeError):
                pass
        return 0
    try:
        mb, days = int(sys.argv[1]), int(sys.argv[2])
    except (IndexError, ValueError):
        return 2
    cap(mb, days, sys.argv[3:])
    return 0

sys.exit(main())
"""


# --- whether two logrotate globs can match one same file (commit-269, commit-274, commit-275) -------------------------

_CLASSES = {
    "alpha": "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "upper": "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "lower": "abcdefghijklmnopqrstuvwxyz",
    "digit": "0123456789",
    "xdigit": "0123456789abcdefABCDEF",
    "space": " \t\n\r\f\v",
    "blank": " \t",
    "punct": "!\"#$%&'()*+,-.:;<=>?@[\\]^_`{|}~",  # "/" never matches a glob part
    "cntrl": "".join(chr(c) for c in range(32)) + chr(127),
}
_CLASSES["alnum"] = _CLASSES["alpha"] + _CLASSES["digit"]
_CLASSES["graph"] = _CLASSES["alnum"] + _CLASSES["punct"]
_CLASSES["print"] = _CLASSES["graph"] + " "


class _Unsure(Exception):
    """A pattern the test cannot read for certain ([=a=], [.a.], an unknown class): taken as able to meet anything."""


def _bracket(part, i):
    """(negated, chars, index after the closing ]) of a [...] at i; None when [ is a literal (no closing ])."""
    k, neg, chars = i + 1, False, set()
    if part[k : k + 1] in ("!", "^"):
        neg, k = True, k + 1
    first = True
    while k < len(part):
        c = part[k]
        if c == "]" and not first:
            return neg, frozenset(chars - {"/"}), k + 1
        first = False
        if c == "[" and part[k + 1 : k + 2] in (":", ".", "="):
            kind = part[k + 1]
            end = part.find(kind + "]", k + 2)
            if end < 0:
                raise _Unsure(part)
            name = part[k + 2 : end]
            if kind != ":" or name not in _CLASSES:
                raise _Unsure(part)
            chars.update(_CLASSES[name])
            k = end + 2
            continue
        if part[k + 1 : k + 2] == "-" and k + 2 < len(part) and part[k + 2] != "]":
            lo, hi = ord(c), ord(part[k + 2])
            chars.update(chr(x) for x in range(lo, hi + 1))
            k += 3
            continue
        chars.add(c)
        k += 1
    return None


def _tokens(part):
    """A path part as glob tokens: "*", or (negated, chars) for a literal, ? or [...]."""
    out, i = [], 0
    while i < len(part):
        c = part[i]
        if c == "*":
            if not out or out[-1] != "*":
                out.append("*")
            i += 1
            continue
        if c == "?":
            out.append((True, frozenset()))
            i += 1
            continue
        if c == "[":
            b = _bracket(part, i)
            if b is not None:
                out.append((b[0], b[1]))
                i = b[2]
                continue
        if c == "\\" and i + 1 < len(part):
            i += 1
            c = part[i]
        out.append((False, frozenset(c)))
        i += 1
    return out


def _sets_meet(x, y):
    (nx, sx), (ny, sy) = x, y
    if not nx and not ny:
        return bool(sx & sy)
    if not nx:
        return bool(sx - sy)
    if not ny:
        return bool(sy - sx)
    return True


_NOT_DOT = (True, frozenset("."))


def _visible(T):
    """commit-277: logrotate's glob() has no GLOB_PERIOD - a name starting with "." is matched only by a literal "."
    there. The token lists a part can be when its name must not start with a dot (a literal first character stays as
    it is: a literal "." means the pattern names hidden files)."""
    if not T:
        return [T]
    t = T[0]
    if t == "*":  # nothing (the next one must not be a dot either) or a first character that is not a dot
        return _visible(T[1:]) + [[_NOT_DOT, "*", *T[1:]]]
    neg, chars = t
    return [[(True, chars | {"."}) if neg else (False, chars - {"."}), *T[1:]]]


def _seq_meet(A, B):
    from functools import cache

    @cache
    def f(i, j):
        if i == len(A) and j == len(B):
            return True
        if i < len(A) and A[i] == "*" and (f(i + 1, j) or (j < len(B) and f(i, j + 1))):
            return True
        if j < len(B) and B[j] == "*" and (f(i, j + 1) or (i < len(A) and f(i + 1, j))):
            return True
        return i < len(A) and j < len(B) and "*" not in (A[i], B[j]) and _sets_meet(A[i], B[j]) and f(i + 1, j + 1)

    return f(0, 0)


def _literal_dot(part):
    return part.startswith(".") or part.startswith("\\.")


def _parts_meet(a, b):
    try:
        A, B = _tokens(a), _tokens(b)
    except _Unsure:
        return True
    da, db = _literal_dot(a), _literal_dot(b)
    if da and db:  # both name hidden files
        return _seq_meet(A, B)
    if da or db:  # one names hidden files: the other's wildcards never give a leading dot
        return any(_seq_meet(A if da else B, x) for x in _visible(B if da else A))
    return any(_seq_meet(x, y) for x in _visible(A) for y in _visible(B))  # a name without a leading dot


def rotation_args(line):
    """commit-276/286: the patterns of one rotation line as logrotate reads them - popt's poptParseArgvString: blanks
    separate them; in quotes a backslash is kept unless the closing quote follows it; outside quotes a backslash makes
    the next character plain. None when it cannot be read (an unclosed quote, a backslash at the end)."""
    s, args, cur, quote, i = str(line or ""), [], "", "", 0
    while i < len(s):
        c = s[i]
        if quote and c == quote:
            quote = ""
        elif quote:
            if c == "\\":
                i += 1
                if i >= len(s):
                    return None
                if s[i] != quote:
                    cur += "\\"
            cur += s[i]
        elif c in " \t\n\v\f\r":  # commit-291: popt's isspace() on bytes (C locale) - ASCII only
            if cur:
                args.append(cur)
            cur = ""
        elif c in "\"'":
            quote = c
        else:
            if c == "\\":
                i += 1
                if i >= len(s):
                    return None
            cur += s[i]
        i += 1
    if quote:
        return None
    if cur:
        args.append(cur)
    return args


def globs_meet(p, q):
    """As logrotate's glob: the same number of parts, each pair able to match one same name; a part the test cannot
    read for certain counts as meeting (a rule is then refused rather than risked)."""
    a, b = p.split("/"), q.split("/")
    return len(a) == len(b) and all(_parts_meet(x, y) for x, y in zip(a, b, strict=True))


# --- the hourly logrotate of 2026-11-21 10:49 (size rotation), gone with the logged services (2026-11-24 09:06: daily
# rotation, Size deletes the oldest): its drop-in is removed where it is found ---------------------------------------

TIMER = "/etc/systemd/system/logrotate.timer.d/claugine.conf"
TIMER_CONF = """[Timer]
OnCalendar=
OnCalendar=hourly
AccuracySec=1m
"""


# commit-283/290: logrotate.timer's whole schedule as `systemctl show -P TimersCalendar` gives it, blanks removed -
# exactly one OnCalendar: hourly where the drop-in is, the package's daily elsewhere
HOURLY = "*-*-**:00:00"
DAILY = "*-*-*00:00:00"


def calendars(shown):
    """The OnCalendar values of `systemctl show -P TimersCalendar` (blanks removed)."""
    import re

    return re.findall(r"OnCalendar=([^;}]*)", str(shown or "").replace(" ", ""))


# --- config changes: settings in a service's own files (owner 2026-11-21 10:49 "BIND B1 auditd and Graylog too") ---
# A service's config: {"changes": [{"file", "find" (a regular expression, multi-line mode), "replace"}],
# "check": a command run after a file changed ({file}: the file), "then": reload|restart|nothing, "command"}.
# Each change must change nothing a second time; a file not on the machine is skipped and reported; the file before a
# change is kept as <file>.claugine-prev and put back when the check fails. The same code runs in the portal (the
# check of a machine's logging) and on the machine (CHANGE_PY), so both read a change alike.

THEN = ("reload", "restart", "nothing")
PREV_SUFFIX = ".claugine-prev"
PENDING = "/var/lib/claugine/config-pending"  # commit-278: a service whose reload/restart has not succeeded yet

CHANGE_PY = r"""
import re


def apply_changes(text, changes):
    for c in changes:
        text = re.sub(c["find"], c["replace"], text, flags=re.M)
    return text
"""
_ns = {}
exec(CHANGE_PY, _ns)  # noqa: S102 - the portal's own code, the one sent to the machines
apply_changes = _ns["apply_changes"]

HOST_PY = r"""
import base64, hashlib, json, os, shlex, shutil, stat, subprocess, sys

def run(cmd):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
    except Exception as exc:  # commit-279: a timeout is a failure like any other
        return 1, [str(exc)[:200]]
    return p.returncode, (p.stdout + p.stderr).strip().splitlines()[-3:]

def private(path, write):
    # commit-288: made by this run only (O_EXCL), readable by root alone while it is written
    if os.path.lexists(path):
        os.remove(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            write(out)
    except BaseException:
        os.remove(path)
        raise
    return path

def place(data, path, st):
    # commit-280/288: the new text staged privately, then given the original's owner, group and mode
    tmp = private(path + ".claugine-new", lambda out: out.write(data))
    try:
        os.chown(tmp, st.st_uid, st.st_gid)
        os.chmod(tmp, stat.S_IMODE(st.st_mode))
    except BaseException:
        os.remove(tmp)
        raise
    return tmp

def keep_prev(path, st):
    prev = path + PREV_SUFFIX
    with open(path, "rb") as src:
        data = src.read()
    private(prev, lambda out: out.write(data))
    os.chown(prev, st.st_uid, st.st_gid)
    os.chmod(prev, stat.S_IMODE(st.st_mode))
    return prev

def put_back(prev, path, st):
    try:
        with open(prev, "rb") as src:
            os.replace(place(src.read(), path, st), path)
        return True, "the previous file is back"
    except Exception as exc:
        return False, f"putting the previous file back FAILED ({exc}); it is in {prev}"

def digest(text):
    return hashlib.sha256(text.encode("utf-8") if isinstance(text, str) else text).hexdigest()

def read_marker(marker):
    # commit-292/295: {"files": {path: [sha256 of each text written and still awaiting the reload]}}; None: no marker;
    # {"legacy": true}: a marker without its files (only an unreleased build wrote them) - dropped, nothing reloaded
    if not os.path.exists(marker):
        return None
    try:
        with open(marker, encoding="utf-8") as src:
            rec = json.load(src)
        files = rec.get("files") if isinstance(rec, dict) else None
        if not isinstance(files, dict) or not files:
            return {"legacy": True, "files": {}}
        return {"files": {p: ([d] if isinstance(d, str) else list(d)) for p, d in files.items()}}
    except Exception:
        return {"legacy": True, "files": {}}

def write_marker(marker, rec):
    files = {p: d for p, d in rec["files"].items() if d}
    if not files:
        if os.path.exists(marker):
            os.remove(marker)
        return
    data = json.dumps({"files": files}).encode("utf-8")
    os.replace(private(marker + ".new", lambda out: out.write(data)), marker)

def still_new(rec):
    # a live file still holds a text this marker is for (else every such change was put back: nothing to reload)
    for path, wants in rec["files"].items():
        try:
            with open(path, "rb") as src:
                if digest(src.read()) in wants:
                    return True
        except OSError:
            pass
    return False

def checks_pass(svc):
    # the files as they are now pass the service's check (before any reload/restart)
    check = svc["check"]
    if not check:
        return True, []
    if "{file}" not in check:
        return (lambda r: (r[0] == 0, r[1]))(run(check))
    for f in svc["files"]:
        if os.path.isfile(f["file"]):
            rc, tail = run(check.replace("{file}", shlex.quote(f["file"])))
            if rc:
                return False, tail
    return True, []

os.makedirs(PENDING, exist_ok=True)
for svc in json.loads(base64.b64decode(sys.argv[1])):
    marker = os.path.join(PENDING, svc.get("marker") or svc["key"])
    # commit-445: a marker left under a name the service had (or its service's key before the logged services) is its own
    # commit-449: only when every file it records is one of this service's config files; else it is left as it is
    mine = {f["file"] for f in svc["files"]}
    for old in svc.get("adopt") or []:
        o = os.path.join(PENDING, old)
        if o == marker or not os.path.exists(o):
            continue
        b = read_marker(o)
        if not b or b.get("legacy") or not b.get("files") or not set(b["files"]) <= mine:
            continue
        a = read_marker(marker) if os.path.exists(marker) else {"files": {}}
        if not a or a.get("legacy"):
            a = {"files": {}}
        for path, digests in b["files"].items():
            for d in digests:
                if d not in a["files"].setdefault(path, []):
                    a["files"][path].append(d)
        write_marker(marker, a)
        os.remove(o)
    activate = svc["then"] != "nothing"
    for f in svc["files"]:
        path = f["file"]
        try:
            if not os.path.isfile(path):
                print(f"C {svc['key']} {path} skipped not on the machine"); continue
            with open(path, encoding="utf-8") as src:
                cur = src.read()
            new = apply_changes(cur, f["changes"])
            if apply_changes(new, f["changes"]) != new:
                print(f"C {svc['key']} {path} failed a change changes the file again (not made)"); continue
            if new == cur:
                print(f"C {svc['key']} {path} same"); continue
            st = os.stat(path)
            check = svc["check"]
            staged = "{file}" in check
            tmp = place(new.encode("utf-8"), path, st)
            if staged:  # commit-279: checked before it replaces the live file
                rc, tail = run(check.replace("{file}", shlex.quote(tmp)))
                if rc:
                    os.remove(tmp)
                    print(f"C {svc['key']} {path} failed the check refused it, the file was not changed: {' / '.join(tail)}")
                    continue
            prev = keep_prev(path, st)
            if activate:  # commit-287/292: the intent, with the text it is for, recorded before the live file changes
                rec = read_marker(marker) or {"files": {}}
                if rec.get("legacy"):
                    rec = {"files": {}}
                # commit-295: added to the texts of this file already awaiting a reload, never in place of them
                if digest(new) not in rec["files"].setdefault(path, []):
                    rec["files"][path].append(digest(new))
                write_marker(marker, rec)
            os.replace(tmp, path)
            if check and not staged:  # a check of the live file: put back on any failure
                rc, tail = run(check)
                if rc:
                    ok, what = put_back(prev, path, st)
                    if ok and activate:  # commit-292: put back - this file no longer waits for a reload
                        rec = read_marker(marker) or {"files": {}}
                        if not rec.get("legacy"):  # commit-295: only this run's text leaves the list
                            rec["files"][path] = [x for x in rec["files"].get(path, []) if x != digest(new)]
                            write_marker(marker, rec)
                    print(f"C {svc['key']} {path} failed the check refused it, {what}: {' / '.join(tail)}")
                    continue
            print(f"C {svc['key']} {path} changed")
        except Exception as exc:
            print(f"C {svc['key']} {path} failed {type(exc).__name__}: {str(exc)[:200]}")
    if not activate:
        continue
    rec = read_marker(marker)
    if rec is None:
        continue
    if rec.get("legacy"):  # commit-292: a marker that does not say which files it is for
        os.remove(marker)
        print(f"T {svc['key']} skipped a marker without its files was dropped, nothing reloaded")
        continue
    if not still_new(rec):  # commit-292: every change it was for was put back: nothing to reload
        os.remove(marker)
        continue
    # commit-278: pending until it succeeded; commit-287: only with files that pass the check
    ok, tail = checks_pass(svc)
    if not ok:
        print(f"T {svc['key']} failed {svc['then']} not run: the check refuses the files - {' / '.join(tail)} (pending)")
        continue
    rc, tail = run(svc["command"])
    if rc:
        print(f"T {svc['key']} failed {svc['then']}: {svc['command']} - {' / '.join(tail)} (pending: tried again by the next Apply)")
    else:
        os.remove(marker)
        print(f"T {svc['key']} ok {svc['then']}: {svc['command']}")
# the states are in the lines (a failure of ssh would hide them): always 0 here
sys.exit(0)
"""


def marker_of(s):
    """commit-445: the name of a service's pending marker on a machine - a logged service's id (its name can change)."""
    pk = getattr(s, "pk", None)
    # commit-449: a dot no service name may have - no row's name can be another row's marker
    return f"logged.{pk}" if pk and hasattr(s, "rsyslog_conf") else s.key


def adoptable(s, marker):
    """commit-449/451: the old names of a logged service's marker on a machine it may take over - its name, the names it
    had, the logged-<id> of 7fafd53 - each only when no other logged service has or had that name (or that id
    form) and no other one changes any of its files; otherwise none (the marker stays, shown "reload pending")."""
    from .models import LoggedService

    if not getattr(s, "pk", None) or not hasattr(s, "rsyslog_conf"):
        return []
    mine = {x.get("file") for x in (s.config or {}).get("changes") or []}
    claims = set()
    for r in LoggedService.objects.exclude(pk=s.pk):
        rc = r.config or {}
        if mine & {x.get("file") for x in rc.get("changes") or []}:
            return []  # a file two services change: whose reload a marker is owed is not known
        claims |= {r.name, f"logged-{r.pk}", *(rc.get("former") or [])}
    names = [s.name, *((s.config or {}).get("former") or []), f"logged-{s.pk}"]
    return [n for n in dict.fromkeys(names) if n and n != marker and n not in claims]


def config_of(s):
    """The service's config changes grouped by file: {"key", "marker", "adopt", "files": [{"file", "changes"}],
    "check", "then", "command"}; None when it has none. adopt: the names its marker may still have (commit-445)."""
    c = s.config or {}
    rows = [x for x in c.get("changes") or [] if x.get("file")]
    if not rows:
        return None
    files = {}
    for x in rows:
        files.setdefault(x["file"], []).append({"find": x.get("find") or "", "replace": x.get("replace") or ""})
    then = c.get("then") or "nothing"
    marker = marker_of(s)
    adopt = adoptable(s, marker)
    return {
        "key": s.key,
        "marker": marker,
        "adopt": list(dict.fromkeys(adopt)),
        "files": [{"file": f, "changes": ch} for f, ch in files.items()],
        "check": (c.get("check") or "").strip(),
        "then": then,
        "command": (c.get("command") or "").strip() if then != "nothing" else "",
    }


def check_config(s):
    """LogConfError for config changes a machine could not use."""
    import re

    c = s.config or {}
    for x in c.get("changes") or []:
        f, find = (x.get("file") or "").strip(), x.get("find") or ""
        if not f.startswith("/") or any(ch.isspace() for ch in f):
            raise LogConfError(f"{s.key}: a config change's file is a full path without spaces: {f or '-'}")
        if not find:
            raise LogConfError(f"{s.key}: a config change of {f} finds nothing")
        try:
            re.compile(find, re.M)
            apply_changes("", [{"find": find, "replace": x.get("replace") or ""}])
        except (re.error, IndexError) as exc:
            raise LogConfError(f"{s.key}: {f}: {find}: {exc}") from exc
    then = c.get("then") or "nothing"
    if then not in THEN:
        raise LogConfError(f"{s.key}: not a way to take the change: {then}")
    if (c.get("changes") or []) and then != "nothing" and not (c.get("command") or "").strip():
        raise LogConfError(f"{s.key}: give the {then} command")


def config_script(found):
    """The shell script making the found services' config changes on a machine (python3 there); None when none has
    any. Output lines: C <key> <file> same|changed|skipped ...|failed ..., T <key> ok|failed <then>: <command> (also
    when only a pending marker is left, commit-278); the
    script ends with 0 when it ran (the lines say what failed), 75 when another logging install runs."""
    specs = [c for c in (config_of(s) for s in found) if c]
    if not specs:
        return None
    import base64
    import json

    arg = base64.b64encode(json.dumps(specs).encode()).decode()
    code = CHANGE_PY + f"PREV_SUFFIX = {PREV_SUFFIX!r}\nPENDING = {PENDING!r}\n" + HOST_PY
    marker = "CLAUGINE_CONFIG_EOF"
    return (
        "set -e\n"
        f"exec 9>{LOCK}\n"
        f"flock -n 9 || {{ echo 'another logging install is running on this host: try again when it ends' >&2; exit {BUSY}; }}\n"
        f"python3 - {arg} <<'{marker}'\n{code}{marker}\n"
    )


def parse_config(out):
    """[{"service", "file", "state": same|changed|skipped|failed, "detail"}] and [{"service", "state", "detail"}]."""
    files, then = [], []
    for line in str(out or "").splitlines():
        kind, _s, rest = line.partition(" ")
        if kind == "C":
            key, path, rest = (rest.split(" ", 2) + ["", ""])[:3]
            state, _s, detail = rest.partition(" ")
            files.append({"service": key, "file": path, "state": state, "detail": detail})
        elif kind == "T":
            key, _s, rest = rest.partition(" ")
            state, _s, detail = rest.partition(" ")
            then.append({"service": key, "state": state, "detail": detail})
    return files, then


# --- the files of a machine -----------------------------------------------------------------------------------------


def rules_of(conf):
    """The logging settings of the portal (tenants.PortalSettings; Profiles → Logging, above the list, owner 09:06)."""
    return {"queue_mb": conf.log_queue_mb, "retry_seconds": conf.log_retry_seconds}


def files(found, col, rules, origins=None):
    """{path: content} for a machine with these logged services (owner 2026-11-24): rsyslog.conf, the forwarding
    (00-claugine.conf), each service's rsyslog file (40-<name>, common 90-common) and logrotate file, the system's
    rotation and claugine-logcap. ``origins``, when given, gets {path: the service's name, or "" for the files of the
    portal} as each is added (commit-384)."""
    common = next((s for s in found if s.name == "common"), None)
    days = int(common.days) if common is not None else 5
    out = {
        "/etc/rsyslog.conf": HEADER + RSYSLOG_CONF.format(dir="/etc/rsyslog.d"),
        FORWARD: common_conf(col, rules),
        "/etc/logrotate.conf": HEADER + LOGROTATE_CONF.format(days=days),
        LOGCAP: LOGCAP_SCRIPT,
    }
    out.update({f"/etc/logrotate.d/{k}": v for k, v in fixed_rotate(common).items()})
    caps = []  # commit-447
    for s in found:
        check_text(s)
        conf, rot = service_conf(s), service_rotate(s)
        for path, text in ((conf_name(s), conf), (rotate_name(s), rot)):
            if text:
                out[path] = text
                if origins is not None:
                    origins[path] = s.name
        budget = [] if s.is_vyos else _cap(s)
        if budget:  # commit-453: every budget with files (common: always, with the system's)
            caps.append(json.dumps([int(s.size_mb), int(s.days), budget]) + "\n")
    # commit-452: one JSON array per line, ASCII only (every other character escaped: no line separator inside a record)
    out[LOGCAP_CONF] = (
        HEADER + "# [Size MB, Days, [files]] (claugine-logcap --all, daily after logrotate)\n" + "".join(caps)
    )
    out[LOGCAP_DROPIN] = HEADER + LOGCAP_DROPIN_TEXT
    # commit-274: two rotation patterns that can match one same file are refused here, before anything is installed
    # (logrotate refuses such a file only once it exists); the install's staged logrotate -d still checks the rest
    overlaps(found)
    return out


def generated(found, rules):
    """[(path, service name or "" for the portal's files)] the logging writes on a machine with these logged services.
    The collector changes no path; LogConfError as files() raises it."""
    origins = {}
    paths = files(found, {"host": "collector", "port": 514, "proto": "tcp"}, rules, origins)
    return [(path, origins.get(path, "")) for path in paths]  # commit-384: as files() added them


SAMPLE_COLLECTOR = {"host": "<the site's collector for the core services>", "port": 15140, "proto": "tcp"}


def shown(s, rules):
    """[(path, text)] a logged service writes on a machine, for Profiles → Logging ("Generated", owner 09:00): common
    with the forwarding; a VyOS row its VyOS commands."""
    if s.is_vyos:
        return [("VyOS", vyos_lines(s, None))]
    out = [(FORWARD, common_conf(SAMPLE_COLLECTOR, rules))] if s.name == "common" else []
    conf, rot = service_conf(s), service_rotate(s)
    out += [(p, t) for p, t in ((conf_name(s), conf), (rotate_name(s), rot)) if t]
    return out


def rotation_paths(found):
    """[(owner, path)] of every logrotate pattern a machine with these logged services gets: the system's, then each
    service's own."""
    out = list(fixed_paths())
    for s in found:
        written, others = rotated(s)
        for p in written + others:
            if rotation_args(p) != [p]:  # commit-276/286: one plain path, as logrotate reads it
                raise LogConfError(f"{s.name}: the rotated file {p} is not one plain path")
            out.append((s.name, p))
    return out


def overlaps(found):
    """commit-274: LogConfError naming the first two rotation patterns that can match one same file."""
    paths = rotation_paths(found)
    for i, (o1, p1) in enumerate(paths):
        for o2, p2 in paths[i + 1 :]:
            if globs_meet(p1, p2):
                raise LogConfError(
                    f"{o1}: {p1} and {o2}: {p2} can rotate one same file (logrotate refuses a file matched twice)"
                )


# --- VyOS (owner 2026-11-24 09:06 Q4, checked 09:30 on a provider egw, VyOS 2026.03): remote syslog has no queue or
# retry; logrotate of /var/log/messages by size only (max-size MB of one file, rotate count) - Days does not apply ---

VYOS_KEEP = 9  # rotated files kept: Size = (9 + the current) x max-size


def vyos_lines(s, col):
    """The VyOS commands of the vyos logged service; ``col``: the site's VyOS collector ({host, port, proto}) or None
    for the page's example."""
    host = col["host"] if col else "<the site's VyOS collector>"
    port = col["port"] if col else 15142
    proto = (col.get("proto") if col else None) or "tcp"
    size = max(1, int(s.size_mb or 0) // (VYOS_KEEP + 1))
    lines = [
        f"set system syslog remote {host} facility all level info",
        f"set system syslog remote {host} port {port}",
        f"set system syslog remote {host} protocol {proto}",
    ]
    if proto == "tcp":
        lines.append(f"set system syslog remote {host} format octet-counted")
    lines += [
        f"set system syslog remote {host} vrf MGMT",
        f"set system logs logrotate messages max-size {min(size, 1024)}",
        f"set system logs logrotate messages rotate {VYOS_KEEP}",
    ]
    return "\n".join(lines) + "\n"


def install_script(content):
    """Writes the files to a staging directory, checks them there (rsyslogd -N1, logrotate -d), then moves the other
    files of /etc/rsyslog.d and /etc/logrotate.d to DIST (kept), installs ours and restarts rsyslog. Nothing is
    changed when a check fails; commit-258: the previous set is saved to PREV first and put back (rsyslog restarted
    with it) when anything after that fails. commit-263: one install at a time on a host (flock on LOCK; a second one
    exits with BUSY)."""
    marker = "CLAUGINE_LOGCONF_EOF"
    lines = [
        "set -e",
        "umask 022",
        # commit-263: Apply logging and a deployment's logging step share STAGE and PREV - the host's lock first, held
        # until the script ends; a second install is refused, never waits
        f"exec 9>{LOCK}",
        f"flock -n 9 || {{ echo 'another logging install is running on this host: try again when it ends' >&2; exit {BUSY}; }}",
        f"rm -rf {STAGE}",
        f"mkdir -p {STAGE}/rsyslog.d {STAGE}/logrotate.d",
    ]
    for path, text in sorted(content.items()):
        if marker in text:
            raise LogConfError(f"{path}: the text holds the end marker")
        rel = {
            TIMER: "timer.conf",
            LOGCAP: "logcap",
            LOGCAP_CONF: "logcap.conf",
            LOGCAP_DROPIN: "logcap-dropin.conf",
        }.get(path) or path.removeprefix("/etc/")
        lines.append(f"cat > {STAGE}/{rel} <<'{marker}'\n{text}{marker}")
    lines += [
        # the check reads the staged files: rsyslog.conf with the staged directory
        f"sed 's#^\\$IncludeConfig /etc/rsyslog.d/#$IncludeConfig {STAGE}/rsyslog.d/#' {STAGE}/rsyslog.conf > {STAGE}/check.conf",
        f"rsyslogd -N1 -f {STAGE}/check.conf >{STAGE}/rsyslog-check.txt 2>&1 || {{ tail -5 {STAGE}/rsyslog-check.txt >&2; exit 1; }}",
        f"sed 's#^include /etc/logrotate.d#include {STAGE}/logrotate.d#' {STAGE}/logrotate.conf > {STAGE}/check-logrotate.conf",
        f"logrotate -d {STAGE}/check-logrotate.conf >{STAGE}/logrotate-check.txt 2>&1 || {{ grep -i error {STAGE}/logrotate-check.txt | tail -5 >&2; exit 1; }}",
        # commit-458: the helper's path must be a file (or a symlink to one) or nothing - refused before any change;
        # commit-459: a symlink pointing nowhere is no helper - it is replaced below
        f"if [ -e {LOGCAP} ] && [ ! -f {LOGCAP} ]; then "
        f"echo '{LOGCAP} is not a file (a directory?): nothing changed - remove it, then apply again' >&2; exit 1; fi",
        # commit-258: the previous set saved; any failure from here on puts it back and restarts rsyslog with it
        f"rm -rf {PREV}; mkdir -p {PREV}",
        f"cp -a /etc/rsyslog.conf /etc/rsyslog.d /etc/logrotate.conf /etc/logrotate.d {PREV}/",
        f"[ ! -f {TIMER} ] || cp -a {TIMER} {PREV}/timer.conf",
        f"[ ! -f {LOGCAP_CONF} ] || cp -a {LOGCAP_CONF} {PREV}/logcap.conf",  # commit-447
        f"[ ! -f {LOGCAP_DROPIN} ] || cp -a {LOGCAP_DROPIN} {PREV}/logcap-dropin.conf",
        # commit-454: the helper goes back with its list; commit-455: saved as the file it is or points to (a relative
        # symlink would not resolve in PREV) - a symlink pointing nowhere is no working helper and is not saved
        # commit-456: owner and mode kept (-a); commit-457: only a regular file, or a symlink to one
        f"if [ -f {LOGCAP} ]; then cp -aL {LOGCAP} {PREV}/logcap; fi",
        "restore() {",
        "  rc=$?",
        '  [ "$installed" = 1 ] && return',
        # commit-258: each part of the way back reported as it really went
        "  if rm -rf /etc/rsyslog.d /etc/logrotate.d && "
        f"cp -a {PREV}/rsyslog.conf {PREV}/rsyslog.d {PREV}/logrotate.conf {PREV}/logrotate.d /etc/ && "
        f"rm -f {TIMER} && {{ [ ! -f {PREV}/timer.conf ] || {{ mkdir -p {TIMER.rsplit('/', 1)[0]} && "
        f"cp -a {PREV}/timer.conf {TIMER}; }}; }} && "
        # commit-447: the daily Days/Size run as it was
        f"rm -f {LOGCAP_CONF} {LOGCAP_DROPIN} && {{ [ ! -f {PREV}/logcap.conf ] || cp -a {PREV}/logcap.conf {LOGCAP_CONF}; }} && "
        f"{{ [ ! -f {PREV}/logcap-dropin.conf ] || {{ mkdir -p {LOGCAP_DROPIN.rsplit('/', 1)[0]} && "
        f"cp -a {PREV}/logcap-dropin.conf {LOGCAP_DROPIN}; }}; }} && "
        # commit-457: something else than a file at the helper's path (a directory) is left as it is
        f"{{ [ -d {LOGCAP} ] || rm -f {LOGCAP}; }} && {{ [ ! -f {PREV}/logcap ] || cp -a {PREV}/logcap {LOGCAP}; }}; then",
        # commit-283: the reload of the units checked and reported
        "    if systemctl daemon-reload; then :; else echo 'systemctl daemon-reload FAILED: the timer may run "
        "with the replaced schedule' >&2; fi",
        "    restored=1; echo 'the previous logging files were put back' >&2",
        "  else",
        "    restored=0",
        f"    echo 'putting the previous logging files back FAILED: they are in {PREV}' >&2",
        "  fi",
        "  if systemctl restart rsyslog && sleep 2 && systemctl is-active rsyslog >/dev/null; then",
        # commit-258: "with them" only when they really were put back
        "    if [ \"$restored\" = 1 ]; then echo 'rsyslog runs with them' >&2;",
        "    else echo 'rsyslog runs, but with files that were not put back: its configuration is not known' >&2; fi",
        "  else",
        "    echo 'rsyslog did NOT start again: the host has no working logging until it is fixed' >&2",
        "  fi",
        # commit-283: the timer of the files put back running, reported
        "  if systemctl restart logrotate.timer && systemctl is-active logrotate.timer >/dev/null; then",
        # commit-283: and on the schedule the put-back files give (hourly with the previous drop-in, else daily)
        f"    if [ -f {TIMER} ]; then want='{HOURLY}'; else want='{DAILY}'; fi",
        # commit-296: the lookup never ends the way back (set -e); nothing read is reported as such
        "    cal=$(systemctl show -P TimersCalendar logrotate.timer 2>/dev/null | tr -d ' ' "
        "| grep -o 'OnCalendar=[^;}]*' || true)",
        '    if [ -z "$cal" ]; then echo "logrotate.timer runs, but its schedule could not be read" >&2;',
        '    elif [ "$cal" = "OnCalendar=$want" ]; then echo "logrotate.timer runs at $want" >&2;',
        '    else echo "logrotate.timer runs at $cal, not $want: check its schedule" >&2; fi',
        "  else",
        "    echo 'logrotate.timer did NOT start again: logs are not rotated until it is fixed' >&2",
        "  fi",
        '  exit "$rc"',
        "}",
        "installed=0",
        "trap restore EXIT",
        # others' files kept aside (the reference machines have only these)
        f"mkdir -p {DIST}/rsyslog.d {DIST}/logrotate.d",
        "for d in rsyslog.d logrotate.d; do",
        "  for f in /etc/$d/*; do",
        '    [ -f "$f" ] || continue',
        '    head -1 "$f" | grep -q "^# written by claugine " && { rm -f "$f"; continue; }',
        f'    mv "$f" {DIST}/$d/',
        "  done",
        "done",
        f"cp {STAGE}/rsyslog.conf /etc/rsyslog.conf; cp {STAGE}/logrotate.conf /etc/logrotate.conf",
        f"cp {STAGE}/rsyslog.d/* /etc/rsyslog.d/; cp {STAGE}/logrotate.d/* /etc/logrotate.d/",
        # owner 2026-11-24 09:06: the Size of each logged service, run by its logrotate file (commit-454: put back with
        # the previous files on the way back)
        f"if [ -L {LOGCAP} ] && [ ! -e {LOGCAP} ]; then rm -f {LOGCAP}; fi",  # commit-459: a link pointing nowhere
        f"[ ! -f {STAGE}/logcap ] || install -T -m 0755 -o root -g root {STAGE}/logcap {LOGCAP}",
        # commit-447: Days and Size daily after logrotate, whether it rotated anything or not (daemon-reload below)
        f"mkdir -p {LOGCAP_CONF.rsplit('/', 1)[0]} {LOGCAP_DROPIN.rsplit('/', 1)[0]}",
        f"if [ -f {STAGE}/logcap.conf ]; then cp {STAGE}/logcap.conf {LOGCAP_CONF}; else rm -f {LOGCAP_CONF}; fi",
        f"if [ -f {STAGE}/logcap-dropin.conf ]; then cp {STAGE}/logcap-dropin.conf {LOGCAP_DROPIN}; "
        f"else rm -f {LOGCAP_DROPIN}; fi",
        "mkdir -p /var/spool/rsyslog",
        # a leftover of an earlier logging script, written by nothing (read 2026-11-21 on 12 of 15 machines)
        "rm -f /var/log/ufnvidiaw.log",
        # owner 10:49: logrotate hourly only where a found service rotates at a size; ours removed elsewhere
        f"if [ -f {STAGE}/timer.conf ]; then mkdir -p {TIMER.rsplit('/', 1)[0]}; cp {STAGE}/timer.conf {TIMER}; "
        f"else rm -f {TIMER}; fi",
        "systemctl daemon-reload",
        # commit-283: the timer running, hourly where the drop-in is there - else the previous files go back
        "systemctl restart logrotate.timer",
        "systemctl is-active logrotate.timer >/dev/null || { echo 'logrotate.timer is not active' >&2; exit 1; }",
        # commit-290: exactly the intended schedule - hourly with the drop-in, daily without it
        f"if [ -f {TIMER} ]; then want='{HOURLY}'; else want='{DAILY}'; fi",
        "cal=$(systemctl show -P TimersCalendar logrotate.timer 2>/dev/null | tr -d ' ' "
        "| grep -o 'OnCalendar=[^;}]*' || true)",  # commit-296
        '[ -n "$cal" ] || { echo "the schedule of logrotate.timer could not be read" >&2; exit 1; }',
        '[ "$cal" = "OnCalendar=$want" ] || { echo "logrotate.timer runs at $cal, not $want" >&2; exit 1; }',
        "systemctl restart rsyslog",
        "sleep 2",
        "systemctl is-active rsyslog >/dev/null || { journalctl -u rsyslog -n 5 --no-pager >&2; exit 1; }",
        "installed=1",
        f"rm -rf {STAGE} {PREV}",
        "echo ok",
    ]
    return "\n".join(lines) + "\n"


KEY = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
