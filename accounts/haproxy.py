"""Settings → Portal and Platform (u02; Settings → HAProxy before): what HAProxy on the portal VMs serves (owner 2026-10-31).

The installation's own names and addresses live here, in the portal's settings (replicated to the other
node with the database), not in the code. Each portal VM applies them by itself: the appliance timer runs
`claugine-lb apply` (or `certificate` for a new name) when they change (accounts/appliance.apply_lb, 2026-11-14); both build the
options of deploy/install-lb.sh from them (manage.py haproxy_args): the global part, the part of the VM's own site
(node.env SITE) and the VM's own addresses and ports (node.env ADDR, PEER_ADDR, STATUS_PORTS, HTTPS_PORT).

    public_names  the portal's names: the host of the Public URL (Settings → Customer), then the host of the Admin
                  Portal URL (unlocked by the owner 2026-11-23 16:39); former names stored are not used
    le_email      the Let's Encrypt account (certificates of the portal and of the sites' front ends)

Nothing about the sites' front ends is entered in Settings (owner 2026-11-15: "ha proxy must use values from platfrom
settings", "remove opennebula frontends - it does not required because public endpoints defined in platfrom
settings"): a site's front end is its shared platform with a public FQDN (the first by key; Infrastructure → the
platform → Edit), served under that FQDN at the platform's VIP. No endpoint is restricted by address (owner
2026-11-17: "no any restriction for endpoints now must be"; OneFlow was the sites' public networks only, and the front
ends' names are proxied by Cloudflare, which hides the client's address): each service asks for its own login.

On 443 under that name (deploy/install-lb.sh): FireEdge (/fireedge and /), the XML-RPC API (/RPC2) and OneFlow
(/oneflow). Not published (owner 2026-11-15): the gRPC API ("until opennebula next
release accepted tls": its client has no TLS) and OneGate (the VMs reach it through their host, OpenNebula's
transparent proxy). The public ports of commit b663e8b and the names entered here before are no longer read.
"""

import re

# owner 2026-11-23 16:39 (U13): "since now use both fields for fqdns inside certificate - Customer and Admin portal"
# (locked 2026-11-19 19:01 until then)
ADMIN_NAME_ON_CERT = True
NAME_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,63}$", re.I)


def defaults():
    return {"public_names": "", "le_email": "", "sites": {}}


def get(s=None):
    """The HAProxy settings, with the defaults for what is not set."""
    if s is None:
        from tenants.models import PortalSettings

        s = PortalSettings.get()
    raw = s.haproxy or {}
    out = defaults()
    main = main_name(s)
    # owner 2026-11-19 16:25 "update cert to use both fqdns defined in settings/customer - customer and admin": the
    # Admin Portal's host goes on the same certificate, next to the main name - locked since 19:01 "just keey only
    # customer fqdn / dont use admin fqdn until i unlock it" (the name is internal for now: ADMIN_NAME_ON_CERT)
    admin = admin_name(s) if ADMIN_NAME_ON_CERT else ""
    first = list(dict.fromkeys(n for n in (main, admin) if n))
    # commit-196: the former names stored stay stored, not used; owner 2026-11-23 16:39: "both fields" - the certificate
    # carries the Customer Portal and Admin Portal names only (a former name no longer pointing here would fail it)
    others = []
    out["public_names"] = ",".join(first + others)
    out["other_names"] = ",".join(others)
    out["le_email"] = raw.get("le_email", "") or ""
    out["sites"] = {}
    for code in site_codes():  # from the inventory only (2026-11-15); the sites stored before are not read
        p = frontend(code)
        if p is not None:
            out["sites"][code] = {
                "one_names": p.public_fqdn,
                "one_addr": p.fe_vip,
                "platform": p.key,
            }
    return out


def frontend(code):
    """The site's published front end: its first shared platform (by key) with a public FQDN and a VIP, or None
    (commit-010, owner 2026-11-15: "publish implemented only for shared platfrom - it is by design")."""
    from inventory.models import Platform

    return (
        Platform.objects.filter(site__code=code, kind="shared")
        .exclude(public_url="")
        .exclude(fe_vip__in=("", "0.0.0.0"))  # commit-012: "0.0.0.0" is a platform saved without its VIP
        .order_by("key")
        .first()
    )


def main_name(s=None):
    """The portal's main name: the host of the Public URL (Settings → Provider); "" when it is not a host name."""
    from urllib.parse import urlsplit

    if s is None:
        from tenants.models import PortalSettings

        s = PortalSettings.get()
    host = (urlsplit((s.public_url or "").strip()).hostname or "").lower()
    return host if NAME_RE.match(host) else ""


def admin_name(s=None):
    """The Admin Portal's name: the host of its URL (Settings → Customer); "" when it is not a host name."""
    from urllib.parse import urlsplit

    if s is None:
        from tenants.models import PortalSettings

        s = PortalSettings.get()
    host = (urlsplit((s.admin_url or "").strip()).hostname or "").lower()
    return host if NAME_RE.match(host) else ""


def site_codes():
    from inventory.models import Site

    return list(Site.objects.order_by("code").values_list("code", flat=True))


def _names(value):
    return [n for n in re.split(r"[\s,]+", (value or "").strip().lower()) if n]


class Incomplete(Exception):
    pass


def _ilo_addresses(site):
    from inventory.models import Host

    qs = Host.objects.filter(site__code=site).exclude(state="retired").exclude(ipmi_host="")
    return list(qs.values_list("name", "ipmi_host"))


def ilo_networks(site):
    """T25 (owner 2026-11-18 11:59: "ISOs must be mounted from portal node an related site"): the /24 networks of the
    iLO addresses of the site's hosts - who may fetch /hostmedia/ from this node. IPv4 addresses only (commit-126):
    a name or an IPv6 address is left out (ilo_unserved lists them) and never reaches install-lb.sh."""
    import ipaddress

    nets = set()
    for _n, ip in _ilo_addresses(site):
        try:
            addr = ipaddress.ip_address(ip.strip())
        except ValueError:
            continue
        if addr.version == 4:
            nets.add(ipaddress.ip_network(f"{addr}/24", strict=False))
    return [str(n) for n in sorted(nets)]


def ilo_unserved(site):
    """[(host, iLO address)] whose address is not IPv4: /hostmedia/ is not opened for them (commit-126)."""
    import ipaddress

    out = []
    for name, ip in _ilo_addresses(site):
        try:
            if ipaddress.ip_address(ip.strip()).version == 4:
                continue
        except ValueError:
            pass
        out.append((name, ip))
    return out


def args_for(conf, node, letsencrypt=False):
    """deploy/install-lb.sh's options for one portal VM: conf = get(), node = its node.env (accounts.thisnode.read)."""
    site, addr, peer = node.get("SITE", ""), node.get("ADDR", ""), node.get("PEER_ADDR", "")
    missing = [k for k, v in (("node.env SITE", site), ("node.env ADDR", addr)) if not v]
    if not conf["public_names"]:
        missing.append("Settings → Platform: the Public URL (the portal's name)")
    if missing:
        raise Incomplete("missing: " + ", ".join(missing))
    apps = sorted(a for a in {addr, peer} if a)
    args = ["--apps", ",".join(apps)]
    if peer:
        args += ["--peer-lb", peer]
    args += ["--public-name", conf["public_names"]]
    s = conf["sites"].get(site) or {}
    if s.get("one_names"):
        args += ["--one-name", s["one_names"], "--one-addr", s["one_addr"]]
    status = (node.get("STATUS_PORTS") or "").split(",")[0].strip()
    if status:
        args += ["--status-port", status]
    if node.get("HTTPS_PORT"):  # the portal's Apache TLS port on the app nodes (install.sh keeps it in node.env)
        args += ["--app-port", node["HTTPS_PORT"]]
    media = ilo_networks(site)  # T25: /hostmedia/ on port 80, from this site's iLOs only, to this node's own Apache
    if media:
        args += ["--hostmedia", ",".join(media), "--media-port", node.get("HTTP_PORT") or "8080"]
    if letsencrypt:
        if not conf["le_email"]:
            raise Incomplete("missing: Settings → Portal: the Let's Encrypt e-mail")
        args += ["--letsencrypt-email", conf["le_email"]]
    return args
