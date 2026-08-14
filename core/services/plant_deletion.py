from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from django.db import transaction
from django.db.models import QuerySet

from core.models import (
    DataIngestState,
    FaultEvent,
    FaultEventMPPT,
    GroundTruthEvent,
    InverterOperationalData,
    InverterSample,
    MeteoImportBatch,
    MeteoRecord,
    MPPTDiagnostic15m,
    PlantDetectorConfiguration,
    PlantDiagnostic15m,
    PlantMonitoringCredential,
    PlantPerformanceRatio,
    PVPlant,
    PVPlantDetails,
    PVPlantMergedRecord15m,
    PVPlantStringConfig,
)


@dataclass(frozen=True)
class PlantDataCounter:
    key: str
    label: str
    count: int


QueryFactory = Callable[[int], QuerySet]


_RELATED_COUNTERS: tuple[tuple[str, str, QueryFactory], ...] = (
    (
        "meteo_records",
        "Registros meteorológicos",
        lambda plant_id: MeteoRecord.objects.filter(plant_id=plant_id),
    ),
    (
        "meteo_batches",
        "Lotes de importação meteorológica",
        lambda plant_id: MeteoImportBatch.objects.filter(plant_id=plant_id),
    ),
    (
        "operational_records",
        "Registros operativos",
        lambda plant_id: InverterOperationalData.objects.filter(plant_id=plant_id),
    ),
    (
        "inverter_samples",
        "Amostras brutas do inversor",
        lambda plant_id: InverterSample.objects.filter(plant_id=plant_id),
    ),
    (
        "merged_records",
        "Registros merged 15 min",
        lambda plant_id: PVPlantMergedRecord15m.objects.filter(plant_id=plant_id),
    ),
    (
        "performance_ratios",
        "Performance ratio corrigido",
        lambda plant_id: PlantPerformanceRatio.objects.filter(plant_id=plant_id),
    ),
    (
        "plant_diagnostics",
        "Diagnósticos FDD da planta",
        lambda plant_id: PlantDiagnostic15m.objects.filter(plant_id=plant_id),
    ),
    (
        "mppt_diagnostics",
        "Diagnósticos FDD por MPPT",
        lambda plant_id: MPPTDiagnostic15m.objects.filter(plant_id=plant_id),
    ),
    (
        "fault_events",
        "Eventos de falha",
        lambda plant_id: FaultEvent.objects.filter(plant_id=plant_id),
    ),
    (
        "fault_event_mppt",
        "Diagnósticos MPPT por evento",
        lambda plant_id: FaultEventMPPT.objects.filter(event__plant_id=plant_id),
    ),
    (
        "ground_truth_events",
        "Eventos de validação",
        lambda plant_id: GroundTruthEvent.objects.filter(plant_id=plant_id),
    ),
    (
        "details",
        "Detalhes cadastrais",
        lambda plant_id: PVPlantDetails.objects.filter(plant_id=plant_id),
    ),
    (
        "string_configs",
        "Configurações de strings",
        lambda plant_id: PVPlantStringConfig.objects.filter(details__plant_id=plant_id),
    ),
    (
        "credentials",
        "Credenciais de monitoramento",
        lambda plant_id: PlantMonitoringCredential.objects.filter(plant_id=plant_id),
    ),
    (
        "detector_configurations",
        "Configurações do detector",
        lambda plant_id: PlantDetectorConfiguration.objects.filter(plant_id=plant_id),
    ),
    (
        "ingest_states",
        "Estados de ingestão",
        lambda plant_id: DataIngestState.objects.filter(plant_id=plant_id),
    ),
)


def summarize_plant_related_data(plant: PVPlant) -> dict:
    plant_id = int(plant.pk)
    counters = [
        PlantDataCounter(key=key, label=label, count=int(factory(plant_id).count()))
        for key, label, factory in _RELATED_COUNTERS
    ]
    counts = {item.key: item.count for item in counters}
    return {
        "plant_id": plant_id,
        "plant_name": plant.nome,
        "items": counters,
        "counts": counts,
        "total_related": sum(item.count for item in counters),
    }


def delete_plant_with_related_data(plant: PVPlant) -> dict:
    with transaction.atomic():
        locked_plant = PVPlant.objects.select_for_update().get(pk=plant.pk)
        summary = summarize_plant_related_data(locked_plant)
        deleted_total, deleted_by_model = locked_plant.delete()

    return {
        **summary,
        "deleted_total": int(deleted_total),
        "deleted_by_model": {key: int(value) for key, value in deleted_by_model.items()},
    }
