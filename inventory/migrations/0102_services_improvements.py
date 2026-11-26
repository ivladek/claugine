"""Owner 2026-11-21 10:04 (T33, analysis-current-config.md 2-4): what the current hosts log or keep that the services
did not cover. Only what is missing is added - a service the owner has changed keeps its changes. commit-268: the way
back changes nothing (what this added is ordinary service data the earlier code handles; nothing of the owner's is
ever removed). commit-269: the swtpm pattern only where libvirt rotates nothing under /var/log/swtpm/ yet (logrotate
refuses a file matched twice): not when any libvirt path can match one same file as the pattern (exact test;
commit-269/275: POSIX classes such as [[:alpha:]] read as classes, a pattern not read for certain taken as meeting;
commit-276: a line read as logrotate does - quotes and blanks; commit-277: wildcards never give a leading dot).
2. "OpenNebula node": the OneGate tproxy logs of a KVM host (/var/log/one_tproxy*.log, one per bridge), sent with
   log_type one and rotated (copy then truncate) - today neither.
3. libvirt: the TPM emulator's logs of the VMs (/var/log/swtpm/libvirt/qemu/*.log) rotated too.
4. OpenNebula front end: the scheduler's rank_sched.log and host_error.log sent (rotated already)."""

from django.db import migrations

NODE = {
    "key": "opennebula-node",
    "name": "OpenNebula node",
    "group": "platform",
    "log_type": "one",
    "present_when": [{"kind": "package", "value": "opennebula-node-kvm"}],
    "sources": [{"kind": "file", "match": "/var/log/one_tproxy*.log", "tag": "one-tproxy", "send": "all"}],
    "rotate": {"paths": ["/var/log/one_tproxy*.log"], "method": "copytruncate"},
    "units": [],
    "notes": "OneGate tproxy of a KVM host: one log per bridge with a tproxy (owner 2026-11-21 10:04).",
}
SWTPM = "/var/log/swtpm/libvirt/qemu/*.log"
ONE_SOURCES = [
    {"kind": "file", "match": "/var/log/one/rank_sched.log", "tag": "one-sched", "send": "all"},
    {"kind": "file", "match": "/var/log/one/host_error.log", "tag": "one-host-error", "send": "all"},
]


# --- a copy of inventory/logconf.py globs_meet (a migration keeps its own code): whether two logrotate globs can match one same file (commit-269, commit-274, commit-275) -------------------------

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


_overlap = globs_meet


def forward(apps, schema_editor):
    HostService = apps.get_model("inventory", "HostService")
    if not HostService.objects.filter(key=NODE["key"]).exists():
        HostService.objects.create(**NODE)
    lv = HostService.objects.filter(key="libvirt").first()
    if lv is not None:
        rotate = dict(lv.rotate or {})
        paths = list(rotate.get("paths") or [])
        # commit-269: any path that can match a file of it; commit-276: each line as logrotate reads it (an
        # unreadable line counts as meeting: nothing added)
        lines = [rotation_args(p) for p in paths]
        if not any(a is None or any(_overlap(x, SWTPM) for x in a) for a in lines):
            rotate["paths"] = paths + [SWTPM]
            lv.rotate = rotate
            lv.save(update_fields=["rotate"])
    one = HostService.objects.filter(key="opennebula").first()
    if one is not None:
        have = {s.get("match") for s in one.sources or []}
        add = [s for s in ONE_SOURCES if s["match"] not in have]
        if add:
            one.sources = list(one.sources or []) + add
            one.save(update_fields=["sources"])


def back(apps, schema_editor):
    """commit-268: nothing removed."""


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0101_host_logging_applied_at"),
    ]

    operations = [migrations.RunPython(forward, back)]
