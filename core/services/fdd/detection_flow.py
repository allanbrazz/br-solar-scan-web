from __future__ import annotations

from typing import Any, Dict, Iterable, Optional, Sequence


LEGACY_HYBRID = "legacy_hybrid"
THESIS_SEQUENTIAL = "thesis_sequential"
DEFAULT_DETECTION_FLOW_MODE = LEGACY_HYBRID

DETECTION_FLOW_LABELS = {
    LEGACY_HYBRID: "Legado - estatistica + regras",
    THESIS_SEQUENTIAL: "Sequencial - EWMA/CUSUM -> RCA",
}

LEGACY_RUNTIME_NORMAL_LABELS = {"", "ok", "normal", "invalid", "low_irradiance"}
LEGACY_BATCH_NORMAL_LABELS = {"ok", "invalid"}
OPERATIONAL_NORMAL_LABELS = {"", "ok", "normal", "invalid", "low_irradiance", "telemetry_invalid"}


def normalize_detection_flow_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    if mode in {LEGACY_HYBRID, THESIS_SEQUENTIAL}:
        return mode
    return DEFAULT_DETECTION_FLOW_MODE


def detection_flow_label(mode: Any) -> str:
    return DETECTION_FLOW_LABELS.get(normalize_detection_flow_mode(mode), DETECTION_FLOW_LABELS[DEFAULT_DETECTION_FLOW_MODE])


def detector_version_for_flow(base_version: Any, mode: Any) -> str:
    base = str(base_version or "mismatch_runtime_v1").strip() or "mismatch_runtime_v1"
    flow = normalize_detection_flow_mode(mode)
    if flow != THESIS_SEQUENTIAL:
        return base
    if base == "mismatch_runtime_v1":
        return "mismatch_runtime_v2_sequential"
    if base == "hybrid_rules_v1":
        return "hybrid_rules_v2_sequential"
    if base.endswith("_sequential") or base.endswith("_thesis_sequential"):
        return base
    return f"{base}_sequential"


def _norm_label(label: Any) -> str:
    return str(label or "").strip().lower()


def is_abnormal_diagnosis_label(label: Any, normal_labels: Optional[Iterable[str]] = None) -> bool:
    normals = {_norm_label(item) for item in (normal_labels or OPERATIONAL_NORMAL_LABELS)}
    return _norm_label(label) not in normals


def decide_anomaly_flag(
    *,
    detection_flow_mode: Any,
    residual_anomaly: Any,
    diagnosis_label: Any = "",
    direct_grid_evidence: Any = False,
    normal_labels: Optional[Iterable[str]] = None,
) -> bool:
    mode = normalize_detection_flow_mode(detection_flow_mode)
    if mode == THESIS_SEQUENTIAL:
        return bool(residual_anomaly)
    normals = normal_labels or LEGACY_RUNTIME_NORMAL_LABELS
    return bool(residual_anomaly) or bool(direct_grid_evidence) or is_abnormal_diagnosis_label(diagnosis_label, normals)


def decide_operational_deviation_flag(
    *,
    diagnosis_label: Any = "",
    direct_grid_evidence: Any = False,
    zero_injection_flag: Any = False,
    rca_code: Any = 0,
    operational_threshold: Any = False,
) -> bool:
    try:
        code_hit = int(rca_code or 0) > 0
    except Exception:
        code_hit = False
    return bool(
        operational_threshold
        or direct_grid_evidence
        or zero_injection_flag
        or code_hit
        or is_abnormal_diagnosis_label(diagnosis_label, OPERATIONAL_NORMAL_LABELS)
    )


def _at(seq: Optional[Sequence[Any]], idx: int, default: Any = None) -> Any:
    try:
        if seq is None:
            return default
        return seq[idx]
    except Exception:
        return default


def build_anomaly_flags(
    *,
    detection_flow_mode: Any,
    residual_anomalies: Sequence[Any],
    diagnosis_labels: Optional[Sequence[Any]] = None,
    direct_grid_evidence: Optional[Sequence[Any]] = None,
    normal_labels: Optional[Iterable[str]] = None,
) -> list[bool]:
    return [
        decide_anomaly_flag(
            detection_flow_mode=detection_flow_mode,
            residual_anomaly=residual,
            diagnosis_label=_at(diagnosis_labels, idx, ""),
            direct_grid_evidence=_at(direct_grid_evidence, idx, False),
            normal_labels=normal_labels,
        )
        for idx, residual in enumerate(residual_anomalies or [])
    ]


def build_operational_deviation_flags(
    *,
    diagnosis_labels: Optional[Sequence[Any]] = None,
    direct_grid_evidence: Optional[Sequence[Any]] = None,
    zero_injection_flags: Optional[Sequence[Any]] = None,
    rca_codes: Optional[Sequence[Any]] = None,
    operational_thresholds: Optional[Sequence[Any]] = None,
    n: Optional[int] = None,
) -> list[bool]:
    if n is None:
        n = max(
            len(seq or [])
            for seq in (diagnosis_labels, direct_grid_evidence, zero_injection_flags, rca_codes, operational_thresholds)
        )
    return [
        decide_operational_deviation_flag(
            diagnosis_label=_at(diagnosis_labels, idx, ""),
            direct_grid_evidence=_at(direct_grid_evidence, idx, False),
            zero_injection_flag=_at(zero_injection_flags, idx, False),
            rca_code=_at(rca_codes, idx, 0),
            operational_threshold=_at(operational_thresholds, idx, False),
        )
        for idx in range(int(n or 0))
    ]


def build_detection_flow_audit(
    *,
    detection_flow_mode: Any,
    valid: Optional[Sequence[Any]],
    ewma_flags: Optional[Sequence[Any]],
    cusum_flags: Optional[Sequence[Any]],
    residual_anomalies: Sequence[Any],
    operational_deviation_flags: Sequence[Any],
    anomaly_flags: Sequence[Any],
    diagnosis_labels: Optional[Sequence[Any]] = None,
    n_events: int = 0,
) -> Dict[str, Any]:
    n = max(len(residual_anomalies or []), len(anomaly_flags or []))

    def count_true(seq: Optional[Sequence[Any]]) -> int:
        return sum(1 for item in (seq or []) if bool(item))

    diagnosis_without_anomaly = 0
    for idx in range(n):
        if is_abnormal_diagnosis_label(_at(diagnosis_labels, idx, ""), OPERATIONAL_NORMAL_LABELS) and not bool(_at(anomaly_flags, idx, False)):
            diagnosis_without_anomaly += 1

    return {
        "detection_flow_mode": normalize_detection_flow_mode(detection_flow_mode),
        "detection_flow_label": detection_flow_label(detection_flow_mode),
        "n_valid": count_true(valid),
        "n_ewma_alert": count_true(ewma_flags),
        "n_cusum_alert": count_true(cusum_flags),
        "n_residual_anomaly": count_true(residual_anomalies),
        "n_operational_deviation": count_true(operational_deviation_flags),
        "n_anomaly_flag": count_true(anomaly_flags),
        "n_events": int(n_events or 0),
        "anomaly_equals_residual": all(bool(_at(anomaly_flags, idx, False)) == bool(_at(residual_anomalies, idx, False)) for idx in range(n)),
        "diagnosis_without_anomaly": int(diagnosis_without_anomaly),
    }

