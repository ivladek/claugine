"""Owner 2026-10-29 13:21: no e-mail to non-platform users until it is unblocked in Settings."""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tenants", "0056_igw_configs_c14")]
    operations = [migrations.AddField(
        model_name="portalsettings", name="client_mail",
        field=models.BooleanField(default=False, help_text="Off: the portal mails only platform (staff) users; messages to anybody else are held and logged.", verbose_name="e-mail to clients"))]
