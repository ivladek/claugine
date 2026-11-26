"""T34 S2, commit-387: the fields migration 0114 records what it changes in, added on their own - on MySQL an added
column stays even when a later step of the same migration fails, so the conversion (0114) runs apart and can be
retried after a refused attempt. commit-386: ServiceProfile.legacy (the profile's values before 0114); commit-388:
ConfigTemplate.origin ("0114" on every template it made, kept when a profile is deleted).

commit-389/390: each column is added only when its table lacks it - a run stopped between the two (MySQL commits each
ADD COLUMN at once), or a database that ran the earlier, unreleased form of 0113, completes; the reverse drops only
the columns that are there. commit-392: each field is attached to the historical model first."""

from django.db import migrations, models

COLUMNS = (
    ("ServiceProfile", "legacy", lambda: models.JSONField(blank=True, default=dict, editable=False)),
    ("ConfigTemplate", "origin", lambda: models.CharField(blank=True, editable=False, max_length=16)),
)


def _has(schema_editor, model, column):
    conn = schema_editor.connection
    with conn.cursor() as cur:
        return column in {c.name for c in conn.introspection.get_table_description(cur, model._meta.db_table)}


def _field(apps, model_name, name, make):
    """commit-392: the field attached to the historical model, so a table SQLite rebuilds (no DROP COLUMN before
    3.35) is rebuilt from a model that declares it."""
    model = apps.get_model("inventory", model_name)
    try:
        return model, model._meta.get_field(name)
    except Exception:  # noqa: BLE001 - FieldDoesNotExist: the state before 0113
        field = make()
        field.contribute_to_class(model, name)
        return model, field


def add_missing(apps, schema_editor):
    for model_name, name, make in COLUMNS:
        model, field = _field(apps, model_name, name, make)
        if not _has(schema_editor, model, field.column):
            schema_editor.add_field(model, field)


def drop_present(apps, schema_editor):
    for model_name, name, make in reversed(COLUMNS):
        model, field = _field(apps, model_name, name, make)
        if _has(schema_editor, model, field.column):
            schema_editor.remove_field(model, field)


class Migration(migrations.Migration):
    dependencies = [("inventory", "0112_service_templates")]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(model_name=m.lower(), name=n, field=make()) for m, n, make in COLUMNS
            ],
            database_operations=[migrations.RunPython(add_missing, drop_present)],
        )
    ]
