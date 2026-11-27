"""Test helpers: secrets written as a directory of YAML files (as the first data sets brought them) are loaded into
the database, where the portal reads them. The portal itself has no import of a secrets directory."""

import os
from contextlib import contextmanager

from django.test import override_settings

from .credentials import SLOT_OF_GROUP, _public_key
from .models import Secret


def _tree(path):
    from .testing_tree import build_tree

    files = {}
    for root, _dirs, names in os.walk(path):
        for name in names:
            if name.endswith((".yaml", ".yml")):
                full = os.path.join(root, name)
                with open(full, encoding="utf-8") as fh:
                    files[os.path.relpath(full, path)] = fh.read()
    tree, errors = build_tree(files)
    assert not errors, errors
    return tree


@contextmanager
def secrets_loaded(path, **more):
    """Settings for a test whose secrets come from a directory: they are loaded into the database first."""
    with override_settings(**more):
        load_from_secrets(path)
        yield


def load_from_secrets(path, user=None):
    """Make secrets from a directory of YAML files: users, OpenNebula front ends, BGP. Returns (created, updated)."""
    tree = _tree(path)
    created = updated = 0

    def get(kind, name, **defaults):
        nonlocal created, updated
        s = Secret.objects.filter(kind=kind, name=name).first()
        if s is None:
            created += 1
            return Secret(kind=kind, name=name, **defaults)
        updated += 1
        return s

    def walk(node, path):
        if not isinstance(node, dict):
            return
        p = ".".join(path)
        if "oneadmin" in node and isinstance(node.get("oneadmin"), str):  # an OpenNebula front end
            s = get(Secret.Kind.ONE, p, db_name="onedb", db_user="onedbuser")
            s.put("one_password", node["oneadmin"])
            db = node.get("onedb")
            if isinstance(db, dict):
                s.db_name = str(db.get("db") or s.db_name)
                s.db_user = str(db.get("user") or s.db_user)
                if db.get("password"):
                    s.put("db_password", str(db["password"]))
            su = (
                (node.get("service_user") or {}).get("oneadmin") if isinstance(node.get("service_user"), dict) else None
            )
            if isinstance(su, dict):
                if su.get("password"):
                    s.put("os_password", str(su["password"]))
                if su.get("key"):
                    s.put("os_private_key", str(su["key"]))
            s.save()
            return
        if len(path) >= 2 and path[-1] == "network":  # <family>.network.<net>.<site>.<group>
            for net, sites in node.items():
                for site, groups in (sites or {}).items() if isinstance(sites, dict) else ():
                    if not isinstance(groups, dict):
                        continue
                    from .models import Network

                    n = Network.objects.filter(site=site, name=net).first()
                    s = get(Secret.Kind.BGP, f"{site}.{net}", site=site, network=net, net_type=n.kind if n else "ix")
                    for g, v in groups.items():
                        if isinstance(v, str) and v:
                            s.put(SLOT_OF_GROUP.get(g, "igw"), v)
                    s.save()
            return
        if isinstance(node.get("password"), str) or isinstance(node.get("key"), str):  # one user
            s = get(Secret.Kind.USER, p, username=str(node.get("user") or path[-1]))
            if isinstance(node.get("password"), str):
                s.put("password", node["password"])
            k = node.get("key")
            if isinstance(k, str) and "PRIVATE KEY" in k:
                s.put("private_key", k)
                s.public_key = _public_key(k)
            elif isinstance(k, str) and k.strip().startswith(("ssh-", "ecdsa-")):
                s.public_key = k.strip()
            s.save()
            return
        for k, v in node.items():
            walk(v, path + [str(k)])

    walk(tree, [])
    return created, updated
