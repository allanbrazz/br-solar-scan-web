# Generated manually for user CSV meteorological sources.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0044_plantdetectorconfiguration"),
    ]

    operations = [
        migrations.AlterField(
            model_name="meteoimportbatch",
            name="source",
            field=models.CharField(
                choices=[
                    ("OPENMETEO", "Open-Meteo"),
                    ("NSRDB", "NSRDB (NREL)"),
                    ("USER_CSV", "CSV do usuario"),
                ],
                db_index=True,
                default="OPENMETEO",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="meteorecord",
            name="source",
            field=models.CharField(
                choices=[
                    ("OPENMETEO", "Open-Meteo"),
                    ("NSRDB", "NSRDB (NREL)"),
                    ("USER_CSV", "CSV do usuario"),
                ],
                default="OPENMETEO",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="pvplantmergedrecord15m",
            name="source_meteo",
            field=models.CharField(
                choices=[
                    ("OPENMETEO", "Open-Meteo"),
                    ("NSRDB", "NSRDB (NREL)"),
                    ("USER_CSV", "CSV do usuario"),
                ],
                db_index=True,
                default="OPENMETEO",
                max_length=20,
            ),
        ),
    ]
