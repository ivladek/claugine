"""What every page needs: the navigation for this user, the counts beside it, the version in the top bar.

A context processor, so it runs on **every** render: keep it cheap and read-only. The version shown is the release
tag from `portal/version.py` with the installed commit beside it.
"""
import functools
from django.conf import settings
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from content.models import Article
from content.views import docs_links

from .models import Role


def _approvals_label(user):
    if not user.is_staff_member:
        return ""
    from tenants.views import pending_for
    n = pending_for(user)
    return f"{_('Approval requests')} ({n})" if n else _("Approval requests")


def _site_items(request, allowed):
    """One menu item per site (hosts, networks, storage, platforms, address registry)."""
    if not allowed:
        return []
    from inventory.models import Site
    match = getattr(request, "resolver_match", None)
    here = match.kwargs.get("code") if match and match.url_name == "infra_site" else None
    return [(f"{s.code} · {s.city}" if s.city else s.code, reverse("infra_site", args=[s.code]), True, s.code == here)
            for s in Site.objects.all()]


def _nav(request, user):
    """Side menu: [(section title, [(label, url name, active)])], only what the user may open."""
    rm = user.has_role(Role.RESOURCE_MANAGER)
    support = user.has_role(Role.SUPPORT)
    staff = user.is_staff_member
    sup = user.is_super_admin
    current = getattr(getattr(request, "resolver_match", None), "url_name", "") or ""
    org = user.is_customer and bool(user.organization_id)
    org_admin = org and user.is_org_admin
    sections = [
        (_("Organization"), [
            (_("Dashboard"), "dashboard", org, ("dashboard",)),
            (_("Projects"), "my_org", org, ("my_org", "new_tenant", "tenant_detail", "tenant_password",
                                          "tenant_resource_request")),
            (_("Users"), "org_users", org_admin, ("org_users", "person")),
            (_("Keys"), "org_keys", org, ("org_keys",)),
            (_("Tasks"), "org_tasks", org_admin, ("org_tasks",)),
            (_("Logs"), "org_logs", org_admin, ("org_logs",)),
        ]),
        (_("Cloud"), [
            (_("Dashboard"), "dashboard", not org, ("dashboard",)),
            (_("Organizations"), "org_list", staff, ("org_list", "org_detail", "org_edit", "org_new", "org_invite")),
            (_("Projects"), "tenant_list", staff, ("tenant_list", "tenant_detail")),
        ]),
        (_("Infrastructure"), [
            (_("Sites"), "inventory", rm or support, ("inventory",)),
            (_("Server profiles"), "infra_profiles", rm or support, ("infra_profiles",)),
            (_("Templates"), "infra_templates", rm or support, ("infra_templates",)),
            (_("Secrets"), "infra_passwords", rm or support, ("infra_passwords",)),
        ] + _site_items(request, rm or support)),
        (_("Content"), [
            (_("News"), "manage_articles", support, ("manage_articles", "article_edit", "article_new")),
        ]),
        (_("Administration"), [
            (_approvals_label(user), "approvals", staff, ("approvals",)),
            (_("Users"), "staff_users", sup, ("staff_users", "staff_roles", "sales_rep_edit", "sales_rep_new")),
            (_("Sales reps"), "sales_reps", rm and not sup, ("sales_reps",)),
            (_("Settings"), "portal_settings", sup, ("portal_settings",)),
            (_("Tasks"), "tasks", sup, ("tasks",)),
            (_("Logs"), "audit_log", sup, ("audit_log",)),
        ]),
        (_("Help"), [
            (_("Documentation") + " ↗", docs_links()["root"], True, False),
        ]),
    ]
    out = []
    for title, items in sections:
        visible = [(label, url if url.startswith(("/", "http")) else reverse(url), on if isinstance(on, bool) else current in on)
                   for label, url, allowed, on in items if allowed]
        if visible:
            out.append((title, visible))
    return out


@functools.lru_cache(maxsize=1)
def _asset_version():
    """A version for the page's own CSS and JS links: browsers fetch them again after a release (2026-10-28: a cached
    portal.css hid the new site form layout)."""
    import os
    from django.contrib.staticfiles import finders
    v = 0
    for name in ("portal.css", "olist.js"):
        path = finders.find(name)
        if not path and settings.STATIC_ROOT:
            path = os.path.join(settings.STATIC_ROOT, name)
        try:
            v = max(v, int(os.path.getmtime(path)))
        except (OSError, TypeError):
            pass
    return v


def _portal_version():
    """owner 2026-11-04: the running build for the top bar — the release and its date, e.g. "v01.00.00 2026-11-11".
    Any missing part is left out."""
    try:
        from portal.version import VERSION, DATE
    except Exception:
        VERSION = DATE = ""
    return " ".join(p for p in (VERSION, DATE) if p)


def portal(request):
    user = request.user
    from tenants.models import PortalSettings
    partial = request.headers.get("X-Partial") == "1" or request.GET.get("partial") == "1"
    ps = PortalSettings.get()
    ctx = {
        "base_template": "partial.html" if partial else "base.html",
        "asset_v": _asset_version(),
        "portal_version": _portal_version(),
        "list_rows": ps.list_rows or 10,
        "Role": Role,
        "one_sunstone_url": settings.ONE_SUNSTONE_URL,
        "docs": docs_links(),
        "incident_banners": Article.visible(Article.Kind.INCIDENT)[:3],
    }
    if user.is_authenticated:
        is_staff = user.is_staff_member
        rm = user.has_role(Role.RESOURCE_MANAGER)
        support = user.has_role(Role.SUPPORT)
        ctx.update(
            is_super=user.is_super_admin,
            nav_staff=is_staff,
            nav_tenants=is_staff,
            nav_orgs=is_staff,
            nav_capacity=rm or support,
            nav_inventory=support,
            nav_publish=support,
            nav_customer=user.is_customer,
            side_nav=_nav(request, user),
        )
        # owner 2026-11-04: licence/update banner for super admins when the control plane flags grace/frozen/
        # conflict, and retired since 2026-11-09 - a released installation must say so, not sit there quietly
        _st = ps.auto_update_state or {}
        if user.is_super_admin and _st.get("lic_state") in ("grace", "frozen", "conflict", "retired"):
            ctx["licence_notices"] = [n for n in (_st.get("notices") or []) if n][:3]
    return ctx
