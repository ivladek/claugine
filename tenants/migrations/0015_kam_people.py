from django.db import migrations


def kam_people(apps, schema_editor):
    """Each key account manager email becomes a staff person with the KAM role (not active until invited)."""
    Organization = apps.get_model("tenants", "Organization")
    User = apps.get_model("accounts", "User")
    for org in Organization.objects.exclude(kam_email=""):
        email = org.kam_email.strip().lower()
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            user = User.objects.create(email=email, kind="staff", is_kam=True, login_enabled=False,
                                       email_verified=True, password="!")
        elif user.kind != "staff":
            continue
        elif not user.is_kam:
            user.is_kam = True
            user.save(update_fields=["is_kam"])
        if org.kind != "corp":
            org.kam_id = user.pk
            org.save(update_fields=["kam"])


class Migration(migrations.Migration):
    dependencies = [("tenants", "0014_org_staffing"), ("accounts", "0004_kam_role")]
    operations = [migrations.RunPython(kam_people, migrations.RunPython.noop)]
