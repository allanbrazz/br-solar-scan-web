from django.db import migrations


def remove_confirmed_legacy_belarmino(apps, schema_editor):
    PVPlant = apps.get_model("core", "PVPlant")
    InverterOperationalData = apps.get_model("core", "InverterOperationalData")

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
    if InverterOperationalData.objects.filter(plant_id=keep.pk).count() < 100:
        return

    legacy.delete()
    keep.nome = "Belarmino"
    keep.timezone = "America/Maceio"
    keep.save(update_fields=["nome", "timezone"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0042_finalize_belarmino_cleanup"),
    ]

    operations = [
        migrations.RunPython(remove_confirmed_legacy_belarmino, migrations.RunPython.noop),
    ]
