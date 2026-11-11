"""One look for types and states everywhere (FireEdge-like): types are soft coloured labels, states a dot and a word.

    {% tp org.kind %}  {% tp tenant.stage tenant.get_stage_display %}  {% st host.state %}
"""
import zlib

from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()

# A type keeps its colour on every page: corp is always blue, cloud/client teal, poc violet, ...
TYPE_COLORS = {
    # organizations and tenants
    "corp": "blue", "cloud": "teal", "client": "teal", "poc": "violet",
    # platforms and clusters
    "shared": "teal", "dedicated": "violet", "mgmt": "grey", "ext-managed": "orange", "gpu": "pink",
    # connectivity and network functions
    "inet": "teal", "corpnet": "blue", "l2": "orange", "egw": "teal", "igw": "green", "cgw": "blue",
    "l2-a": "orange", "l2-b": "orange", "rr": "grey", "host": "grey",
    # credentials
    "opennebula": "teal", "ssh_key": "violet", "bgp": "orange", "ipmi": "pink", "password": "blue", "db": "grey",
    "jump": "grey", "secret": "grey",
    # storage and networks
    "ceph": "violet", "nfs": "blue", "local": "grey", "vlan": "blue", "vxlan": "violet",
}
PALETTE = ("teal", "blue", "violet", "orange", "pink", "green")

OK = {"active", "deployed", "done", "approved", "ok", "defined", "accepted", "on", "running", "applied", "current", "valid",
      "found", "yes", "same", "used"}
WARN = {"planned", "pending", "updated", "waiting", "plan", "preview", "draft", "paused", "changed", "new", "differs", "replaced"}
BAD = {"retired", "failed", "rejected", "error", "missing", "suspended", "bad", "off", "unreachable", "removed"}


def type_color(value):
    v = str(value or "").lower()
    return TYPE_COLORS.get(v) or PALETTE[zlib.crc32(v.encode()) % len(PALETTE)]


def state_class(value):
    v = str(value or "").lower()
    return "ok" if v in OK else "warn" if v in WARN else "bad" if v in BAD else "muted"


@register.simple_tag
def tp(value, label=None):
    if value in (None, ""):
        return "—"
    return format_html('<span class="tp c-{}">{}</span>', type_color(value), str(label if label not in (None, "") else value).lower())


@register.simple_tag
def st(value, label=None):
    if value in (None, ""):
        return "—"
    return format_html('<span class="st {}">{}</span>', state_class(value), label if label not in (None, "") else value)


@register.filter
def gb(value):
    """Owner 2026-10-29 20:59: RAM (and other sizes in GB) as a whole number everywhere: 1.0 -> 1, 15.6 -> 16."""
    if value in (None, ""):
        return value
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return value


def _n(v):
    """A number out of a template value; None for anything missing, so a pool with no reading shows the quota alone."""
    if v in (None, ""):
        return None
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _level(pct):
    """Owner 2026-11-09: "red color when 100 / yellow when 75-99"."""
    return "bad" if pct >= 100 else ("warn" if pct >= 75 else "ok")


def _pct(quota, usage):
    return 0 if not quota else min(100, int(round(usage * 100 / quota)))


@register.simple_tag
def ubar(quota, usage):
    """The 3px fill bar on its own, for a value whose number is written elsewhere in the cell."""
    q, u = _n(quota), _n(usage)
    if q is None or u is None:
        return ""
    p = _pct(q, u)
    return format_html('<span class="ub {}"><i style="width:{}%"></i></span>', _level(p), p)


@register.simple_tag
def qu(quota, usage=None, prefix="", bar=True):
    """A resource pool's number: the quota in bold, what is in use beside it smaller and lighter, and the fill bar.

    Owner 2026-11-09: "8 / 6 - 8 quota values, make it bold / 6 - usage value, make it thin", then "maybe drop /
    and just smaller font size". The thresholds live here so every column reads the same.
    """
    q, u = _n(quota), _n(usage)
    pre = format_html('{} ', prefix) if prefix else ""
    if q is None:
        return format_html("{}—", pre)
    if u is None:
        return format_html("{}<b>{}</b>", pre, q)
    return format_html('{}<b>{}</b> <span class="qu">{}</span>{}', pre, q, u,
                       mark_safe(ubar(q, u)) if bar else "")


@register.filter
def state_cls(value):
    return state_class(value)


@register.filter
def get_item(mapping, key):
    """{{ dict|get_item:key }}"""
    try:
        return mapping.get(key)
    except AttributeError:
        return None


@register.filter
def get_item_attr(obj, name):
    return getattr(obj, str(name), None)


@register.filter
def admin_tenant_ids(user):
    """Tenants the (tenant) user is tenant admin of."""
    try:
        return {m.tenant_id for m in user.memberships.all() if m.role == "tenant_admin"}
    except AttributeError:
        return set()


@register.filter
def secret(ref):
    """{{ host.ipmi_user|secret }}: the secret a reference points to, by type and name."""
    from inventory.credentials import describe
    return describe(ref) if ref else "—"


_CLUSTER_NAMES = {"at": 0.0, "map": {}}


def cluster_names():
    """{(site, quota code or name): full cluster name}, kept a minute."""
    import time
    if time.time() - _CLUSTER_NAMES["at"] > 60:
        from inventory.models import Cluster
        m = {}
        for c in Cluster.objects.select_related("platform__site").all():
            site = getattr(getattr(c.platform, "site", None), "code", "") or ""
            m[(site, c.name)] = c.name
            if c.quota_code:
                m.setdefault((site, c.quota_code), c.name)
                m.setdefault(("", c.quota_code), c.name)
        _CLUSTER_NAMES.update(at=time.time(), map=m)
    return _CLUSTER_NAMES["map"]


@register.filter
def cluster_name(code, site=""):
    """The cluster's full name (owner 2026-10-29: never the old quota codes like ca1)."""
    if not code:
        return code
    m = cluster_names()
    return m.get((site or "", code)) or m.get(("", code)) or code


@register.simple_tag
def provider_name():
    """The provider's name (Settings → Provider; owner 2026-10-29)."""
    from accounts.branding import provider
    return provider()


@register.simple_tag
def provider_logo(absolute=False):
    """The provider's logo address ("" without one); absolute=True for e-mails."""
    from accounts.branding import logo_path
    p = logo_path()
    if p and absolute:
        from accounts.utils import site_link
        return site_link(p)
    return p
