from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0099_mail_template_names"),
    ]

    operations = [
        migrations.AddField(
            model_name="portalsettings",
            name="fe_backup_login",
            field=models.CharField(blank=True, max_length=200, verbose_name="connect to Front-Ends using"),
        ),
    ]
