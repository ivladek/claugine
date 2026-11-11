"""Infrastructure → Sites → Networks: site networks, their ranges, routes and BGP settings (the portal's records).

BGP of a network has up to three peer groups, each a list of hosts (generated from a range, then editable) and a
password kept in Secrets:

    rrs  route reflectors (addresses only)                          group rrs
    igw  internal gateways: tenant gateways on an exchange,         group egw (cgw on corpnet-ix);
         KVM hosts on EVPN                                          group hosts on EVPN
    egw  external gateways of an exchange (ix only), remote AS      group ext
"""
import ipaddress
import json

from django import forms
from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from accounts.utils import staff_required

from accounts.models import audit

from . import networks as nw
from .models import DelegatedSubnet, Network, NetworkRange, PeerGroup

NAME = r"^[a-z][a-z0-9-]{1,40}$"


class RowsTextarea(forms.Textarea):
    """A one-item-per-line textarea shown as a table of inputs (static/olist.js: textarea[data-rows]); labels are
    translated when the page is drawn."""

    def __init__(self, cols, add_label, sep=" ", attrs=None):
        super().__init__(attrs={"rows": 3, "class": "mono", **(attrs or {})})
        self.cols, self.add_label, self.sep = cols, add_label, sep

    def get_context(self, name, value, attrs):
        spec = []
        for c in self.cols:
            d = {"n": c[0], "l": str(c[1]), "p": c[2] if len(c) > 2 else ""}
            if len(c) > 3 and c[3]:
                d["w"] = c[3]
            if len(c) > 4 and c[4]:
                if c[4] == "@dynamic":
                    d["o"], d["dyn"] = [], True
                else:
                    d["o"] = list(c[4])
            spec.append(d)
        attrs = {**(attrs or {}), "data-rows": json.dumps(spec, ensure_ascii=False), "data-add-label": str(self.add_label),
                 "data-sep": self.sep}
        return super().get_context(name, value, attrs)


def rows_widget(field, cols, add_label, sep=" "):
    """Show a one-item-per-line field as a table of inputs."""
    keep = {k: v for k, v in field.widget.attrs.items() if k not in ("data-rows", "data-add-label", "data-sep")}
    field.widget = RowsTextarea(cols, add_label, sep, attrs=keep)


class NetworkForm(forms.Form):
    name = forms.RegexField(label=_l("Name"), regex=NAME,
                            help_text=_l("e.g. cloud-ix. It cannot be changed later."))
    kind = forms.ChoiceField(label=_l("Type"), required=False, choices=[("", _l("by the name"))] + Network.Kind.choices,
                             help_text=_l("core-services, core-cloud, core-vyos, core-s2s, core-kvm: one of each per site. "
                                          "ix and evpn (BGP) cannot be changed later."))
    interface = forms.RegexField(label=_l("Interface"), regex=r"^[a-z][a-z0-9.]{1,15}$", initial="bond0")
    bridge = forms.RegexField(label=_l("Bridge"), regex=r"^[a-z][a-z0-9_-]{0,15}$", required=False,
                              help_text=_l("e.g. vxlan; empty for none"))
    vlan = forms.IntegerField(label=_l("VLAN"), min_value=1, max_value=4094)
    mtu = forms.IntegerField(label=_l("MTU"), min_value=576, max_value=9216, initial=1500)
    subnet = forms.GenericIPAddressField(label=_l("Subnet"), protocol="IPv4")
    prefix = forms.IntegerField(label=_l("Prefix"), min_value=8, max_value=32)
    gateway = forms.GenericIPAddressField(label=_l("Gateway"), protocol="IPv4", required=False)
    state = forms.ChoiceField(label=_l("State"), choices=Network.State.choices, initial=Network.State.ACTIVE)
    routes = forms.CharField(label=_l("Routes"), required=False, widget=forms.Textarea(attrs={"rows": 4, "class": "mono"}),
                             help_text=_l("Subnet, next hop and metric (optional) of each route."))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        rows_widget(self.fields["routes"], [("subnet", _("Subnet"), "10.82.0.0/16"), ("nexthop", _("Next hop"), "10.82.101.1"),
                                            ("metric", _("Metric"), "75", "6em")], _("Route"))

    def clean(self):
        d = super().clean()
        net = None
        if d.get("subnet") and d.get("prefix"):
            try:
                net = ipaddress.ip_network(f"{d['subnet']}/{d['prefix']}", strict=True)
            except ValueError:
                self.add_error("subnet", _("%(s)s is not the first address of a /%(p)s.") % {"s": d["subnet"], "p": d["prefix"]})
        if net and d.get("gateway") and ipaddress.ip_address(d["gateway"]) not in net:
            self.add_error("gateway", _("The gateway must be inside the subnet."))
        routes = []
        for line in (d.get("routes") or "").splitlines():
            parts = line.replace(",", " ").split()
            if not parts:
                continue
            try:
                dest = str(ipaddress.ip_network(parts[0], strict=True))
                gw = str(ipaddress.ip_address(parts[1]))
                metric = int(parts[2]) if len(parts) > 2 else None
            except (ValueError, IndexError):
                self.add_error("routes", _("Not a route: %(l)s") % {"l": line.strip()})
                continue
            r = {"network": dest, "gw": gw}
            if metric is not None:
                r["metric"] = metric
            routes.append(r)
        d["route_list"] = routes
        return d


class RangeForm(forms.Form):
    name = forms.RegexField(label=_l("Name"), regex=r"^[a-z][a-z0-9_-]{0,31}$")
    start = forms.GenericIPAddressField(label=_l("Start address"), protocol="IPv4")
    length = forms.IntegerField(label=_l("Length"), min_value=1, max_value=65536)


def _back(site, name=None):
    url = reverse("infra_site", args=[site]) + "?tab=networks"
    return redirect(url + (f"&sel=n-{name}" if name else ""))


def _errors(request, errs):
    for e in errs:
        messages.error(request, e)




def _routes_text(routes):
    out = []
    for r in routes or []:
        if isinstance(r, dict) and r.get("network"):
            out.append(" ".join(str(x) for x in (r.get("network"), r.get("gw"), r.get("metric")) if x not in (None, "")))
    return "\n".join(out)


def context(site_code, platforms, can_edit):
    """What the Networks tab shows."""
    nets = list(Network.objects.filter(site=site_code).prefetch_related("ranges__peer_groups", "ranges__vnets__platform",
                                                                      "peer_groups__range", "vnets__platform"))
    pools = list(DelegatedSubnet.objects.filter(site__code=site_code))
    for n in nets:
        n.platforms = sorted({vn.platform.key for vn in n.vnets.all()})
        n.pools = [d for d in pools if d.segment == n.name]
        n.missing = nw.missing_bgp(n)
        n.routes = [r for r in (n.extra or {}).get("routes") or [] if isinstance(r, dict)]
        have = {g.name: g for g in n.peer_groups.all()}
        n.sections = []
        for sec in nw.bgp_sections(n):
            g = have.pop(sec["group"], None)
            hosts = (nw.expand(g.cidrs()) if sec["ip_only"] else g.cidrs()) if g else []
            n.sections.append(dict(sec, g=g, cidrs=g.cidrs() if g else [], hosts_text="\n".join(hosts)))
        n.other_groups = list(have.values())  # groups of older data beyond the three: shown, kept
        n.announced_text = "\n".join(n.announced or [])
        if n.kind == Network.Kind.IX:  # u10: connect the site's IP subnets (of this exchange's type) to its BGP
            from django.db.models import Q
            from .models import IPSubnet
            from .networks import subnet_purpose
            n.subnet_purpose = subnet_purpose(n.name)
            n.subnet_choices = list(IPSubnet.objects.filter(site__code=site_code, purpose=n.subnet_purpose)
                                    .filter(Q(network=n) | Q(network__isnull=True)).order_by("subnet"))
            for s in n.subnet_choices:
                s.connected = s.network_id == n.pk
        n.range_list = list(n.ranges.all())
        for r in n.range_list:
            r.ips, r.nets = " ".join(nw.range_hosts(r)), " ".join(r.cidrs())
            r.users = [g.name for g in r.peer_groups.all()] + [f"{vn.platform.key}/{vn.name}" for vn in r.vnets.all()]
        if can_edit:
            n.form = NetworkForm(initial={"name": n.name, "kind": n.kind, "interface": n.interface, "bridge": (n.extra or {}).get("bridge", ""),
                                          "vlan": n.vlan, "mtu": n.mtu, "subnet": n.subnet, "prefix": n.prefix,
                                          "gateway": n.gateway, "state": n.state, "routes": _routes_text(n.routes)},
                                 auto_id=f"id_nw_{n.pk}_%s")
            n.form.fields["name"].disabled = True
    from accounts.policy import policy_list
    return {"nets": nets, "net_form": NetworkForm(auto_id="id_nw_new_%s"), "range_form": RangeForm(auto_id="id_rg_new_%s"),
            "policy_list": policy_list("form")}


@require_POST
@staff_required(super_only=True)
def network_save_view(request, site, pk=None):
    n = get_object_or_404(Network, pk=pk, site=site) if pk else None
    form = NetworkForm(request.POST, initial={"name": n.name} if n else None)
    if n:
        form.fields["name"].disabled = True
    if not form.is_valid():
        _errors(request, [f"{k}: {' '.join(v)}" for k, v in form.errors.items()])
        return _back(site, n.name if n else None)
    d = form.cleaned_data
    name = n.name if n else d["name"]
    new = n is None
    if new and Network.objects.filter(site=site, name=name).exists():
        messages.error(request, _("%(n)s already exists on %(s)s.") % {"n": name, "s": site})
        return _back(site)
    net = ipaddress.ip_network(f"{d['subnet']}/{d['prefix']}")
    if n is not None:
        for r in n.ranges.all():
            if r.first not in net or r.last not in net:
                messages.error(request, _("Range %(r)s would be outside %(n)s.") % {"r": r.name, "n": net})
                return _back(site, name)
    kind = d.get("kind") or (n.kind if n else Network.kind_for(name))
    bgp = (Network.Kind.IX, Network.Kind.EVPN)
    if n is not None and kind != n.kind and (kind in bgp or n.kind in bgp):
        messages.error(request, _("The type of a BGP network (ix, evpn) cannot be changed, nor a network made one."))
        return _back(site, name)
    if kind in Network.ONE_PER_SITE:
        other = Network.objects.filter(site=site, kind=kind).exclude(pk=n.pk if n else None).first()
        if other is not None:
            messages.error(request, _("%(s)s already has a %(k)s network: %(o)s.") % {"s": site, "k": kind, "o": other.name})
            return _back(site, name if not new else None)
    n = n or Network(site=site, name=name, kind=kind)
    n.kind = kind
    extra = dict(n.extra or {})
    for k, v in (("bridge", d.get("bridge")), ("routes", d["route_list"])):
        if v:
            extra[k] = v
        else:
            extra.pop(k, None)
    n.interface, n.vlan, n.mtu = d["interface"], d["vlan"], d["mtu"]
    n.subnet, n.prefix, n.gateway, n.state = str(d["subnet"]), d["prefix"], (str(d["gateway"]) if d.get("gateway") else None), d["state"]
    n.extra, n.updated_by = extra, request.user
    errs = nw.check_network(n)
    if errs:
        _errors(request, errs)
        return _back(site, name if not new else None)
    with transaction.atomic():
        if new and n.has_bgp:
            n.local_asn = n.default_asn("00" if n.kind == Network.Kind.EVPN else "02")
            if n.kind == Network.Kind.IX:
                n.remote_asn = n.default_asn("01")
        n.save()
        if new:
            nw.ensure_default_ranges(n)
        nw.sync_index(site)
    audit(request.user, "network_saved", n, network=str(n), new=new)
    messages.success(request, _("Network %(n)s saved.") % {"n": name})
    return _back(site, name)


@require_POST
@staff_required(super_only=True)
def network_delete_view(request, site, pk):
    n = get_object_or_404(Network, pk=pk, site=site)
    used = sorted({vn.platform.key for vn in n.vnets.select_related("platform")})
    if used:
        messages.error(request, _("%(n)s is used by %(p)s.") % {"n": n.name, "p": ", ".join(used)})
        return _back(site, n.name)
    from tenants.models import PrefixAssignment
    pools = [ipaddress.ip_network(x) for x in n.announced or []]
    taken = [a.cidr for a in PrefixAssignment.active().filter(site=site)
             if any(ipaddress.ip_network(a.cidr).subnet_of(p) for p in pools)]
    if taken:
        messages.error(request, _("%(n)s has assigned addresses: %(a)s.") % {"n": n.name, "a": ", ".join(taken[:5])})
        return _back(site, n.name)
    name = n.name
    with transaction.atomic():
        n.delete()
        nw.sync_index(site)
    audit(request.user, "network_deleted", None, network=f"{name}_{site}")
    messages.success(request, _("Network %(n)s deleted.") % {"n": name})
    return _back(site, None)


@require_POST
@staff_required(super_only=True)
def range_save_view(request, site, pk, rid=None):
    n = get_object_or_404(Network, pk=pk, site=site)
    old = n.ranges.filter(pk=rid).first() if rid else None
    form = RangeForm(request.POST)
    if not form.is_valid():
        _errors(request, [f"{k}: {' '.join(v)}" for k, v in form.errors.items()])
        return _back(site, n.name)
    d = form.cleaned_data
    errs = nw.check_range(n, d["name"], d["start"], d["length"], exclude_pk=old.pk if old else None)
    if errs:
        _errors(request, errs)
        return _back(site, n.name)
    r = old or NetworkRange(network=n)
    r.name, r.start, r.length = d["name"], str(d["start"]), d["length"]
    r.save()
    audit(request.user, "network_range_saved", r, network=str(n), range=r.name)
    messages.success(request, _("Range %(r)s saved.") % {"r": d["name"]})
    return _back(site, n.name)


@require_POST
@staff_required(super_only=True)
def range_delete_view(request, site, pk, rid):
    n = get_object_or_404(Network, pk=pk, site=site)
    r = get_object_or_404(n.ranges, pk=rid)
    users = [vn.platform.key + "/" + vn.name for vn in r.vnets.select_related("platform")]
    if users:
        messages.error(request, _("Range %(r)s is used by %(u)s.") % {"r": r.name, "u": ", ".join(users[:5])})
        return _back(site, n.name)
    name = r.name
    r.delete()
    audit(request.user, "network_range_deleted", None, network=str(n), range=name)
    messages.success(request, _("Range %(r)s deleted.") % {"r": name})
    return _back(site, n.name)


@require_POST
@staff_required(super_only=True)
def bgp_values_view(request, site, pk):
    """The peer groups' passwords for the BGP dialog (shown when the field gets focus). Logged."""
    from django.http import JsonResponse
    from .credentials import value
    n = get_object_or_404(Network, site=site, pk=pk)
    out = {}
    for sec in nw.bgp_sections(n):
        g = n.peer_groups.filter(name=sec["group"]).first()
        v = value(g.password if g and g.password else n.password_ref(sec["group"]))
        if v:
            out[f"password_{sec['key']}"] = v
    audit(request.user, "secret_revealed", n, network=str(n), where="BGP dialog")
    return JsonResponse(out)


@require_POST
@staff_required(super_only=True)
def bgp_save_view(request, site, pk):
    """Local AS, delegated subnets (ix) and the peer groups: rrs, igw and egw (ix, with the remote AS)."""
    n = get_object_or_404(Network, pk=pk, site=site)
    if not n.has_bgp:
        return _back(site, n.name)
    p = request.POST
    errs = []
    net = n.net

    def asn(field, label):
        v = (p.get(field) or "").strip()
        if not v:
            errs.append(_("%(f)s: required.") % {"f": label})
            return None
        if not v.isdigit() or not 1 <= int(v) <= 4294967295:
            errs.append(_("%(f)s: an AS number (1-4294967295).") % {"f": label})
            return None
        return int(v)

    def items(text, what, ip_only=False, inside=True):
        out = []
        for x in (text or "").replace(",", " ").split():
            try:
                if "/" in x:
                    if ip_only:
                        raise ValueError
                    v = str(ipaddress.ip_network(x, strict=True))
                else:
                    v = str(ipaddress.ip_address(x))
            except ValueError:
                errs.append((_("%(w)s: %(x)s is not an address.") if ip_only else
                             _("%(w)s: %(x)s is not an address or a network.")) % {"w": what, "x": x})
                continue
            if inside and not ipaddress.ip_network(v, strict=False).subnet_of(net):
                errs.append(_("%(w)s: %(x)s is not inside %(n)s.") % {"w": what, "x": v, "n": net})
                continue
            if v not in out:
                out.append(v)
        return out

    local_asn = asn("local_asn", _("Local AS"))
    remote_asn = asn("remote_asn", _("Remote AS")) if n.kind == Network.Kind.IX else None
    selected_subs = []
    if n.kind == Network.Kind.IX:  # u10: connect the site's IP subnets (of this exchange's type), not free text
        from .models import IPSubnet
        from .networks import subnet_purpose
        want = subnet_purpose(n.name)
        selected_subs = list(IPSubnet.objects.filter(site__code=site, pk__in=request.POST.getlist("ip_subnets")))
        for s in selected_subs:
            if s.purpose != want:
                errs.append(_("%(c)s is not a %(w)s subnet.") % {"c": s.cidr, "w": want})
            elif s.network_id and s.network_id != n.pk:
                errs.append(_("%(c)s is already connected to another exchange.") % {"c": s.cidr})
    announced = [s.cidr for s in selected_subs]
    groups = {}
    for sec in nw.bgp_sections(n):
        hosts = items(p.get(f"hosts_{sec['key']}"), sec["title"], ip_only=sec["ip_only"])
        if not hosts:
            errs.append(_("%(g)s: choose a range or list the hosts.") % {"g": sec["title"]})
        rid = p.get(f"range_{sec['key']}") or ""
        rng = n.ranges.filter(pk=int(rid)).first() if rid.isdigit() else None
        g = n.peer_groups.filter(name=sec["group"]).first()
        ref = g.password if g and g.password else n.password_ref(sec["group"])
        value = p.get(f"password_{sec['key']}") or ""
        if not value and not nw.has_password(ref):
            errs.append(_("%(g)s: give the password.") % {"g": sec["title"]})
        if value:
            from accounts.policy import problems
            bad = problems(value)
            if bad:
                errs.append(_("%(g)s: the password needs: %(what)s.") % {"g": sec["title"], "what": "; ".join(bad)})
        groups[sec["group"]] = (sec, hosts, rng, ref, value)
    if errs:
        _errors(request, errs)
        return _back(site, n.name)
    with transaction.atomic():
        n.local_asn, n.remote_asn, n.announced = local_asn, remote_asn, announced
        rr = groups.get("rrs")
        n.rrs = list(rr[1]) if rr else []
        n.updated_by = request.user
        n.save()
        if n.kind == Network.Kind.IX:  # u10: move the connected subnets onto this exchange (and off it when unchecked)
            from .models import IPSubnet
            keep = [s.pk for s in selected_subs]
            IPSubnet.objects.filter(network=n).exclude(pk__in=keep).update(network=None)
            IPSubnet.objects.filter(pk__in=keep).update(network=n)
        for gname, (sec, hosts, rng, ref, value) in groups.items():
            exact = rng if rng and nw.range_hosts(rng, sec["ip_only"]) == hosts else None
            PeerGroup.objects.update_or_create(network=n, name=gname, defaults=dict(
                session=sec["session"], hosts=hosts, range=exact, password=ref))
        nw.sync_index(site)
    for gname, (sec, hosts, rng, ref, value) in groups.items():
        if value:
            nw.store_password(ref, value, n, gname, request.user)
    audit(request.user, "network_bgp_saved", n, network=str(n))
    messages.success(request, _("BGP settings of %(n)s saved.") % {"n": n.name})
    return _back(site, n.name)
