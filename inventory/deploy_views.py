"""Infrastructure → site → Hosts: Deploy / Reinstall a host and follow its run (owner 2026-11-18, T25;
inventory/hostdeploy.py). Super admins only. The dialog takes the target cluster, "Reset to factory" (cleared by
default, owner 12:58), the confirmation for a host that is on (09:09) and the host's name typed (the disks are wiped,
§B0.8)."""

import re

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts.utils import staff_required

from . import hostdeploy, hostretire
from .models import IN_SERVICE, Cluster, Host, HostDeployment


def _back(h):
    return redirect(f"{reverse('infra_site', args=[h.site.code])}?tab=hosts&sel=h-{h.name}")


def log_extra(d):
    """Owner 2026-11-20 10:59: the run's log without what its steps already show - each step's "started" line and its
    result (done or failed); what is left is the work inside the steps (a VM migrated, a command's output)."""
    if d is None or not d.log:
        return ""
    steps = d.steps or {}
    out = []
    for line in d.log.splitlines():
        body = line[9:] if re.match(r"\d\d:\d\d:\d\d ", line) else line
        key, _sep, rest = body.partition(": ")
        if key in steps:
            det = str((steps[key] or {}).get("detail") or "")
            rest = rest.removeprefix("FAILED: ")
            # commit-215, core-061: a result is left out only when the step table shows all of it (240 characters)
            if rest == "started" or (det and rest == det and len(det) <= 240):
                continue
        out.append(line)
    return "\n".join(out)


def context_for(hosts):
    """Each host's last run, its steps and the clusters it may go into (the page)."""
    from accounts import tasks

    last, last_deploy = {}, {}
    for d in (
        HostDeployment.objects.filter(host__in=hosts)
        .select_related("cluster", "started_by")
        .order_by("host_id", "-started_at", "-pk")
    ):
        last.setdefault(d.host_id, d)
        if d.kind == HostDeployment.Kind.DEPLOY:  # commit-535: each host's latest Deploy, from this one read
            last_deploy.setdefault(d.host_id, d)
    fe = {}  # commit-535: a front end per platform, read once
    from .hostlogging import restart_names, restarts_of
    from .models import LoggedService

    by_key = {s.name: s for s in LoggedService.objects.all()}  # owner 2026-11-24: the logged services found
    for h in hosts:
        found = [by_key[k] for k in h.services or [] if k in by_key]
        h.restarts, h.restart_names = restarts_of(found), restart_names(found)  # commit-285 (keys sent)
        h.deployment = last.get(h.pk)
        d = h.deployment
        h.deploy_steps = (
            (hostretire if d and d.kind == HostDeployment.Kind.RETIRE else hostdeploy).steps_view(d) if d else []
        )
        h.deploy_running = bool(tasks.running("host deploy", h))
        h.deploy_log = log_extra(d)
        h.deploy_clusters = list(
            Cluster.objects.filter(platform__site=h.site)
            .exclude(state="retired")
            .select_related("platform")
            .order_by("platform__key", "name")
        )
        h.deploy_default = next(iter(h.clusters.all()), None)
        # T36 B4b: Connect - a host in a cluster, its deployment finished, not deployed yet (or to check it again)
        # commit-532: the same target as the POST; commit-535: from the page's reads, checked only for a host that
        # could be connected (planned or non-compliant)
        mine = list(h.clusters.all())
        h.connect_target = hostdeploy.connect_target(h, last=last_deploy.get(h.pk), clusters=mine)
        t = h.connect_target
        if t is not None and t.platform_id not in fe:
            fe[t.platform_id] = hostdeploy.has_front_end(t.platform)
        h.can_connect = bool(  # commit-529/531: the same check as start_connect
            t is not None
            and h.state in ("planned", "non-compliant")
            and not hostdeploy.connect_problems(
                h, t, last=last_deploy.get(h.pk), members={c.pk for c in mine}, fe=fe[t.platform_id]
            )
        )
        h.powered_on = str((h.health or {}).get("power") or "") == "On"


@require_POST
@staff_required(super_only=True)
def deploy_view(request, site, name):
    h = get_object_or_404(Host.objects.select_related("site", "profile"), site__code=site, name=name)
    if (request.POST.get("confirm_name") or "").strip() != h.name:
        messages.error(request, _("Type the host's name to confirm: its disks are wiped."))
        return _back(h)
    cluster = (
        Cluster.objects.filter(pk=request.POST.get("cluster"), platform__site=h.site).select_related("platform").first()
    )
    if cluster is None:
        messages.error(request, _("Choose the cluster the host goes into."))
        return _back(h)
    on = request.POST.get("power_confirmed") == "on"
    why = hostdeploy.problems(h, cluster, confirmed=True)  # power is read by the run itself
    if why:
        messages.error(request, _("Not started: %(w)s.") % {"w": "; ".join(why)})
        return _back(h)
    try:
        hostdeploy.start(
            request.user, h, cluster, reset_factory=request.POST.get("reset_factory") == "on", power_confirmed=on
        )
    except Exception as exc:  # noqa: BLE001 - shown
        messages.error(request, str(exc))
        return _back(h)
    messages.success(request, _("The deployment of %(h)s started; its steps show on the host.") % {"h": h.name})
    return _back(h)


@require_POST
@staff_required(super_only=True)
def connect_view(request, site, name):
    """T36 B4b (owner 2026-11-25 19:25, QB4-1/QB4-2): Connect a deployed host into OpenNebula - its key, added
    disabled, monitored, compared with its profile, then enabled and deployed."""
    h = get_object_or_404(Host.objects.select_related("site"), site__code=site, name=name)
    cluster = hostdeploy.connect_target(h)  # commit-532: the same target as the button
    if cluster is None:
        messages.error(request, _("The host has no cluster to connect to: deploy it first."))
        return _back(h)
    try:
        hostdeploy.start_connect(request.user, h, cluster)
    except hostdeploy.DeployError as exc:
        messages.error(request, _("Not started: %(w)s.") % {"w": exc})
        return _back(h)
    messages.success(request, _("Connect of %(h)s started; its steps show on the host.") % {"h": h.name})
    return _back(h)


@require_POST
@staff_required(super_only=True)
def connect_cluster_view(request, site, pk):
    """T36 B4b: Connect every host of the cluster that is not deployed yet (planned or non-compliant)."""
    cluster = get_object_or_404(Cluster.objects.select_related("platform"), pk=pk, platform__site__code=site)
    started, refused = [], []
    if cluster.state == "retired":  # commit-531
        messages.error(request, _("Cluster %(c)s is retired.") % {"c": cluster.name})
        return redirect(f"{reverse('infra_site', args=[site])}?tab=clusters")
    for h in cluster.hosts.filter(state__in=["planned", "non-compliant"]).order_by("name"):
        try:
            hostdeploy.start_connect(request.user, h, cluster)
            started.append(h.name)
        except hostdeploy.DeployError as exc:
            refused.append(f"{h.name}: {exc}")
    if started:
        messages.success(request, _("Connect started: %(h)s.") % {"h": ", ".join(started)})
    if refused:
        messages.error(request, _("Not started: %(w)s.") % {"w": "; ".join(refused)})
    if not started and not refused:
        messages.info(request, _("No host of %(c)s waits to be connected.") % {"c": cluster.name})
    return redirect(f"{reverse('infra_site', args=[site])}?tab=clusters")


@require_POST
@staff_required(super_only=True)
def hosts_file_view(request, site, pk):
    """T36 B4c: /etc/hosts written again on the hosts in service of a mgmt or simple platform's cluster."""
    from . import hostsfile

    cluster = get_object_or_404(Cluster.objects.select_related("platform"), pk=pk, platform__site__code=site)
    p = cluster.platform
    back = redirect(f"{reverse('infra_site', args=[site])}?tab=clusters")
    if not hostsfile.applies(p):
        messages.error(request, _("/etc/hosts is managed only on the hosts of a mgmt or simple platform."))
        return back
    try:
        hostsfile.block(p)
    except hostsfile.HostsFileError as exc:
        messages.error(request, _("Not started: %(w)s.") % {"w": exc})
        return back
    hostsfile.refresh(request.user, p, "written by hand")
    n = len(hostsfile.targets(p))
    messages.success(
        request, _("/etc/hosts is being written on %(n)s host(s) of %(p)s (Tasks).") % {"n": n, "p": p.key}
    )
    if m := hostsfile.missing_fe(p):
        messages.warning(request, m)
    return back


@require_POST
@staff_required(super_only=True)
def resume_view(request, site, name):
    h = get_object_or_404(Host, site__code=site, name=name)
    dep = HostDeployment.objects.filter(
        host=h, state__in=[HostDeployment.State.FAILED, HostDeployment.State.RUNNING]
    ).first()  # commit-140: an interrupted run (still "running", its task gone) is resumed too
    if dep is None:
        messages.error(request, _("There is no failed run of this host to resume."))
        return _back(h)
    try:
        if dep.kind == HostDeployment.Kind.RETIRE:
            hostretire.start(request.user, h, dep.cluster)
        elif dep.kind == HostDeployment.Kind.CONNECT:  # T36 B4b
            hostdeploy.start_connect(request.user, h, dep.cluster)
        else:
            hostdeploy.start(request.user, h, dep.cluster, power_confirmed=request.POST.get("power_confirmed") == "on")
        messages.success(request, _("Resumed from the failed step."))
    except Exception as exc:  # noqa: BLE001
        messages.error(request, str(exc))
    return _back(h)


@require_POST
@staff_required(super_only=True)
def abandon_view(request, site, name):
    h = get_object_or_404(Host, site__code=site, name=name)
    try:
        if hostdeploy.abandon(request.user, h) is not None:
            messages.success(request, _("The run is abandoned; the host stays as it is."))
    except hostdeploy.DeployError as exc:
        messages.error(request, str(exc))
    return _back(h)


@staff_required(super_only=True)
def retire_view(request, site, name):
    """Owner 2026-11-20 10:15-10:17: the host's VMs moved away, the host deleted from OpenNebula and powered off
    (inventory/hostretire.py). commit-207 (owner 10:38-10:39): a page first lists what is on the host's local storage -
    datastores, images, and the VMs that cannot be moved from it - deleted with no way back when confirmed. The host's
    name typed to confirm."""
    from django.shortcuts import render

    h = get_object_or_404(Host.objects.select_related("site"), site__code=site, name=name)
    cluster = h.clusters.exclude(state="retired").select_related("platform").first()
    if cluster is None or h.state not in IN_SERVICE:
        messages.error(
            request, _("Only a host in service (deployed or non-compliant) in a cluster is retired this way.")
        )
        return _back(h)
    if request.method != "POST":
        local, error = None, ""
        try:
            other = hostretire.foreign(h, cluster)
            if other:  # commit-207: storage the run cannot see or delete - not retired while it is used
                raise hostretire.DeployError(
                    _("The host's storage is used by datastores of other platforms: %(d)s. Move them first.")
                    % {"d": ", ".join(other)}
                )
            local = hostretire.local_items(hostretire._one_of(cluster), h, cluster)
        except Exception as exc:  # noqa: BLE001 - shown: nothing can be confirmed without it
            error = str(exc)
        return render(
            request, "inventory/host_retire.html", {"h": h, "cluster": cluster, "local": local, "error": error}
        )
    if (request.POST.get("confirm_name") or "").strip() != h.name:
        messages.error(request, _("Type the host's name to confirm: it is taken out of OpenNebula and powered off."))
        return redirect("inv_host_retire", site, name)
    wipe = request.POST.get("wipe") or ""
    if wipe != hostretire.local_items_none() and request.POST.get("confirm_delete") != "on":
        messages.error(request, _("Confirm that what is on the host's local storage is deleted with no way back."))
        return redirect("inv_host_retire", site, name)
    try:
        hostretire.start(request.user, h, cluster, wipe=wipe)
    except Exception as exc:  # noqa: BLE001 - shown
        messages.error(request, str(exc))
        return _back(h)
    messages.success(request, _("The retirement of %(h)s started; its steps show on the host.") % {"h": h.name})
    return _back(h)
