from decimal import Decimal

from django.db import migrations


def seed_renovigi_catalog(apps, schema_editor):
    PVModule = apps.get_model("core", "PVModule")
    PVInverter = apps.get_model("core", "PVInverter")

    PVModule.objects.update_or_create(
        fabricante="Renovigi",
        nome="RENO-R 550",
        defaults={
            "pmp_w": Decimal("550.00"),
            "vmp_v": Decimal("41.960"),
            "imp_a": Decimal("13.110"),
            "voc_v": Decimal("49.900"),
            "isc_a": Decimal("14.000"),
            "eficiencia_pct": Decimal("21.30"),
            "power_tolerance": "+-10",
            "num_celulas": 72,
            "temp_coeff_voc_pct_c": Decimal("-0.350"),
            "temp_coeff_isc_pct_c": Decimal("0.050"),
            "rs_ohm": Decimal("0.0630"),
            "rp_ohm": Decimal("256.660"),
            "diode_a": Decimal("1.300"),
        },
    )

    PVInverter.objects.update_or_create(
        fabricante="Renovigi",
        modelo="RENO-5K-PLUS",
        defaults={
            "p_ac_nom_w": Decimal("5500.00"),
            "v_ac_nom_v": 220,
            "vdc_mppt_min_v": Decimal("90.0"),
            "vdc_mppt_max_v": Decimal("520.0"),
            "vdc_abs_max_v": Decimal("600.0"),
            "mppt_count": 2,
            "strings_por_mppt_max": 1,
            "eficiencia_max_pct": Decimal("98.10"),
        },
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0038_accountnotification")]

    operations = [
        migrations.RunPython(seed_renovigi_catalog, migrations.RunPython.noop),
    ]
