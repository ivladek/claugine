"""The site igw's own template, cloud-igw (correction 12): the stored tenant igw template with the management part (as
the site igw was built until now: the tenant igw + nfconfig's management part). Made once; then edited in
Infrastructure → Templates. Backward: removed."""
from django.db import migrations


def forward(apps, schema_editor):
    from inventory import nf_templates as T
    ConfigTemplate = apps.get_model("inventory", "ConfigTemplate")
    if ConfigTemplate.objects.filter(role="cloud-igw").exists():
        return
    igw = ConfigTemplate.objects.filter(role="igw").first()
    body = (igw.body if igw else T.IGW).rstrip("\n") + "\n" + T.CLOUD_IGW_MGMT
    ConfigTemplate.objects.create(role="cloud-igw", name="cloud-igw — the site's igw", os="vyos", os_version="2026.03",
                                  description=T.CLOUD_IGW_DESCRIPTION, body=body)


def backward(apps, schema_editor):
    apps.get_model("inventory", "ConfigTemplate").objects.filter(role="cloud-igw").delete()


class Migration(migrations.Migration):
    dependencies = [("inventory", "0069_portal_gw")]
    operations = [migrations.RunPython(forward, backward)]
