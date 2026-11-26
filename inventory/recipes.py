"""Services as whole recipes and the service profiles (Profiles → Services; owner 2026-11-21 12:29-13:44, T34).

A service (HostService) says what it puts on a machine in ``install``: apt repositories with their keys, packages,
files (a whole text made from a template with the instance's values, or ``set`` - key = value lines ensured in a file
a package installed), units to enable, users and their keys, mounts, commands, and ``tools`` (packages a super admin
adds, the jump host's Tools). A profile (ServiceProfile) is the services in order plus its own lines; render_script
turns a profile and an instance's values into the machine's first-boot script (the redeploy runs it, T34 B4).

Since T34 S2 (owner 2026-11-22 16:59 / 17:08 / 20:00) the machine is built from the profile's templates (Profiles →
Templates, scope "service"): its installation (kind steps: a YAML list of steps, each a name, the install keys
above and ``configs`` - the paths of the profile's config files written at that step; kind script: a shell script)
and its config files (profile.configs: [{"path", "template", "owner", "mode", "check", "then"}], each a whole file
made from its template), written at the step naming it after the step's ``set`` lines; a config no step names is
written after the installation. A step's ``files`` only
``set`` lines in a file a package installed: a whole file is a config. The services stay for their logs
(inventory/logconf.py).

The values come from the instance (site, node, service, platform, secrets, storage, dns): ``{{ … }}`` is a Django
template over them. Secrets are never stored in a service: ``{{ secret.<name> }}`` takes the instance's secret,
masked (••••) in a preview. The portal is the single source of truth (owner 13:10): nothing is read from a machine.
"""

import base64
import json
import re
import shlex

import yaml
from django.template import Context, Engine, Library, TemplateSyntaxError
from django.utils.translation import gettext as _

register = Library()


@register.filter(name="sh")
def sh_quote(value):
    """{{ value|sh }}: the value as one shell word (commit-302: a secret with an apostrophe)."""
    return shlex.quote(str(value))


MASK = "••••"
INSTALL_KEYS = ("repos", "packages", "files", "enable", "users", "mounts", "commands", "tools")
FILE_KEYS = ("path", "owner", "mode", "text", "set", "check", "then")
THEN = ("reload", "restart", "nothing")
_ENGINE = Engine(autoescape=False, string_if_invalid="", builtins=["inventory.recipes"])
# commit-299: what could fail only when rendered, or reach outside the recipe
_FORBIDDEN = re.compile(r"{%-?\s*(include|extends|load|ssi|debug|templatetag|url|static)\b")
_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
_PATH = re.compile(r"^/[A-Za-z0-9_./@+-]+$")


def plain_path(p):
    """commit-381: an absolute path written plainly - no //, no . or .. part, no / at the end - so one file has one
    spelling and the checks that compare paths see it once."""
    p = str(p or "")
    return bool(_PATH.match(p)) and "//" not in p and not p.endswith("/") and not ({".", ".."} & set(p.split("/")))


_MODE = re.compile(r"^0?[0-7]{3,4}$")


class RecipeError(ValueError):
    pass


# --- the install of a service, as YAML in its edit form -----------------------------------------------------------


def dump(install):
    """The install as the YAML of the edit form ("" when empty)."""
    data = {k: install[k] for k in INSTALL_KEYS if install.get(k)}
    if not data:
        return ""
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120, default_flow_style=False)


def _text_keys(d, where):
    """commit-300: a mapping's keys are texts (YAML can make numbers of them)."""
    bad = [k for k in d if not isinstance(k, str)]
    if bad:
        raise RecipeError(_("%(w)s: %(k)s is not a name.") % {"w": where, "k": bad[0]})
    return d


def _list(data, key):
    """A section that must be a list (commit-300)."""
    v = data.get(key)
    if v in (None, ""):
        return []
    if not isinstance(v, list):
        raise RecipeError(_("%(k)s: a list.") % {"k": key})
    return v


def _strs(v, what):
    if v in (None, ""):
        return []
    if isinstance(v, str):
        v = v.split()
    if not isinstance(v, list) or not all(isinstance(x, (str, int)) and str(x).strip() for x in v):
        raise RecipeError(_("%(w)s: a list of names.") % {"w": what})
    return [str(x).strip() for x in v]


def parse(text):
    """The edit form's YAML → the install, checked; RecipeError says what is wrong."""
    text = (text or "").strip()
    if not text:
        return {}
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RecipeError(_("Install is not valid YAML: %(e)s") % {"e": str(exc).splitlines()[0]}) from exc
    return parse_data(data)


def parse_data(data):
    """An install as a mapping (parse, a step of parse_steps) → the install, checked."""
    if not isinstance(data, dict):
        raise RecipeError(_("Install: a mapping of repos, packages, files, enable, users, mounts, commands, tools."))
    _text_keys(data, "Install")
    unknown = sorted(set(data) - set(INSTALL_KEYS))
    if unknown:
        raise RecipeError(_("Install: unknown %(k)s.") % {"k": ", ".join(map(str, unknown))})
    out = {}
    for k in ("packages", "enable", "tools"):
        names = _strs(data.get(k), k)
        bad = [n for n in names if not _NAME.match(n) and not (k == "enable" and re.match(r"^[A-Za-z0-9@_.:-]+$", n))]
        if bad:
            raise RecipeError(_("%(k)s: %(n)s is not a name.") % {"k": k, "n": bad[0]})
        if names:
            out[k] = names
    repos = []
    for r in _list(data, "repos"):
        if isinstance(r, dict):
            _text_keys(r, "repos")
        if not isinstance(r, dict) or not _NAME.match(str(r.get("name") or "")) or not str(r.get("line") or "").strip():
            raise RecipeError(_("repos: each has a name and a line (deb …)."))
        key = str(r.get("key") or "").strip()
        if key and not key.startswith("https://"):
            raise RecipeError(_("repos: %(n)s's key is an https address.") % {"n": r["name"]})
        _template(str(r["line"]), r["name"])
        repos.append({"name": str(r["name"]), "line": str(r["line"]).strip(), "key": key})
    if repos:
        out["repos"] = repos
    files = []
    for f in _list(data, "files"):
        if not isinstance(f, dict) or not plain_path(f.get("path")):
            raise RecipeError(_("files: each has an absolute path, written plainly (no //, . or .. parts)."))
        _text_keys(f, "files")
        if set(f) - set(FILE_KEYS):
            raise RecipeError(
                _("files: %(p)s has unknown %(k)s.") % {"p": f["path"], "k": ", ".join(set(f) - set(FILE_KEYS))}
            )
        if ("text" in f) == ("set" in f):
            raise RecipeError(_("files: %(p)s has either text or set.") % {"p": f["path"]})
        row = {"path": f["path"], "owner": str(f.get("owner") or "root:root"), "mode": str(f.get("mode") or "0644")}
        if not re.match(r"^[a-z_][a-z0-9_-]*(:[a-z_][a-z0-9_-]*)?$", row["owner"]) or not _MODE.match(row["mode"]):
            raise RecipeError(_("files: %(p)s - owner user:group, mode like 0644.") % {"p": f["path"]})
        if "text" in f:
            if not isinstance(f["text"], (str, int, float)):
                raise RecipeError(_("files: %(p)s - text is a text.") % {"p": f["path"]})
            row["text"] = str(f["text"])
            _template(row["text"], f["path"])
        else:
            if not isinstance(f["set"], dict) or not f["set"]:
                raise RecipeError(_("files: %(p)s - set is key: value lines.") % {"p": f["path"]})
            _text_keys(f["set"], f["path"])
            if any(isinstance(v, (dict, list)) for v in f["set"].values()):
                raise RecipeError(_("files: %(p)s - set is key: value lines.") % {"p": f["path"]})
            row["set"] = {str(k): str(v) for k, v in f["set"].items()}
            for v in row["set"].values():
                _template(v, f["path"])
        if f.get("check"):
            row["check"] = str(f["check"])
        then = str(f.get("then") or "nothing")
        if then not in THEN and not then.startswith(("reload ", "restart ")):
            raise RecipeError(_("files: %(p)s - then is reload <unit>, restart <unit> or nothing.") % {"p": f["path"]})
        row["then"] = then
        files.append(row)
    if files:
        out["files"] = files
    users = []
    for u in _list(data, "users"):
        if isinstance(u, dict):
            _text_keys(u, "users")
        if not isinstance(u, dict) or not re.match(r"^[a-z_][a-z0-9_-]{0,31}$", str(u.get("name") or "")):
            raise RecipeError(_("users: each has a name."))
        uid = u.get("uid")
        if uid in (None, ""):
            uid = None
        elif not re.match(r"^[0-9]{1,5}$", str(uid)) or not 1 <= int(uid) <= 65534:
            raise RecipeError(_("users: %(n)s's uid is a number from 1 to 65534.") % {"n": u["name"]})
        else:
            uid = int(uid)
        row = {"name": u["name"], "uid": uid}
        row["groups"] = _strs(u.get("groups"), "groups")
        row["keys"] = _strs(u.get("keys"), "keys")  # secret refs or "sa"
        users.append(row)
    if users:
        out["users"] = users
    mounts = []
    for m in _list(data, "mounts"):
        if isinstance(m, dict):
            _text_keys(m, "mounts")
        if not isinstance(m, dict) or not m.get("what") or not plain_path(m.get("where")):
            raise RecipeError(_("mounts: each has what and where (an absolute path, written plainly)."))
        for x, default in (("type", "nfs4"), ("options", "defaults,_netdev")):  # commit-306: empty = the default
            if m.get(x) in (None, ""):
                m = {**m, x: default}
        if not all(isinstance(m.get(x, ""), (str, int)) for x in ("what", "type", "options")):
            raise RecipeError(_("mounts: what, type and options are texts."))
        _template(str(m["what"]), m["where"])
        mounts.append(
            {
                "what": str(m["what"]),
                "where": m["where"],
                "type": str(m.get("type") or "nfs4"),
                "options": str(m.get("options") or "defaults,_netdev"),
            }
        )
    if mounts:
        out["mounts"] = mounts
    cmds = data.get("commands") or []
    if isinstance(cmds, str):
        cmds = [cmds]
    if not isinstance(cmds, list) or any(isinstance(c, (dict, list)) for c in cmds):
        raise RecipeError(_("commands: a list of shell lines."))
    cmds = [str(c) for c in cmds if str(c).strip()]
    for c in cmds:
        _template(c, "commands")
    if cmds:
        out["commands"] = cmds
    return out


def summary(install):
    """packages · files · units, as the Services list shows them."""
    i = install or {}
    return (
        len(i.get("packages") or []) + len(i.get("tools") or []),
        len(i.get("files") or []),
        len(i.get("enable") or []),
    )


# --- the installation steps of a service profile (a template of the kind steps; T34 S2) ---------------------------

STEP_KEYS = ("name", "configs") + INSTALL_KEYS
_STEP_NAME = re.compile(r"^[a-z0-9][a-z0-9 _.-]{0,62}$")


def parse_steps(text):
    """A steps template's body → [{"name", "configs": [paths], <install keys>}], checked; RecipeError says what is
    wrong. A step writes no whole file: that is a config of the profile (its path in ``configs``)."""
    text = (text or "").strip()
    if not text:
        return []
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RecipeError(_("The steps are not valid YAML: %(e)s") % {"e": str(exc).splitlines()[0]}) from exc
    if not isinstance(data, list):
        raise RecipeError(_("The steps: a list, each step a name and what it installs."))
    out = []
    for i, step in enumerate(data, 1):
        if not isinstance(step, dict):
            raise RecipeError(_("Step %(n)s: a mapping.") % {"n": i})
        _text_keys(step, _("Step %(n)s") % {"n": i})
        name = str(step.get("name") or "").strip()
        if not _STEP_NAME.match(name):
            raise RecipeError(_("Step %(n)s: a name (small letters, digits, spaces, _ . -).") % {"n": i})
        unknown = sorted(set(step) - set(STEP_KEYS))
        if unknown:
            raise RecipeError(_("Step %(s)s: unknown %(k)s.") % {"s": name, "k": ", ".join(unknown)})
        configs = _list(step, "configs")
        if not all(isinstance(c, str) and plain_path(c) for c in configs):
            raise RecipeError(_("Step %(s)s: configs are the paths of the profile's config files.") % {"s": name})
        install = parse_data({k: v for k, v in step.items() if k in INSTALL_KEYS})
        whole = [f["path"] for f in install.get("files") or [] if "text" in f]
        if whole:
            raise RecipeError(
                _("Step %(s)s: %(p)s is a whole file - make it a config of the profile.") % {"s": name, "p": whole[0]}
            )
        out.append({"name": name, "configs": [str(c) for c in configs], **install})
    return out


class _Literal(str):
    pass


def _literal(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")


class _StepsDumper(yaml.SafeDumper):
    pass


_StepsDumper.add_representer(_Literal, _literal)


def dump_steps(steps):
    """Steps as a steps template's body (a text of several lines as a block)."""

    def lit(v):
        if isinstance(v, str) and "\n" in v:
            return _Literal(v)
        if isinstance(v, list):
            return [lit(x) for x in v]
        if isinstance(v, dict):
            return {k: lit(x) for k, x in v.items()}
        return v

    rows = []
    for s in steps:
        row = {"name": s["name"]}
        if s.get("configs"):
            row["configs"] = list(s["configs"])
        row.update({k: lit(s[k]) for k in INSTALL_KEYS if s.get(k)})
        rows.append(row)
    if not rows:
        return ""
    return yaml.dump(
        rows, Dumper=_StepsDumper, sort_keys=False, allow_unicode=True, width=4096, default_flow_style=False
    )


# --- rendering -----------------------------------------------------------------------------------------------------


def _compile(text, where):
    m = _FORBIDDEN.search(text)
    if m:
        raise RecipeError(_("%(w)s: {%% %(t)s %%} is not allowed in a recipe.") % {"w": where, "t": m.group(1)})
    try:
        return _ENGINE.from_string(text)
    except TemplateSyntaxError as exc:
        raise RecipeError(_("%(w)s: %(e)s") % {"w": where, "e": exc}) from exc


def _template(text, where):
    """Checked as the preview renders it (commit-299): compiled, then rendered with the example values."""
    t = _compile(text, where)
    try:
        t.render(Context(sample_values(), autoescape=False))
    except Exception as exc:  # noqa: BLE001 - whatever a template raises, the recipe is refused
        raise RecipeError(_("%(w)s: %(e)s") % {"w": where, "e": exc}) from exc
    return t


def render(text, values):
    try:
        return _compile(text, "template").render(Context(values, autoescape=False))
    except RecipeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RecipeError(str(exc)) from exc


def _heredoc(text):
    tag = "CLAUGINE_EOF"
    while tag in text:
        tag += "_"
    return tag


SET_PY = r"""import base64, grp, json, os, pwd, re, sys, tempfile
import stat
path, sep = os.path.realpath(sys.argv[1]), sys.argv[2]  # commit-305: through a link to its target
if os.path.lexists(path) and not stat.S_ISREG(os.stat(path).st_mode):
    sys.exit(f"{sys.argv[1]} is not a regular file")
pairs = json.loads(base64.b64decode(sys.argv[3]))
uid, mode = pwd.getpwnam(sys.argv[4]).pw_uid, int(sys.argv[6], 8)
# commit-304: no group named - the replaced file's group, or the creation group (-1: left as made) for a new one
gid = grp.getgrnam(sys.argv[5]).gr_gid if sys.argv[5] else (os.stat(path).st_gid if os.path.exists(path) else -1)
try:
    lines = open(path).read().splitlines()
except FileNotFoundError:
    lines = []
for k, v in pairs:
    pat = re.compile(r"^\s*#?\s*" + re.escape(k) + r"\s*" + re.escape(sep.strip() or " "))
    hit = [i for i, line in enumerate(lines) if pat.match(line)]
    new = f"{k}{sep}{v}"
    if hit:
        lines[hit[0]] = new
        for i in reversed(hit[1:]):
            del lines[i]
    else:
        lines.append(new)
fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".claugine.", dir=os.path.dirname(path))
try:
    os.fchown(fd, uid, gid)
    os.fchmod(fd, mode)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
except BaseException:
    os.unlink(tmp)
    raise
"""


def script_of(service, values, sep=" = "):
    """The lines one service adds to a machine's script (bash)."""
    i = service.install or {}
    k = service.key
    out = []
    q = shlex.quote
    if i.get("repos"):
        out.append(f'step "{k}: repositories"')
        out.append("install -d -m 0755 /etc/apt/keyrings")
        for r in i["repos"]:
            if r.get("key"):
                out.append(f"wget -qO- {q(r['key'])} | gpg --dearmor --yes -o /etc/apt/keyrings/{r['name']}.gpg")
            out.append(f"echo {q(render(r['line'], values))} > /etc/apt/sources.list.d/{r['name']}.list")
        out.append("apt-get update -q")
    pkgs = list(i.get("packages") or []) + list(i.get("tools") or [])
    if pkgs:
        out.append(f'step "{k}: packages"')
        out.append("DEBIAN_FRONTEND=noninteractive apt-get install -y -q " + " ".join(q(p) for p in pkgs))
    for u in i.get("users") or []:
        out.append(f'step "{k}: user {u["name"]}"')
        uid = f"-u {u['uid']} " if u.get("uid") else ""
        out.append(f"id {u['name']} >/dev/null 2>&1 || useradd -m -s /bin/bash {uid}{u['name']}")
        for g in u.get("groups") or []:
            out.append(f"usermod -aG {q(g)} {u['name']}")
        if u.get("keys"):
            home = f"$(getent passwd {u['name']} | cut -d: -f6)"
            keys = "\n".join(values.get("keys", {}).get(ref, MASK) for ref in u["keys"])
            tag = _heredoc(keys)
            out.append(f'install -d -m 0700 -o {u["name"]} "{home}/.ssh"')
            out.append(f"cat > \"{home}/.ssh/authorized_keys\" <<'{tag}'\n{keys}\n{tag}")
            out.append(f'chown {u["name"]}: "{home}/.ssh/authorized_keys"; chmod 0600 "{home}/.ssh/authorized_keys"')
    then = []
    for f in i.get("files") or []:
        out.append(f'step "{k}: {f["path"]}"')
        out.append(f"install -d $(dirname {q(f['path'])})")
        # commit-303: written to a private temporary file, its owner and mode set, then moved into place
        if "text" in f:
            body = render(f["text"], values).rstrip("\n")
            tag = _heredoc(body)
            # commit-305: through a link to its target; anything but a regular file refused
            out.append(
                # commit-307: a sentinel keeps a trailing newline of the resolved name; a failed resolution stops
                f"dst=$(readlink -f -- {q(f['path'])} && echo x) || exit 1; dst=${{dst%x}}; dst=${{dst%$'\\n'}}; "
                f'if [ -e "$dst" ] && [ ! -f "$dst" ]; then '
                f"echo {q(f['path'] + ' is not a regular file')} >&2; exit 1; fi"
            )
            out.append('tmp=$(umask 077; mktemp "$dst.claugine.XXXXXX")')
            out.append(f"cat > \"$tmp\" <<'{tag}'\n{body}\n{tag}")
            if ":" in f["owner"]:
                out.append(f'chown {f["owner"]} "$tmp"')
            else:  # commit-304: no group named - the replaced file's, or the creation group for a new one
                out.append(f'chown {f["owner"]} "$tmp"; if [ -e "$dst" ]; then chgrp --reference="$dst" "$tmp"; fi')
            out.append(f'chmod {f["mode"]} "$tmp"; mv -f "$tmp" "$dst"')
        else:
            pairs = [[a, render(b, values)] for a, b in f["set"].items()]
            arg = base64.b64encode(json.dumps(pairs).encode()).decode()
            own = f["owner"].split(":")
            out.append(
                f"python3 - {q(f['path'])} {q(sep)} {arg} {q(own[0])} {q(own[1] if len(own) > 1 else '')} "
                f"{f['mode']} <<'CLAUGINE_PY'\n{SET_PY}CLAUGINE_PY"
            )
        if f.get("check"):  # commit-394: {file} is the file just written, as in the services' config changes
            out.append(f["check"].replace("{file}", q(f["path"])))
        if f.get("then") and f["then"] != "nothing" and f["then"] not in then:
            then.append(f["then"])
    if i.get("commands"):
        out.append(f'step "{k}: commands"')
        out.extend(render(c, values) for c in i["commands"])
    for m in i.get("mounts") or []:
        what, where = render(m["what"], values), m["where"]
        out.append(f'step "{k}: mount {where}"')
        line = f"{what} {where} {m['type']} {m['options']} 0 0"
        out.append(
            f"install -d {q(where)}; grep -qsF {q(' ' + where + ' ')} /etc/fstab || echo {q(line)} >> /etc/fstab"
        )
        out.append(f"mountpoint -q {q(where)} || mount {q(where)}")
    if i.get("enable"):
        out.append(f'step "{k}: units"')
        out.append("systemctl daemon-reload")
        out.append("systemctl enable --now " + " ".join(q(u) for u in i["enable"]))
    for t in then:
        verb, _sp, unit = t.partition(" ")
        units = [unit] if unit else list(i.get("enable") or [])
        for u in units:
            out.append(f"systemctl {verb} {q(u)}")
    return out


class _Part:
    """A step as script_of takes it: its name (the step lines' prefix) and what it installs."""

    def __init__(self, key, install):
        self.key, self.install = key, install


def _configs(profile, templates):
    """The profile's config files, checked as a service's files are: [{"path", "owner", "mode", "text", "check",
    "then"}], the text the template's body (T34 S2)."""
    rows = []
    for c in profile.configs or []:
        c = c if isinstance(c, dict) else {}
        t = templates.get(str(c.get("template") or ""))
        if t is None or t.kind != "config":
            raise RecipeError(
                _("Profile %(p)s: %(f)s needs a template of the kind config.")
                % {"p": profile.key, "f": c.get("path") or "?"}
            )
        then = str(c.get("then") or "nothing")
        if then in ("reload", "restart"):  # commit-396: no step's enable list to take the unit from
            raise RecipeError(
                _("Profile %(p)s: %(f)s - then %(t)s needs its unit (%(t)s <unit>).")
                % {"p": profile.key, "f": c.get("path") or "?", "t": then}
            )
        row = {"path": c.get("path"), "text": t.body, "then": then}
        row.update({k: c[k] for k in ("owner", "mode", "check") if c.get(k)})
        rows.append(row)
    files = parse_data({"files": rows}).get("files") or [] if rows else []
    paths = [f["path"] for f in files]
    dup = next((x for x in paths if paths.count(x) > 1), None)
    if dup:
        raise RecipeError(_("Profile %(p)s: %(f)s is written twice.") % {"p": profile.key, "f": dup})
    return files


def render_script(profile, templates, values, title=""):
    """The machine's first-boot script from the profile's templates (T34 S2): its installation - each step in order,
    writing the configs it names -, then the configs no step names. ``templates``: {key: ConfigTemplate} with every
    key of profile.template_keys() (one missing there is an error)."""
    missing = [k for k in profile.template_keys() if k not in templates]
    if missing:
        raise RecipeError(
            _("Profile %(p)s: no template %(k)s (Profiles → Templates).") % {"p": profile.key, "k": ", ".join(missing)}
        )
    configs = {f["path"]: f for f in _configs(profile, templates)}
    lines = [
        "#!/bin/bash",
        f"# claugine first boot: {title or profile.key} (profile {profile.key})",
        "set -euo pipefail",
        # commit-312: the step's name (service keys and paths, never values) also to its own file when the first boot
        # names one, so the portal reads the step from there and never from the recipe's output
        'step() { echo "== $*"; if [ -n "${CLAUGINE_STEP_FILE:-}" ]; then printf \'%s\\n\' "$*" > "$CLAUGINE_STEP_FILE"; fi; }',
    ]
    written = set()
    inst = templates.get(profile.install_template) if profile.install_template else None
    if inst is not None and inst.kind == "script":
        lines.append('step "installation"')
        lines.append(render(inst.body, values).rstrip("\n"))
    elif inst is not None and inst.kind == "steps":
        steps = parse_steps(inst.body)
        for st in steps:  # commit-381: a config is the whole file - its template is where it changes
            hit = next((f["path"] for f in st.get("files") or [] if f["path"] in configs), None)
            if hit:
                raise RecipeError(
                    _("Step %(s)s: %(f)s is a config of the profile - change its template, not set lines.")
                    % {"s": st["name"], "f": hit}
                )
        for st in steps:
            files = []
            for path in st["configs"]:
                if path not in configs:
                    raise RecipeError(
                        _("Step %(s)s: %(f)s is not a config of the profile.") % {"s": st["name"], "f": path}
                    )
                if path in written:
                    raise RecipeError(_("Step %(s)s: %(f)s is written twice.") % {"s": st["name"], "f": path})
                written.add(path)
                files.append(configs[path])
            install = {k: st[k] for k in INSTALL_KEYS if st.get(k)}
            install["files"] = list(install.get("files") or []) + files  # after its set lines, as the files were
            lines.extend(script_of(_Part(st["name"], install), values))
    elif inst is not None:
        raise RecipeError(
            _("Profile %(p)s: %(t)s is not an installation template.") % {"p": profile.key, "t": inst.role}
        )
    rest = [f for p, f in configs.items() if p not in written]
    if rest:
        lines.extend(script_of(_Part("configs", {"files": rest}), values))
    lines.append('step "done"')
    return "\n".join(lines) + "\n"


_REF = re.compile(r"\b(site|node|service|platform|os|storage|dns|secret)\.([A-Za-z_][A-Za-z0-9_]*)")


def references(profile, templates):
    """What a profile's script takes from an instance (T34 B4, S2): ({namespace: {name}} of the values its templates
    use - site, node, service, platform, os, storage, dns, secret -, {key refs of its steps' users}). Read from the
    templates, so a redeploy is offered only when every value is there."""
    used, keys = {}, set()
    texts = []
    for k in profile.template_keys():
        t = templates.get(k)
        if t is None:
            continue
        texts.append(t.body or "")
        if t.kind == "steps":
            try:
                steps = parse_steps(t.body)
            except RecipeError:  # the script itself says what is wrong
                steps = []
            for st in steps:
                for u in st.get("users") or []:
                    keys.update(u.get("keys") or [])
    for text in texts:
        for ns, name in _REF.findall(str(text)):
            used.setdefault(ns, set()).add(name)
    return used, keys


def config_file(profile, templates, path):
    """(config, template body) of the profile's config file ``path``, or None (Infrastructure → DNS: the ns
    profile's bind files)."""
    for c in profile.configs or []:
        if isinstance(c, dict) and c.get("path") == path:
            t = templates.get(str(c.get("template") or ""))
            return (c, t.body) if t is not None else None
    return None


def check_lines(profile):
    """The profile's checks as shell lines: each exits non-zero when it fails."""
    out = []
    for c in profile.checks or []:
        v = str(c.get("value") or "")
        if c.get("kind") == "unit":
            out.append(f"systemctl is-active --quiet {shlex.quote(v)}")
        elif c.get("kind") == "port":
            out.append(f"ss -Hltnu | grep -q ':{int(v)} '")
        elif c.get("kind") == "command":
            out.append(v)
    return out


def sample_values(profile=None):
    """Example values for a preview without an instance (Profiles → Services → Preview): the at1 ns VM's shape."""
    return {
        "site": {"code": "at1", "timezone": "Asia/Almaty", "domain": "portal.internal", "internal": ["10.61.0.0/16"]},
        "node": {
            "name": "sat1ns2",
            "hostname": "sat1ns2",
            "ips": ["10.61.101.12"],
            "index": 2,
            "ssh_port": 22,
            "rdp_port": 55275,
        },
        "service": {"name": getattr(profile, "stype", "ns"), "stype": getattr(profile, "stype", "ns")},
        "platform": {"key": "at1.mgmt"},
        "os": {"name": "Ubuntu", "version": "26.04"},
        "secret": _Masked(),
        "keys": _MaskedKeys(),
        "storage": {
            "nfs": "10.61.111.240:/log_one",
            "portals": ["10.61.112.240", "10.61.112.241"],
            "iqn": "iqn.1992-08.com.netapp:example",
            "vg": "vg_sat1onebkp1",
            "lv": "lv_sat1onebkp1",
        },
        "dns": {
            "role": "secondary",
            "primary": "10.201.101.11",
            "forwarders": ["8.8.8.8", "8.8.4.4"],
            "forward_zones": [],
            "sites": [
                {
                    "code": "at1",
                    "internal": ["10.61.0.0/16"],
                    "public": ["85.116.182.0/24"],
                    "ns": ["10.61.101.11", "10.61.101.12"],
                },
                {
                    "code": "zk1",
                    "internal": ["10.201.0.0/16"],
                    "public": ["91.212.93.0/24"],
                    "ns": ["10.201.101.11", "10.201.101.12"],
                },
            ],
            "internal_extra": [],
            "notify_extra": [],
            "zones": [{"name": "portal.internal"}, {"name": "101.61.10.in-addr.arpa"}],
        },
    }


class _Masked(dict):
    def __missing__(self, key):
        return MASK

    def __contains__(self, key):
        return True


class _MaskedKeys(dict):
    def get(self, key, default=None):
        return f"<{key}: {MASK}>"
