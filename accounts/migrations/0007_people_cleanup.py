from django.db import migrations

ROLES = ("is_super_admin", "is_resource_manager", "is_support", "is_sales", "is_kam")


def cleanup(apps, schema_editor):
    """Archived people who never got in are deleted (they are not kept as archived); archived people and platform
    users who were never invited (or whose invitation is gone) have no roles; a sales rep directory entry keeps its
    place in the directory."""
    from django.db.models import ProtectedError
    from django.utils import timezone
    User = apps.get_model("accounts", "User")
    Invitation = apps.get_model("accounts", "Invitation")
    Membership = apps.get_model("tenants", "Membership")
    now = timezone.now()

    def accepted(u):
        return u.login_enabled and u.password and not u.password.startswith("!")

    for u in User.objects.filter(archived_at__isnull=False):
        if u.last_login is None and not accepted(u):
            try:
                Invitation.objects.filter(user=u).delete()
                u.delete()
                continue
            except ProtectedError:
                pass
        User.objects.filter(pk=u.pk).update(is_org_admin=False, is_org_readonly=False, is_staff=False, is_superuser=False,
                                            **{f: False for f in ROLES})
        Membership.objects.filter(user=u).delete()
    for u in User.objects.filter(kind="staff", archived_at__isnull=True):
        if accepted(u) or u.last_login is not None:
            continue
        open_ = Invitation.objects.filter(user=u, accepted_at__isnull=True, revoked_at__isnull=True,
                                          expires_at__gt=now).exists() or \
            Invitation.objects.filter(email__iexact=u.email, accepted_at__isnull=True, revoked_at__isnull=True,
                                      expires_at__gt=now).exists()
        if open_:
            continue
        directory = u.is_sales and not u.login_enabled
        values = {f: False for f in ROLES}
        values.update(is_staff=False, is_superuser=False, is_sales=directory)
        User.objects.filter(pk=u.pk).update(**values)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0006_password_policy"), ("tenants", "0036_password_policy")]
    operations = [migrations.RunPython(cleanup, migrations.RunPython.noop)]
