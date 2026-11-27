"""Infrastructure → DNS (owner 2026-11-21 13:38-13:50, T34 B5): the tabs Zones and DNS servers (inventory/dns.py)."""

import ipaddress
import re

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from accounts.models import Role, audit
from accounts.utils import staff_required

from . import dns
from .models import DnsRecord, DnsSettings, DnsZone


def _ips(text, what):
    out = []
    for x in (text or "").replace(",", " ").split():
        try:
            out.append(str(ipaddress.ip_address(x)))
        except ValueError:
            raise forms.ValidationError(_("%(w)s: %(x)s is not an address.") % {"w": what, "x": x}) from None
    return out


def _nets(text, what):
    out = []
    for x in (text or "").replace(",", " ").split():
        try:
            out.append(str(ipaddress.ip_network(x, strict=False)))
        except ValueError:
            raise forms.ValidationError(_("%(w)s: %(x)s is not a network.") % {"w": what, "x": x}) from None
    return out


class SettingsForm(forms.Form):
    forwarders = forms.CharField(label=_l("Forwarders"), required=False, help_text=_l("Addresses, space separated."))
    forward_zones = forms.CharField(
        label=_l("Forwarded zones"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "class": "mono"}),
        help_text=_l("One per line: zone: server addresses, e.g. corp.example: 10.83.128.249 10.83.128.250"),
    )
    sites = forms.CharField(
        label=_l("Views per site"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "class": "mono"}),
        help_text=_l(
            "One per line: site | internal networks | public networks, e.g. at1 | 10.61.0.0/16 | 85.116.182.0/24"
        ),
    )
    internal_extra = forms.CharField(
        label=_l("More internal networks"), required=False, help_text=_l("All sites; e.g. a corporate 10.0.0.0/8.")
    )
    notify_extra = forms.CharField(
        label=_l("More servers that may transfer the zones"), required=False, help_text=_l("Addresses.")
    )
    ttl = forms.IntegerField(label="TTL", min_value=30, max_value=604800)
    refresh = forms.IntegerField(label=_l("Refresh"), min_value=60, max_value=2419200)
    retry = forms.IntegerField(label=_l("Retry"), min_value=60, max_value=2419200)
    expire = forms.IntegerField(label=_l("Expire"), min_value=3600, max_value=31536000)
    minimum = forms.IntegerField(label=_l("Negative TTL"), min_value=30, max_value=86400)

    def clean_forwarders(self):
        return _ips(self.cleaned_data.get("forwarders"), _("Forwarders"))

    def clean_notify_extra(self):
        return _ips(self.cleaned_data.get("notify_extra"), _("Servers"))

    def clean_internal_extra(self):
        return _nets(self.cleaned_data.get("internal_extra"), _("Networks"))

    def clean_forward_zones(self):
        out = []
        for ln in (self.cleaned_data.get("forward_zones") or "").splitlines():
            if not ln.strip():
                continue
            name, _sep, rest = ln.partition(":")
            name = name.strip().strip(".").lower()
            if not dns.ZONE_NAME.match(name):
                raise forms.ValidationError(_("%(z)s is not a zone name.") % {"z": name})
            servers = _ips(rest, name)
            if not servers:
                raise forms.ValidationError(_("%(z)s: give its servers.") % {"z": name})
            out.append({"name": name, "servers": servers})
        return out

    def clean_sites(self):
        from .models import Site

        codes = set(Site.objects.values_list("code", flat=True))
        out = {}
        for ln in (self.cleaned_data.get("sites") or "").splitlines():
            if not ln.strip():
                continue
            parts = [p.strip() for p in ln.split("|")] + ["", ""]
            if parts[0] not in codes:
                raise forms.ValidationError(_("%(s)s is not a site.") % {"s": parts[0]})
            out[parts[0]] = {"internal": _nets(parts[1], parts[0]), "public": _nets(parts[2], parts[0])}
        return out


def settings_initial(st):
    s = dns.soa()
    return {
        "forwarders": " ".join(st.forwarders or []),
        "forward_zones": "\n".join(f"{z['name']}: {' '.join(z.get('servers') or [])}" for z in st.forward_zones or []),
        "sites": "\n".join(
            f"{c} | {' '.join(v.get('internal') or [])} | {' '.join(v.get('public') or [])}"
            for c, v in sorted((st.sites or {}).items())
        ),
        "internal_extra": " ".join(st.internal_extra or []),
        "notify_extra": " ".join(st.notify_extra or []),
        **{k: s[k] for k in ("ttl", "refresh", "retry", "expire", "minimum")},
    }


class ZoneForm(forms.Form):
    name = forms.CharField(label=_l("Zone"), max_length=253, help_text=_l("e.g. corp.example or 20.83.10.in-addr.arpa"))

    def clean_name(self):
        n = self.cleaned_data["name"].strip().strip(".").lower()
        if not dns.ZONE_NAME.match(n):
            raise forms.ValidationError(_("Not a zone name."))
        return n


class RecordForm(forms.Form):
    name = forms.CharField(
        label=_l("Name"), max_length=253, help_text=_l("Relative to the zone; @ for the zone itself.")
    )
    rtype = forms.ChoiceField(label=_l("Type"), choices=[(t, t) for t in DnsRecord.TYPES])
    value = forms.CharField(label=_l("Value"), max_length=1000)
    ttl = forms.IntegerField(label="TTL", required=False, min_value=30, max_value=604800)

    def clean_name(self):
        n = self.cleaned_data["name"].strip().rstrip(".")
        if not dns.NAME.match(n):
            raise forms.ValidationError(_("Not a record name."))
        return n

    def clean(self):
        d = super().clean()
        t, v = d.get("rtype"), (d.get("value") or "").strip()
        if not t or not v:
            return d
        if "\n" in v or "\r" in v or (";" in v and t != "TXT"):
            self.add_error("value", _("One line, no comments."))
        elif t == "A" and not dns._ip(v):
            self.add_error("value", _("An IPv4 address."))
        elif t == "AAAA":
            try:
                ipaddress.IPv6Address(v)
            except ValueError:
                self.add_error("value", _("An IPv6 address."))
        elif t in ("CNAME", "PTR", "NS") and not re.match(r"^[A-Za-z0-9_.-]+\.?$", v):
            self.add_error("value", _("A host name."))
        d["value"] = v
        return d


def _back(tab="zones", sel=None):
    return redirect(reverse("infra_dns") + f"?tab={tab}" + (f"&sel={sel}" if sel else ""))


@staff_required(Role.RESOURCE_MANAGER, Role.SUPPORT)
def dns_view(request):
    tab = request.GET.get("tab", "zones")
    tab = tab if tab in ("zones", "servers") else "zones"
    try:  # the automatic zones as the portal's records say (a standby node reads them as they are)
        dns.sync_zones()
    except Exception:  # noqa: BLE001
        pass
    st = DnsSettings.get()
    srv = dns.servers()
    auto, notes = dns.automatic()
    zones = list(DnsZone.objects.prefetch_related("records"))
    files = (st.last_check or {}).get("files") or {}
    for z in zones:
        z.rows = dns.rows(z, auto)
        z.auto_count = sum(1 for r in z.rows if not r["added"])
        z.added_count = len(z.rows) - z.auto_count
        z.state = dns.state(z, auto, srv)
        z.on_server = files.get(z.name, "")
        z.form = ZoneForm(initial={"name": z.name}, auto_id=f"id_dz_{z.pk}_%s")
        for r in z.rows:
            if r["added"] is not None:
                r["form"] = RecordForm(
                    initial={"name": r["name"], "rtype": r["rtype"], "value": r["value"], "ttl": r.get("ttl")},
                    auto_id=f"id_dr_{r['added'].pk}_%s",
                )
        z.record_form = RecordForm(auto_id=f"id_dr_new_{z.pk}_%s")
        if z.source.startswith("network:"):
            from .models import Network

            n = Network.objects.filter(pk=int(z.source.split(":")[1])).first()
            z.from_text = f"{n.site} {n.name} ({n.kind}) {n.cidr}" if n else z.source
    checked = (st.last_check or {}).get("servers") or {}
    names = [z.name for z in zones]
    # commit-327: the last check counts only for the configuration it saw (settings, servers, roles, addresses)
    current = dns.revision({"st": st, "soa": dns.soa(st), "domain": dns.domain(), "srv": srv})
    stale = (st.last_check or {}).get("revision") != current
    for x in srv:
        c = checked.get(x["host"]) or {}
        x["error"] = c.get("error", "")
        got = c.get("serials") or {}
        sers = {got[n] for n in names if got.get(n)}
        x["serial"] = ", ".join(sorted(sers)) or "—"
        # commit-327: in sync only with the applied serial for every zone that exists now
        x["unchecked"] = (
            names
            if stale or (c.get("role"), c.get("ip")) != (x["role"], x["ip"])
            else [n for n in names if n not in got]
        )
        x["halfway"] = c.get("halfway") or []
        x["unknown"] = bool(st.uncertain)  # commit-340: the primary changed - not green until an Apply succeeds
        x["in_sync"] = (
            not x["unknown"]
            and bool(c)
            and not x["error"]
            and not x["unchecked"]
            and not x["halfway"]
            and all(got[n] == str(st.serial) for n in names)
        )
    return render(
        request,
        "inventory/dns.html",
        {
            "tab": tab,
            "zones": zones,
            "notes": notes,
            "settings": st,
            "soa": dns.soa(),
            "domain": dns.domain(),
            "servers": srv,
            "primary": next((x for x in srv if x["role"] == "primary"), None),
            "stale_primary": dns.stale_primary(st, srv),  # commit-355
            "first_pending": dns.first_pending(st, srv),  # commit-358: named, with Release
            "settings_form": SettingsForm(initial=settings_initial(st)),
            "zone_form": ZoneForm(auto_id="id_dz_new_%s"),
            "can_edit": request.user.is_super_admin,
            "running": _running(),
        },
    )


def _running():
    from accounts.models import Task

    return Task.objects.filter(kind="dns", state__in=[Task.State.QUEUED, Task.State.RUNNING]).first()


def _errors(request, form):
    for f, errs in form.errors.items():
        label = form.fields[f].label if f in form.fields else ""
        messages.error(request, f"{label}: {' '.join(errs)}" if label else " ".join(errs))


@require_POST
@staff_required(super_only=True)
def settings_save_view(request):
    form = SettingsForm(request.POST)
    if not form.is_valid():
        _errors(request, form)
        return _back("servers")
    d = form.cleaned_data
    from django.db import transaction
    from django.utils import timezone

    fields = {
        "forwarders": d["forwarders"],
        "forward_zones": d["forward_zones"],
        "sites": d["sites"],
        "internal_extra": d["internal_extra"],
        "notify_extra": d["notify_extra"],
        "soa": {k: d[k] for k in ("ttl", "refresh", "retry", "expire", "minimum")},
    }
    with transaction.atomic():  # commit-332: the form's own fields only - a claim, a publication or a serial written
        st = DnsSettings.objects.select_for_update().get(pk=DnsSettings.get().pk)  # meanwhile stays as it is
        DnsSettings.objects.filter(pk=st.pk).update(**fields, updated_at=timezone.now())
    audit(request.user, "dns_settings_saved", None)
    messages.success(request, _("DNS servers saved; Apply puts them on the servers."))
    return _back("servers")


@require_POST
@staff_required(super_only=True)
def zone_save_view(request, pk=None):
    z = get_object_or_404(DnsZone, pk=pk) if pk else None
    if z is not None and z.automatic:
        messages.error(request, _("An automatic zone is renamed where it comes from."))
        return _back(sel=f"dz-{z.pk}")
    form = ZoneForm(request.POST)
    if not form.is_valid():
        _errors(request, form)
        return _back(sel=f"dz-{z.pk}" if z else None)
    name = form.cleaned_data["name"]
    if DnsZone.objects.filter(name=name).exclude(pk=z.pk if z else None).exists():
        messages.error(request, _("Zone %(z)s exists.") % {"z": name})
        return _back(sel=f"dz-{z.pk}" if z else None)
    kind = DnsZone.Kind.REVERSE if name.endswith(".in-addr.arpa") else DnsZone.Kind.DIRECT
    if z is None:
        z = DnsZone.objects.create(name=name, kind=kind, source="")
    else:
        z.name, z.kind = name, kind
        z.save(update_fields=["name", "kind"])
    audit(request.user, "dns_zone_saved", z, zone=name)
    messages.success(request, _("Zone saved; Apply puts it on the servers."))
    return _back(sel=f"dz-{z.pk}")


@require_POST
@staff_required(super_only=True)
def zone_delete_view(request, pk):
    z = get_object_or_404(DnsZone, pk=pk)
    if z.automatic:
        messages.error(request, _("An automatic zone goes when what it comes from goes."))
        return _back(sel=f"dz-{z.pk}")
    name = z.name
    z.delete()
    audit(request.user, "dns_zone_deleted", None, zone=name)
    messages.success(request, _("Zone deleted; Apply removes it from the servers."))
    return _back()


@require_POST
@staff_required(super_only=True)
def record_save_view(request, zone, pk=None):
    z = get_object_or_404(DnsZone, pk=zone)
    r = get_object_or_404(DnsRecord, pk=pk, zone=z) if pk else None
    form = RecordForm(request.POST)
    if not form.is_valid():
        _errors(request, form)
        return _back(sel=f"dz-{z.pk}")
    d = form.cleaned_data
    if r is None:
        r = DnsRecord(zone=z)
    r.name, r.rtype, r.value, r.ttl = d["name"], d["rtype"], d["value"], d.get("ttl")
    r.save()
    audit(request.user, "dns_record_saved", z, zone=z.name, name=r.name, type=r.rtype)
    messages.success(request, _("Record saved; Apply puts it on the servers."))
    return _back(sel=f"dz-{z.pk}")


@require_POST
@staff_required(super_only=True)
def record_delete_view(request, zone, pk):
    r = get_object_or_404(DnsRecord, pk=pk, zone__pk=zone)
    audit(request.user, "dns_record_deleted", r.zone, zone=r.zone.name, name=r.name, type=r.rtype)
    r.delete()
    messages.success(request, _("Record deleted; Apply removes it from the servers."))
    return _back(sel=f"dz-{zone}")


def _start(request, title, fn, by):
    """commit-324: the DNS claim taken under the settings row's lock before the task exists; the task gives it back."""
    from accounts import tasks

    token = dns.claim(by)
    if token is None or _running():
        if token is not None:
            dns.release(token)
        messages.info(request, _("A DNS task is running; see Tasks."))
        return

    def run(task):
        try:
            return fn(task, token)
        finally:
            dns.release(token)

    try:
        tasks.start("dns", title, run, user=request.user)
    except Exception:
        dns.release(token)
        raise
    messages.success(request, _("Started; see Tasks."))


@require_POST
@staff_required(Role.SUPPORT)
def check_view(request):
    _start(request, _("Check the name servers"), lambda task, token: dns.check(task, token), "check")
    return _back("servers")


@require_POST
@staff_required(super_only=True)
def release_first_view(request):
    """commit-358 (owner 12:28): the operator states that no VM made for the pending primary-to-be can answer as a
    primary any more (typed its host name); another name server may then become the primary."""
    st = DnsSettings.get()
    pending = dns.first_pending(st, dns.servers(st))
    if pending is None:
        messages.info(request, _("No name server is becoming the primary."))
        return _back("servers")
    if request.POST.get("confirm") != pending["host"]:
        messages.error(request, _("Type its host name to confirm."))
        return _back("servers")
    try:  # commit-363: the host and the revision the page showed, checked again under the DNS claim
        dns.release_first(request.user, request.POST.get("confirm") or "", request.POST.get("rev") or "")
    except dns.DnsError as exc:
        messages.error(request, str(exc))
        return _back("servers")
    messages.success(request, _("Released: another name server may become the primary."))
    return _back("servers")


@require_POST
@staff_required(super_only=True)
def apply_view(request):
    if request.POST.get("confirm") != "apply":
        messages.error(request, _("Type apply to confirm."))
        return _back("servers")
    user = request.user
    _start(request, _("Apply the zones to the name servers"), lambda task, token: dns.apply(task, user, token), "apply")
    return _back("servers")
