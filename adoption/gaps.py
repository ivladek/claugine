"""What is still missing before a tenant is fully described in the portal."""
from django.utils.translation import gettext as _

from tenants.models import Connectivity, PrefixAssignment, TenantKind, TenantStage


def gaps_for(t, prefixes=None, conns=None):
    out = []
    org = t.organization
    if org is None:
        out.append(_("organization"))
    else:
        if not org.tax_id:
            out.append(_("IIN/BIN"))
        if t.kind == TenantKind.CLOUD and not org.sales_rep_id:
            out.append(_("sales rep"))
    if t.stage == TenantStage.CLIENT and not t.contract_end:
        out.append(_("contract"))
    conns = conns if conns is not None else t.current_connectivity()
    kinds = prefixes if prefixes is not None else {(p.site, p.kind) for p in PrefixAssignment.active().filter(tenant=t)}
    for c in conns:
        if "corpnet" in c.types and (c.site, PrefixAssignment.Kind.CORPNET) not in kinds:
            out.append(_("corporate subnet on %(s)s") % {"s": c.site})
        if "inet" in c.types and (c.site, PrefixAssignment.Kind.INET_MAIN) not in kinds:
            out.append(_("internet subnet on %(s)s") % {"s": c.site})
    return out
