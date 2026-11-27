"""Owner 2026-11-23 (Profiles updates U2, Q8): the VyOS release in the baseline ("Stream 2026.03", a reference value)
and the OpenNebula repository as public or private (with its secret) - no address field. An installation whose
repository asks for a login, or names the enterprise repository, is private."""

from django.db import migrations, models


def private_from_login(apps, schema_editor):
    PortalSettings = apps.get_model("tenants", "PortalSettings")
    for s in PortalSettings.objects.all():
        private = bool(s.one_repo_login) or "enterprise.opennebula.io" in (s.one_repo or "")
        if private:
            PortalSettings.objects.filter(pk=s.pk).update(one_repo_private=True)


class Migration(migrations.Migration):
    dependencies = [("tenants", "0108_mail_via")]

    operations = [
        migrations.AddField(
            model_name="portalsettings",
            name="one_repo_private",
            field=models.BooleanField(default=False, verbose_name="OpenNebula private repository"),
        ),
        migrations.AddField(
            model_name="portalsettings",
            name="baseline_vyos",
            field=models.CharField(default="Stream 2026.03", max_length=32, verbose_name="VyOS version"),
        ),
        migrations.RunPython(private_from_login, migrations.RunPython.noop),
    ]
