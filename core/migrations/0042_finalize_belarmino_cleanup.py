from django.db import migrations


def finalize_belarmino_cleanup(apps, schema_editor):
    PVPlant = apps.get_model("core", "PVPlant")
    InverterOperationalData = apps.get_model("core", "InverterOperationalData")
    PVPlantMergedRecord15m = apps.get_model("core", "PVPlantMergedRecord15m")

    legacy = PVPlant.objects.filter(
        pk=1,
        nome__iexact="Belarmino",
        timezone="America/Sao_Paulo",
    ).first()
    keep = PVPlant.objects.filter(pk=2, timezone="America/Maceio").first()
    if legacy is None or keep is None:
        return
    if str(keep.nome or "").casefold() not in {"belarmino", "berlarmino"}:
        return

    legacy_operational = InverterOperationalData.objects.filter(plant_id=legacy.pk).count()
    legacy_merged = PVPlantMergedRecord15m.objects.filter(plant_id=legacy.pk).count()
    keep_operational = InverterOperationalData.objects.filter(plant_id=keep.pk).count()

    if legacy_operational > 5 or legacy_merged > 5 or keep_operational < 100:
        return

    legacy.delete()
    keep.nome = "Belarmino"
    keep.timezone = "America/Maceio"
    keep.save(update_fields=["nome", "timezone"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0041_cleanup_duplicate_belarmino"),
    ]

    operations = [
        migrations.RunPython(finalize_belarmino_cleanup, migrations.RunPython.noop),
    ]
