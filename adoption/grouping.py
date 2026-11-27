"""Moving a project to another organization, and merging two organizations (staff regrouping)."""

from django.db import transaction

from accounts.models import User, audit
from tenants.models import OrgKind


@transaction.atomic
def move_tenant(tenant, org, actor):
    old = tenant.organization
    tenant.organization = org
    tenant.save(update_fields=["organization"])
    audit(actor, "tenant_moved", tenant, frm=old.name if old else "", to=org.name)
    return old


@transaction.atomic
def merge(source, target, actor):
    """Move every tenant, person and join request of `source` into `target`; `source` stays, empty."""
    n_t = source.tenants.update(organization=target)
    n_u = User.objects.filter(organization=source).update(organization=target)
    source.join_requests.update(organization=target)
    if not target.sales_rep_id and source.sales_rep_id:
        target.sales_rep_id = source.sales_rep_id
        target.save(update_fields=["sales_rep"])
    if source.kind == OrgKind.CORP and target.kind != OrgKind.CORP:
        target.kind = OrgKind.CORP
        target.save(update_fields=["kind"])
    audit(actor, "organization_merged", target, frm=source.name, tenants=n_t, users=n_u)
    return n_t, n_u
