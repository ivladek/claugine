"""Inventory tree: load, merge, resolve references, validate, index.

The structure follows the claugine inventory format (docs/data/inventory.md):
files are merged by path (``resources/dc2/hosts/h1.yaml`` -> ``resources.dc2.hosts.h1``),
a file ``dc2.yaml`` and a directory ``dc2/`` fill the same object, and string
values such as ``server_profiles.ca1`` are references into the tree.
"""
import io
import ipaddress
import posixpath
import re
import tarfile
import zipfile

import yaml

TOP_LEVEL = ("providers", "server_profiles", "resources", "platforms")
REF_RE = re.compile(r"^(providers|server_profiles|resources|platforms|secrets)\.[A-Za-z0-9_.\-]+$")
QUOTA_CODE_RE = re.compile(r"^[A-Za-z0-9]+$")
MAX_FILES = 5000

# ext-managed: the platform is managed by Claugine, its payload by a third-party system (earlier called "portal")
PLATFORM_KINDS = {"mgmt", "shared", "ext-managed"}
CLUSTER_KINDS_FOR = {"mgmt": {"mgmt"}, "shared": {"shared", "dedicated", "service"}, "ext-managed": {"ext-managed"}}
OLD_KINDS = {"portal": "ext-managed"}  # inventory files written with the old term still load


def kind_term(value):
    """A platform or cluster kind in today's terms."""
    v = str(value or "")
    return OLD_KINDS.get(v, v)
FE_MODES = {"single", "local", "primary", "secondary"}


class UploadError(Exception):
    pass


# --- reading an upload -----------------------------------------------------------

def _safe_path(name):
    name = name.replace("\\", "/")
    norm = posixpath.normpath(name)
    if norm.startswith("/") or norm.startswith("..") or "/../" in f"/{norm}/":
        raise UploadError(f"unsafe path in archive: {name}")
    return norm


def read_archive(data: bytes, filename: str) -> dict:
    """Return {path relative to the inventory root: text} from a zip or tar(.gz) archive."""
    entries = {}
    try:
        if zipfile.is_zipfile(io.BytesIO(data)):
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for info in zf.infolist():
                    if not info.is_dir():
                        entries[_safe_path(info.filename)] = zf.read(info)
        else:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
                for member in tf.getmembers():
                    if member.isfile():
                        entries[_safe_path(member.name)] = tf.extractfile(member).read()
    except (zipfile.BadZipFile, tarfile.TarError) as exc:
        raise UploadError(f"cannot read {filename}: {exc}") from exc

    yaml_paths = [p for p in entries if p.endswith((".yaml", ".yml")) and "/." not in f"/{p}" and "__MACOSX" not in p]
    if not yaml_paths:
        raise UploadError("the archive has no YAML files")
    if len(yaml_paths) > MAX_FILES:
        raise UploadError(f"too many files ({len(yaml_paths)})")

    # The inventory root is the shallowest directory that has a top-level part in it.
    roots = set()
    for p in yaml_paths:
        parts = p.split("/")
        for i, part in enumerate(parts[:-1]):
            if part in TOP_LEVEL:
                roots.add("/".join(parts[:i]))
                break
    if not roots:
        raise UploadError("no providers/, server_profiles/, resources/ or platforms/ directory found")
    root = min(roots, key=lambda r: r.count("/") if r else -1)
    prefix = f"{root}/" if root else ""

    files = {}
    for p in yaml_paths:
        if not p.startswith(prefix):
            continue
        rel = p[len(prefix):]
        if rel.split("/")[0] not in TOP_LEVEL:
            continue
        try:
            files[rel] = entries[p].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UploadError(f"{rel}: not UTF-8 text") from exc
    return dict(sorted(files.items()))


def write_archive(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, text in sorted(files.items()):
            zf.writestr(f"inventory/{path}", text)
    return buf.getvalue()


# --- merging -------------------------------------------------------------------

def _deep_merge(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = _deep_merge(out[k], v) if k in out else v
        return out
    return b


def build_tree(files: dict):
    """Parse and merge files. Returns (tree, errors)."""
    tree, errors = {}, []
    for path in sorted(files):
        try:
            data = yaml.safe_load(files[path])
        except yaml.YAMLError as exc:
            errors.append(f"{path}: YAML error: {exc}".replace("\n", " "))
            continue
        if data is None:
            data = {}
        keys = re.sub(r"\.ya?ml$", "", path).split("/")
        node = data
        for k in reversed(keys):
            node = {k: node}
        tree = _deep_merge(tree, node)
    return tree, errors


def resolve(tree, ref):
    node = tree
    for k in ref.split("."):
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    return node


def _iter_strings(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _iter_strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _iter_strings(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def _d(x):
    return x if isinstance(x, dict) else {}


def _ref_name(ref, prefix):
    if isinstance(ref, str) and ref.startswith(prefix + "."):
        return ref[len(prefix) + 1:]
    return None


# --- validation ------------------------------------------------------------------

SOFT_SECTIONS = (".datastores.", ".networks.", ".services.")


def _soft(path, value):
    """Datastores, virtual networks and services are not used by the portal yet: inconsistencies
    there are reported as warnings and do not block the import."""
    return any(sec in f".{path}." for sec in SOFT_SECTIONS) or ".services." in value


def validate(tree):
    """Checks from the claugine inventory rules. Returns (errors, warnings)."""
    errors, warnings = [], []
    secrets_refs = 0
    for path, value in _iter_strings(tree):
        if REF_RE.match(value):
            if value.startswith("secrets."):
                secrets_refs += 1
                continue
            if resolve(tree, value) is None:
                (warnings if _soft(path, value) else errors).append(f"{path}: reference {value} does not resolve")
    warnings.sort(key=lambda w: (w.startswith("platforms") is False, w))
    if secrets_refs:
        warnings.append(f"{secrets_refs} references into secrets are not checked (secrets are not part of the inventory)")

    profiles = _d(tree.get("server_profiles"))
    resources = _d(tree.get("resources"))
    platforms = _d(tree.get("platforms"))

    for site, sdata in resources.items():
        for host, h in _d(_d(sdata).get("hosts")).items():
            prof = _ref_name(_d(h).get("server_profile"), "server_profiles")
            if not prof:
                errors.append(f"resources.{site}.hosts.{host}: server_profile missing")
            elif prof not in profiles:
                errors.append(f"resources.{site}.hosts.{host}: server profile {prof} does not exist")

    quota_codes = {}
    for site, sp in platforms.items():
        site_res = _d(resources.get(site))
        if not site_res:
            errors.append(f"platforms.{site}: site has no resources/{site}")
        for pname, p in _d(sp).items():
            key = f"platforms.{site}.{pname}"
            meta = _d(_d(p).get("platform"))
            kind = kind_term(meta.get("kind"))
            state = meta.get("state", "deployed")
            if kind not in PLATFORM_KINDS:
                errors.append(f"{key}.platform.kind: must be one of {', '.join(sorted(PLATFORM_KINDS))}")
                continue
            clusters = _d(_d(p).get("clusters"))
            fe = _d(_d(p).get("fe"))
            mode = fe.get("mode")
            if mode and mode not in FE_MODES:
                errors.append(f"{key}.fe.mode: unknown mode {mode}")
            if mode == "secondary" and not fe.get("primary"):
                errors.append(f"{key}.fe: secondary needs primary")
            if mode != "secondary" and fe.get("primary"):
                errors.append(f"{key}.fe: only a secondary has primary")
            if fe.get("type") == "host":
                for node in fe.get("nodes") or []:
                    if node not in _d(site_res.get("hosts")):
                        errors.append(f"{key}.fe.nodes: {node} is not a host of site {site}")
            if mode == "primary":
                for sec in fe.get("secondaries") or []:
                    sec_fe = _d(resolve(tree, f"{sec}.fe")) if isinstance(sec, str) else {}
                    if sec_fe.get("primary") != key:
                        errors.append(f"{key}.fe.secondaries: {sec} does not point back to {key}")

            if kind == "mgmt":
                kinds = [kind_term(_d(c).get("kind")) for c in clusters.values()]
                if kinds != ["mgmt"]:
                    errors.append(f"{key}: a mgmt platform has exactly one cluster, of kind mgmt")
            if kind == "shared" and state == "deployed" and not any(kind_term(_d(c).get("kind")) == "shared" for c in clusters.values()):
                errors.append(f"{key}: a shared platform needs at least one shared cluster")
            for cname, c in clusters.items():
                c = _d(c)
                ckey = f"{key}.clusters.{cname}"
                ckind = kind_term(c.get("kind"))
                if ckind not in CLUSTER_KINDS_FOR[kind]:
                    errors.append(f"{ckey}.kind: {ckind} not allowed on a {kind} platform")
                if not str(cname).endswith(f"_{site}"):
                    errors.append(f"{ckey}: name must end with _{site}")
                qc = c.get("quota_code")
                if ckind in ("shared", "dedicated"):
                    if not qc:
                        errors.append(f"{ckey}: quota_code required for kind {ckind}")
                    elif not QUOTA_CODE_RE.match(str(qc)):
                        errors.append(f"{ckey}.quota_code: letters and digits only")
                    else:
                        other = quota_codes.get((site, str(qc).lower()))
                        if other:
                            errors.append(f"{ckey}.quota_code: {qc} already used on site {site} by {other}")
                        quota_codes[(site, str(qc).lower())] = ckey
                elif qc:
                    errors.append(f"{ckey}: quota_code not allowed for kind {ckind}")
                for h in c.get("hosts") or []:
                    if isinstance(h, str) and not h.startswith(f"resources.{site}.hosts."):
                        errors.append(f"{ckey}.hosts: {h} is not a host of site {site}")
            for section in ("datastores", "networks"):
                for oname, o in _d(_d(p).get(section)).items():
                    okey = f"{key}.{section}.{oname}"
                    if not str(oname).endswith(f"_{site}"):
                        warnings.append(f"{okey}: name must end with _{site}")
                    for cl in _d(o).get("clusters") or []:
                        if cl not in clusters:
                            warnings.append(f"{okey}.clusters: {cl} is not a cluster of {key}")
                    if section == "networks":
                        seg = resolve(tree, _d(o).get("network", "")) if _d(o).get("network") else None
                        for rng in _d(o).get("ranges") or []:
                            if rng not in _d(_d(seg).get("ranges")):
                                warnings.append(f"{okey}.ranges: {rng} is not a range of {_d(o).get('network')}")
    errors += _separation_errors(tree)
    for site, sdata in resources.items():
        for host, h in _d(_d(sdata).get("hosts")).items():
            st = _d(h).get("state")
            if st is not None and str(st) not in ITEM_STATES:
                errors.append(f"resources.{site}.hosts.{host}.state: one of {', '.join(ITEM_STATES)}")
    for site, sp in platforms.items():
        for pname, p in _d(sp).items():
            for cname, c in _d(_d(p).get("clusters")).items():
                st = _d(c).get("state")
                if st is not None and str(st) not in ITEM_STATES:
                    errors.append(f"platforms.{site}.{pname}.clusters.{cname}.state: one of {', '.join(ITEM_STATES)}")
    return errors, warnings


def _frontend_of(tree, site, pname):
    fe = _d(_d(_d(_d(tree.get("platforms")).get(site)).get(pname)).get("fe"))
    if fe.get("mode") == "secondary" and isinstance(fe.get("primary"), str):
        return fe["primary"].replace("platforms.", "")
    return f"{site}.{pname}"


def _separation_errors(tree):
    """Shared front ends are completely separate: no exchange (IX) network and no host may be used by two.
    A secondary zone belongs to the front end of its primary."""
    errors = []
    resources = _d(tree.get("resources"))
    ix_users, host_users = {}, {}
    for site, sp in _d(tree.get("platforms")).items():
        for pname, p in _d(sp).items():
            if _d(_d(p).get("platform")).get("kind") != "shared":
                continue
            fe = _frontend_of(tree, site, pname)
            for o in _d(_d(p).get("networks")).values():
                net = _ref_name(_d(o).get("network"), f"resources.{site}.networks")
                if net and _d(_d(_d(_d(resources.get(site)).get("networks")).get(net)).get("bgp")).get("subnets"):
                    ix_users.setdefault(f"resources.{site}.networks.{net}", set()).add(fe)
            for c in _d(_d(p).get("clusters")).values():
                for h in _d(c).get("hosts") or []:
                    if isinstance(h, str):
                        host_users.setdefault(h, set()).add(fe)
    for net, fes in sorted(ix_users.items()):
        if len(fes) > 1:
            errors.append(f"{net}: an exchange network can serve only one shared front end, used by {', '.join(sorted(fes))}")
    for h, fes in sorted(host_users.items()):
        if len(fes) > 1:
            errors.append(f"{h}: a host can belong to only one shared front end, used by {', '.join(sorted(fes))}")
    return errors


def stats(files, tree):
    platforms = _d(tree.get("platforms"))
    n_platforms = sum(len(_d(v)) for v in platforms.values())
    n_clusters = sum(len(_d(_d(p).get("clusters"))) for sp in platforms.values() for p in _d(sp).values())
    n_hosts = sum(len(_d(_d(s).get("hosts"))) for s in _d(tree.get("resources")).values())
    return {"files": len(files), "sites": len(_d(tree.get("resources"))), "platforms": n_platforms,
            "clusters": n_clusters, "hosts": n_hosts, "profiles": len(_d(tree.get("server_profiles")))}


def subnet_purpose(segment_name):
    name = segment_name.lower()
    if "corpnet" in name:
        return "corpnet"
    if name.endswith("-ix") or "-ix-" in name:
        return "inet"
    return "other"


def _int(x, default=0):
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


ITEM_STATES = ("planned", "active", "retired")


def _item_state(value):
    """State of a host or cluster in the inventory: planned (to be deployed), active, retired (to be removed)."""
    v = str(value or "active").lower()
    return {"deployed": "active"}.get(v, v if v in ITEM_STATES else "active")


def rebuild_index(tree):
    """Replace the portal's infrastructure records with the content of `tree` (tests, a fresh portal)."""
    from .loader import load
    load(tree, replace=True)


def check_and_load(files):
    """Full pipeline without saving: (tree, errors, warnings, stats)."""
    tree, errors = build_tree(files)
    v_errors, warnings = validate(tree)
    return tree, errors + v_errors, warnings, stats(files, tree)
