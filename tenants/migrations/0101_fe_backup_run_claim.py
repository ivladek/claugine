from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0100_fe_backup_login"),
    ]

    operations = [
        migrations.AddField(
            model_name="portalsettings",
            name="fe_backup_run_id",
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name="portalsettings",
            name="fe_backup_run_node",
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.AddField(
            model_name="portalsettings",
            name="fe_backup_run_since",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
