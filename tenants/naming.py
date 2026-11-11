"""Names of tenants in OpenNebula.

Organizations have no OpenNebula object. Each tenant is one OpenNebula group, and the group
name is the tenant ID shown in the UI (correction 13, 2026-10-29):

    _<org short name>_<tenant name without "-">        e.g. _romashka_gpulab

A new project's OpenNebula admin is <org short name>-<project name>-adm (owner 2026-10-31); its VMs are
_<org>_<project>_<role>_<site> (egw, igw, …), its networks <group>-main_<site>, … Nothing on the platforms is renamed:
admins made before keep <org>-<user name>. Groups made before keep their names: _t_<org>_<tenant> (the portal until correction 13, admin
<org>-<tenant>-admin) and tenant-<name> (made before the portal, admin as recorded).
Both names are lowercase latin letters and digits, starting with a letter: organization 3-15
characters, tenant 4-15 (owner 2026-10-31, u02; longer names made before are kept), so a group name is at most 32
characters.
Customers enter only the tenant name.
OpenNebula groups cannot be renamed, so a tenant ID never changes once the group
exists.
"""
import re

TRANSLIT = {
    "а": "a", "ә": "a", "б": "b", "в": "v", "г": "g", "ғ": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "i", "і": "i", "к": "k", "қ": "q", "л": "l", "м": "m", "н": "n", "ң": "n",
    "о": "o", "ө": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ұ": "u", "ү": "u", "ф": "f",
    "х": "kh", "һ": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e",
    "ю": "yu", "я": "ya",
}
LEGAL_FORMS = {
    "too", "llp", "ip", "ao", "jsc", "ooo", "ltd", "llc", "inc", "tdo", "gu", "ku", "rgp", "gkp", "pk", "chu",
    "ojsc", "pao", "zao", "oao", "kh", "fl", "ul", "tovarischestvo", "ogranichennoi", "otvetstvennostyu", "s",
}
MAX_LEN = 32
ORG_RE = re.compile(r"^[a-z][a-z0-9]{2,14}$")
TENANT_RE = re.compile(r"^[a-z](?!.*--)[a-z0-9-]{2,13}[a-z0-9]$")  # correction 12: lowercase letters, digits and "-"
GROUP_PREFIX = "_"
LEGACY_PREFIX = "_t_"  # the portal's groups before correction 13
GROUP_RE = re.compile(r"^_(?:t_)?([a-z][a-z0-9]{2,14})_([a-z][a-z0-9-]{2,13}[a-z0-9])$")
ADMIN_USER_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")
ORG_MIN, ORG_MAX, TENANT_MIN, TENANT_MAX = 3, 15, 4, 15


def group_name(org_short, tenant_short):
    """The OpenNebula group of a new tenant: _<org>_<tenant>, no "-" in either (correction 13)."""
    return f"{GROUP_PREFIX}{org_short.replace('-', '')}_{tenant_short.replace('-', '')}"


def admin_name(org_short, project_short):
    """The OpenNebula admin of a new project: <org>-<project>-adm, the project without "-" as in its group (owner
    2026-10-31). Admins made before keep their names (<org>-<user name>, <org>-<tenant>-admin)."""
    return f"{org_short}-{project_short.replace('-', '')}-adm"


def _token(text, fallback):
    t = re.sub(r"[^a-z0-9]", "", str(text or "").lower())
    return t or fallback


def tenant_parts(tenant):
    """(org, tenant) of a tenant's names: from its group _t_<org>_<tenant>; a tenant-<name> group:
    its organization's short name and its own name."""
    m = GROUP_RE.match(tenant.group_name or "")
    if m:
        return m.group(1), _token(m.group(2), "tenant")  # VyOS names: letters and digits only
    org = getattr(getattr(tenant, "organization", None), "short_name", "") or ""
    name = tenant.name or (tenant.group_name or "")[len("tenant-"):]
    return _token(org, "org"), _token(name, "tenant")


def vyos_names(tenant, role, site):
    """(VM name, host name) of a tenant's VyOS VM (correction 11, 2026-10-28): VM _<org>_<tenant>_<role>_<site>, host
    name <role>-<org>-<tenant>-<site> (VyOS host names take no "_"). role: egw, cgw, igw, l2src<vlan>, l2dst<vlan>,
    l2br<vlan>."""
    org, ten = tenant_parts(tenant)
    return f"_{org}_{ten}_{role}_{site}", f"{role}-{org}-{ten}-{site}"


def _words(text):
    text = "".join(TRANSLIT.get(ch, ch) for ch in (text or "").lower())
    words = [w for w in re.split(r"[^a-z0-9]+", text) if w]
    return [w for w in words if w not in LEGAL_FORMS] or words


def compact(text, max_len, min_len=3, filler="x"):
    """Words joined without separators, starting with a letter, cut at a word boundary when possible."""
    out = ""
    for w in _words(text):
        if not out:
            w = w.lstrip("0123456789")
            if not w:
                continue
        if len(out) + len(w) > max_len:
            if not out:
                out = w[:max_len]
            break
        out += w
    if out and len(out) < min_len:
        out = (out + filler * min_len)[:min_len]
    return out


def _free(base, taken, max_len):
    if base not in taken:
        return base
    for n in range(2, 10000):
        name = base[:max_len - len(str(n))] + str(n)
        if name not in taken:
            return name
    raise ValueError("no free name")


def suggest_org_short_name(org_name, exclude_pk=None):
    from .models import Organization
    taken = set(Organization.objects.exclude(short_name__isnull=True).exclude(pk=exclude_pk)
                .values_list("short_name", flat=True))
    return _free(compact(org_name, 8) or "org", taken, 8)  # u10: suggested default up to 8 chars


def org_tenant_names(org, exclude_pk=None):
    """Short names already used by live requests and tenants of an organization — each also without its "-" (the VyOS
    names use the name without them: "ab-cd" and "abcd" would be one name there)."""
    from .models import Approval
    names = set(org.tenants.exclude(pk=exclude_pk).exclude(approval__in=[Approval.REJECTED, Approval.CANCELLED])
                .exclude(name__isnull=True).values_list("name", flat=True))
    return names | {n.replace("-", "") for n in names}


def taken(name, org, exclude_pk=None):
    """Whether a tenant name collides with one of the organization's (with or without its "-")."""
    used = org_tenant_names(org, exclude_pk)
    return name in used or name.replace("-", "") in used


def observed_group_names():
    from adoption.models import Observation
    return set(Observation.objects.values_list("group_name", flat=True))


