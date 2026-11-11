"""Claugine portal settings.

Configuration comes from environment variables, optionally loaded from the
file named by PORTAL_ENV_FILE (default /etc/claugine-portal/portal.env).
"""
import os
import sys
from pathlib import Path

from django.utils.translation import gettext_lazy as _

BASE_DIR = Path(__file__).resolve().parent.parent

ENV_FILE = os.environ.get("PORTAL_ENV_FILE", "/etc/claugine-portal/portal.env")
if os.path.isfile(ENV_FILE) and os.access(ENV_FILE, os.R_OK):
    with open(ENV_FILE, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    return env(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


DEBUG = env_bool("DEBUG")
SECRET_KEY = env("SECRET_KEY") or ("dev-only-insecure-key" if DEBUG else None)
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY is not set")

SITE_URL = env("SITE_URL", "http://localhost:8000").rstrip("/")
# addresses the portal connects to routers from (allowed in their SSHFROM); empty: found from the default route
PORTAL_MGMT_IPS = [x for x in env("PORTAL_MGMT_IPS", "").replace(",", " ").split() if x]
ALLOWED_HOSTS = [h.strip() for h in env("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [SITE_URL]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "tenants",
    "content",
    "inventory",
    "adoption",
]


MIDDLEWARE = [
    "accounts.appliance.PublicHostMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "accounts.testmount.TestMountMiddleware",
    "accounts.ha.RoleMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "accounts.middleware.StaffTwoFactorMiddleware",
    "accounts.middleware.ActAsMiddleware",  # u12: super admin "act as" another user (after the 2FA check of the real user)
    "accounts.middleware.ListReturnMiddleware",
]

ROOT_URLCONF = "portal.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # ee/templates: the Enterprise edition's page parts (editions split step 3, 2026-11-11); absent in Standard
        "DIRS": [BASE_DIR / "templates"] + ([BASE_DIR / "ee" / "templates"] if (BASE_DIR / "ee" / "__init__.py").exists() else []),
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "django.template.context_processors.i18n",
                "accounts.context.portal",
                "accounts.testmount.context",
            ],
            "builtins": ["accounts.templatetags.ui"],
        },
    },
]

WSGI_APPLICATION = "portal.wsgi.application"

if env("DB_NAME"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": env("DB_NAME"),
            "USER": env("DB_USER", "portal"),
            "PASSWORD": env("DB_PASSWORD", ""),
            "HOST": env("DB_HOST", "localhost"),
            "PORT": env("DB_PORT", ""),
            "OPTIONS": {"charset": "utf8mb4", "init_command": "SET sql_mode='STRICT_TRANS_TABLES'"},
            "CONN_MAX_AGE": 60,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "var" / "dev.sqlite3",
        }
    }

AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = ["accounts.backends.PortalBackend"]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "accounts.policy.PolicyValidator"},
]
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "home"

# Russian and Kazakh are switched on once their translation files exist
# (locale/ru, locale/kk); until then the portal is English only, so pages
# never mix languages.
LANGUAGE_CODE = "en"
_ALL_LANGUAGES = [("en", _("English")), ("ru", _("Russian")), ("kk", _("Kazakh"))]
LANGUAGES = [(c, n) for c, n in _ALL_LANGUAGES if c == "en" or (BASE_DIR / "locale" / c).is_dir()]
LOCALE_PATHS = [BASE_DIR / "locale"]
TIME_ZONE = env("TIME_ZONE", "Asia/Almaty")  # u01: portal.env may set it (the portal's time: reports, timers, dates)
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = Path(env("STATIC_ROOT", str(BASE_DIR / "var" / "static")))
# Uploaded files (photos) live outside the web root and are served by views
# that check permissions.
MEDIA_ROOT = Path(env("MEDIA_ROOT", str(BASE_DIR / "var" / "media")))
MEDIA_URL = "/protected-media/"
FILE_UPLOAD_PERMISSIONS = 0o640
DATA_UPLOAD_MAX_MEMORY_SIZE = 8 * 1024 * 1024
PHOTO_MAX_BYTES = 5 * 1024 * 1024

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "portal@localhost")
# Settings → Portal appliance → Mail: the VM's relay (127.0.0.1:25) or an SMTP server; kept in files when unreachable.
# EMAIL_BACKEND in portal.env overrides it (e.g. the console backend on a workstation).
EMAIL_BACKEND = env("EMAIL_BACKEND", "accounts.appliance.EmailBackend")
EMAIL_FILE_PATH = env("EMAIL_FILE_PATH", str(BASE_DIR / "var" / "mail"))

# Licence / update control plane (owner 2026-11-04, migration to licensed updates). When CHECKIN_URL is set, auto-update
# asks cp.example.com what release to run (and the licence state) instead of chasing the newest GitHub tag. All three
# are planted by the installer in portal.env; without them there are no updates (v01.00.00).
CHECKIN_URL = env("CLAUGINE_CHECKIN_URL", "").rstrip("/")
LICENCE_KEY = env("CLAUGINE_LICENCE_KEY", "")
INSTALL_ID = env("CLAUGINE_INSTALL_ID", "")
# v01.00.00: the id this installation had before it became its FQDN, sent once with the check-in so cp.example.com
# renames the record in place (deploy/install.sh sets it; accounts/checkin.py drops it after a check-in that took it)
PREVIOUS_INSTALL_ID = env("CLAUGINE_PREVIOUS_INSTALL_ID", "")

# Key that encrypts stored platform credentials (Fernet, urlsafe base64, 32 bytes)
FIELD_ENCRYPTION_KEY = env("FIELD_ENCRYPTION_KEY", "")
INVENTORY_MAX_UPLOAD = 20 * 1024 * 1024
# Secrets of the data set (secrets/ next to inventory/), readable only by the portal; never in the database.
INVENTORY_SECRETS_DIR = env("INVENTORY_SECRETS_DIR", "/etc/claugine-portal/secrets")
# Tenant deployments run in a background thread; the tenant page shows the progress of each step.
DEPLOY_ASYNC = env_bool("DEPLOY_ASYNC", True) and "test" not in sys.argv[1:2]
ONE_SUNSTONE_URL = env("ONE_SUNSTONE_URL", "")
# the public documentation (the claugine-web project); the portal links there and keeps no docs of its own
DOCS_URL = env("DOCS_URL", "https://docs.claugine.com").rstrip("/")
# This node of the portal on two sites (accounts/ha.py): its role, site and peer; not replicated
NODE_ENV_FILE = env("NODE_ENV_FILE", "/etc/claugine-portal/node.env")
# HAProxy terminates HTTPS and talks to the portal over HTTPS too; the client's address comes in X-Forwarded-For
HA_TOKEN = env("HA_TOKEN", "")
HA_AGENT_PORT = int(env("HA_AGENT_PORT", "8090"))  # portal.env (install.sh keeps the one a node runs on)

INVITATION_TTL_HOURS = 72
REGISTRATION_TTL_HOURS = 48

SESSION_COOKIE_AGE = 8 * 3600
SESSION_COOKIE_HTTPONLY = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

# owner 2026-10-31: the new version under <public URL>/test-new-version/ (accounts/testmount.py, portal/wsgi_test.py).
# Apache gives the prefix (SCRIPT_NAME); sessions in a signed cookie of its own (nothing written to the database).
if env("CLAUGINE_TEST_MOUNT", "") == "1":
    STATIC_URL = "/test-new-version/static/"
    SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"
    SESSION_COOKIE_NAME = "claugine_test_session"
    SESSION_COOKIE_PATH = "/test-new-version/"
    CSRF_COOKIE_NAME = "claugine_test_csrf"
    CSRF_COOKIE_PATH = "/test-new-version/"
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "claugine-test"}}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
}

TEST_RUNNER = "portal.test_runner.Runner"  # signed-in test users have 2FA (correction 11: mandatory for every user)
