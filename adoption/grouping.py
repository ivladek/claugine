"""Group adopted tenants into organizations by the legal name the old system kept.

The old system has free-text legal names ("Example Cloud", "ТОО “Example Cloud”", "Example CLoud").
They are reduced to a key (legal forms, quotes, IIN/BIN digits and case removed) and tenants with the
same key share one organization. A 12-digit number with a valid check digit inside the name becomes
the organization's IIN/BIN. Staff can regroup afterwards: move a tenant to another organization or merge
two organizations.
"""
import re
from collections import Counter, defaultdict

from django.db import transaction

from accounts.kz import guess_tax_id_type, tax_id_checksum_ok
from accounts.models import User, audit
from tenants.models import Approval, Organization, OrgKind, Origin, Tenant, TenantKind

LEGAL_FORMS = [
    "товарищество с ограниченной ответственностью", "объединение юридических лиц и индивидуальных предпринимателей",
    "акционерное общество", "тоо", "ао", "гу", "фл", "ип", "too", "llp", "jsc", "llc", "ltd", "inc",
]
QUOTES = "\"'«»“”„<>"


def tax_id_in(text):
    digits = re.sub(r"(?<=\d) (?=\d)", "", text or "")
    for m in re.findall(r"(?<!\d)\d{12}(?!\d)", digits):
        if tax_id_checksum_ok(m):
            return m
    return None


def clean_name(text):
    """The legal name without an IIN/BIN in it."""
    t = re.sub(r"(?<=\d) (?=\d)", "", text or "")
    t = re.sub(r"(?<!\d)\d{12}(?!\d)", "", t)
    return " ".join(t.split()).strip(" ,.-")


def name_key(text):
    t = clean_name(text).lower()
    for ch in QUOTES:
        t = t.replace(ch, " ")
    for form in LEGAL_FORMS:
        t = re.sub(rf"(?<![\w]){re.escape(form)}(?![\w])", " ", t)
    return " ".join(re.sub(r"[^\w.]+", " ", t).split())


def _best_name(names):
    """Prefer the most frequent spelling; among equals the one with a legal form (more formal)."""
    counts = Counter(clean_name(n) for n in names if clean_name(n))
    return sorted(counts, key=lambda n: (-counts[n], -int(bool(re.search(r"ТОО|АО|LLP|ГУ", n))), len(n)))[0]


def plan():
    """[(key, name, tax_id, [tenants], existing organization or None)] for adopted tenants without an organization."""
    tenants = list(Tenant.objects.filter(origin=Origin.ADOPTED, organization__isnull=True)
                   .exclude(approval__in=[Approval.REJECTED, Approval.CANCELLED]).order_by("name"))
    groups = defaultdict(list)
    for t in tenants:
        key = name_key(t.hint_legal_name) or f"tenant:{t.name}"
        groups[key].append(t)
    existing = {name_key(o.name): o for o in Organization.objects.all()}
    by_tax = {o.tax_id: o for o in Organization.objects.exclude(tax_id__isnull=True)}
    out = []
    for key, members in sorted(groups.items()):
        names = [t.hint_legal_name for t in members if t.hint_legal_name]
        tax = next((tax_id_in(n) for n in names if tax_id_in(n)), None)
        org = by_tax.get(tax) if tax else None
        org = org or existing.get(key)
        name = _best_name(names) if names else members[0].name
        out.append((key, name, tax, members, org))
    return out


@transaction.atomic
def group(actor=None):
    """Create or reuse organizations for adopted tenants without one. Returns counts."""
    created = linked = 0
    for _key, name, tax, members, org in plan():
        if org is None:
            org = Organization.objects.create(name=name, tax_id=tax, tax_id_type=guess_tax_id_type(tax) if tax else "")
            audit(actor, "organization_saved", org, created=True, source="grouping", name=name)
            created += 1
        if any(t.kind == TenantKind.CORP for t in members) and org.kind != OrgKind.CORP:
            org.kind = OrgKind.CORP
            org.save(update_fields=["kind"])
        if not org.sales_rep_id:
            emails = Counter(t.hint_manager_email for t in members if t.hint_manager_email)
            if emails:
                rep = User.objects.filter(email=emails.most_common(1)[0][0], is_sales=True).first()
                if rep:
                    org.sales_rep = rep
                    org.save(update_fields=["sales_rep"])
        for t in members:
            t.organization = org
            t.save(update_fields=["organization"])
            audit(actor, "tenant_moved", t, to=org.name, source="grouping")
            linked += 1
    return {"organizations": created, "tenants": linked}


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
