"""Read tenant groups from OpenNebula over XML-RPC. Read-only.

Uses the standard library XML-RPC client, so it works with any OpenNebula
version that has one.group.info and one.grouppool.info.
"""
import http.client
import xmlrpc.client
import xml.etree.ElementTree as ET

from django.utils import timezone

from .models import Observation, PlatformStats

HOST_ON, HOST_ERROR = {0, 1, 2, 6}, {3, 5}  # everything else (disabled, offline) counts as off
VM_ACTIVE, VM_RUNNING_LCM, VM_POWEROFF = 3, 3, 8

TIMEOUT = 20


class OneError(Exception):
    pass


class _TimeoutTransport(xmlrpc.client.Transport):
    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = TIMEOUT
        return conn


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = TIMEOUT
        return conn


def xml_to_dict(elem):
    """OpenNebula XML to plain data: repeated tags become lists, leaves become strings."""
    children = list(elem)
    if not children:
        return (elem.text or "").strip()
    out = {}
    for child in children:
        value = xml_to_dict(child)
        if child.tag in out:
            if not isinstance(out[child.tag], list):
                out[child.tag] = [out[child.tag]]
            out[child.tag].append(value)
        else:
            out[child.tag] = value
    return out


class OneClient:
    def __init__(self, url, session):
        transport = _TimeoutSafeTransport() if url.startswith("https") else _TimeoutTransport()
        self.proxy = xmlrpc.client.ServerProxy(url, transport=transport, allow_none=True)
        self.session = session

    def _call(self, method, *args):
        fn = self.proxy
        for part in method.split("."):
            fn = getattr(fn, part)
        try:
            result = fn(self.session, *args)
        except (OSError, http.client.HTTPException, xmlrpc.client.Error) as exc:
            raise OneError(f"{method}: {exc}") from exc
        if not result[0]:
            raise OneError(f"{method}: {result[1]}")
        return result[1]

    def version(self):
        return self._call("one.system.version")

    def groups(self):
        root = ET.fromstring(self._call("one.grouppool.info"))
        return [(int(g.findtext("ID")), g.findtext("NAME")) for g in root.findall("GROUP")]

    def group(self, gid):
        return xml_to_dict(ET.fromstring(self._call("one.group.info", gid, False)))

    def vms(self):
        """[(group name, state, lcm state)] of all VMs that are not done."""
        root = ET.fromstring(self._call("one.vmpool.info", -2, -1, -1, -1))
        return [(v.findtext("GNAME") or "", int(v.findtext("STATE") or -1), int(v.findtext("LCM_STATE") or -1))
                for v in root.findall("VM")]

    def users(self):
        """{user id: {"id", "name", "email", "group"}} of every OpenNebula user."""
        root = ET.fromstring(self._call("one.userpool.info"))
        out = {}
        for u in root.findall("USER"):
            uid = int(u.findtext("ID") or -1)
            out[uid] = {"id": uid, "name": u.findtext("NAME") or "", "group": u.findtext("GNAME") or "",
                        "email": (u.findtext("TEMPLATE/EMAIL") or "").strip().lower(),
                        "enabled": (u.findtext("ENABLED") or "1") == "1"}
        return out

    def address_ranges(self):
        """[(network name, first IPv4 address, size)] of every address range of every virtual network."""
        root = ET.fromstring(self._call("one.vnpool.info", -2, -1, -1))
        out = []
        for v in root.findall("VNET"):
            for ar in v.findall("AR_POOL/AR"):
                if ar.findtext("IP"):
                    out.append([v.findtext("NAME") or "", ar.findtext("IP"), int(ar.findtext("SIZE") or 0)])
        return out

    def datastore_usage(self):
        """[{id, name, type, total_mb, used_mb, free_mb, clusters}] of every datastore (read-only); clusters by name."""
        root = ET.fromstring(self._call("one.datastorepool.info"))
        try:
            names = {c.findtext("ID"): c.findtext("NAME") or "" for c in
                     ET.fromstring(self._call("one.clusterpool.info")).findall("CLUSTER")}
        except Exception:  # noqa: BLE001 - membership is extra information
            names = None
        types = {"0": "images", "1": "system", "2": "files", "3": "backups"}
        out = []
        for d in root.findall("DATASTORE"):
            row = {"id": int(d.findtext("ID") or -1), "name": d.findtext("NAME") or "", "type": types.get(d.findtext("TYPE") or "", ""),
                   "total_mb": int(d.findtext("TOTAL_MB") or 0), "used_mb": int(d.findtext("USED_MB") or 0),
                   "free_mb": int(d.findtext("FREE_MB") or 0)}
            if names is not None:
                row["clusters"] = sorted(names.get(x.text or "", x.text or "") for x in d.findall("CLUSTERS/ID"))
            out.append(row)
        return out

    def clusters(self):
        """{cluster id: name} of the platform's clusters."""
        return {c.findtext("ID"): c.findtext("NAME") or "" for c in
                ET.fromstring(self._call("one.clusterpool.info")).findall("CLUSTER")}

    def host_states(self):
        root = ET.fromstring(self._call("one.hostpool.info"))
        return [int(h.findtext("STATE") or -1) for h in root.findall("HOST")]


def is_tenant_group(name):
    from tenants.naming import GROUP_RE
    return bool(name) and (name.startswith("tenant-") or bool(GROUP_RE.match(name)))


def collect_stats(one, control, now):
    vms = {"total": 0, "running": 0, "poweroff": 0, "other": 0, "tenant_total": 0, "tenant_running": 0, "by_group": {}}
    hosts = {"total": 0, "on": 0, "error": 0, "off": 0}
    errors = []
    try:
        for gname, state, lcm in one.vms():
            running = state == VM_ACTIVE and lcm == VM_RUNNING_LCM
            bucket = "running" if running else "poweroff" if state == VM_POWEROFF else "other"
            vms["total"] += 1
            vms[bucket] += 1
            if is_tenant_group(gname):
                vms["tenant_total"] += 1
                vms["tenant_running"] += running
                g = vms["by_group"].setdefault(gname, {"total": 0, "running": 0})
                g["total"] += 1
                g["running"] += running
    except OneError as exc:
        errors.append(str(exc))
    try:
        for st in one.host_states():
            hosts["total"] += 1
            hosts["on" if st in HOST_ON else "error" if st in HOST_ERROR else "off"] += 1
    except OneError as exc:
        errors.append(str(exc))
    defaults = {"collected_at": now, "vms": vms, "hosts": hosts}
    try:
        defaults["address_ranges"] = one.address_ranges()
    except OneError as exc:
        errors.append(str(exc))
    try:
        defaults["datastores"] = one.datastore_usage()
    except OneError as exc:
        errors.append(str(exc))
    try:
        defaults["clusters"] = one.clusters()
    except OneError as exc:
        errors.append(str(exc))
    defaults["error"] = "; ".join(errors)[:2000]
    PlatformStats.objects.update_or_create(platform_key=control.key, defaults=defaults)


def client_for(control):
    from inventory.secrets import SecretsError
    try:
        url, auth = control.credentials()
    except SecretsError as exc:
        raise OneError(str(exc))
    if not url or auth.endswith(":"):
        raise OneError(f"{control.key}: endpoint or credentials not set")
    return OneClient(url, auth)


def collect(control):
    """Replace the observations of one platform. Returns the number of tenant groups seen."""
    one = client_for(control)
    now = timezone.now()
    seen = []
    try:
        all_users = one.users()
    except OneError:
        all_users = {}
    for gid, name in one.groups():
        if not is_tenant_group(name):
            continue
        g = one.group(gid)
        template = g.get("TEMPLATE") if isinstance(g.get("TEMPLATE"), dict) else {}
        quotas = {k: g.get(k) for k in ("VM_QUOTA", "DATASTORE_QUOTA", "NETWORK_QUOTA", "IMAGE_QUOTA") if g.get(k)}
        ids = g.get("USERS", {}).get("ID", []) if isinstance(g.get("USERS"), dict) else []
        ids = ids if isinstance(ids, list) else [ids]
        admins = g.get("ADMINS", {}).get("ID", []) if isinstance(g.get("ADMINS"), dict) else []
        admins = {int(a) for a in (admins if isinstance(admins, list) else [admins]) if str(a).isdigit()}
        users = [{**all_users.get(int(i), {"id": int(i), "name": "", "email": ""}), "admin": int(i) in admins}
                 for i in ids if str(i).isdigit()]
        Observation.objects.update_or_create(
            platform_key=control.key, group_id=gid,
            defaults={"group_name": name, "template": template, "quotas": quotas,
                      "users": users, "collected_at": now},
        )
        seen.append(gid)
    Observation.objects.filter(platform_key=control.key).exclude(group_id__in=seen).delete()
    collect_stats(one, control, now)
    control.last_collected_at = now
    control.last_error = ""
    control.save(update_fields=["last_collected_at", "last_error"])
    return len(seen)


def ensure_controls():
    """Every inventory platform has a control row: shared platforms are collected by default, others not."""
    from inventory.models import Platform, PlatformControl
    known = set(PlatformControl.objects.values_list("key", flat=True))
    for p in Platform.objects.exclude(key__in=known):
        PlatformControl.objects.create(key=p.key, collect_enabled=p.kind == "shared")


def collect_stats_only():
    """Owner 2026-10-29 21:10: the numbers (datastores, hosts, VMs) of the platforms not read for tenants (management,
    portal) too — the dashboard shows every datastore. Read only. Returns {key: "ok" | "error: …"}."""
    from inventory.models import PlatformControl
    out = {}
    for c in PlatformControl.objects.filter(collect_enabled=False):
        if not c.configured:
            continue
        try:
            collect_stats(client_for(c), c, timezone.now())
            out[c.key] = "ok"
        except Exception as exc:  # noqa: BLE001 - one platform's error does not stop the rest
            out[c.key] = f"error: {exc}"[:200]
    return out


def collect_all():
    from inventory.models import PlatformControl

    ensure_controls()
    results = {}
    for c in PlatformControl.objects.filter(collect_enabled=True):
        if not c.configured:
            continue
        try:
            results[c.key] = collect(c)
        except Exception as exc:  # noqa: BLE001 - recorded on the control
            c.last_error = str(exc)[:2000]
            c.save(update_fields=["last_error"])
            results[c.key] = f"error: {exc}"
    return results
