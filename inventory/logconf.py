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

RSYSLOG_CONF = """module( load = "imuxsock" )
module( load = "imklog" permitnonkernelfacility = "on" )
module( load = "imfile" )
$RepeatedMsgReduction on
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


def service_rotate(s):
    r = s.rotate or {}
    paths = [p for p in r.get("paths") or [] if p]
    method = r.get("method") or "copytruncate"
    if not paths or method == "own":
        return None
    body = "\n".join(paths) + "\n{\n"
    if s.kept_days:
        body += f"  rotate {int(s.kept_days)}\n"
    if method == "copytruncate":
        body += "  copytruncate\n"
    elif method in ("reopen", "command"):
        cmd = REOPEN if method == "reopen" else (r.get("command") or "").strip()
        if not cmd:
            raise LogConfError(f"{s.key}: rotation runs a command, but none is given")
        body += f"  postrotate\n    {cmd}\n  endscript\n"
    return HEADER + f"# {s.name}\n" + body + "}\n"


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
        conf, rot = service_conf(s), service_rotate(s)
        if conf is None and rot is None:
            continue
        if n > 89:
            raise LogConfError("more than 80 services with files on one machine")
        if conf:
            out[f"/etc/rsyslog.d/{n:02d}-{s.key}.conf"] = conf
        if rot:
            out[f"/etc/logrotate.d/{n:02d}-{s.key}.conf"] = rot
        n += 1
    return out


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
    with it) when anything after that fails."""
    marker = "CLAUGINE_LOGCONF_EOF"
    lines = ["set -e", "umask 022", f"rm -rf {STAGE}", f"mkdir -p {STAGE}/rsyslog.d {STAGE}/logrotate.d"]
    for path, text in sorted(content.items()):
        if marker in text:
            raise LogConfError(f"{path}: the text holds the end marker")
        rel = path.removeprefix("/etc/")
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
        "restore() {",
        "  rc=$?",
        '  [ "$installed" = 1 ] && return',
        # commit-258: each part of the way back reported as it really went
        "  if rm -rf /etc/rsyslog.d /etc/logrotate.d && "
        f"cp -a {PREV}/rsyslog.conf {PREV}/rsyslog.d {PREV}/logrotate.conf {PREV}/logrotate.d /etc/; then",
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
        "systemctl restart rsyslog",
        "sleep 2",
        "systemctl is-active rsyslog >/dev/null || { journalctl -u rsyslog -n 5 --no-pager >&2; exit 1; }",
        "installed=1",
        f"rm -rf {STAGE} {PREV}",
        "echo ok",
    ]
    return "\n".join(lines) + "\n"


KEY = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
