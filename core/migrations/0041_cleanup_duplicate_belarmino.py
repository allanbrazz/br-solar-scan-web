from django.db import migrations


def cleanup_duplicate_belarmino(apps, schema_editor):
    PVPlant = apps.get_model("core", "PVPlant")
    InverterOperationalData = apps.get_model("core", "InverterOperationalData")
    PVPlantMergedRecord15m = apps.get_model("core", "PVPlantMergedRecord15m")

    legacy = PVPlant.objects.filter(pk=1, nome__iexact="Belarmino").first()
    keep = PVPlant.objects.filter(pk=2).first()
    if legacy is None or keep is None:
        return
    if str(keep.nome or "").casefold() not in {"belarmino", "berlarmino"}:
        return

    legacy_operational = InverterOperationalData.objects.filter(plant_id=legacy.pk).count()
    legacy_merged = PVPlantMergedRecord15m.objects.filter(plant_id=legacy.pk).count()
    keep_operational = InverterOperationalData.objects.filter(plant_id=keep.pk).count()

    if legacy_operational != 0 or legacy_merged != 0:
        return
    if keep.timezone != "America/Maceio" or keep_operational < 100:
        return

    legacy.delete()
    keep.nome = "Belarmino"
    keep.timezone = "America/Maceio"
    keep.save(update_fields=["nome", "timezone"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0040_mark_existing_account_notifications"),
    ]

    operations = [
        migrations.RunPython(cleanup_duplicate_belarmino, migrations.RunPython.noop),
    ]
