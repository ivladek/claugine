"""Hardware profiles → Boot storage (owner 2026-11-18 12:17 / 12:40, T25; the tab since 2026-11-19): the boot-device
layouts that server profiles
use, one per profile. A host's boot device is always GPT with EFI (512 MiB), /boot (1 GiB) and an LVM volume group over
the rest; a layout lists the logical volumes in that group: mount point, size in GiB (or the rest) and file system.
The first layout (made by migration inventory 0094) is the one the hosts have: / 50, /var/lib 200, /var/log 100 and
/home the rest, all xfs, in the volume group vg_data (logical volumes lv_root, lv_lib, lv_log, lv_home). A layout in use by a profile cannot be deleted; a change is checked against
the boot device of every profile that uses it.
"""

import re

from django import forms
from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from accounts.models import Role, audit
from accounts.utils import staff_required

from .models import LAYOUT_FS, DiskLayout, mount_point, reserved_mount


class LayoutError(Exception):
    pass


class LayoutForm(forms.Form):
    code = forms.RegexField(label=_l("Code"), regex=r"^(?!new$)[a-z][a-z0-9-]{0,30}$", help_text=_l("e.g. standard"))
    title = forms.CharField(label=_l("Description"), max_length=140, required=False)
    volumes = forms.CharField(
        label=_l("Volumes"),
        widget=forms.Textarea(attrs={"rows": 6, "class": "mono"}),
        help_text=_l("Mount point, size in GiB (empty or -1: the rest) and file system of each logical volume."),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .network_views import rows_widget

        rows_widget(
            self.fields["volumes"],
            [
                ("path", _("Mount point"), "/var/log"),
                ("size", _("Size, GiB"), "100", "6em"),
                ("fs", _("File system"), "", "7em", LAYOUT_FS),
            ],
            _("Volume"),
        )

    def clean_volumes(self):
        return parse_volumes(self.cleaned_data.get("volumes") or "")


def parse_volumes(text):
    """ "/var/log 100 xfs" lines → [{"path", "size_gib", "fs"}]; size empty or -1: the rest (None)."""
    out = []
    for line in str(text or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        path = parts[0]
        size, fs = None, "xfs"
        rest = parts[1:]
        if rest and re.match(r"^-?\d+$", rest[0]):
            n = int(rest.pop(0))
            size = None if n < 0 else n
        if rest:
            fs = rest[0]
        if fs not in LAYOUT_FS:
            raise forms.ValidationError(_l("Not a file system: %(l)s") % {"l": line.strip()})
        if fs == "swap":
            path = "swap"
        else:  # commit-118: one spelling, and never /boot or under it (the fixed partitions)
            norm = mount_point(path) if re.match(r"^/[A-Za-z0-9/_.-]*$", path) else ""
            if not norm or reserved_mount(norm):
                raise forms.ValidationError(_l("Not a mount point: %(l)s") % {"l": line.strip()})
            path = norm
        if size == 0:
            raise forms.ValidationError(_l("A volume needs a size: %(l)s") % {"l": line.strip()})
        out.append({"path": path, "size_gib": size, "fs": fs})
    if not out:
        raise forms.ValidationError(_l("Give the volumes."))
    return out


def text(layout):
    return "\n".join(
        f"{v['path']} {'-1' if v.get('size_gib') is None else v['size_gib']} {v.get('fs') or 'xfs'}"
        for v in layout.volumes or []
    )


def save(user, code, title, volumes, new=False):
    """commit-120: in one transaction with the layout's row locked, so a profile taking it meanwhile is seen."""
    from django.db import transaction

    with transaction.atomic():
        return _save(user, code, title, volumes, new)


def _save(user, code, title, volumes, new):
    lay = DiskLayout.objects.select_for_update().filter(code=code).first()
    if new and lay is not None:
        raise LayoutError(f"Boot storage {code} already exists.")
    if not new and lay is None:
        raise LayoutError(f"Boot storage {code} does not exist.")
    lay = lay or DiskLayout(code=code)
    lay.title, lay.volumes = title, volumes
    why = lay.problems()
    for p in lay.profiles.all() if lay.pk else []:
        why += [f"{p.code}: {w}" for w in lay.problems((p.boot or {}).get("size_gb")) if w not in why]
    if why:
        raise LayoutError(f"Boot storage {code}: {'; '.join(why)}.")
    lay.save()
    audit(user, "disk_layout_saved", lay, code=code, new=new)
    return lay


def delete(user, code):
    from django.db import transaction

    with transaction.atomic():
        return _delete(user, code)


def _delete(user, code):
    lay = DiskLayout.objects.select_for_update().filter(code=code).first()
    if lay is None:
        raise LayoutError(f"Boot storage {code} does not exist.")
    used = list(lay.profiles.values_list("code", flat=True))
    if used:
        raise LayoutError(f"Boot storage {code} is used by {', '.join(used)}.")
    lay.delete()
    audit(user, "disk_layout_deleted", None, code=code)


@staff_required(Role.SUPPORT, Role.RESOURCE_MANAGER)
def layouts_view(request):
    layouts = list(DiskLayout.objects.prefetch_related("profiles"))
    for lay in layouts:
        lay.form = LayoutForm(
            initial={"code": lay.code, "title": lay.title, "volumes": text(lay)}, auto_id=f"id_dl_{lay.code}_%s"
        )
        lay.form.fields["code"].disabled = True
        lay.profile_list = list(lay.profiles.all())
        lay.issues = lay.problems()
        for p in lay.profile_list:
            lay.issues += [f"{p.code}: {w}" for w in lay.problems((p.boot or {}).get("size_gb")) if w not in lay.issues]
    return render(
        request,
        "inventory/layouts.html",
        {
            "layouts": layouts,
            "can_edit": request.user.is_super_admin,
            "layout_form": LayoutForm(auto_id="id_dl_new_%s", initial={"volumes": "/ 50 xfs\n/home -1 xfs"}),
        },
    )


@require_POST
@staff_required(super_only=True)
def layout_save_view(request, code=None):
    new = code is None
    form = LayoutForm(request.POST)
    if not new:
        form.fields["code"].required = False
    if form.is_valid():
        d = form.cleaned_data
        code = code or d["code"]
        try:
            save(request.user, code, d.get("title") or "", d["volumes"], new=new)
            messages.success(request, _("Boot storage saved."))
        except LayoutError as exc:
            messages.error(request, str(exc))
    else:
        for f, errs in form.errors.items():
            messages.error(request, f"{form.fields[f].label if f in form.fields else ''}: {' '.join(errs)}")
    return redirect(reverse("infra_layouts") + (f"?sel=dl-{code}" if code else ""))


@require_POST
@staff_required(super_only=True)
def layout_delete_view(request, code):
    try:
        delete(request.user, code)
        messages.success(request, _("Boot storage deleted."))
    except LayoutError as exc:
        messages.error(request, str(exc))
    return redirect(reverse("infra_layouts"))
