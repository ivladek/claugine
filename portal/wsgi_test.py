"""The new portal version under <public URL>/test-new-version/ (owner 2026-10-31; accounts/testmount.py): the same
application with CLAUGINE_TEST_MOUNT=1, in an Apache process group of its own (deploy/install.sh)."""
import os

os.environ["CLAUGINE_TEST_MOUNT"] = "1"
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "portal.settings")

from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()

from accounts import testmount  # noqa: E402

testmount.setup()
