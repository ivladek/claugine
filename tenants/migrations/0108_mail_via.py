from django.db import migrations, models


class Migration(migrations.Migration):
    """Owner 2026-11-23 (A): Settings → Mail "Send mail from" - the node every portal mail leaves from."""

    dependencies = [("tenants", "0107_machine_logging")]

    operations = [
        migrations.AddField(
            model_name="portalsettings",
            name="mail_via",
            field=models.CharField(blank=True, default="", max_length=16, verbose_name="send mail from"),
        ),
    ]
