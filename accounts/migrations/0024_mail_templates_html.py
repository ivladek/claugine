"""Owner 2026-11-17: one HTML template per e-mail. A text template edited in Settings → Mail is carried over as the
HTML template of the same e-mail (its paragraphs, lists and buttons, as the portal drew the text until now). The old
rows stay (rollback-safe); the portal no longer reads them."""

from django.db import migrations

CONVERTED = {
    "auto_update_failed", "backup_failed", "join_decided", "join_request_owner", "join_request_staff", "mail_test",
    "member_invitation", "network_requested", "org_assigned", "org_registration_approved", "org_registration_pending",
    "org_registration_rejected", "organization_invitation", "password_reset", "staff_invitation",
    "super_admin_removal_closed", "super_admin_removal_requested", "tenant_contract_ends", "tenant_credentials",
    "tenant_deletion_requested", "tenant_move_requested", "tenant_pending", "tenant_poc_ends", "tenant_rejected",
    "verify_email",
}  # fmt: skip


def forward(apps, schema_editor):
    from accounts.mailhtml import html_from_text_template

    MailTemplate = apps.get_model("accounts", "MailTemplate")
    for m in MailTemplate.objects.filter(name__endswith=".txt"):
        base = m.name[:-4]
        if base in CONVERTED and not MailTemplate.objects.filter(name=f"{base}.html").exists():
            MailTemplate.objects.create(
                name=f"{base}.html", body=html_from_text_template(m.body), updated_by_id=m.updated_by_id
            )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0023_superadminremoval_archive")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
