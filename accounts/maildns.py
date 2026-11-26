"""The DNS records the direct sending needs (owner 2026-11-17, Settings → Mail: "show required DNS records in direct and
reverse zone (use nodes IPs)").

Each portal node sends from its own mail server under its mail name (MAIL_NAME in node.env) and its public address
(typed per node in Settings → Mail: "A field per node"). The records: the mail name's A record, the sender domain's SPF,
the DKIM key (the portal makes it) and DMARC, and each address's PTR. The page shows whether DNS has each one now, read
with a small resolver of its own (A, TXT, PTR over UDP to the node's resolver) so the portal needs no DNS library.
"""

import ipaddress
import random
import socket
import struct
from concurrent.futures import ThreadPoolExecutor

TYPES = {"A": 1, "PTR": 12, "TXT": 16}


def _nameservers():
    out = []
    try:
        with open("/etc/resolv.conf", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    out.append(parts[1])
    except OSError:
        pass
    return out or ["127.0.0.53"]


def _name(data, off):
    """A (possibly compressed) domain name at off: (name, offset after it)."""
    labels, jumped, end = [], False, off
    for _i in range(64):
        n = data[off]
        if n == 0:
            off += 1
            break
        if n & 0xC0 == 0xC0:
            if not jumped:
                end = off + 2
            off, jumped = ((n & 0x3F) << 8) | data[off + 1], True
            continue
        labels.append(data[off + 1 : off + 1 + n].decode("ascii", "replace"))
        off += 1 + n
    return ".".join(labels), (end if jumped else off)


# owner 2026-11-23 11:48: the nodes' own DNS may hold a local copy of the sender's zone without these records (split
# horizon) - when it has no answer, external DNS is asked
EXTERNAL = ("1.1.1.1", "8.8.8.8")


def query(name, rtype, timeout=1.5, servers=None):
    """The answers of one DNS question: addresses (A), names (PTR) or texts (TXT, the strings joined). [] when there are
    none or DNS cannot be asked. servers: the resolvers to ask (default: the node's own, /etc/resolv.conf)."""
    qid = random.randint(0, 0xFFFF)
    q = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    for label in name.rstrip(".").split("."):
        q += bytes([len(label)]) + label.encode("ascii", "ignore")
    q += b"\0" + struct.pack(">HH", TYPES[rtype], 1)
    for ns in (list(servers) if servers else _nameservers())[:2]:
        try:
            with socket.socket(socket.AF_INET6 if ":" in ns else socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(timeout)
                s.sendto(q, (ns, 53))
                data, _a = s.recvfrom(4096)
        except OSError:
            continue
        if len(data) < 12 or struct.unpack(">H", data[:2])[0] != qid:
            continue
        an = struct.unpack(">H", data[6:8])[0]
        off = _name(data, 12)[1] + 4
        out = []
        for _i in range(an):
            _n, off = _name(data, off)
            t, _c, _ttl, ln = struct.unpack(">HHIH", data[off : off + 10])
            off += 10
            rd = data[off : off + ln]
            if t == TYPES[rtype] == 1 and ln == 4:
                out.append(socket.inet_ntoa(rd))
            elif t == TYPES[rtype] == 12:
                out.append(_name(data, off)[0])
            elif t == TYPES[rtype] == 16:
                parts, i = [], 0
                while i < len(rd):
                    parts.append(rd[i + 1 : i + 1 + rd[i]].decode("utf-8", "replace"))
                    i += 1 + rd[i]
                out.append("".join(parts))
            off += ln
        return out
    return []


def reverse_name(ip):
    return ipaddress.ip_address(ip).reverse_pointer


def reverse_zone(ip):
    return ".".join(reverse_name(ip).split(".")[1:])


def nodes():
    """[{site, mail_name}] of this node and, on a pair, the other one (from its agent)."""
    from . import ha_agent, thisnode

    n = thisnode.env()
    out = [{"site": n.get("SITE", "") or "this node", "mail_name": n.get("MAIL_NAME", "")}]
    if n.get("PEER_ADDR"):
        try:
            for label, _addr, st in ha_agent.nodes_status():
                if label == "other node":
                    out.append(
                        {
                            "site": st.get("site") or n.get("PEER_SITE", "") or "other node",
                            "mail_name": st.get("mail_name", ""),
                        }
                    )
        except Exception:  # noqa: BLE001 - the other node's row stays without a name
            out.append({"site": n.get("PEER_SITE", "") or "other node", "mail_name": ""})
    return out


def records(s, node_list=None, check=True):
    """The records, each {zone, name, type, value, state: ok | missing | other | unknown, now}."""
    from email.utils import parseaddr

    from .appliance import dkim_domain, dkim_public, dkim_selector

    node_list = node_list if node_list is not None else nodes()
    ips = {k: v for k, v in (s.mail_public_ips or {}).items() if v}
    domain = dkim_domain(s)
    out = []
    for nd in node_list:
        ip, mn = ips.get(nd["site"], ""), nd["mail_name"]
        if mn and ip:
            out.append({"zone": mn.split(".", 1)[-1], "name": mn, "type": "A", "value": ip})
    if domain:
        mine = " ".join(f"ip4:{ips[nd['site']]}" for nd in node_list if ips.get(nd["site"]))
        if mine:
            out.append({"zone": domain, "name": domain, "type": "TXT", "value": f"v=spf1 {mine} -all", "spf": True})
        pub = dkim_public(s)
        if pub:
            out.append(
                {
                    "zone": domain,
                    "name": f"{dkim_selector(s)}._domainkey.{domain}",
                    "type": "TXT",
                    "value": f"v=DKIM1; k=rsa; p={pub}",
                }
            )
        sender = parseaddr(s.mail_from or "")[1]
        rua = f"; rua=mailto:{sender}" if sender else ""
        out.append(
            {
                "zone": domain,
                "name": f"_dmarc.{domain}",
                "type": "TXT",
                "value": f"v=DMARC1; p=quarantine{rua}",
                "dmarc": True,
            }
        )
    for nd in node_list:
        ip, mn = ips.get(nd["site"], ""), nd["mail_name"]
        if ip and mn:
            out.append({"zone": reverse_zone(ip), "name": reverse_name(ip), "type": "PTR", "value": mn})
    if check:
        with ThreadPoolExecutor(max_workers=8) as ex:
            found = list(ex.map(lambda r: _answer(r), out))
        for r, (have, external, st) in zip(out, found, strict=True):
            r["now"], r["external"], r["state"] = have, external, st
    return out


def _answer(rec):
    """(answers, from external DNS, state): the node's own DNS first; when what it answers leaves the record missing
    (no answer, or only other records - commit-404: e.g. a site-verification TXT in the local copy of the zone),
    external DNS (owner 11:48). A local record with another value stays "other": the node's DNS keeps precedence."""
    have = query(rec["name"], rec["type"])
    st = state(rec, have)
    if st != "missing":
        return have, False, st
    ext = query(rec["name"], rec["type"], servers=EXTERNAL)
    ext_st = state(rec, ext) if ext else "missing"
    if ext_st == "missing":
        return have, False, st
    return ext, True, ext_st


def _tags(text):
    """The tags of a DKIM or DMARC record: {"v": "DKIM1", "p": "MIIB...", ...} (spaces and splitting ignored)."""
    out = {}
    for part in text.replace(" ", "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.lower()] = v
    return out


def _spf_passes(record, ip):
    """True when the SPF record certainly passes mail from ip: the mechanisms are taken in order and the first one
    that matches decides by its qualifier (commit-041). ip4: (with or without a prefix length) and all are evaluated;
    any other mechanism (a, mx, include, exists, ptr, ip6) is not looked up here - before the address is decided it
    might match, or end the evaluation with an error (an include of a domain without SPF is a permerror, whatever its
    qualifier), so the answer is no: check by hand. Modifiers are skipped; with no match, no."""
    addr = ipaddress.ip_address(ip)
    for term in record.split()[1:]:
        q, mech = (term[0], term[1:]) if term[:1] in "+-~?" else ("+", term)
        name = mech.split(":", 1)[0].split("/", 1)[0].lower()
        if "=" in mech.split(":", 1)[0]:  # a modifier (redirect=, exp=): not a mechanism
            continue
        if name == "all":
            return q == "+"
        if name == "ip4":
            try:
                if addr in ipaddress.ip_network(mech.split(":", 1)[1], strict=False):
                    return q == "+"
            except ValueError:
                return False
            continue
        return False  # a, mx, include, exists, ptr, ip6: not evaluated here, and any of them may decide or fail first
    return False


def state(rec, have):
    """ok | missing | other: whether DNS has what the record needs (2026-11-17, the owner on production: "all
    records are valid" - a record is right when it does the job, not only when it is written as the page writes it).
    SPF: one v=spf1 record that passes every node's address, its mechanisms taken in order (commit-041); DKIM: a record with the same key (p=), whatever other
    tags it has; DMARC: any v=DMARC1 record (the policy is the provider's choice); A and PTR: the value among the
    answers."""
    if rec["type"] == "TXT" and rec.get("spf"):
        spf = [h for h in have if h.lower().split()[:1] == ["v=spf1"]]
        if not spf:
            return "missing"
        if len(spf) > 1:  # commit-041: two SPF records are an error (permerror) - a domain may have one
            return "other"
        need = [x[4:] for x in rec["value"].split() if x.startswith("ip4:")]
        return "ok" if all(_spf_passes(spf[0], ip) for ip in need) else "other"
    if rec["type"] == "TXT" and rec.get("dmarc"):
        return "ok" if any(h.replace(" ", "").lower().startswith("v=dmarc1") for h in have) else "missing"
    if rec["type"] == "TXT":  # DKIM
        keys = [_tags(h).get("p", "") for h in have if _tags(h).get("v", "").lower() == "dkim1"]
        if not keys:
            return "missing"
        return "ok" if _tags(rec["value"]).get("p", "") in keys else "other"
    vals = [h.rstrip(".").lower() for h in have]
    return "missing" if not vals else ("ok" if rec["value"].lower() in vals else "other")


def summary(recs):
    missing = sum(1 for r in recs if r.get("state") == "missing")
    other = sum(1 for r in recs if r.get("state") == "other")
    return missing, other
