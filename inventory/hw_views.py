"""Hardware through the iLOs (Redfish): unify the IPMI settings of a site's hosts."""
from collections import Counter

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts.models import Role, audit
from accounts.utils import staff_required

from . import ops, redfish
from .models import Host, ServerProfile, Site

def _ipmi_refs():
    """IPMI users as references: [(reference, secret)]."""
    from .models import Secret
    return [(x.ref, x) for x in Secret.objects.filter(kind=Secret.Kind.USER, state=Secret.State.ACTIVE).order_by("name")
            if x.has("password")]


@staff_required(Role.SUPPORT)
def site_ipmi_view(request, code):
    """IPMI settings of a site: the same user, NTP, DNS and time zone on every iLO."""
    site = get_object_or_404(Site, code=code)
    hosts = list(Host.objects.filter(site=site).exclude(state="retired").order_by("name"))
    refs = _ipmi_refs()
    want_user = (site.ipmi or {}).get("user") or next((h.ipmi_user for h in hosts if h.ipmi_user), "")
    tz = site.tz  # the IPMI takes its UTC offset (redfish.utc_offset)
    offset = redfish.utc_offset(tz)
    desired = {"user": want_user, "ntp": site.ntp_servers, "dns": site.dns_servers, "timezone": f"{tz} (UTC{offset})"}
    action = request.POST.get("action") if request.method == "POST" else ""
    results = {}
    if action == "save" and request.user.is_super_admin:
        user_ref = request.POST.get("user") or want_user
        try:
            ops.site_ipmi_save(request.user, code, {"user": user_ref, "ntp": "site", "dns": "site", "timezone": tz},
                                  {h.name: {"user": user_ref} for h in hosts})
            messages.success(request, _("IPMI user set for all hosts of %(s)s.") % {"s": code})
        except ops.EditError as exc:
            messages.error(request, str(exc))
        return redirect("infra_site_ipmi", code=code)
    if action in ("check", "apply"):
        picked = set(request.POST.getlist("hosts")) if action == "apply" else None
        targets = [h for h in hosts if picked is None or h.name in picked]

        def work(c, h):
            out = {**c.manager()}
            if action == "apply":
                if not request.user.is_super_admin:
                    raise redfish.RedfishError("only the super admin applies settings")
                cur = c.settings()
                zone = None if cur.get("tz_offset") == offset else redfish.zone_for(cur.get("zones") or [], offset)
                out["applied"] = c.apply(ntp=desired["ntp"] or None, dns=desired["dns"] or None, timezone=zone)
            out.update(c.settings())
            return out
        results = redfish.for_hosts(targets, work)
        types = {n: r.get("type") for n, r in results.items() if r.get("type")}
        changed = {h.name: {"type": types[h.name]} for h in targets if types.get(h.name) and types[h.name] != h.ipmi_type}
        if changed and request.user.is_super_admin:
            try:
                ops.site_ipmi_save(request.user, code, None, changed)
            except ops.EditError:
                pass
        audit(request.user, f"ipmi_{action}", site, hosts=len(targets),
              errors=sum(1 for r in results.values() if "error" in r))
    rows = []
    for h in hosts:
        r = results.get(h.name)
        row = {"host": h, "r": r, "user_ok": h.ipmi_user == want_user}
        if r and "error" not in r:
            row["ntp_ok"] = not desired["ntp"] or r.get("ntp") == desired["ntp"]
            row["dns_ok"] = not desired["dns"] or r.get("dns") == desired["dns"]
            row["tz_ok"] = r.get("tz_offset") == offset
            row["ok"] = row["ntp_ok"] and row["dns_ok"] and row["tz_ok"] and row["user_ok"]
        rows.append(row)
    return render(request, "inventory/site_ipmi.html", {
        "site": site, "rows": rows, "desired": desired, "refs": refs, "action": action,
        "can_edit": request.user.is_super_admin, "types": sorted(Counter(h.ipmi_type or "?" for h in hosts).items())})


# --- server profiles: the site's settings on every server's IPMI ----------------------------------------------

def site_settings_of(h):
    """What a host's IPMI should have from its site: NTP, DNS, the IPMI log collector, the time zone offset."""
    site = h.site
    tz = site.tz  # the IPMI takes its UTC offset (redfish.utc_offset)
    log = (site.log_collectors or {}).get("ipmi") or {}
    return {"ntp": list(site.ntp_servers or []), "dns": list(site.dns_servers or []),
            "syslog": {"host": log["host"], "port": int(log.get("port") or 514)} if log.get("host") else {},
            "offset": redfish.utc_offset(tz), "tz": tz}


def plan_site_settings(hosts):
    """{host: {"want", "now", "changes": [(what, now, planned)]} or {"error"}} read from each IPMI now."""
    wants = {h.name: site_settings_of(h) for h in hosts}
    got = redfish.for_hosts(hosts, lambda c, h: c.settings())
    out = {}
    for h in hosts:
        r, w = got.get(h.name) or {"error": "not read"}, wants[h.name]
        if "error" in r:
            out[h.name] = {"error": r["error"], "want": w}
            continue
        ch = []
        if w["ntp"] and r.get("ntp") != w["ntp"]:
            ch.append(("NTP", ", ".join(r.get("ntp") or []) or "—", ", ".join(w["ntp"])))
        if w["dns"] and r.get("dns") != w["dns"]:
            ch.append(("DNS", ", ".join(r.get("dns") or []) or "—", ", ".join(w["dns"])))
        now_log = r.get("syslog") or {}
        if (now_log.get("host"), now_log.get("port")) != (w["syslog"].get("host"), w["syslog"].get("port")):
            fmt = lambda x: f"{x['host']}:{x['port']}" if x.get("host") else "—"  # noqa: E731
            ch.append((_("log collector"), fmt(now_log), fmt(w["syslog"])))
        if r.get("tz_offset") != w["offset"]:
            ch.append((_("time zone"), f"{r.get('timezone') or ''} {r.get('tz_offset') or ''}".strip() or "—",
                       f"{w['tz']} (UTC{w['offset']})"))
        out[h.name] = {"want": w, "now": r, "changes": ch}
    return out


def _apply_task(host_names, user):
    def run(task):
        hosts = list(Host.objects.filter(name__in=host_names).select_related("site"))
        plan = plan_site_settings(hosts)
        todo = [h for h in hosts if plan.get(h.name, {}).get("changes")]
        task.step("plan", "ok", f"{len(todo)} of {len(hosts)} hosts to change")

        def work(c, h):
            w, now = plan[h.name]["want"], plan[h.name]["now"]
            zone = None if now.get("tz_offset") == w["offset"] else redfish.zone_for(now.get("zones") or [], w["offset"])
            return {"messages": c.apply(ntp=w["ntp"] or None, dns=w["dns"] or None, timezone=zone, syslog=w["syslog"])}
        done = redfish.for_hosts(todo, work) if todo else {}
        for name, r in done.items():
            task.step(name, "failed" if "error" in r else "ok", r.get("error") or ", ".join(r.get("messages") or []) or "applied")
        failed = [n for n, r in done.items() if "error" in r]
        audit(user, "site_settings_applied", None, hosts=len(todo), failed=len(failed))
        if failed:
            raise RuntimeError(f"{len(failed)} of {len(todo)} hosts failed: {', '.join(failed[:5])}")
        return {"changed": len(todo), "hosts": len(hosts)}
    return run


@staff_required(Role.SUPPORT)
def profile_site_settings_view(request, code):
    """Server profiles → Apply site settings: the planned changes of every host's IPMI, then apply them as a task."""
    from accounts import tasks
    p = get_object_or_404(ServerProfile, code=code)
    hosts = list(p.hosts.exclude(state="retired").select_related("site").order_by("name"))
    if request.method == "POST":
        if not request.user.is_super_admin:
            raise PermissionDenied
        names = [h.name for h in hosts if h.name in set(request.POST.getlist("hosts"))]
        if not names:
            messages.warning(request, _("Choose the hosts to change."))
            return redirect("infra_profile_site_settings", code=code)
        t = tasks.start("site settings", _("Site settings on the IPMI of %(p)s: %(n)s hosts") % {"p": code, "n": len(names)},
                        _apply_task(names, request.user), user=request.user, target=p)
        messages.success(request, _("Applying the site settings: see Tasks."))
        return redirect(reverse("tasks") + f"?sel=task-{t.pk}")
    plan = plan_site_settings(hosts)
    rows = [{"host": h, **plan.get(h.name, {})} for h in hosts]
    return render(request, "inventory/profile_site_settings.html", {"profile": p, "rows": rows,
                                                                     "can_apply": request.user.is_super_admin})
