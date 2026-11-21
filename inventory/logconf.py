"""The machines' logging from Infrastructure → Services (owner 2026-11-21, T33): which services a machine runs and the
rsyslog and logrotate files they make.

Owner's rules (worklog/decisions.md, 2026-11-20 22:22 .. 2026-11-21 07:46):
- nobody assigns services to machines: a deployment looks at what is installed (detect_script) and takes every service
  whose present_when matches (07:17);
- every service sends its logs to the site's own log collector (Sites → the site → Log collectors, "Core services"),
  with the service's log_type (07:03, 07:08);
- the queue when the collector is down is in MB on disk, about 30 minutes: 100 MB (07:08);
- logs are kept locally the default number of days (5), a service may keep its own (07:03);
- rsyslog's --mark-- lines are dropped everywhere (07:32); the files' order is the portal's (07:32).

The scheme is the one the platform's machines have (read 2026-11-21; commit-184: no machine names here): 00-common.conf with the
templates and the sending rulesets, a file per service, then the common sources (ufw, auth, kern, apt, everything else
from the syslog level up). Every file written starts with HEADER; files of other origin in /etc/rsyslog.d and
/etc/logrotate.d are moved to /etc/claugine/dist/ (kept, not deleted), as the reference machines have only these.
"""

import re
import shlex

HEADER = "# written by claugine (Infrastructure → Services); a change here is overwritten by the next deployment\n"
DIST = "/etc/claugine/dist"
STAGE = "/etc/claugine/logging.new"
PREV = "/etc/claugine/logging.prev"  # commit-258
LOCK = "/run/claugine-logging.lock"  # commit-263: one logging install at a time on a host
BUSY = 75  # commit-263: the exit code of an install refused because another one runs
PRESENT_KINDS = ("package", "unit", "file", "module", "pci")
SOURCE_KINDS = ("file", "program", "contains")
SEND = ("all", "warning", "matching", "not_matching")
ROTATE = ("copytruncate", "reopen", "command", "own")
REOPEN = "/usr/lib/rsyslog/rsyslog-rotate"
# commit-257: an action() ignores the legacy $FileCreateMode, so every file action names its mode
FILE_OPTS = 'fileCreateMode = "0640" fileOwner = "syslog" fileGroup = "adm"'


class LogConfError(Exception):
    pass


def _q(v):
    """A string inside rsyslog's double quotes."""
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _qre(v):
    """A regular expression inside rsyslog's double quotes; \\n in it is a line break (entries over several lines)."""
    return _q(v).replace("\\\\n", "\\n")


def _present_test(c):
    v = str(c.get("value") or "").strip()
    q = shlex.quote(v)
    kind = c.get("kind")
    if kind == "package":
        return f"dpkg-query -W -f='${{Status}}' {q} 2>/dev/null | grep -q 'install ok installed'"
    if kind == "unit":
        return f"systemctl cat {q} >/dev/null 2>&1"
    if kind == "file":
        return f"[ -e {q} ]"
    if kind == "module":
        return f"grep -q '^'{q}' ' /proc/modules"
    if kind == "pci":  # a vendor id: 10de (NVIDIA)
        vid = "0x" + v.lower().removeprefix("0x")
        return f"cat /sys/bus/pci/devices/*/vendor 2>/dev/null | grep -qix {shlex.quote(vid)}"
    raise LogConfError(f"not a kind of check: {kind}")


def detect_script(services):
    """A shell script printing the key of every service found on the machine, one per line."""
    lines = []
    for s in services:
        tests = [_present_test(c) for c in s.present_when or []]
        if not tests:
            lines.append(f"echo {s.key}")
        else:
            lines.append(f"if {' || '.join('{ ' + t + '; }' for t in tests)}; then echo {s.key}; fi")
    return "\n".join(lines) + "\n"


def parse_detected(out, services):
    known = {s.key for s in services}
    return [k for k in (x.strip() for x in str(out or "").splitlines()) if k in known]


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
    return (
        HEADER
        # owner 2026-11-21 07:32 "drop mark"; commit-260: first, before anything is written or sent (00-common is the
        # first file of the default ruleset; every file input's ruleset starts with it too)
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


def _cond(src):
    send = src.get("send") or "all"
    f = src.get("filter") or ""
    if send == "warning":
        return "$syslogseverity <= 4"
    if send == "matching":
        return f"re_match($msg, {_qre(f)})"
    if send == "not_matching":
        return f"not re_match($msg, {_qre(f)})"
    return ""


def _send(src, log_type, indent="  "):
    vm = bool(src.get("vm_id"))
    body = f"{indent}set $.log_type = {_q(log_type)};\n"
    if vm:
        body += (
            f"{indent}set $.vmid = re_extract($!metadata!filename, '.*/([0-9]+)\\\\.log$', 0, 1, \"\");\n"
            f"{indent}call send_logs_onevm\n"
        )
    else:
        body += f"{indent}call send_logs\n"
    c = _cond(src)
    if c:
        body = f"{indent}if ( {c} ) then {{\n" + "".join("  " + ln + "\n" for ln in body.splitlines()) + f"{indent}}}\n"
    return body


def service_conf(s):
    """The rsyslog file of a service; None when it has no sources (its messages go with the common ones)."""
    sources = list(s.sources or [])
    if not sources:
        return None
    lt = s.log_type or s.key
    out = [HEADER, f"# {s.name} - log_type {lt}\n"]
    for i, src in enumerate(sources, 1):
        kind, match = src.get("kind"), src.get("match") or ""
        tag = src.get("tag") or s.key
        rs = f"{s.key.replace('-', '_')}_{i}"
        if kind == "file":
            extra = ""
            if src.get("start"):
                extra += f'  startmsg.regex = {_qre(src["start"])}\n  readTimeout = "60"\n'
            else:
                extra += '  readMode = "0"\n'
            if src.get("vm_id") or "*" in match:
                extra += '  addMetadata = "on"\n'
            out.append(
                f'\ninput (\n  type = "imfile"\n  file = {_q(match)}\n  tag = {_q(tag)}\n'
                f'  severity = "info"\n  facility = "daemon"\n{extra}'
                f'  freshStartTail = "on"\n  reopenOnTruncate = "on"\n  Ruleset = "{rs}"\n)\n'
                f'ruleset ( name = "{rs}" ) {{\n  {MARK}{_send(src, lt)}}}\n'
            )
        elif kind == "program":
            out.append(
                f"\nif ( $programname == {_q(match)} ) then {{\n"
                f'  action ( type = "omfile" file = "/var/log/syslog" {FILE_OPTS} )\n'
                f"{_send(src, lt)}  stop\n}}\n"
            )
        elif kind == "contains":
            out.append(
                f"\nif ( $msg contains_i {_q(match)} ) then {{\n"
                f'  action ( type = "omfile" file = "/var/log/{s.key}.log" {FILE_OPTS} )\n'
                f"{_send(src, lt)}  stop\n}}\n"
            )
        else:
            raise LogConfError(f"{s.key}: not a kind of source: {kind}")
    return "".join(out)


def _rule_file(cond, path, log_type):
    return (
        HEADER
        + f"if ( {cond} ) then {{\n"
        + (f'  action ( type = "omfile" file = "{path}" {FILE_OPTS} )\n' if path else "")
        + f"  set $.log_type = {_q(log_type)};\n  call send_logs\n  stop\n}}\n"
    )


def common_sources(rules):
    """The sources every Linux machine has (Logging rules → Common sources)."""
    return {
        "91-ufw.conf": _rule_file('$msg contains_i "[UFW "', "/var/log/ufw.log", "ufw"),
        "93-auth.conf": _rule_file(
            '$syslogfacility-text == "auth" or $syslogfacility-text == "authpriv"', "/var/log/auth.log", "auth"
        ),
        "94-kern.conf": _rule_file('$syslogfacility-text == "kern"', "/var/log/kern.log", "kern"),
        "95-apt.conf": HEADER
        + 'input ( type = "imfile" file = "/var/log/apt/history.log" tag = "apt" severity = "info" facility = "local0"\n'
        '  readMode = "1" freshStartTail = "on" Ruleset = "apt_history" )\n'
        'ruleset ( name = "apt_history" ) {\n  ' + MARK + '  set $.log_type = "apt";\n  call send_logs\n}\n',
        "99-syslog.conf": HEADER
        + f'action ( type = "omfile" file = "/var/log/syslog" {FILE_OPTS} )\n'
        + 'if ( $inputname == "imuxsock" ) then {\n'
        + f"  if ( $syslogseverity <= {int(rules['syslog_level'])} ) then {{\n"
        + '    set $.log_type = "syslog";\n    call send_logs\n  }\n  stop\n}\n',
    }


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

COMMON_ROTATE = {
    "90-rsyslog.conf": "/var/log/syslog\n/var/log/mail.log\n/var/log/kern.log\n/var/log/auth.log\n/var/log/user.log\n"
    "/var/log/cron.log\n/var/log/ufw.log\n{\n  sharedscripts\n  postrotate\n    " + REOPEN + "\n  endscript\n}\n",
    "90-packages.conf": "/var/log/dpkg.log\n/var/log/alternatives.log\n/var/log/ubuntu-advantage*.log\n{\n"
    "  create 0644 root root\n}\n\n/var/log/apt/*.log {}\n/var/log/cloud-init*.log {}\n/var/log/unattended-upgrades/*.log {}\n",
    "90-system.conf": "/var/log/dmesg {}\n/var/log/apport.log {}\n/var/log/btmp\n{\n  create 0660 root utmp\n}\n"
    "/var/log/wtmp\n{\n  create 0664 root utmp\n}\n",
}


def max_mb(s):
    """The size a service's files are rotated at too (owner 2026-11-21 10:49, BIND B1); 0: daily only."""
    r = s.rotate or {}
    try:
        n = int(r.get("max_mb") or 0)
    except (TypeError, ValueError) as exc:
        raise LogConfError(f"{s.key}: the rotation size is not a number of MB") from exc
    if n < 0:
        raise LogConfError(f"{s.key}: the rotation size is below 0")
    return n if (r.get("method") or "copytruncate") != "own" and r.get("paths") else 0


def service_rotate(s, days=5):
    """The logrotate file of a service; None when it rotates nothing. With a size (max_mb) the files are rotated daily
    and at that size - logrotate runs hourly on the machine then (TIMER) - and kept by age: each name carries the hour
    (dateext), at most 24 a day, removed after the kept days (maxage)."""
    r = s.rotate or {}
    paths = [p for p in r.get("paths") or [] if p]
    method = r.get("method") or "copytruncate"
    if not paths or method == "own":
        return None
    body = "\n".join(paths) + "\n{\n"
    size = max_mb(s)
    kept = int(s.kept_days or days)
    if size:
        body += (
            f"  maxsize {size}M\n  dateext\n  dateformat -%Y%m%d%H\n  maxage {kept}\n  rotate {24 * kept}\n"
            "  nodelaycompress\n"
        )
    elif s.kept_days:
        body += f"  rotate {int(s.kept_days)}\n"
    if method == "copytruncate":
        body += "  copytruncate\n"
    elif method in ("reopen", "command"):
        cmd = REOPEN if method == "reopen" else (r.get("command") or "").strip()
        if not cmd:
            raise LogConfError(f"{s.key}: rotation runs a command, but none is given")
        body += f"  postrotate\n    {cmd}\n  endscript\n"
    return HEADER + f"# {s.name}\n" + body + "}\n"


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


# --- logrotate hourly where a service rotates at a size (owner 2026-11-21 10:49) ------------------------------------

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


def needs_hourly(found):
    return any(max_mb(s) for s in found)


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
    marker = os.path.join(PENDING, svc["key"])
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


def config_of(s):
    """The service's config changes grouped by file: {"key", "files": [{"file", "changes"}], "check", "then",
    "command"}; None when it has none."""
    c = s.config or {}
    rows = [x for x in c.get("changes") or [] if x.get("file")]
    if not rows:
        return None
    files = {}
    for x in rows:
        files.setdefault(x["file"], []).append({"find": x.get("find") or "", "replace": x.get("replace") or ""})
    then = c.get("then") or "nothing"
    return {
        "key": s.key,
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


def files(found, col, rules):
    """{path: content} for the machine whose found services (in the list's order) are given; the services' files are
    numbered by the portal from 10 on (owner 07:32 "you make any order - it does not matter")."""
    out = {
        "/etc/rsyslog.conf": HEADER + RSYSLOG_CONF.format(dir="/etc/rsyslog.d"),
        "/etc/rsyslog.d/00-common.conf": common_conf(col, rules),
        "/etc/logrotate.conf": HEADER + LOGROTATE_CONF.format(days=int(rules["kept_days"])),
    }
    out.update({f"/etc/rsyslog.d/{k}": v for k, v in common_sources(rules).items()})
    out.update({f"/etc/logrotate.d/{k}": HEADER + v for k, v in COMMON_ROTATE.items()})
    n = 10
    for s in found:
        conf, rot = service_conf(s), service_rotate(s, int(rules["kept_days"]))
        if conf is None and rot is None:
            continue
        if n > 89:
            raise LogConfError("more than 80 services with files on one machine")
        if conf:
            out[f"/etc/rsyslog.d/{n:02d}-{s.key}.conf"] = conf
        if rot:
            out[f"/etc/logrotate.d/{n:02d}-{s.key}.conf"] = rot
        n += 1
    # commit-274: two rotation patterns that can match one same file are refused here, before anything is installed
    # (logrotate refuses such a file only once it exists); the install's staged logrotate -d still checks the rest
    overlaps(found)
    if needs_hourly(found):  # owner 10:49: logrotate hourly where a found service rotates at a size
        out[TIMER] = HEADER + TIMER_CONF
    return out


def rotation_paths(found):
    """[(owner, path)] of every logrotate stanza a machine with these services gets: the common ones, then each
    service's own (a service that rotates its own logs has none)."""
    out = []
    for name, text in COMMON_ROTATE.items():
        for line in text.splitlines():
            if line.startswith("/"):
                out.append((name, line.removesuffix(" {}").strip()))
    for s in found:
        r = s.rotate or {}
        if (r.get("method") or "copytruncate") != "own":
            for line in r.get("paths") or []:
                args = rotation_args(line)
                if args is None:  # commit-276: refused rather than guessed
                    raise LogConfError(f"{s.key}: the rotation line {line} cannot be read (an unclosed quote)")
                out += [(s.key, a) for a in args]
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


def rules_of(conf):
    """The logging rules of the portal's settings (tenants.PortalSettings)."""
    return {
        "kept_days": conf.log_kept_days,
        "queue_mb": conf.log_queue_mb,
        "retry_seconds": conf.log_retry_seconds,
        "syslog_level": conf.log_syslog_level,
    }


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
        rel = "timer.conf" if path == TIMER else path.removeprefix("/etc/")
        lines.append(f"cat > {STAGE}/{rel} <<'{marker}'\n{text}{marker}")
    lines += [
        # the check reads the staged files: rsyslog.conf with the staged directory
        f"sed 's#^\\$IncludeConfig /etc/rsyslog.d/#$IncludeConfig {STAGE}/rsyslog.d/#' {STAGE}/rsyslog.conf > {STAGE}/check.conf",
        f"rsyslogd -N1 -f {STAGE}/check.conf >{STAGE}/rsyslog-check.txt 2>&1 || {{ tail -5 {STAGE}/rsyslog-check.txt >&2; exit 1; }}",
        f"sed 's#^include /etc/logrotate.d#include {STAGE}/logrotate.d#' {STAGE}/logrotate.conf > {STAGE}/check-logrotate.conf",
        f"logrotate -d {STAGE}/check-logrotate.conf >{STAGE}/logrotate-check.txt 2>&1 || {{ grep -i error {STAGE}/logrotate-check.txt | tail -5 >&2; exit 1; }}",
        # commit-258: the previous set saved; any failure from here on puts it back and restarts rsyslog with it
        f"rm -rf {PREV}; mkdir -p {PREV}",
        f"cp -a /etc/rsyslog.conf /etc/rsyslog.d /etc/logrotate.conf /etc/logrotate.d {PREV}/",
        f"[ ! -f {TIMER} ] || cp -a {TIMER} {PREV}/timer.conf",
        "restore() {",
        "  rc=$?",
        '  [ "$installed" = 1 ] && return',
        # commit-258: each part of the way back reported as it really went
        "  if rm -rf /etc/rsyslog.d /etc/logrotate.d && "
        f"cp -a {PREV}/rsyslog.conf {PREV}/rsyslog.d {PREV}/logrotate.conf {PREV}/logrotate.d /etc/ && "
        f"rm -f {TIMER} && {{ [ ! -f {PREV}/timer.conf ] || {{ mkdir -p {TIMER.rsplit('/', 1)[0]} && "
        f"cp -a {PREV}/timer.conf {TIMER}; }}; }}; then",
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
