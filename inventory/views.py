"""Infrastructure: sites (with their platforms, clusters, subnets, hosts, storage and networks), server profiles and
the endpoints of the platforms. The portal's records are the source; they are edited in dialogs (see ops.py)."""

from django import forms
from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from accounts.models import Role, audit
from accounts.utils import staff_required

from .models import Platform, PlatformControl, ServerProfile, ServiceImage, Site


def site_initial(st):
    from .models import LOG_REQUIRED, SSH_REQUIRED

    m = st.management or {}
    ports = m.get("ports") or {}
    from .nfconfig import MGMT_PORTS

    ssh = [f"{label} | {ports.get(k, MGMT_PORTS.get(k, 22))}" for k, label in SSH_REQUIRED]
    ssh += [f"{x.get('purpose')} | {x.get('port')}" for x in m.get("ports_extra") or []]
    logs = st.log_collectors or {}
    rows = []
    for k, label in LOG_REQUIRED:  # the required lines first, filled in or to fill
        c = logs.get(k) or {}
        rows.append(f"{label} | {c.get('host', '')} | {c.get('proto', 'udp')} | {c.get('port', '')}")
    for k, c in logs.items():
        if k not in dict(LOG_REQUIRED):
            rows.append(f"{c.get('label') or k} | {c.get('host', '')} | {c.get('proto', 'udp')} | {c.get('port', '')}")
    return {
        "code": st.code,
        "number": st.number,
        "country": st.country,
        "city": st.city,
        "dc": st.dc,
        "timezone": st.timezone,
        "edge_cluster": st.edge_cluster.name if st.edge_cluster_id else "",
        "ntp_servers": "\n".join(st.ntp_servers or []),
        "dns_servers": "\n".join(st.dns_servers or []),
        "bootstrap_dns": " ".join((st.bootstrap_dns or [])[:1]),
        "mgmt_hosts": "\n".join(m.get("hosts") or []),
        "ssh_ports": "\n".join(ssh),
        "log_collectors": "\n".join(rows),
    }


def _can_edit(user):
    return user.is_super_admin


@staff_required(Role.RESOURCE_MANAGER, Role.SUPPORT)
def inventory_view(request):
    """Infrastructure → Sites, and the endpoints that are not platforms of a site."""
    from . import edit_views as ev

    platforms = list(Platform.objects.select_related("site"))
    rows = []
    for st in Site.objects.all():
        sp = [p for p in platforms if p.site_id == st.pk]
        from . import redfish
        from .models import LOG_REQUIRED, SSH_REQUIRED, Network

        logs, mg = st.log_collectors or {}, st.management or {}
        log_rows = [(label, logs.get(k)) for k, label in LOG_REQUIRED] + [
            (c.get("label") or k, c) for k, c in logs.items() if k not in dict(LOG_REQUIRED)
        ]
        ssh_rows = [(label, (mg.get("ports") or {}).get(k)) for k, label in SSH_REQUIRED] + [
            (x.get("purpose"), x.get("port")) for x in mg.get("ports_extra") or []
        ]
        # owner 2026-11-09: the site panel lists what it has instead of counting it
        host_rows = list(st.hosts.select_related("profile").order_by("name"))
        net_rows = list(Network.objects.filter(site=st.code).order_by("name"))
        storage_rows = list(st.storages.order_by("name"))
        rows.append(
            {
                "site": st,
                "platforms": sp,
                "hosts": host_rows,
                "networks": net_rows,
                "log_rows": log_rows,
                "ssh_rows": ssh_rows,
                "storages": storage_rows,
                "utc_offset": redfish.utc_offset(st.tz),
                "form": ev.SiteForm(initial=site_initial(st), auto_id=f"id_site_{st.code}_%s"),
                "empty": not (sp or host_rows or net_rows or storage_rows),
                "state": "active" if any(p.is_active for p in sp) else "planned",
            }
        )
    return render(
        request,
        "inventory/sites.html",
        {
            "rows": rows,
            "can_edit": _can_edit(request.user),
            "can_edit_endpoints": request.user.is_super_admin,
            "can_collect": request.user.has_role(Role.SUPPORT),
            "site_form": ev.SiteForm(),
        },
    )


@staff_required(Role.RESOURCE_MANAGER, Role.SUPPORT)
def profiles_view(request):
    from . import profiles as prof

    return render(request, "inventory/profiles.html", prof.context(request))


SITE_TABS = ("platforms", "clusters", "subnets", "hosts", "storage", "networks", "core")
SITE_TAB_LABELS = {
    "platforms": _l("Platforms"),
    "clusters": _l("Clusters"),
    "subnets": _l("IP subnets"),
    "hosts": _l("Hosts"),
    "storage": _l("Storage"),
    "networks": _l("Networks"),
    "core": _l("Core services"),
}


@staff_required(Role.RESOURCE_MANAGER, Role.SUPPORT)
def site_view(request, code):
    """Infrastructure → <site>: platforms, clusters, subnets, hosts, storage, networks."""
    from . import edit_views as ev
    from . import registry_views as reg
    from .models import Host

    st = get_object_or_404(Site, code=code)
    tab = request.GET.get("tab", "platforms")
    tab = "subnets" if tab == "addresses" else tab
    if tab not in SITE_TABS:
        tab = "platforms"
    can_edit = _can_edit(request.user)
    ctx = {
        "site": st,
        "tab": tab,
        "can_edit": can_edit,
        "tabs": SITE_TABS,
        # the status refresh at the top of every tab (correction 10)
        "can_read_core": request.user.has_role(Role.SUPPORT),
        # owner 2026-10-29 18:38: no periodic refresh; the statuses are read again only while a task of the site runs
        "task_running": __import__("inventory.core_views", fromlist=["x"]).site_task_running(code),
        "refresh_while_running": __import__("inventory.core_views", fromlist=["x"]).REFRESH_WHILE_RUNNING,
        "tab_labels": [(t, SITE_TAB_LABELS[t]) for t in SITE_TABS],
        "site_form": ev.SiteForm(initial=site_initial(st), auto_id="id_site_%s"),
        "can_ipmi": request.user.has_role(Role.SUPPORT),
    }
    platforms = list(
        Platform.objects.filter(site=st).prefetch_related(
            "clusters__hosts__profile",
            "clusters__datastores",
            "clusters__networks",
            "datastores__volume__storage",
            "vnets__network",
            "vnets__ranges",
        )
    )
    if tab == "hosts":
        hosts = list(
            Host.objects.filter(site=st).select_related("profile").prefetch_related("clusters__platform", "storages")
        )
        for h in hosts:
            h.used = bool(h.clusters.all())
            h.form = ev.HostEditForm(
                initial={
                    "name": h.name,
                    "profile": h.profile.code if h.profile else "",
                    "ip": h.ip,
                    "ipmi": h.ipmi_host,
                    "ipmi_user": h.ipmi_user,
                    "ipmi_port": h.ipmi_port,
                },
                auto_id=f"id_host_{h.name}_%s",
            )
            from .profiles import compare

            h.diffs = compare(h.profile, h.facts) if h.profile and h.facts else []
        ctx.update(hosts=hosts, host_form=ev.HostForm(initial={"site": st.code}, auto_id="id_host_new_%s"))
    elif tab == "networks":
        from . import network_views

        ctx.update(
            network_views.context(st.code, platforms, request.user.is_super_admin),
            can_edit_networks=request.user.is_super_admin,
        )
    elif tab == "core":
        from . import core_views

        ctx.update(core_views.context(st.code, can_edit), can_read_core=request.user.has_role(Role.SUPPORT))
    elif tab == "storage":
        from .models import Storage

        storages = list(
            Storage.objects.filter(site=st).select_related("host").prefetch_related("volumes__datastores__platform")
        )
        for s_ in storages:
            s_.form = ev.StorageForm(
                site=st,
                initial={
                    "name": s_.name,
                    "kind": s_.kind,
                    "targets": "\n".join(s_.targets or []),
                    "port": s_.port,
                    "fstab": s_.fstab,
                    "host": s_.host.name if s_.host else "",
                },
                auto_id=f"id_st_{s_.pk}_%s",
            )
            s_.used_by = [f"{d.platform.name}/{d.name}" for v in s_.volumes.all() for d in v.datastores.all()]
            for v in s_.volumes.all():
                v.form = ev.VolumeForm(
                    initial={
                        "name": v.name,
                        "device": v.device,
                        "size_gb": v.size_gb,
                        "clients": "\n".join(v.clients or []),
                        "mount": v.mount,
                    },
                    auto_id=f"id_vol_{v.pk}_%s",
                )
        ctx.update(
            storages=storages,
            storage_form=ev.StorageForm(site=st, auto_id="id_st_new_%s"),
            volume_form=ev.VolumeForm(auto_id="id_vol_new_%s"),
        )
    elif tab == "platforms":
        from adoption.collector import ensure_controls

        ensure_controls()
        controls = {c.key: c for c in PlatformControl.objects.all()}
        from . import service_images

        for p in platforms:
            p.control = controls.get(p.key)
            p.service_images = service_images.ensure(p.control) if p.control else []
            e = p.site.edge_cluster  # chosen on the site; shown on the platform that carries it
            p.edge = (
                e if (p.kind == "shared" and e is not None and e.state == "active" and e.platform_id == p.pk) else None
            )
            # owner 2026-11-09: the platform page carries the FireEdge links, the oneadmin password, the
            # front-end nodes and the gateway cluster, which is set here rather than in the edit dialog.
            base = (p.control.fireedge_url if p.control else "") or ""
            p.fe_vip_url = base
            p.fe_fqdn_url = ""
            p.fe_fqdn_label = p.fe_name or (p.fe or {}).get("hostname") or ""
            if base and p.fe_fqdn_label and p.fe_vip and p.fe_vip in base:
                p.fe_fqdn_url = base.replace(p.fe_vip, p.fe_fqdn_label)
            from . import credentials as _cr

            try:
                p.secret_row = _cr.find(p.control.secrets_ref) if p.control and p.control.secrets_ref else None
            except Exception:  # noqa: BLE001 - a missing or malformed reference must not break the page
                p.secret_row = None
            fe = p.fe or {}
            p.fe_node_rows = [
                {
                    "ip": str(x),
                    "name": fe.get("hostname", "") if len(fe.get("nodes") or []) == 1 else "",
                    "status": "",
                    "state": "",
                }
                for x in (fe.get("nodes") or [])
            ]
            p.gateway_choices = list(p.clusters.exclude(state="retired").order_by("name"))
            from .models import _gateways_deployed

            p.gateway_locked = _gateways_deployed(p)
            p.form = ev.PlatformForm(
                locked=p.fe_locked, site=st.code, initial=ev.platform_initial(p), auto_id=f"id_pl_{p.name}_%s"
            )
            p.ds_form = ev.DatastoreForm(platform=p, auto_id=f"id_ds_{p.name}_new_%s")
            p.vn_form = ev.VnetForm(platform=p, auto_id=f"id_vn_{p.name}_new_%s")
            for d in p.datastores.all():
                d.form = ev.DatastoreForm(
                    platform=p,
                    initial={
                        "name": d.name,
                        "kind": d.kind,
                        "volume": d.volume_id,
                        "reserved_pct": d.reserved_pct,
                        "path": d.path,
                        "backend": d.backend,
                        "restic_ip": d.restic_ip,
                        "s3_endpoint": d.s3_endpoint,
                        "s3_bucket": d.s3_bucket,
                        "s3_secret": d.s3_secret,
                        "restic_secret": d.restic_secret,
                    },
                    auto_id=f"id_ds_{d.pk}_%s",
                )
            for vn in p.vnets.all():
                vn.form = ev.VnetForm(
                    platform=p,
                    initial={
                        "name": vn.name,
                        "kind": vn.kind,
                        "network": vn.network_id,
                        "bridge": vn.bridge,
                        "ranges": [r.pk for r in vn.ranges.all()],
                    },
                    auto_id=f"id_vn_{vn.pk}_%s",
                )
        ctx.update(
            platforms=platforms,
            all_platforms=Platform.objects.select_related("site"),
            platform_form=ev.PlatformForm(initial={"site": st.code}, site=st.code, auto_id="id_pl_new_%s"),
            can_edit_endpoints=request.user.is_super_admin,
            can_repair=request.user.is_super_admin,
            can_collect=request.user.has_role(Role.SUPPORT),
            image_types=ServiceImage.TYPES,
        )
    elif tab == "clusters":
        from .capacity import clusters as cluster_capacity

        clusters = [(p, c) for p in platforms for c in p.clusters.all()]
        for p, c in clusters:
            c.form = ev.ClusterEditForm(
                platform=p,
                cluster=c,
                initial={
                    "kind": c.kind,
                    "datastores": [d.pk for d in c.datastores.all()],
                    "networks": [n.pk for n in c.networks.all()],
                    "hosts": [h.name for h in c.hosts.all() if h.state != "retired"],
                    "hosts_shown": "1",
                    "vcpu_ratio": c.vcpu_ratio,
                    "spare_hosts": c.spare_hosts,
                    "reserved_cpu_pct": c.reserved_cpu_pct,
                    "reserved_ram_pct": c.reserved_ram_pct,
                },
                auto_id=f"id_cl_{c.pk}_%s",
            )
        ctx.update(
            clusters=clusters,
            free_hosts=free_hosts(st),
            platforms=platforms,
            profiles=ServerProfile.objects.all(),
            capacity=cluster_capacity(site=st.code),
            cluster_form=ev.ClusterForm(site=st.code, auto_id="id_cl_new_%s"),
        )
    else:
        resp = reg.site_addresses(request, st, ctx)
        if resp:
            return resp
    return render(request, "inventory/site.html", ctx)


def free_hosts(site):
    """Hosts of a site that are in no cluster (retired clusters do not count) and not retired themselves."""
    from .models import Host

    return [
        h
        for h in Host.objects.filter(site=site).exclude(state="retired").prefetch_related("clusters")
        if not any(c.state != "retired" for c in h.clusters.all())
    ]


def _first_site_url(tab):
    st = Site.objects.first()
    return f"{reverse('infra_site', args=[st.code])}?tab={tab}" if st else reverse("inventory")


@staff_required(Role.RESOURCE_MANAGER, Role.SUPPORT)
def platforms_view(request):
    return redirect(_first_site_url("platforms"))


@staff_required(Role.RESOURCE_MANAGER, Role.SUPPORT)
def capacity_view(request):
    return redirect(_first_site_url("clusters") + "#capacity")


@require_POST
@staff_required(Role.SUPPORT)
def control_test_view(request, key):
    """Check the platform now: the endpoint answers and the network function image is usable."""
    from .health import check

    c = check(get_object_or_404(PlatformControl, key=key))
    h = c.health or {}
    if c.health_ok:
        img = h.get("image")
        messages.success(
            request,
            _("%(k)s answers: OpenNebula %(v)s") % {"k": c.key, "v": h.get("version", "?")}
            + (f"; {c.nf_image} #{img['id']} {img['state']}" if img else ""),
        )
    else:
        messages.error(request, _("%(k)s: %(e)s") % {"k": c.key, "e": "; ".join(h.get("problems") or [])})
    return redirect(_control_back(key))


@require_POST
@staff_required(super_only=True)
def control_consistency_repair_view(request, key):
    """Owner 2026-11-11: run the planned repair of the last consistency check (tenants/consistency.py), in the
    background; the platform is checked again when it is done."""
    from tenants import consistency

    c = get_object_or_404(PlatformControl, key=key)
    if c.mode != PlatformControl.Mode.EXECUTION:
        messages.error(request, _("%(k)s is read-only: switch it to full mode to repair.") % {"k": c.key})
    elif (c.consistency or {}).get("repair", {}).get("state") == "running":
        messages.warning(request, _("A repair is already running."))
    elif not (c.consistency or {}).get("actions"):
        messages.info(request, _("Nothing to repair."))
    else:
        consistency.start_repair(c, request.user)
        messages.success(request, _("Repair started. Reload the page to follow it."))
    return redirect(_control_back(key))


@require_POST
@staff_required(super_only=True)
def control_mode_view(request, key, mode):
    """Enable (execution) or disable (read-only) operations of the portal on a platform."""
    if mode not in PlatformControl.Mode.values:
        raise Http404
    c = PlatformControl.objects.filter(key=key).first()
    if c is None:
        if not Platform.objects.filter(key=key).exists():
            raise Http404
        c = PlatformControl(key=key)
    if mode == PlatformControl.Mode.EXECUTION and c.platform is not None and not c.platform.is_active:
        messages.error(request, _("%(k)s is not deployed: operations cannot be enabled.") % {"k": key})
        return redirect(_control_back(key))
    old = c.mode if c.pk else None
    c.mode = mode
    c.save()
    if old != mode:
        audit(request.user, "platform_mode_changed", c, key=c.key, old=old or "", new=mode)
    messages.success(
        request,
        (
            _("Operations enabled on %(k)s.")
            if mode == PlatformControl.Mode.EXECUTION
            else _("Operations disabled on %(k)s: read-only.")
        )
        % {"k": key},
    )
    return redirect(_control_back(key))


class ServiceImageForm(forms.Form):
    purpose = forms.ChoiceField(label=_l("Type"), choices=ServiceImage.TYPES)
    kind = forms.ChoiceField(
        label=_l("Image or template"), choices=[("image", _l("image")), ("template", _l("template"))]
    )
    name = forms.CharField(label=_l("Name"), max_length=200, help_text=_l("As named on the platform (OpenNebula)."))
    customization = forms.ChoiceField(
        label=_l("Customization"), choices=[("ssh", "ssh"), ("context", "context")], required=False
    )


def _customization(value, default):
    return value if value in ("ssh", "context") else default


def _platform_control(key):
    c = PlatformControl.objects.filter(key=key).first()
    if c is None:
        get_object_or_404(Platform, key=key)
        c = PlatformControl.objects.create(key=key)
    return c


@require_POST
@staff_required(super_only=True)
def service_image_save_view(request, key, pk=None):
    """Add a service image to a platform, or change one (mandatory rows: the name only; the router image: nothing)."""
    from . import service_images
    from .models import ServiceImage

    c = _platform_control(key)
    service_images.ensure(c)
    r = get_object_or_404(ServiceImage, pk=pk, control=c) if pk else ServiceImage(control=c)
    if r.role:
        name = (request.POST.get("name") or "").strip()[:200] if r.name_editable else r.name
        if not name:
            messages.error(request, _("This row cannot be changed."))
            return redirect(_control_back(key))
        old, r.name = r.name, name
        r.customization = _customization(request.POST.get("customization"), r.customization)
        if old != name:
            r.one_id, r.state = None, ""
        r.save()
        service_images.sync(c)
        audit(
            request.user,
            "service_image_saved",
            c,
            key=key,
            role=r.role,
            old=old,
            name=name,
            customization=r.customization,
        )
    else:
        form = ServiceImageForm(request.POST)
        if not form.is_valid():
            messages.error(request, "; ".join(f"{k}: {' '.join(v)}" for k, v in form.errors.items()))
            return redirect(_control_back(key))
        created = r.pk is None
        r.purpose, r.kind, r.name = form.cleaned_data["purpose"], form.cleaned_data["kind"], form.cleaned_data["name"]
        r.customization = form.cleaned_data["customization"] or r.customization or "ssh"
        r.one_id, r.state = None, ""
        r.save()
        audit(
            request.user,
            "service_image_saved",
            c,
            key=key,
            purpose=r.purpose,
            kind=r.kind,
            name=r.name,
            customization=r.customization,
            created=created,
        )
    messages.success(request, _("Saved. The id and status are read at the next check (Test)."))
    return redirect(_control_back(key))


@require_POST
@staff_required(super_only=True)
def service_image_delete_view(request, key, pk):
    from .models import ServiceImage

    r = get_object_or_404(ServiceImage, pk=pk, control__key=key)
    if r.mandatory:
        messages.error(
            request,
            _(
                "The VyOS image cannot be deleted: network functions and VyOS core services are made "
                "from it. Its name can be changed."
            ),
        )
        return redirect(_control_back(key))
    rows = [r]
    if r.role in (ServiceImage.NR_TEMPLATE, ServiceImage.NR_IMAGE):  # the native router's template and image together
        rows = list(
            ServiceImage.objects.filter(control=r.control, role__in=[ServiceImage.NR_TEMPLATE, ServiceImage.NR_IMAGE])
        )
    used = [s.name for x in rows for s in x.core_services.all()]
    if used:
        messages.error(
            request, _("Used by the core services %(l)s: choose another image there first.") % {"l": ", ".join(used)}
        )
        return redirect(_control_back(key))
    for x in rows:
        audit(request.user, "service_image_deleted", x.control, key=key, purpose=x.purpose, kind=x.kind, name=x.name)
        x.delete()
    if any(x.role for x in rows):
        from . import service_images

        service_images.sync(r.control)
    messages.success(request, _("Deleted."))
    return redirect(_control_back(key))


@require_POST
@staff_required(super_only=True)
def control_collect_view(request, key):
    """Import tenant data on or off: whether the portal reads the platform's tenant groups, users, quotas and usage."""
    c = _platform_control(key)
    c.collect_enabled = not c.collect_enabled
    c.save(update_fields=["collect_enabled", "updated_at"])
    audit(request.user, "platform_collect_changed", c, key=key, on=c.collect_enabled)
    messages.success(
        request,
        (
            _("Collection of project data on for %(k)s.")
            if c.collect_enabled
            else _("Collection of project data off for %(k)s.")
        )
        % {"k": key},
    )
    return redirect(_control_back(key))


def _control_back(key):
    """Back to the site of an inventory platform, or to Sites for an endpoint outside the inventory."""
    p = Platform.objects.select_related("site").filter(key=key or "").first()
    if p:
        return f"{reverse('infra_site', args=[p.site.code])}?tab=platforms"
    return reverse("inventory")
