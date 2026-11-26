"""T36 B4a (owner 2026-11-24, rules v2 B1-B4; 2026-11-25 19:22 "go ahead"): each KVM host is made from one host
service (Profiles → Services, type host) - which one follows from its platform:

- a mgmt platform's hosts: kvm-mgmt (kvm, mysql, one-fe, time; no frr - rules v2 B1);
- a simple platform's first three hosts: kvm-simple-fe (the front end and the time servers - 20:13), the rest
  (4-6): kvm-simple;
- any other platform's hosts: kvm-shared, or kvm-shared-gpu when the server profile has GPUs.

A profile is found by its host slot (ServiceProfile.host_slot), never by its name. The service is chosen when the host
is deployed and kept on the host (Host.service): a host already made keeps its slot, so a simple platform's front-end
hosts never change when hosts are added or renamed; the new ones fill the free front-end slots first, by name. The
deployment's steps follow the service's functions (frr only with frr, gpu only with gpu)."""

from .models import Host, ServiceProfile

SIMPLE_FE = 3  # owner 2026-11-24 20:13: "on a simple platform hosts 1-3 always host one-fe"
SIMPLE_MAX = 6  # rules v2 B2: a simple platform has 3-6 hosts


class HostServiceError(Exception):
    pass


def of_slot(slot):
    return ServiceProfile.objects.filter(stype=ServiceProfile.HOST, host_slot=slot).first()


def platform_hosts(platform):
    """The hosts of the platform's clusters and - commit-522 - those a deployment into one of its clusters holds (a run
    running or failed: the host joins the cluster only at its records step), by name."""
    from django.db.models import Q

    from .models import HostDeployment

    pending = HostDeployment.objects.filter(
        kind=HostDeployment.Kind.DEPLOY,
        cluster__platform=platform,
        state__in=[HostDeployment.State.RUNNING, HostDeployment.State.FAILED],
    ).values("host_id")
    return list(
        Host.objects.filter(Q(clusters__platform=platform) | Q(pk__in=pending))
        .distinct()
        .select_related("service")
        .order_by("name")
    )


def slot_for(h, platform):
    """The slot the host takes on the platform (its own when it already has one of that platform's kind)."""
    kind = platform.kind
    if kind == "mgmt":
        return "mgmt"
    if kind == "simple":
        held = platform_hosts(platform)
        have = (h.service.host_slot if h.service_id else "") or ""
        # commit-522: a saved slot is kept only by a member of the platform's clusters or a host whose run into it is
        # running or failed; one whose run was abandoned chooses again (its slot may have been taken meanwhile)
        if have in ("simple-fe", "simple") and any(x.pk == h.pk for x in held):
            return have
        others = [x for x in held if x.pk != h.pk]
        taken = sum(1 for x in others if x.service_id and x.service.host_slot == "simple-fe")
        # the hosts without a slot yet (and this one) fill the free front-end slots by name
        waiting = sorted([x.name for x in others if not (x.service_id and x.service.host_slot)] + [h.name])
        return "simple-fe" if waiting.index(h.name) < SIMPLE_FE - taken else "simple"
    gpus = bool(h.profile_id and h.profile.gpu_count)
    return "shared-gpu" if gpus else "shared"


def pick(h, platform):
    """The host service the host is made from on the platform; HostServiceError when there is none to take."""
    slot = slot_for(h, platform)
    if platform.kind == "simple":
        n = len({x.pk for x in platform_hosts(platform)} | {h.pk})
        if n > SIMPLE_MAX:
            raise HostServiceError(f"a simple platform has at most {SIMPLE_MAX} hosts ({platform.key} would have {n})")
    p = of_slot(slot)
    if p is None:
        raise HostServiceError(f"no host service for {slot} (Profiles → Services: a host service with that slot)")
    return p


def assign(h, platform):
    """Pick and keep the host's service; returns it. commit-522: under the platform's lock (inside the caller's
    transaction), so two deployments into one platform choose one after the other."""
    from .models import Platform

    Platform.objects.select_for_update().filter(pk=platform.pk).first()
    p = pick(h, platform)
    if h.service_id != p.pk:
        h.service = p
        h.save(update_fields=["service"])
    return p


def functions(h, platform=None):
    """The functions the host is made with: its service's (or the one it would get on the platform)."""
    p = h.service if h.service_id else (pick(h, platform) if platform is not None else None)
    return p.functions() if p is not None else []
