"""Health of the hosts, from their IPMI (2026-10-29), read once a day (`manage.py check_platforms`, systemd timer every
5 minutes: a host is read when its last reading is older than a day) and with Read hardware.

A host is healthy when:
- its boot device (the boot volume) is OK;
- every network port its server profile uses (the ports of the NIC aliases: 4, or 2 with one fabric) has its link up and
  no error;
- its CPUs, GPUs and memory report no error.
The BIOS and the IPMI firmware versions are kept too.
"""
from datetime import timedelta

from django.utils import timezone

PERIOD = timedelta(hours=23, minutes=30)
UP = {"LinkUp", "Up"}


def expected_ports(profile):
    """[(alias, port name, slot, index)] the profile's NIC aliases use."""
    if profile is None:
        return []
    from .profiles import card_links
    by_name = {}
    for c in profile.cards:
        for i, name in enumerate(card_links(c)):
            by_name[name] = (str(c.get("slot") or ""), i)
    out = []
    a = profile.assign
    for alias in profile.aliases_needed():
        port = a.get(alias) or ""
        slot, idx = by_name.get(port, ("", -1))
        out.append((alias, port, slot, idx))
    return out


def evaluate(host, data):
    """The verdict on what the IPMI said: {ok, problems, ports, …} (data kept with it)."""
    problems = []
    boot = data.get("boot") or {}
    if not boot:
        problems.append("boot device: not found")
    elif boot.get("health") and boot["health"] != "OK":
        problems.append(f"boot device: {boot['health']}")
    elif not boot.get("health") and boot.get("controller_health") not in ("", "OK"):
        problems.append(f"boot controller: {boot['controller_health']}")
    ports = []
    exp = expected_ports(host.profile)
    if not exp:
        problems.append("network: no server profile with its NIC aliases")
    for alias, port, slot, idx in exp:
        st = ((data.get("ports") or {}).get(slot) or {}).get(str(idx)) if port else None
        row = {"alias": alias, "port": port, "slot": slot, "link": (st or {}).get("link", ""),
               "health": (st or {}).get("health", ""), "speed": (st or {}).get("speed")}
        if not port:
            row["problem"] = "not assigned in the profile"
        elif st is None:
            row["problem"] = "not reported by the IPMI"
        elif st.get("link") not in UP:
            row["problem"] = f"link {st.get('link') or 'unknown'}"
        elif st.get("health") not in ("", "OK"):
            row["problem"] = st["health"]
        if row.get("problem"):
            problems.append(f"{alias} ({port or '—'}): {row['problem']}")
        ports.append(row)
    procs = data.get("processors") or []
    if not any(p.get("type", "CPU").upper() == "CPU" for p in procs):
        if data.get("cpu_summary") not in ("", "OK") or not data.get("cpu_summary"):
            problems.append(f"CPU: {data.get('cpu_summary') or 'not reported'}")
    for p in procs:
        if p.get("health") not in ("", "OK"):
            problems.append(f"{p.get('type') or 'CPU'} {p.get('id')}: {p.get('health')}")
    if data.get("memory") not in ("", "OK"):
        problems.append(f"memory: {data['memory']}")
    gpus = [p for p in procs if p.get("type", "").upper() == "GPU"]
    return {"ok": not problems, "problems": problems, "ports": ports, "bios": data.get("bios", ""),
            "ipmi_fw": data.get("ipmi_fw", ""), "ipmi_model": data.get("ipmi_model", ""), "power": data.get("power", ""),
            "memory": data.get("memory", ""), "boot": boot,
            "cpus": [p for p in procs if p.get("type", "CPU").upper() != "GPU"], "gpus": gpus,
            "at": timezone.now().isoformat()}


def store(host, result):
    host.health, host.health_at = result, timezone.now()
    host.save(update_fields=["health", "health_at"])


def check(hosts):
    """Read the IPMI of the hosts now (in parallel) and keep each verdict. Returns {name: ok}."""
    from . import redfish
    hosts = list(hosts)
    got = redfish.for_hosts(hosts, lambda c, _h: c.health())
    out = {}
    for h in hosts:
        r = got.get(h.name) or {"error": "no answer"}
        if "error" in r:
            res = {"ok": False, "problems": [f"IPMI: {r['error']}"[:300]], "error": True, "at": timezone.now().isoformat()}
            prev = h.health or {}
            for k in ("bios", "ipmi_fw", "ipmi_model"):  # what was known stays
                if prev.get(k):
                    res[k] = prev[k]
        else:
            res = evaluate(h, r)
        store(h, res)
        out[h.name] = res["ok"]
    return out


def due(force=False):
    from .models import Host
    qs = Host.objects.select_related("profile", "site").exclude(state="retired").exclude(ipmi_host="")
    if force:
        return list(qs)
    limit = timezone.now() - PERIOD
    return [h for h in qs if h.health_at is None or h.health_at < limit]


def check_due(force=False):
    hosts = due(force)
    return check(hosts) if hosts else {}
