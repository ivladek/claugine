"""Encryption of stored credentials with a key kept outside the database.

`FIELD_ENCRYPTION_KEY` (portal.env) is one Fernet key or several, comma-separated (MultiFernet, the same as
claugine-web's, owner 2026-11-11): the first key encrypts, every key decrypts. To change the key, put the new one
first, run `manage.py rotate_keys` (every stored secret is encrypted again with it), then drop the old one. Every
stored secret sits in a model field named `<name>_encrypted`.
"""

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def _keys():
    keys = [k.strip() for k in (settings.FIELD_ENCRYPTION_KEY or "").split(",") if k.strip()]
    if not keys:
        raise ImproperlyConfigured("FIELD_ENCRYPTION_KEY is not set")
    return keys


def _fernet():
    return MultiFernet([Fernet(k.encode()) for k in _keys()])


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ImproperlyConfigured("Stored credential cannot be decrypted with FIELD_ENCRYPTION_KEY") from exc


def encrypted_fields():
    """(model, field name) of every stored secret: the fields named <name>_encrypted."""
    from django.apps import apps

    return [
        (m, f.name)
        for m in apps.get_models()
        for f in m._meta.concrete_fields
        if f.name.endswith("_encrypted") and f.get_internal_type() in ("TextField", "CharField")
    ]


def rotate(dry=False):
    """Encrypt every stored secret again with the first key. Returns {"model.field": values changed}; a value no
    key opens is left as it is and counted under "unreadable"."""
    keys = _keys()
    primary = Fernet(keys[0].encode())
    multi = _fernet()
    report = {}
    for model, name in encrypted_fields():
        changed = 0
        for pk, token in model.objects.exclude(**{name: ""}).values_list("pk", name):
            try:
                primary.decrypt(token.encode())
                continue  # already under the first key
            except InvalidToken:
                pass
            try:
                new = multi.rotate(token.encode()).decode()
            except InvalidToken:
                report["unreadable"] = report.get("unreadable", 0) + 1
                continue
            if not dry:
                model.objects.filter(pk=pk).update(**{name: new})
            changed += 1
        if changed:
            report[f"{model._meta.label}.{name}"] = changed
    return report
