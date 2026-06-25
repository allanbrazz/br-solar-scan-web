# Generated manually for CAMS Solar Radiation Service meteorological source.

from django.db import migrations, models


METEO_SOURCE_CHOICES = [
    ("OPENMETEO", "Open-Meteo"),
    ("CAMS", "CAMS Solar Radiation Service"),
    ("NSRDB", "NSRDB (NREL)"),
    ("USER_CSV", "CSV do usuario"),
]


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0047_plantmonitoringcredential_growatt_datalogger_sn_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="meteoimportbatch",
            name="source",
            field=models.CharField(
                choices=METEO_SOURCE_CHOICES,
                db_index=True,
                default="OPENMETEO",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="meteorecord",
            name="source",
            field=models.CharField(
                choices=METEO_SOURCE_CHOICES,
                default="OPENMETEO",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="pvplantmergedrecord15m",
            name="source_meteo",
            field=models.CharField(
                choices=METEO_SOURCE_CHOICES,
                db_index=True,
                default="OPENMETEO",
                max_length=20,
            ),
        ),
    ]
