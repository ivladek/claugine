"""Infrastructure → site → Hosts: Deploy / Reinstall a host and follow its run (owner 2026-11-18, T25;
inventory/hostdeploy.py). Super admins only. The dialog takes the target cluster, "Reset to factory" (cleared by
default, owner 12:58), the confirmation for a host that is on (09:09) and the host's name typed (the disks are wiped,
§B0.8)."""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts.utils import staff_required

from . import hostdeploy
from .models import Cluster, Host, HostDeployment


def _back(h):
    return redirect(f"{reverse('infra_site', args=[h.site.code])}?tab=hosts&sel=h-{h.name}")


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
    for h in hosts:
        h.deployment = last.get(h.pk)
        h.deploy_steps = hostdeploy.steps_view(h.deployment) if h.deployment else []
        h.deploy_running = bool(tasks.running("host deploy", h))
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
        hostdeploy.start(request.user, h, dep.cluster, power_confirmed=request.POST.get("power_confirmed") == "on")
        messages.success(request, _("Resumed from the failed step."))
    except Exception as exc:  # noqa: BLE001
        messages.error(request, str(exc))
    return _back(h)


@require_POST
@staff_required(super_only=True)
def abandon_view(request, site, name):
    h = get_object_or_404(Host, site__code=site, name=name)
    dep = HostDeployment.objects.filter(host=h, state=HostDeployment.State.FAILED).first()
    if dep is not None:
        hostdeploy.abandon(request.user, dep)
        messages.success(request, _("The run is abandoned; the host stays as it is."))
    return _back(h)
