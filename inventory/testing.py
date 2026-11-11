"""Test helpers."""
from contextlib import contextmanager

from django.test import override_settings


@contextmanager
def secrets_loaded(path, **more):
    """Settings with a secrets directory whose values are loaded into the database (the portal reads only there)."""
    with override_settings(INVENTORY_SECRETS_DIR=path, **more):
        from .credentials import load_from_secrets
        load_from_secrets()
        yield
