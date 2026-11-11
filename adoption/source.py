"""Read the old system's tenant data into one normalized shape.

Shape (per tenant name):
    {
      "records": [{"site", "cluster", "cpu", "ram_gb", "gpu", "disk_gb", "images_gb", "files_gb", "backups_gb"}],
      "sites": {site: {"connection_type", "inet_mbps", "inet_main_prefix", "corpnet_prefixes", "l2_vlans",
                       "inet_main_subnet"}},
      "hints": {"legal_name", "status", "contacts", "manager_email"},
    }
Trusted: the tenant list, resources and the internet subnet. Everything in
"hints" only helps staff fill the gaps. Start/End and State are ignored.
"""
import csv
import io
import ipaddress
import re

CSV_COLUMNS = {
    "tenant": "Тенант", "site": "Site", "cluster": "Clusters",
    "cpu": "CPU", "ram_gb": "RAM", "gpu": "GPU", "disk_gb": "Storage",
    "images_gb": "Images", "files_gb": "Files", "backups_gb": "Backups",
    "mbps": "Mbps", "inet_pfx": "Inet pfx", "corp_pfx": "Corp pfx", "l2_vlan": "L2 VLAN", "subnet": "Subnet",
    "legal_name": "Юр. лицо", "status": "Статус", "contacts": "Контакты", "manager": "Менеджер",
}
REQUIRED = ("tenant", "site", "cluster", "cpu", "ram_gb")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SourceError(Exception):
    pass


def _int(value, default=None):
    v = (value or "").strip()
    if v == "":
        return default
    try:
        return int(float(v.replace(",", ".")))
    except ValueError:
        raise ValueError(f"not a number: {value!r}")


def parse_csv(data: bytes):
    """Return (tenants, problems)."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceError("the file is not UTF-8") from exc
    rows = list(csv.reader(io.StringIO(text)))
    header_idx = next((i for i, r in enumerate(rows) if CSV_COLUMNS["tenant"] in [c.strip() for c in r]), None)
    if header_idx is None:
        raise SourceError(f"no header row with a {CSV_COLUMNS['tenant']!r} column")
    header = [c.strip() for c in rows[header_idx]]
    idx = {}
    for key, title in CSV_COLUMNS.items():
        if title in header:
            idx[key] = header.index(title)
    missing = [CSV_COLUMNS[k] for k in REQUIRED if k not in idx]
    if missing:
        raise SourceError(f"missing columns: {', '.join(missing)}")

    raw_rows = []
    for n, r in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        if not any(c.strip() for c in r):
            continue
        get = lambda k: (r[idx[k]].strip() if k in idx and idx[k] < len(r) else "")  # noqa: E731
        raw_rows.append((n, {k: get(k) for k in CSV_COLUMNS}))
    return normalize(raw_rows)


def normalize(raw_rows):
    """raw_rows: [(line, {column key: text})] -> (tenants, problems)."""
    tenants, problems = {}, []

    def problem(line, tenant, text):
        problems.append({"line": line, "tenant": tenant, "text": text})

    for line, r in raw_rows:
        name = r["tenant"].strip().lower()
        if not name:
            problem(line, "", "empty tenant name")
            continue
        if not NAME_RE.match(name):
            problem(line, name, "tenant name is not a valid short name")
        site, cluster = r["site"].strip().lower(), r["cluster"].strip().lower()
        if not site or not cluster:
            problem(line, name, "site or cluster missing")
            continue
        t = tenants.setdefault(name, {"records": [], "sites": {}, "hints": {}, "lines": []})
        t["lines"].append(line)
        try:
            # the old system keeps files apart from the storage; the portal has one storage number with
            # images and files as parts of it (unset: the old system's defaults, 1 GB files and 10 GB images)
            storage, files, images = _int(r["disk_gb"], 0), _int(r["files_gb"]), _int(r["images_gb"])
            files = 1 if files is None else files
            images = 10 if images is None else images
            rec = {
                "site": site, "cluster": cluster,
                "cpu": _int(r["cpu"], 0), "ram_gb": _int(r["ram_gb"], 0), "gpu": _int(r["gpu"], 0),
                "disk_gb": storage + files, "images_gb": images,
                "files_gb": files, "backups_gb": _int(r["backups_gb"]),
            }
            mbps, inet_pfx, corp_pfx = _int(r["mbps"]), _int(r["inet_pfx"]), _int(r["corp_pfx"])
        except ValueError as exc:
            problem(line, name, str(exc))
            continue
        if any(x["site"] == site and x["cluster"] == cluster for x in t["records"]):
            problem(line, name, f"duplicate record {site}/{cluster}")
            continue
        t["records"].append(rec)

        subnet_text = r["subnet"].strip()
        subnet = None
        if subnet_text and subnet_text.upper() not in ("CORP FW", "NONE", "-"):
            try:
                subnet = str(ipaddress.ip_network(subnet_text, strict=False))
            except ValueError:
                problem(line, name, f"subnet {subnet_text!r} is not a valid network")
        if corp_pfx or subnet_text.upper() == "CORP FW":
            ctype = "corpnet"
        elif inet_pfx or subnet:
            ctype = "inet"
        elif r["l2_vlan"]:
            ctype = "none"
        else:
            ctype = ""
        conn = {
            "connection_type": ctype,
            "inet_mbps": mbps if ctype == "inet" else None,
            "inet_main_prefix": inet_pfx if ctype == "inet" else None,
            "corpnet_prefixes": str(corp_pfx) if corp_pfx else "",
            "l2_vlans": " ".join(r["l2_vlan"].replace(",", " ").split()),
            "inet_main_subnet": subnet if ctype == "inet" else None,
        }
        if ctype == "inet" and subnet and inet_pfx and ipaddress.ip_network(subnet).prefixlen != inet_pfx:
            problem(line, name, f"subnet {subnet} does not match prefix length /{inet_pfx}")
        if ctype == "inet" and not subnet:
            problem(line, name, "internet connection without a subnet")
        prev = t["sites"].get(site)
        if prev is None:
            t["sites"][site] = conn
        else:
            for k, v in conn.items():
                if v in (None, ""):
                    continue
                if prev.get(k) in (None, ""):
                    prev[k] = v
                elif prev[k] != v:
                    problem(line, name, f"{site}: {k} differs between rows ({prev[k]} / {v})")

        hints = t["hints"]
        for key in ("legal_name", "status", "contacts"):
            v = " ".join(r[key].split())
            if v and v not in ("-", "?"):
                if hints.get(key) and hints[key] != v:
                    if key != "contacts":
                        problem(line, name, f"{key} differs between rows")
                    continue
                hints[key] = v
        manager = r["manager"].strip().lower()
        if manager:
            if EMAIL_RE.match(manager):
                if hints.get("manager_email") and hints["manager_email"] != manager:
                    problem(line, name, "manager differs between rows")
                hints.setdefault("manager_email", manager)
            elif manager not in ("-", "?"):
                problem(line, name, f"manager {r['manager']!r} is not an email")
    for t in tenants.values():
        t["records"].sort(key=lambda x: (x["site"], x["cluster"]))
    return dict(sorted(tenants.items())), problems


# Columns of the old system's `tenants` table, mapped to the CSV column keys used by normalize().
SQLITE_COLUMNS = {
    "tenant": "tenant", "site": "site", "cluster": "clusters",
    "cpu": "cpu_n", "ram_gb": "ram_gb", "gpu": "gpu_n", "disk_gb": "storage_gb",
    "images_gb": "images_gb", "files_gb": "files_gb", "backups_gb": "backups_gb",
    "mbps": "inet_mbps", "inet_pfx": "inet_prefixes", "corp_pfx": "corpnet_prefixes", "l2_vlan": "l2_vlans",
    "subnet": "inet_subnet", "legal_name": "legal_entity", "status": "status", "contacts": "client_contacts",
    "manager": "sale",
}
