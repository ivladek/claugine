"""SSH keys of a tenant: upload several (pasted or as .pub files), assign them to the igw when needed."""
import re

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from accounts.models import audit

from .forms import SSH_KEY_RE
from .models import Membership, Tenant, TenantSshKey

NAME_RE = re.compile(r"[^A-Za-z0-9_.@-]+")


def can_manage(tenant, user):
    return user.is_authenticated and not tenant.is_deleted and (
        user.is_super_admin or tenant.membership_role(user) in (Membership.Role.ORG_ADMIN, Membership.Role.TENANT_ADMIN))


def _back(t):
    return redirect(reverse("tenant_detail", args=[t.pk]) + "#sshkeys")


def _name_for(tenant, key, given=""):
    """A key name for VyOS (public-keys <name>): the given one, the key's comment, or key<N>; unique in the tenant."""
    base = NAME_RE.sub("-", given or (" ".join(key.split()[2:]) or "key")).strip("-")[:48] or "key"
    name, n = base, 1
    while tenant.ssh_keys.filter(name=name).exists():
        n += 1
        name = f"{base}-{n}"
    return name


def parse_keys(text):
    """(keys, bad lines) from pasted text or files: one key per line, blank and # lines skipped."""
    keys, bad = [], []
    for line in (text or "").splitlines():
        line = " ".join(line.split())
        if not line or line.startswith("#"):
            continue
        (keys if re.match(SSH_KEY_RE, line) else bad).append(line)
    return keys, bad


def igw_commands(tenant):
    """VyOS commands that make the running igw match the assigned keys (to paste on the igw)."""
    user = tenant.igw_username
    if not user:
        return ""
    out = [f"delete system login user {user} authentication public-keys"]
    for k in tenant.ssh_keys.filter(on_igw=True):
        out += [f"set system login user {user} authentication public-keys {k.name} key {k.parts[1]}",
                f"set system login user {user} authentication public-keys {k.name} type {k.key_type}"]
    return "\n".join(out + ["commit", "save"])


def _posted_keys(request):
    text = request.POST.get("keys") or ""
    for f in request.FILES.getlist("files")[:20]:
        if f.size <= 64 * 1024:
            text += "\n" + f.read().decode("utf-8", "replace")
    return parse_keys(text)


@require_POST
def ssh_keys_add_view(request, pk):
    t = get_object_or_404(Tenant, pk=pk)
    if not can_manage(t, request.user):
        raise PermissionDenied
    keys, bad = _posted_keys(request)
    added = add_keys(request, t, keys, request.POST.get("on_igw") == "on")
    _report(request, added, bad)
    return back_or(request, _back(t))


def back_or(request, default):
    from .views import back_to
    return back_to(request, default.url)


def _report(request, added, bad):
    if added:
        messages.success(request, _("%(n)s key(s) added.") % {"n": added})
    if bad:
        messages.error(request, _("Not ssh public keys, skipped: %(n)s line(s).") % {"n": len(bad)})
    if not added and not bad:
        messages.info(request, _("No new keys."))


def add_keys(request, t, keys, assign):
    added = 0
    existing = {" ".join(k.key.split()[:2]) for k in t.ssh_keys.all()}
    for key in keys:
        if " ".join(key.split()[:2]) in existing:
            continue
        given = (request.POST.get("name") or "").strip() if len(keys) == 1 else ""
        k = TenantSshKey.objects.create(tenant=t, name=_name_for(t, key, given), key=key, on_igw=assign,
                                        created_by=request.user)
        existing.add(" ".join(key.split()[:2]))
        added += 1
        audit(request.user, "ssh_key_added", t, name=k.name, fingerprint=k.fingerprint, on_igw=assign)
    if assign and added:
        t.ssh_keys.first().sync_igw_key()
    return added


@require_POST
def ssh_key_action_view(request, pk, kid, action):
    t = get_object_or_404(Tenant, pk=pk)
    k = get_object_or_404(TenantSshKey, pk=kid, tenant=t)
    if not can_manage(t, request.user):
        raise PermissionDenied
    if action in ("unassign", "delete") and k.on_igw and t.ssh_keys.filter(on_igw=True).count() == 1 and t.igw_username:
        messages.error(request, _("The igw needs at least one key: ssh login is by key only. Assign another key first."))
        return back_or(request, _back(t))
    if action == "assign":
        k.on_igw = True
        k.save(update_fields=["on_igw"])
    elif action == "unassign":
        k.on_igw = False
        k.save(update_fields=["on_igw"])
    elif action == "delete":
        audit(request.user, "ssh_key_deleted", t, name=k.name, fingerprint=k.fingerprint)
        k.delete()
        TenantSshKey(tenant=t).sync_igw_key()
        messages.success(request, _("Key deleted."))
        return back_or(request, _back(t))
    else:
        raise PermissionDenied
    k.sync_igw_key()
    audit(request.user, f"ssh_key_{action}ed", t, name=k.name, fingerprint=k.fingerprint)
    messages.success(request, _("Keys of the igw changed. A running igw takes them when the commands below are applied on it."))
    return back_or(request, _back(t))


def org_keys_view(request):
    """Organization → Keys: the ssh keys of every tenant the person manages; add to several tenants at once."""
    from django.shortcuts import render
    from .forms import SshKeyForm
    user = request.user
    if not user.is_authenticated or not user.organization_id:
        raise PermissionDenied
    org = user.organization
    tenants = [t for t in org.tenants.exclude(status__in=["saved", "archived", "deleting"]).order_by("name") if can_manage(t, user)]
    if request.method == "POST" and request.POST.get("section") == "add":
        keys, bad = _posted_keys(request)
        picked = set(request.POST.getlist("tenants"))
        added = sum(add_keys(request, t, keys, request.POST.get("on_igw") == "on") for t in tenants if str(t.pk) in picked)
        if not picked:
            messages.error(request, _("Choose the projects to add the keys to."))
        else:
            _report(request, added, bad)
        return redirect("org_keys")
    key_form = None
    if user.is_org_admin:
        key_form = SshKeyForm(request.POST or None, prefix="key", initial={"default_ssh_key": org.default_ssh_key})
        if request.method == "POST" and key_form.is_valid():
            org.default_ssh_key = key_form.cleaned_data["default_ssh_key"]
            org.save(update_fields=["default_ssh_key"])
            audit(user, "org_ssh_key_changed", org, set=bool(org.default_ssh_key))
            messages.success(request, _("Default ssh key saved."))
            return redirect("org_keys")
    keys = list(TenantSshKey.objects.filter(tenant__in=tenants).select_related("tenant", "created_by"))
    parts = (org.default_ssh_key or "").split()
    default_type = parts[0] if parts else ""
    try:
        default_fp = TenantSshKey(key=org.default_ssh_key).fingerprint if parts else ""
    except Exception:  # noqa: BLE001 - shown as is
        default_fp = ""
    return render(request, "tenants/org_keys.html", {"org": org, "keys": keys, "tenants": tenants, "key_form": key_form,
                                                      "default_type": default_type, "default_fp": default_fp})


@require_POST
def config_check_view(request, pk):
    """Check the configuration of the tenant's routers now (staff and the tenant's admins)."""
    from . import configcheck
    t = get_object_or_404(Tenant, pk=pk)
    user = request.user
    staff = user.is_authenticated and user.is_staff_member and (user.is_super_admin or user.has_role("support")
                                                                or user.has_role("resource_manager"))
    if t.is_deleted or not (staff or can_manage(t, user)):
        raise PermissionDenied
    c = configcheck.start(t, user)
    if c.state == "running":
        messages.info(request, _("Configuration check started."))
    return redirect(reverse("tenant_detail", args=[t.pk]) + "#configcheck")
