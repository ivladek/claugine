"""commit-089/090: additional public prefixes added from now on are egw routes only; the ones already assigned with an
OpenNebula network of their own keep it (own_network, vnet_id)."""

from django.db import migrations, models


def mark_existing(apps, schema_editor):
    """commit-089: own_network and vnet_id only for a prefix this assignment's own lifetime recorded with a network.

    The records are the deployments' public_networks rows of the same tenant and site. When the tenant had the same
    prefix before (a released assignment), a record may be that earlier one's, so only deployments started after this
    assignment was made count; otherwise every record of the site is this assignment's (a network change records into
    the deployment that was the newest then, which may be older than the assignment)."""
    PrefixAssignment = apps.get_model("tenants", "PrefixAssignment")
    Deployment = apps.get_model("tenants", "Deployment")
    for p in PrefixAssignment.objects.filter(kind="inet_extra", released_at__isnull=True, tenant__isnull=False):
        earlier = (
            PrefixAssignment.objects.filter(tenant_id=p.tenant_id, site=p.site, cidr=p.cidr, released_at__isnull=False)
            .exclude(pk=p.pk)
            .exists()
        )
        deps = Deployment.objects.filter(tenant_id=p.tenant_id).exclude(state="plan").order_by("-started_at", "-pk")
        newer = deps.filter(started_at__gte=p.created_at) if earlier else deps
        if not _mark(p, newer) and earlier and _mark(p, deps, check=True):
            # commit-103: only a deployment older than this assignment records the prefix - this assignment's network or
            # the earlier one's. No network is made for it; its removal stops while a recorded one is still there.
            p.network_check = True
            p.save(update_fields=["network_check"])


def _mark(p, deps, check=False):
    """Whether a deployment among `deps` (same site) records the prefix with a network; sets own_network and vnet_id
    from the newest such record unless `check`."""
    for dep in deps:
        if (dep.platform_key or "").partition(".")[0] != p.site:
            continue
        row = next(
            (
                r
                for r in (dep.resources or {}).get("public_networks") or []
                if r.get("prefix") == p.cidr and str(r.get("id") or "").isdigit()
            ),
            None,
        )
        if row is not None:
            if not check:
                p.own_network, p.vnet_id = True, int(row["id"])
                p.save(update_fields=["own_network", "vnet_id"])
            return True
    return False


class Migration(migrations.Migration):
    dependencies = [("tenants", "0101_fe_backup_run_claim")]

    operations = [
        migrations.AddField(
            model_name="prefixassignment", name="own_network", field=models.BooleanField(default=False)
        ),
        migrations.AddField(
            model_name="prefixassignment", name="vnet_id", field=models.PositiveIntegerField(blank=True, null=True)
        ),
        migrations.AddField(
            model_name="prefixassignment", name="network_check", field=models.BooleanField(default=False)
        ),
        migrations.RunPython(mark_existing, migrations.RunPython.noop),
    ]
