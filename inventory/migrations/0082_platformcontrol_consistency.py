from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("inventory", "0081_edge_and_gateway_selectors")]
    operations = [migrations.AddField(model_name="platformcontrol", name="consistency",
                                      field=models.JSONField(blank=True, default=dict))]
