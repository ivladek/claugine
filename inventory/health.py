"""Health of the platforms: the OpenNebula endpoint answers, the network function image (VyOS) is usable, and so is
the native router (the Service Virtual Router template and the images of its disks).

Checked every PortalSettings.health_interval_minutes (default 30) by `manage.py check_platforms`
(systemd timer every 5 minutes; a platform is checked when its last check is older than the period),
and at once with the Test button.
"""
import xml.etree.ElementTree as ET
from datetime import timedelta

from django.utils import timezone

from .models import Platform, PlatformControl

IMAGE_STATES = {"0": "init", "1": "ready", "2": "used", "3": "disabled", "4": "locked", "5": "error", "6": "clone",
                "7": "delete", "8": "used_pers", "9": "locked_used", "10": "locked_used_pers"}
USABLE = {"ready", "used", "used_pers"}


def needs_image(control):
    """Platforms that host tenants (shared) need the image of their network functions."""
    p = Platform.objects.filter(key=control.key).first()
    return p is not None and p.kind == "shared"


def native_router(one, control, images):
    """The native igw's template (a virtual router template) and the images of its disks."""
    name = getattr(control, "nr_template", "") or "Service Virtual Router"
    out = {"template": name, "problems": []}
    tpls = ET.fromstring(one._call("one.templatepool.info", -2, -1, -1))
    t = next((x for x in tpls.findall("VMTEMPLATE") if x.findtext("NAME") == name), None)
    if t is None:
        out["problems"].append(f"native router template {name!r} not found")
        return out
    out["id"] = int(t.findtext("ID"))
    by_id = {i.findtext("ID"): i for i in images.findall("IMAGE")}
    by_name = {i.findtext("NAME"): i for i in images.findall("IMAGE")}
    for d in t.findall("TEMPLATE/DISK"):
        img = by_id.get(d.findtext("IMAGE_ID") or "") or by_name.get(d.findtext("IMAGE") or "")
        if img is None:
            out["problems"].append(f"native router template {name!r}: disk image {d.findtext('IMAGE_ID') or d.findtext('IMAGE')} not found")
            continue
        state = IMAGE_STATES.get(img.findtext("STATE") or "", img.findtext("STATE"))
        out.setdefault("images", []).append({"id": int(img.findtext("ID")), "name": img.findtext("NAME"), "state": state})
        if state not in USABLE:
            out["problems"].append(f"native router image {img.findtext('NAME')!r} #{img.findtext('ID')} is {state}")
    return out


def check(control):
    """Check one platform now and save the result. Returns the PlatformControl."""
    from adoption.collector import client_for
    from . import service_images
    h, problems = {"image_name": control.nf_image}, []
    try:
        one = client_for(control)
        h["version"] = one.version()
        root = ET.fromstring(one._call("one.imagepool.info", -2, -1, -1))
        tpls = ET.fromstring(one._call("one.templatepool.info", -2, -1, -1))
        found = service_images.check(one, control, root, tpls)  # id and status of every service image
        if needs_image(control):
            problems += found
            img = next((i for i in root.findall("IMAGE") if i.findtext("NAME") == control.nf_image), None)
            if img is not None:
                state = IMAGE_STATES.get(img.findtext("STATE") or "", img.findtext("STATE"))
                h["image"] = {"id": int(img.findtext("ID")), "state": state, "datastore": img.findtext("DATASTORE")}
            if control.service_images.filter(role="one-router-template").exists():  # deleted: no native igw here
                nr = native_router(one, control, root)
                h["native_router"] = nr
                ids = {f"#{r.one_id} " for r in control.service_images.all() if r.one_id is not None}
                problems += [x for x in nr["problems"]  # what the service images did not report already
                             if "template" not in x.split(":")[0] and not any(i in x for i in ids)]
    except Exception as exc:  # noqa: BLE001 - shown on the platform
        problems.append(f"{type(exc).__name__}: {exc}"[:500])
    h["problems"] = problems
    control.health, control.health_ok, control.health_checked_at = h, not problems, timezone.now()
    control.save(update_fields=["health", "health_ok", "health_checked_at", "updated_at"])
    return control


def check_due(force=False):
    """Check every configured platform whose last check is older than the period. Returns {key: ok}."""
    from tenants.models import PortalSettings
    period = timedelta(minutes=max(1, PortalSettings.get().health_interval_minutes or 30))
    now = timezone.now()
    out = {}
    for c in PlatformControl.objects.all():
        if not c.configured:
            continue
        if force or c.health_checked_at is None or now - c.health_checked_at >= period:
            out[c.key] = check(c).health_ok
    return out
