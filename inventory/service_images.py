"""Service images of a platform (inventory.models.ServiceImage): the mandatory rows, their names in the platform's
settings (nf_image, nr_template) and the id and status found on the platform."""
import xml.etree.ElementTree as ET

from django.utils import timezone

from .models import ServiceImage

IMAGE_STATES = {"0": "init", "1": "ready", "2": "used", "3": "disabled", "4": "locked", "5": "error", "6": "clone",
                "7": "delete", "8": "used_pers", "9": "locked_used", "10": "locked_used_pers"}
USABLE = {"ready", "used", "used_pers"}


def ensure(control):
    """The rows of a platform. A platform without rows gets the rows with a role (names from its settings); later only
    the VyOS image is made again when missing — the native router's rows stay deleted once deleted."""
    first = not control.service_images.exists()
    rows = {r.role: r for r in control.service_images.exclude(role="")}
    names = {ServiceImage.VYOS: control.nf_image, ServiceImage.NR_TEMPLATE: control.nr_template,
             ServiceImage.NR_IMAGE: control.nr_template}
    for pos, (role, purpose, kind, default) in enumerate(ServiceImage.MANDATORY):
        if role not in rows and (first or role == ServiceImage.VYOS):
            ServiceImage.objects.create(control=control, role=role, purpose=purpose, kind=kind,
                                        name=names.get(role) or default, position=pos,
                                        customization=ServiceImage.Customization.CONTEXT)
    return list(control.service_images.all())


def sync(control):
    """The mandatory rows' names become the platform's settings; the router image takes the template's name."""
    rows = {r.role: r for r in ensure(control) if r.role}
    tpl = rows[ServiceImage.NR_TEMPLATE].name if ServiceImage.NR_TEMPLATE in rows else ""
    if tpl and ServiceImage.NR_IMAGE in rows and rows[ServiceImage.NR_IMAGE].name != tpl:
        ServiceImage.objects.filter(pk=rows[ServiceImage.NR_IMAGE].pk).update(name=tpl, one_id=None, state="")
    control.nf_image, control.nr_template = rows[ServiceImage.VYOS].name, tpl
    control.save(update_fields=["nf_image", "nr_template", "updated_at"])


def check(one, control, images=None, templates=None):
    """Fill the id and status of every row from the platform. Returns the problems of the rows that are not usable."""
    images = images if images is not None else ET.fromstring(one._call("one.imagepool.info", -2, -1, -1))
    templates = templates if templates is not None else ET.fromstring(one._call("one.templatepool.info", -2, -1, -1))
    by_name = {"image": {i.findtext("NAME"): i for i in images.findall("IMAGE")},
               "template": {t.findtext("NAME"): t for t in templates.findall("VMTEMPLATE")}}
    now, problems = timezone.now(), []
    for r in ensure(control):
        x = by_name.get(r.kind, {}).get(r.name)
        if x is None:
            r.one_id, r.state = None, "missing"
            problems.append(f"{r.purpose} {r.kind} {r.name!r} not found")
        else:
            r.one_id = int(x.findtext("ID"))
            r.state = IMAGE_STATES.get(x.findtext("STATE") or "", x.findtext("STATE")) if r.kind == "image" else "found"
            if r.kind == "image" and r.state not in USABLE:
                problems.append(f"{r.purpose} {r.kind} {r.name!r} #{r.one_id} is {r.state}")
        r.checked_at = now
        r.save(update_fields=["one_id", "state", "checked_at"])
    return problems
