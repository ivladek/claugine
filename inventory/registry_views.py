"""Infrastructure → Address registry: delegated subnets and their assignments, one tab per platform."""

import ipaddress

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from accounts.models import Role, audit
from accounts.utils import staff_required
from tenants.models import PrefixAssignment, Source, Tenant

from .models import IPSubnet, Site


class PrefixForm(forms.Form):
    cidr = forms.CharField(label=_l("Subnet"), max_length=43, help_text=_l("e.g. 10.82.104.0/28 or 198.18.93.4/32"))
    site = forms.ChoiceField(label=_l("Site"))
    kind = forms.ChoiceField(label=_l("Use"), choices=PrefixAssignment.Kind.choices)
    tenant = forms.ChoiceField(label=_l("Project"), required=False, help_text=_l("Empty for a reservation."))
    note = forms.CharField(label=_l("Note"), max_length=200, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["site"].choices = [(s.code, s.code) for s in Site.objects.all()]
        from tenants.models import Lifecycle

        self.fields["tenant"].choices = [("", "—")] + [
            (str(t.pk), f"{t.group_name or t.name} · {t.organization}")
            for t in Tenant.objects.filter(approval="approved")
            .exclude(status__in=[Lifecycle.ARCHIVED, Lifecycle.DELETING])
            .select_related("organization")
            .order_by("name")
        ]

    def clean_cidr(self):
        try:
            return str(ipaddress.ip_network(self.cleaned_data["cidr"].strip(), strict=True))
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc

    def clean(self):
        d = super().clean()
        name = d.get("tenant") or ""
        d["tenant_obj"] = Tenant.objects.filter(pk=int(name)).first() if name.isdigit() else None
        if d.get("kind") != PrefixAssignment.Kind.RESERVED and not name:
            self.add_error("tenant", _("A project is required unless the subnet is reserved."))
        if d.get("cidr"):
            net = ipaddress.ip_network(d["cidr"])
            for p in PrefixAssignment.active():
                if ipaddress.ip_network(p.cidr).overlaps(net):
                    self.add_error(
                        "cidr",
                        _("Overlaps %(c)s already assigned (%(t)s).")
                        % {"c": p.cidr, "t": p.tenant or p.note or p.kind},
                    )
                    break
        return d


class IPSubnetForm(forms.Form):
    """u10: define a site IP subnet (immutable once added). Type is auto-detected (private → corp, public → inet)."""

    subnet = forms.GenericIPAddressField(label=_l("Subnet"), protocol="IPv4")
    prefix = forms.IntegerField(label=_l("Prefix"), min_value=8, max_value=32)
    max_prefix = forms.IntegerField(
        label=_l("Max"),
        min_value=8,
        max_value=32,
        required=False,
        help_text=_l(
            "the largest block (smallest prefix length) a project may be given; "
            "left empty it defaults to the subnet itself (the most permissive)"
        ),
    )
    routing = forms.ChoiceField(label=_l("Routing"), choices=IPSubnet.Routing.choices)
    static_gateway = forms.GenericIPAddressField(label=_l("Static gateway"), protocol="IPv4", required=False)

    def clean(self):
        d = super().clean()
        subnet, prefix = d.get("subnet"), d.get("prefix")
        net = None
        if subnet and prefix:
            try:
                net = ipaddress.ip_network(f"{subnet}/{prefix}", strict=True)
                d["subnet"] = str(net.network_address)
            except ValueError:
                self.add_error("subnet", _("Not the network address for this prefix (e.g. 10.0.0.0/24)."))
        mx = d.get("max_prefix")
        if prefix:  # owner 2026-11-03: biggest block a project may get = subnet prefix + 2, capped at /24
            ceiling = min(max(prefix + 2, 24), 31)
            if mx is None:
                d["max_prefix"] = mx = ceiling  # default to the most permissive allowed
            elif mx < ceiling:
                self.add_error(
                    "max_prefix", _("The largest block is /%(c)s (subnet + 2, no bigger than /24).") % {"c": ceiling}
                )
            elif mx > 31:
                self.add_error("max_prefix", _("A delegated block is at most a /31."))
        if d.get("routing") == IPSubnet.Routing.STATIC:
            gw = d.get("static_gateway")
            if not gw:
                self.add_error("static_gateway", _("A static gateway is required for static routing."))
            elif net is not None and ipaddress.ip_address(gw) not in net:
                self.add_error("static_gateway", _("The gateway must be inside the subnet."))
        return d


def ipsubnet_in_use(s):
    """A subnet is in use once an ix network carries it or a prefix inside it is assigned (then it cannot be deleted)."""
    if s.network_id:
        return True
    net = s.net
    for a in PrefixAssignment.active().filter(site=s.site.code):
        try:
            n = ipaddress.ip_network(a.cidr)
        except ValueError:
            continue
        if n.version == net.version and (n.subnet_of(net) or n.overlaps(net)):
            return True
    return False


def delegated_prefixlens(s):
    """Prefix lengths of the active blocks delegated to projects from this IP subnet (owner 2026-11-03)."""
    net = s.net
    out = []
    for a in PrefixAssignment.active().filter(site=s.site.code):
        try:
            n = ipaddress.ip_network(a.cidr)
        except ValueError:
            continue
        if n.version == net.version and n.subnet_of(net):
            out.append(n.prefixlen)
    return out


def max_prefix_choices(s):
    """Allowed values for a subnet's max delegated prefix: from the ceiling (subnet + 2, capped at /24; the biggest
    block) up to the largest block already delegated — or /31 when no project uses the subnet (owner 2026-11-03)."""
    ceiling = s.max_prefix_ceiling
    used = delegated_prefixlens(s)
    hi = min(used) if used else 31  # must still permit the biggest block already delegated (else /31)
    lo = min(ceiling, hi)  # the ceiling, but never above what is already delegated
    return list(range(lo, hi + 1))


class IPSubnetEditForm(forms.Form):
    """owner 2026-11-03: change a subnet's max allowed prefix (and, for a non-IX subnet, its routing and static
    gateway); the subnet itself is immutable. A subnet connected to an IX (BGP) exposes only the max allowed prefix.
    The max-prefix choices are bounded by the ceiling and by the blocks already delegated."""

    max_prefix = forms.TypedChoiceField(label=_l("Max allowed prefix"), coerce=int)
    routing = forms.ChoiceField(label=_l("Routing"), choices=IPSubnet.Routing.choices)
    static_gateway = forms.GenericIPAddressField(label=_l("Static gateway"), protocol="IPv4", required=False)

    def __init__(self, *args, subnet=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.subnet = subnet
        choices = max_prefix_choices(subnet) if subnet is not None else []
        self.fields["max_prefix"].choices = [(n, f"/{n}") for n in choices]
        if subnet is not None and subnet.routing == IPSubnet.Routing.BGP:  # IX subnet: only the max allowed prefix
            for f in ("routing", "static_gateway"):
                self.fields.pop(f, None)

    def clean(self):
        d = super().clean()
        if d.get("routing") == IPSubnet.Routing.STATIC:
            gw = d.get("static_gateway")
            if not gw:
                self.add_error("static_gateway", _("A static gateway is required for static routing."))
            elif self.subnet is not None and ipaddress.ip_address(gw) not in self.subnet.net:
                self.add_error("static_gateway", _("The gateway must be inside the subnet."))
        return d


@require_POST
@staff_required(Role.SUPPORT)
def ipsubnet_edit_view(request, pk):
    s = get_object_or_404(IPSubnet, pk=pk)
    url = f"{reverse('infra_site', args=[s.site.code])}?tab=subnets"
    form = IPSubnetEditForm(request.POST, subnet=s, prefix=f"se{s.pk}")
    if form.is_valid():
        d = form.cleaned_data
        s.max_prefix = d["max_prefix"]
        fields = ["max_prefix"]
        if "routing" in form.fields:  # a non-IX subnet keeps its routing and static gateway editable
            s.routing = d["routing"]
            s.static_gateway = d.get("static_gateway") or None
            fields += ["routing", "static_gateway"]
        s.save(update_fields=fields)
        audit(request.user, "ipsubnet_edited", s, cidr=s.cidr, max_prefix=s.max_prefix, routing=s.routing)
        messages.success(request, _("%(c)s updated (max /%(m)s).") % {"c": s.cidr, "m": s.max_prefix})
    else:
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
    return redirect(url)


@require_POST
@staff_required(Role.SUPPORT)
def ipsubnet_add_view(request, code):
    site = get_object_or_404(Site, code=code)
    url = f"{reverse('infra_site', args=[code])}?tab=subnets"
    form = IPSubnetForm(request.POST)
    if form.is_valid():
        d = form.cleaned_data
        if IPSubnet.objects.filter(site=site, subnet=d["subnet"], prefix=d["prefix"]).exists():
            messages.error(request, _("That subnet already exists on this site."))
        else:
            s = IPSubnet.objects.create(
                site=site,
                subnet=d["subnet"],
                prefix=d["prefix"],
                max_prefix=d["max_prefix"],
                routing=d["routing"],
                static_gateway=d.get("static_gateway") or None,
                created_by=request.user,
            )
            audit(request.user, "ipsubnet_added", s, cidr=s.cidr, routing=s.routing, purpose=s.purpose)
            messages.success(request, _("%(c)s added (%(t)s).") % {"c": s.cidr, "t": s.get_purpose_display()})
    else:
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
    return redirect(url)


@require_POST
@staff_required(Role.SUPPORT)
def ipsubnet_delete_view(request, pk):
    s = get_object_or_404(IPSubnet, pk=pk)
    url = f"{reverse('infra_site', args=[s.site.code])}?tab=subnets"
    if ipsubnet_in_use(s):
        messages.error(
            request, _("This subnet is in use (carried by a network or has assignments): it cannot be deleted.")
        )
    else:
        audit(request.user, "ipsubnet_deleted", s, cidr=s.cidr)
        s.delete()
        messages.success(request, _("Subnet removed."))
    return redirect(url)


def site_addresses(request, site, ctx):
    """The Address registry tab of a site: its delegated subnets grouped by platform, assignments, the form."""
    url = f"{reverse('infra_site', args=[site.code])}?tab=subnets"
    form = PrefixForm(request.POST or None, initial={"site": site.code})
    if request.method == "POST":
        if not (request.user.has_role(Role.SUPPORT) or request.user.is_super_admin):
            messages.error(request, _("Only support engineers assign subnets."))
        elif form.is_valid():
            d = form.cleaned_data
            p = PrefixAssignment.objects.create(
                cidr=d["cidr"],
                site=d["site"],
                kind=d["kind"],
                tenant=d["tenant_obj"],
                note=d["note"],
                source=Source.MANUAL,
                created_by=request.user,
            )
            audit(request.user, "prefix_assigned", p, cidr=p.cidr, tenant=str(p.tenant or ""), kind=p.kind)
            messages.success(request, _("%(c)s recorded.") % {"c": p.cidr})
            return redirect(url)
    assignments = list(PrefixAssignment.active().filter(site=site.code).select_related("tenant"))
    ip_subnets = list(IPSubnet.objects.filter(site=site).select_related("network"))
    placed = set()
    for s in ip_subnets:  # u10: each defined subnet with its assignments and usage
        net = s.net
        mine = []
        for a in assignments:
            try:
                n = ipaddress.ip_network(a.cidr)
            except ValueError:
                continue
            if n.version == net.version and n.subnet_of(net):
                mine.append(a)
                placed.add(a.pk)
        size = net.num_addresses
        used = sum(ipaddress.ip_network(a.cidr).num_addresses for a in mine)
        s.assignments = mine
        s.used, s.size, s.pct = used, size, (round(used * 100 / size) if size else 0)
        s.in_use = bool(s.network_id or mine)
        s.edit_form = IPSubnetEditForm(
            subnet=s,
            prefix=f"se{s.pk}",
            initial={"max_prefix": s.max_prefix, "routing": s.routing, "static_gateway": s.static_gateway},
        )
    overlaps = []
    nets = [(a, ipaddress.ip_network(a.cidr)) for a in assignments]
    for i, (a, na) in enumerate(nets):
        for b, nb in nets[i + 1 :]:
            if na.version == nb.version and na.overlaps(nb):
                overlaps.append((a, b))
    ctx.update(
        ip_subnets=ip_subnets,
        ip_subnet_form=IPSubnetForm(),
        prefix_form=form,
        outside=[a for a in assignments if a.pk not in placed],
        overlaps=overlaps,
    )
    return None


@staff_required(Role.RESOURCE_MANAGER, Role.SUPPORT)
def registry_view(request):
    """Old link: the address registry now lives on each site."""
    st = Site.objects.first()
    return redirect(f"{reverse('infra_site', args=[st.code])}?tab=subnets" if st else reverse("inventory"))


@require_POST
@staff_required(Role.SUPPORT)
def prefix_release_view(request, pk):
    p = get_object_or_404(PrefixAssignment, pk=pk, released_at__isnull=True)
    p.released_at = timezone.now()
    p.save(update_fields=["released_at"])
    audit(request.user, "prefix_released", p, cidr=p.cidr)
    nxt = request.POST.get("next") or ""
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = reverse("registry")
    return redirect(nxt)
