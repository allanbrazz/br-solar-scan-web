from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from core.models import PVPlant
from core.services.fdd.events import EventBuildParams, build_fault_events_for_range
from core.services.fdd.dashboard_common import MISMATCH_VERSION_SUMMARY, canonical_source_oper, upsert_diag15m
from core.services.fdd.detection_flow import (
    LEGACY_RUNTIME_NORMAL_LABELS,
    build_anomaly_flags,
    build_operational_deviation_flags,
    detection_flow_label,
    detector_version_for_flow,
)
from core.services.fdd.runtime_types import MismatchDashboardParams

def persist_runtime_outputs(
    *,
    plant: PVPlant,
    params: MismatchDashboardParams,
    src_meteo: str,
    selected_sources: List[str],
    times_utc: List[datetime],
    model: Dict[str, Any],
    agg: Dict[str, Any],
    pipeline: Dict[str, Any],
    confidence: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if not params.persist:
        return None
    base_detector_version = str(MISMATCH_VERSION_SUMMARY.get("detector_version") or "mismatch_runtime_v1")
    detector_version = detector_version_for_flow(base_detector_version, params.detection_flow_mode)
    canonical_oper = canonical_source_oper(selected_sources)
    final_anomaly_flags = list(pipeline.get("anomaly_flag") or build_anomaly_flags(
        detection_flow_mode=params.detection_flow_mode,
        residual_anomalies=pipeline["anomaly"],
        diagnosis_labels=confidence["diag_diagnosis_labels"],
        direct_grid_evidence=confidence["diag_direct_grid"],
        normal_labels=LEGACY_RUNTIME_NORMAL_LABELS,
    ))
    operational_deviation_flags = list(pipeline.get("operational_deviation_flag") or build_operational_deviation_flags(
        diagnosis_labels=confidence["diag_diagnosis_labels"],
        direct_grid_evidence=confidence["diag_direct_grid"],
        zero_injection_flags=confidence["diag_zero_inj"],
        rca_codes=pipeline["codes"],
        n=len(times_utc),
    ))
    detector_scores_runtime: List[Optional[float]] = []
    alarm_code_runtime: List[Optional[int]] = []
    alarm_sev_runtime: List[Optional[int]] = []
    for i in range(len(times_utc)):
        diag_label = str(confidence["diag_diagnosis_labels"][i] or "")
        detector_scores_runtime.append(max(
            abs(float(pipeline["ewma_z"][i])) if pipeline["ewma_z"][i] is not None else 0.0,
            float(pipeline["cusum_score"][i]) if pipeline["cusum_score"][i] is not None else 0.0,
            abs(float(model["mismatch_rel"][i])) if model["mismatch_rel"][i] is not None and diag_label not in {"ok", "invalid"} else 0.0,
        ))
        alarm_code_runtime.append(int(pipeline["alarm_code"][i]) if pipeline["alarm_code"][i] is not None else None)
        alarm_sev_runtime.append(int(pipeline["alarm_sev"][i]) if pipeline["alarm_sev"][i] is not None else None)

    upsert_diag = upsert_diag15m(
        plant=plant,
        source_oper=canonical_oper,
        source_meteo=src_meteo,
        detector_version=detector_version,
        times_utc=times_utc,
        rca_codes=pipeline["codes"],
        rca_labels=pipeline["labels"],
        valid=pipeline["valid_period"],
        anomaly_flags=final_anomaly_flags,
        detector_scores=detector_scores_runtime,
        ewma_z=pipeline["ewma_z"],
        cusum_scores=pipeline["cusum_score"],
        stable_sky=pipeline["stable_sky"],
        g_poa=model["g_poa_used"],
        tcell_c=model["tcell_c"],
        pac_real_w=agg["p_ac_w"],
        pac_model_w=model["pac_model_w"],
        mismatch_rel=model["mismatch_rel"],
        irradiance_tier=pipeline["irradiance_tier"],
        fine_diag_allowed=pipeline["fine_period"],
        meteo_quality_ok=pipeline["meteo_quality_ok"],
        direct_grid_evidence=confidence["diag_direct_grid"],
        zero_injection_flag=confidence["diag_zero_inj"],
        state_labels=confidence["diag_state_labels"],
        domain_labels=confidence["diag_domain_labels"],
        diagnosis_labels=confidence["diag_diagnosis_labels"],
        diagnosis_confidence_score=confidence["diagnosis_confidence_score"],
        data_reliability_score=confidence["data_reliability_score"],
        data_reliability_level=confidence["data_reliability_level"],
        detection_confidence_score=confidence["detection_confidence_score"],
        detection_confidence_level=confidence["detection_confidence_level"],
        diagnosis_confidence_level=confidence["diagnosis_confidence_level"],
        v_ac_v=agg["v_ac_v"],
        i_ac_a=agg["i_ac_a"],
        freq_hz=pipeline["freq_hz"],
        alarm_code_oper=alarm_code_runtime,
        alarm_sev_oper=alarm_sev_runtime,
        evidence_json=confidence["diag_evidence_json"],
        confidence_notes_json=confidence["confidence_notes"],
        detection_flow_mode=params.detection_flow_mode,
        residual_anomaly_flags=pipeline["anomaly"],
        operational_deviation_flags=operational_deviation_flags,
    )
    upsert_events = build_fault_events_for_range(
        plant_id=plant.id,
        ts_start_utc=params.dt0_utc,
        ts_end_utc=params.dt1_utc,
        params=EventBuildParams(
            detector_version=detector_version,
            source_oper=canonical_oper,
            source_meteo=src_meteo,
            replace_existing=True,
            detection_flow_mode=params.detection_flow_mode,
        ),
    )
    return {
        "diagnostics": upsert_diag,
        "events": upsert_events,
        "detection_flow_mode": params.detection_flow_mode,
        "detection_flow_label": detection_flow_label(params.detection_flow_mode),
        "detector_version": detector_version,
    }

