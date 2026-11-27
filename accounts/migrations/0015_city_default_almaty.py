"""Owner 2026-10-31: the default city (for a user who does not choose one) becomes Almaty, matching the site.
Only the field default changes; existing users' cities are untouched."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0014_project_wording"),
    ]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="city",
            field=models.CharField(
                choices=[
                    ("astana", "Astana"),
                    ("almaty", "Almaty"),
                    ("shymkent", "Shymkent"),
                    ("aktau", "Aktau"),
                    ("aktobe", "Aktobe"),
                    ("atyrau", "Atyrau"),
                    ("balkhash", "Balkhash"),
                    ("ekibastuz", "Ekibastuz"),
                    ("karaganda", "Karaganda"),
                    ("kokshetau", "Kokshetau"),
                    ("konaev", "Konaev"),
                    ("kostanay", "Kostanay"),
                    ("kyzylorda", "Kyzylorda"),
                    ("oral", "Oral (Uralsk)"),
                    ("oskemen", "Oskemen (Ust-Kamenogorsk)"),
                    ("pavlodar", "Pavlodar"),
                    ("petropavl", "Petropavl"),
                    ("rudny", "Rudny"),
                    ("semey", "Semey"),
                    ("taldykorgan", "Taldykorgan"),
                    ("taraz", "Taraz"),
                    ("temirtau", "Temirtau"),
                    ("turkistan", "Turkistan"),
                    ("zhanaozen", "Zhanaozen"),
                    ("zhezkazgan", "Zhezkazgan"),
                    ("other", "Other locality"),
                ],
                default="almaty",
                max_length=32,
                verbose_name="city",
            ),
        ),
    ]
