from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0043_remove_confirmed_legacy_belarmino"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PlantDetectorConfiguration",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120, verbose_name="Nome da configuracao")),
                ("config", models.JSONField(default=dict)),
                ("is_default", models.BooleanField(db_index=True, default=False, verbose_name="Configuracao padrao")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="detector_configurations_created", to=settings.AUTH_USER_MODEL)),
                ("plant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="detector_configurations", to="core.pvplant")),
            ],
            options={"ordering": ["-is_default", "name"]},
        ),
        migrations.AddConstraint(
            model_name="plantdetectorconfiguration",
            constraint=models.UniqueConstraint(fields=("plant", "name"), name="uniq_detector_config_plant_name"),
        ),
        migrations.AddIndex(
            model_name="plantdetectorconfiguration",
            index=models.Index(fields=["plant", "is_default"], name="idx_detector_cfg_default"),
        ),
    ]
