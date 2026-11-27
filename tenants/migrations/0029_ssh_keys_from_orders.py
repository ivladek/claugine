from django.db import migrations


def seed(apps, schema_editor):
    """The key given with each order becomes the tenant's first key, assigned to the igw."""
    Tenant = apps.get_model("tenants", "Tenant")
    Key = apps.get_model("tenants", "TenantSshKey")
    for t in Tenant.objects.exclude(igw_ssh_key=""):
        if not Key.objects.filter(tenant=t).exists():
            Key.objects.create(tenant=t, name=(t.igw_username or "igw-admin")[:64], key=t.igw_ssh_key, on_igw=True)


class Migration(migrations.Migration):
    dependencies = [("tenants", "0028_tenantsshkey")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
