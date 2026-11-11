from django.db import migrations, models


class Migration(migrations.Migration):
    """owner u12: the super-admin request covers granting too, and stores how many must agree (snapshot at request)."""

    dependencies = [("accounts", "0016_platform_org_role_eligibility")]

    operations = [
        migrations.AddField(
            model_name="superadminremoval",
            name="op",
            field=models.CharField(choices=[("add", "grant"), ("remove", "remove")], default="remove", max_length=8),
        ),
        migrations.AddField(
            model_name="superadminremoval",
            name="required",
            field=models.PositiveSmallIntegerField(default=1,
                                                   help_text="super admins who must agree, including the asker"),
        ),
    ]
