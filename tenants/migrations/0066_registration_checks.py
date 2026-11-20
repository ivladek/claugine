"""Owner 2026-10-29 20:30: the checks of a registration (accounts.regcheck)."""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tenants", "0065_igw_size")]

    operations = [
        migrations.AddField(model_name="organization", name="reg_check", field=models.JSONField(blank=True, default=dict)),
        migrations.AddField(model_name="registration", name="reg_check", field=models.JSONField(blank=True, default=dict)),
    ]
