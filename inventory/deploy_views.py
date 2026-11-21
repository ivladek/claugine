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
from .models import Cluster, Host, HostDeployment


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

    last = {}
    for d in (
        HostDeployment.objects.filter(host__in=hosts)
        .select_related("cluster", "started_by")
        .order_by("host_id", "-started_at")
    ):
        last.setdefault(d.host_id, d)
    from .hostlogging import restart_names, restarts_of
    from .models import HostService

    by_key = {s.key: s for s in HostService.objects.all()}
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
    if cluster is None or h.state != "active":
        messages.error(request, _("Only an active host in a cluster is retired this way."))
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
