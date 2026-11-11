"""A secrets directory of a data set: an import source only (Infrastructure → Secrets → Load).

The portal reads secrets only from its database (inventory.credentials, encrypted). A data set may bring its
secrets as a directory tree; loading it copies the values into the database once. The tree has a free
structure; like the inventory, directories and files are keys and ``a.yaml`` next to ``a/`` fills the same
object. A reference keeps its full path from ``secrets.``; when the tree is rooted one level lower
(``secrets/cloud/one.yaml`` for ``secrets.provider.cloud.one``) the provider level is skipped.

Values are never logged or shown here; the UI only shows what loading would change.
"""
import os
import stat

from django.conf import settings

from .tree import build_tree


class SecretsError(Exception):
    pass


def secrets_dir():
    return getattr(settings, "INVENTORY_SECRETS_DIR", "") or ""


def status():
    """What the UI may show: directory, whether it is usable, number of files, permission warning."""
    d = secrets_dir()
    out = {"dir": d, "ok": False, "files": 0, "warning": ""}
    if not d or not os.path.isdir(d):
        return out
    try:
        mode = os.stat(d).st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            out["warning"] = "readable by other users: chmod 700"
        out["files"] = sum(1 for _r, _d, fs in os.walk(d) for f in fs if f.endswith((".yaml", ".yml")))
        out["ok"] = out["files"] > 0
    except OSError:
        pass
    return out


def _tree():
    d = secrets_dir()
    if not d or not os.path.isdir(d):
        raise SecretsError(f"secrets directory {d or '(not configured)'} not found")
    files = {}
    for root, _dirs, names in os.walk(d):
        for name in names:
            if name.endswith((".yaml", ".yml")):
                full = os.path.join(root, name)
                with open(full, encoding="utf-8") as fh:
                    files[os.path.relpath(full, d)] = fh.read()
    tree, errors = build_tree(files)
    if errors:
        raise SecretsError(f"secrets: {len(errors)} file(s) are not valid YAML")
    return tree


def _walk(node, parts):
    for k in parts:
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    return node


def resolve(ref, tree=None):
    """The object a ``secrets.…`` reference points to, or raise SecretsError (the message never has values)."""
    if not isinstance(ref, str) or not ref.startswith("secrets."):
        raise SecretsError(f"not a secrets reference: {ref!r}")
    tree = _tree() if tree is None else tree
    parts = ref.split(".")[1:]
    for candidate in (parts, parts[1:]):
        node = _walk(tree, candidate) if candidate else None
        if node is not None:
            return node
    raise SecretsError(f"{ref} does not resolve in the secrets directory")


def has(ref, key=None):
    try:
        node = resolve(ref)
    except SecretsError:
        return False
    return bool(node.get(key)) if key else True
