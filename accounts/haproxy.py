"""Settings → Portal and Platform (u02; Settings → HAProxy before): what HAProxy on the portal VMs serves (owner 2026-10-31).

The installation's own names, addresses and public ports live here, in the portal's settings (replicated to the other
node with the database), not in the code. On each portal VM `claugine-lb apply` and `claugine-lb certificate` build the
options of deploy/install-lb.sh from them (manage.py haproxy_args): the global part, the part of the VM's own site
(node.env SITE) and the VM's own addresses and ports (node.env ADDR, PEER_ADDR, STATUS_PORTS, HTTPS_PORT).

    public_names  the portal's names: the main one is the host of the Public URL (Settings → Provider, u02), the
                  others (former names) stay on the same certificate
    le_email      the Let's Encrypt account (certificates of the portal and of the sites' front ends)
    ports         the public ports of the OpenNebula front end through HAProxy; the defaults are OpenNebula's own
    sites         per site: one_addr (the front end's VIP, chosen from the site's platforms), one_names (its names),
                  local_nets (who may reach OneGate and OneFlow): the sites' public networks, i.e. the delegated
                  internet subnets of the exchange segments (ix) of the platform whose VIP it is (Infrastructure);
                  they are not entered here (u01)
"""
import re

# (key, label, the OpenNebula service port behind it = the default public port, open to: "all" | "local")
SERVICES = [
    ("fireedge", "FireEdge", 2616, "all"),
    ("grpc", "gRPC API", 2634, "all"),
    ("xmlrpc", "XML-RPC API", 2633, "all"),
    ("onegate", "OneGate", 5030, "local"),
    ("oneflow", "OneFlow", 2474, "local"),
]
RESERVED = {80, 443, 8888}  # HAProxy itself and the ACME relay
NAME_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,63}$", re.I)


def defaults():
    return {"public_names": "", "le_email": "", "ports": {k: d for k, _l, d, _o in SERVICES}, "sites": {}}


def get(s=None):
    """The HAProxy settings, with the defaults for what is not set."""
    if s is None:
        from tenants.models import PortalSettings
        s = PortalSettings.get()
    raw = s.haproxy or {}
    out = defaults()
    main = main_name(s)
    others = [n for n in _names(raw.get("public_names")) if n != main]
    out["public_names"] = ",".join(([main] if main else []) + others)
    out["other_names"] = ",".join(others)
    out["le_email"] = raw.get("le_email", "") or ""
    out["ports"].update({k: int(v) for k, v in (raw.get("ports") or {}).items() if k in out["ports"] and str(v).isdigit()})
    out["sites"] = {code: {"one_names": (v or {}).get("one_names", ""), "one_addr": (v or {}).get("one_addr", ""),
                           "local_nets": (v or {}).get("local_nets", "")} for code, v in (raw.get("sites") or {}).items()}
    for code, v in out["sites"].items():  # u01: the public networks follow the inventory
        nets = public_nets(code, v["one_addr"])
        if nets is not None:
            v["local_nets"] = ",".join(nets)
    return out


def main_name(s=None):
    """The portal's main name: the host of the Public URL (Settings → Provider); "" when it is not a host name."""
    from urllib.parse import urlsplit
    if s is None:
        from tenants.models import PortalSettings
        s = PortalSettings.get()
    host = (urlsplit((s.public_url or "").strip()).hostname or "").lower()
    return host if NAME_RE.match(host) else ""


def fe_choices(code):
    """The front ends of a site to choose from: [(VIP, label)] of its platforms that have one."""
    from inventory.models import Platform
    return [(p.fe_vip, f"{p.fe_vip} · {p.key}{' · ' + p.fe_name if p.fe_name else ''}")
            for p in Platform.objects.filter(site__code=code).exclude(fe_vip="").order_by("key")]


def public_nets(code, vip):
    """The site's public networks for the front end with this VIP: the internet subnets delegated to its platform's
    exchange segments; None when no platform of the site has that VIP."""
    from inventory.models import DelegatedSubnet, Platform
    p = Platform.objects.filter(site__code=code, fe_vip=vip).first() if vip else None
    if p is None:
        return None
    return [d.cidr for d in p.delegated_subnets(DelegatedSubnet.Purpose.INET)]


def site_codes():
    from inventory.models import Site
    return list(Site.objects.order_by("code").values_list("code", flat=True))


def _names(value):
    return [n for n in re.split(r"[\s,]+", (value or "").strip().lower()) if n]


def clean(data, codes, main=None):
    """Validate what the form sent ({field: value}); returns (settings, errors). The main name comes from the Public
    URL (main_name); the form gives only the other names (other_names)."""
    errors = {}
    main = main_name() if main is None else main
    others = [n for n in _names(data.get("other_names")) if n != main]
    names = ([main] if main else []) + others
    if not main:
        errors["other_names"] = "set the Public URL first (Settings → Provider): its host is the portal's main name"
    elif bad := [n for n in others if not NAME_RE.match(n)]:
        errors["other_names"] = "not a host name: " + ", ".join(bad)
    email = (data.get("le_email") or "").strip()
    if email and not EMAIL_RE.match(email):
        errors["le_email"] = "not an e-mail address"
    ports = {}
    for key, label, default, _o in SERVICES:
        raw = str(data.get(f"port_{key}") or default).strip()
        if not raw.isdigit() or not 1 <= int(raw) <= 65535:
            errors[f"port_{key}"] = "a port, 1-65535"
        elif int(raw) in RESERVED:
            errors[f"port_{key}"] = f"{raw} is HAProxy's own (80, 443) or the ACME relay (8888)"
        else:
            ports[key] = int(raw)
    if len(set(ports.values())) < len(ports):
        errors["ports"] = "every service needs its own port"
    sites = {}
    for code in codes:
        one_names = _names(data.get(f"one_names_{code}"))
        one_addr = (data.get(f"one_addr_{code}") or "").strip()
        nets = public_nets(code, one_addr)
        if one_addr and nets is None:
            errors[f"one_addr_{code}"] = "not the VIP of a platform of this site"
        elif one_addr and not nets:
            errors[f"one_addr_{code}"] = ("its platform has no internet subnet on an exchange segment "
                                          "(Infrastructure → the site's networks): nobody could reach OneGate and OneFlow")
        if bad := [n for n in one_names if not NAME_RE.match(n)]:
            errors[f"one_names_{code}"] = "not a host name: " + ", ".join(bad)
        nets = nets or []
        if one_names and not one_addr:
            errors[f"one_addr_{code}"] = "the front end's address is needed with its name"
        if one_names or one_addr or nets:
            sites[code] = {"one_names": ",".join(one_names), "one_addr": one_addr, "local_nets": ",".join(nets)}
    return {"public_names": ",".join(names), "le_email": email, "ports": ports, "sites": sites}, errors


class Incomplete(Exception):
    pass


def args_for(conf, node, letsencrypt=False):
    """deploy/install-lb.sh's options for one portal VM: conf = get(), node = its node.env (accounts.thisnode.read)."""
    site, addr, peer = node.get("SITE", ""), node.get("ADDR", ""), node.get("PEER_ADDR", "")
    missing = [k for k, v in (("node.env SITE", site), ("node.env ADDR", addr)) if not v]
    if not conf["public_names"]:
        missing.append("Settings → Provider: the Public URL (the portal's name)")
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
        if s.get("local_nets"):
            args += ["--local-nets", s["local_nets"]]
    for key, _l, _d, _o in SERVICES:
        args += [f"--{key}-port", str(conf["ports"][key])]
    status = (node.get("STATUS_PORTS") or "").split(",")[0].strip()
    if status:
        args += ["--status-port", status]
    if node.get("HTTPS_PORT"):  # the portal's Apache TLS port on the app nodes (install.sh keeps it in node.env)
        args += ["--app-port", node["HTTPS_PORT"]]
    if letsencrypt:
        if not conf["le_email"]:
            raise Incomplete("missing: Settings → Portal: the Let's Encrypt e-mail")
        args += ["--letsencrypt-email", conf["le_email"]]
    return args


def preview(conf, codes, node):
    """[(site, options as one line, problem)] for the tab: this VM's addresses stand for both nodes' order."""
    rows = []
    for code in codes:
        n = {**node, "SITE": code}
        if node.get("SITE") and code != node.get("SITE") and node.get("PEER_ADDR"):
            n["ADDR"], n["PEER_ADDR"] = node.get("PEER_ADDR"), node.get("ADDR")
        n["ADDR"] = n.get("ADDR") or "<this VM>"
        try:
            rows.append((code, " ".join(args_for(conf, n)), ""))
        except Incomplete as exc:
            rows.append((code, "", str(exc)))
    return rows
