from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Literal, Optional

import pandas as pd

from core.models import PVPlant
from core.services.series_juntar.timeseries_io import FetchConfig, fetch_inverter_df, fetch_meteo_df
from core.services.series_juntar.timeseries_merge import (
    InverterAggregationConfig,
    MeteoPreparationConfig,
    aggregate_inverter_to_15min,
    densify_15min_grid,
    join_inverter_meteo_15min,
    prepare_meteo_15min,
    rollup_15min_to_hour,
)
from core.services.series_juntar.time_shift_alignment import (
    TimeShiftAlignmentConfig,
    TimeShiftAlignmentResult,
    estimate_time_shift_alignment,
)
from core.services.series_juntar.merged15m_store import upsert_merged_15m_df


JoinHow = Literal["left", "inner", "right", "outer"]


@dataclass(frozen=True)
class MergeRunResult:
    df15: pd.DataFrame
    df_hour: pd.DataFrame
    stats: Dict[str, Any]


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _safe_iso(dt_like) -> str:
    try:
        if dt_like is None:
            return ""
        if hasattr(dt_like, "to_pydatetime"):
            dt_like = dt_like.to_pydatetime()
        if getattr(dt_like, "tzinfo", None) is not None:
            dt_like = dt_like.astimezone(timezone.utc)
        return dt_like.isoformat()
    except Exception:
        return str(dt_like)


def _safe_float(x, default: float = 0.0) -> float:
    try:
        if x is None or x is pd.NA:
            return default
        if pd.isna(x):
            return default
        return float(x)
    except Exception:
        return default


def _time_shift_fetch_padding_minutes(cfg: Optional[TimeShiftAlignmentConfig]) -> int:
    if cfg is None:
        return 0
    mode = str(getattr(cfg, "mode", "none") or "none").lower()
    if mode in ("auto", "suggest_only"):
        return int(abs(getattr(cfg, "max_abs_shift_minutes", 0) or 0))
    if mode == "manual":
        return int(abs(getattr(cfg, "manual_shift_minutes", 0.0) or 0.0))
    return 0


def _apply_alignment_to_configs(
    *,
    inv_cfg: InverterAggregationConfig,
    met_cfg: MeteoPreparationConfig,
    alignment: TimeShiftAlignmentResult,
) -> tuple[InverterAggregationConfig, MeteoPreparationConfig]:
    op_shift = float(getattr(alignment, "operational_shift_minutes", 0.0) or 0.0)
    met_shift = float(getattr(alignment, "meteo_shift_minutes", 0.0) or 0.0)

    inv_cfg_out = replace(inv_cfg, oper_time_shift_minutes=op_shift)
    met_cfg_out = replace(met_cfg, meteo_time_shift_minutes=met_shift)
    return inv_cfg_out, met_cfg_out


def build_plant_merged_dataset(
    *,
    plant: PVPlant,
    dt_start_utc: datetime,
    dt_end_utc: datetime,
    want_hourly: bool = True,
    fetch_cfg: FetchConfig = FetchConfig(),
    inv_cfg: InverterAggregationConfig = InverterAggregationConfig(ts_col="ts_utc", freq="15min"),
    met_cfg: MeteoPreparationConfig = MeteoPreparationConfig(ts_col="ts_utc", freq="15min"),
    how: JoinHow = "left",
    persist: bool = True,
    source_oper: str = "SHINEMONITOR",
    source_meteo: str = "OPENMETEO",
    interval_min: int = 15,
    densify: bool = True,
    time_shift_cfg: Optional[TimeShiftAlignmentConfig] = None,
) -> MergeRunResult:
    if how not in ("left", "inner", "right", "outer"):
        raise ValueError("how inválido. Use: left|inner|right|outer")

    dt_start_utc = _ensure_utc(dt_start_utc)
    dt_end_utc = _ensure_utc(dt_end_utc)
    if dt_end_utc <= dt_start_utc:
        raise ValueError("dt_end_utc deve ser maior que dt_start_utc")

    # A calibração temporal precisa enxergar dados antes/depois do intervalo final,
    # caso o deslocamento escolhido mova amostras para dentro da janela analisada.
    padding_min = _time_shift_fetch_padding_minutes(time_shift_cfg)
    dt_fetch_start_utc = dt_start_utc - timedelta(minutes=padding_min)
    dt_fetch_end_utc = dt_end_utc + timedelta(minutes=padding_min)

    # 1) Extrai do banco. A janela pode estar expandida apenas para permitir
    # calibração/aplicação de deslocamento; a malha final é densificada no intervalo
    # dt_start_utc <= ts < dt_end_utc.
    df_inv = fetch_inverter_df(
        plant=plant,
        dt_start_utc=dt_fetch_start_utc,
        dt_end_utc=dt_fetch_end_utc,
        cfg=fetch_cfg,
    )
    df_met = fetch_meteo_df(
        plant=plant,
        dt_start_utc=dt_fetch_start_utc,
        dt_end_utc=dt_fetch_end_utc,
        cfg=fetch_cfg,
    )

    inv_meta = (df_inv.attrs.get("meta") or {}) if hasattr(df_inv, "attrs") else {}

    # 2) Calibração temporal física entre telemetria e modelo DC.
    # A rotina automática compara P_DC medido agregado em 15 min contra
    # P_DC_model calculado pelo modelo físico a partir da meteorologia e do
    # cadastro da planta. O cadastro físico é lido aqui para evitar que a
    # calibração dependa de rótulos ou saídas do detector FDD.
    if time_shift_cfg is None:
        time_shift_cfg = TimeShiftAlignmentConfig(mode="none")

    try:
        details = getattr(plant, "details", None)
    except Exception:
        details = None

    alignment = estimate_time_shift_alignment(
        df_inv=df_inv,
        df_met=df_met,
        cfg=time_shift_cfg,
        inv_cfg=inv_cfg,
        met_cfg=met_cfg,
        plant=plant,
        details=details,
        dt_start_utc=dt_start_utc,
        dt_end_utc=dt_end_utc,
    )
    inv_cfg_eff, met_cfg_eff = _apply_alignment_to_configs(
        inv_cfg=inv_cfg,
        met_cfg=met_cfg,
        alignment=alignment,
    )

    # 3) Agrega/prepara com o deslocamento decidido pela etapa de alinhamento.
    inv15 = aggregate_inverter_to_15min(df_inv, cfg=inv_cfg_eff, tz_work="UTC", assume_tz_if_naive="UTC")
    met15 = prepare_meteo_15min(df_met, cfg=met_cfg_eff, tz_work="UTC", assume_tz_if_naive="UTC")

    # 4) Join e densify no intervalo final solicitado.
    if densify:
        # join outer para não perder buckets meteo-only
        df15_raw = join_inverter_meteo_15min(inv15, met15, how="outer")
        df15 = densify_15min_grid(
            df15_raw,
            start_utc=dt_start_utc,
            end_utc=dt_end_utc,
            freq=inv_cfg_eff.freq,
            coverage_threshold=inv_cfg_eff.coverage_threshold,
        )
    else:
        df15 = join_inverter_meteo_15min(inv15, met15, how=how)
        if not df15.empty:
            t0 = pd.Timestamp(dt_start_utc)
            t1 = pd.Timestamp(dt_end_utc)
            t0 = t0.tz_localize("UTC") if t0.tzinfo is None else t0.tz_convert("UTC")
            t1 = t1.tz_localize("UTC") if t1.tzinfo is None else t1.tz_convert("UTC")
            df15 = df15[(df15.index >= t0) & (df15.index < t1)]

    expected_15 = int((dt_end_utc - dt_start_utc).total_seconds() // (15 * 60))

    alignment_dict = alignment.to_dict()
    if not df15.empty:
        df15.attrs["time_shift_alignment"] = alignment_dict
        df15.attrs["oper_time_shift_applied_minutes"] = float(alignment.operational_shift_minutes)
        df15.attrs["meteo_time_shift_applied_minutes"] = float(alignment.meteo_shift_minutes)

    stats: Dict[str, Any] = {
        "plant_id": getattr(plant, "id", None),
        "dt_start_utc": dt_start_utc.isoformat(),
        "dt_end_utc": dt_end_utc.isoformat(),
        "dt_fetch_start_utc": dt_fetch_start_utc.isoformat(),
        "dt_fetch_end_utc": dt_fetch_end_utc.isoformat(),
        "how": ("outer+dense" if densify else how),

        "inv_rows_raw": int(inv_meta.get("inv_rows_raw", len(df_inv))),
        "inv_rows_in_window": int(inv_meta.get("inv_rows_in_window", len(df_inv))),
        "plant_tz": str(inv_meta.get("plant_tz", "UTC")),

        # Correção anterior em timeseries_io, baseada no timestamp do payload.
        "ts_shift_h_median": _safe_float(inv_meta.get("ts_shift_h_median", 0.0), 0.0),
        "ts_shift_h_min": _safe_float(inv_meta.get("ts_shift_h_min", 0.0), 0.0),
        "ts_shift_h_max": _safe_float(inv_meta.get("ts_shift_h_max", 0.0), 0.0),
        "ts_shift_applied_minutes_io": _safe_float(inv_meta.get("ts_shift_applied_minutes", 0.0), 0.0),

        # Nova calibração física por RMSE entre P_DC medido e P_DC_model calculado pelo modelo físico.
        "time_shift_mode": alignment.mode,
        "time_shift_applied": bool(alignment.applied),
        "time_shift_apply_target": alignment.apply_target,
        "time_shift_selected_minutes": float(alignment.selected_shift_minutes),
        "oper_time_shift_applied_minutes": float(alignment.operational_shift_minutes),
        "meteo_time_shift_applied_minutes": float(alignment.meteo_shift_minutes),
        "time_shift_confidence": alignment.confidence,
        "time_shift_reason": alignment.reason,
        "time_shift_reference": alignment.reference,
        "time_shift_corr_gain": alignment.corr_gain,
        "time_shift_rmse_reduction_pct": alignment.rmse_reduction_pct,
        "time_shift_daily_median_minutes": alignment.daily_median_shift_minutes,
        "time_shift_daily_consistency_pct": alignment.daily_consistency_pct,
        "time_shift_days_used": int(alignment.days_used),
        "time_shift_zero_score": alignment.zero_shift,
        "time_shift_selected_score": alignment.selected_shift,
        "time_shift_scores": alignment.scores,

        "meteo_rows_raw": int(len(df_met)),
        "inv15_rows": int(len(inv15)),
        "met15_rows": int(len(met15)),
        "merged_rows_15": int(len(df15)),
        "expected_rows_15": int(expected_15),
    }

    if not df15.empty:
        idx = df15.index
        stats["merged_min_ts_utc"] = _safe_iso(idx.min())
        stats["merged_max_ts_utc"] = _safe_iso(idx.max())

        if "flag_meteo_missing" in df15.columns:
            stats["meteo_missing_frac"] = _safe_float(df15["flag_meteo_missing"].mean(), 0.0)

        if "flag_inv_missing" in df15.columns:
            stats["inv_missing_frac"] = _safe_float(df15["flag_inv_missing"].mean(), 0.0)
        else:
            stats["inv_missing_frac"] = 0.0

        # cobertura média apenas onde há inversor (inv_n>0), pois inv_coverage é NA quando missing
        if "inv_coverage" in df15.columns:
            stats["inv_coverage_mean_present"] = _safe_float(pd.to_numeric(df15["inv_coverage"], errors="coerce").mean(), 0.0)
        else:
            stats["inv_coverage_mean_present"] = 0.0

        # low coverage apenas onde há inversor (por construção, missing => False)
        if "flag_low_coverage" in df15.columns:
            stats["inv_lowcov_frac_present"] = _safe_float(df15["flag_low_coverage"].mean(), 0.0)
        else:
            stats["inv_lowcov_frac_present"] = 0.0

        # buckets bons: tem inversor, tem meteo e não é low coverage
        good = pd.Series(True, index=df15.index)
        if "flag_inv_missing" in df15.columns:
            good &= ~df15["flag_inv_missing"].fillna(True)
        if "flag_meteo_missing" in df15.columns:
            good &= ~df15["flag_meteo_missing"].fillna(True)
        if "flag_low_coverage" in df15.columns:
            good &= ~df15["flag_low_coverage"].fillna(True)

        stats["good_bucket_frac"] = _safe_float(good.mean(), 0.0)

    else:
        stats["merged_min_ts_utc"] = ""
        stats["merged_max_ts_utc"] = ""
        stats["meteo_missing_frac"] = 1.0
        stats["inv_missing_frac"] = 1.0
        stats["inv_coverage_mean_present"] = 0.0
        stats["inv_lowcov_frac_present"] = 0.0
        stats["good_bucket_frac"] = 0.0

    # Rollup horário
    df_hour = pd.DataFrame()
    if want_hourly and not df15.empty:
        df_hour = rollup_15min_to_hour(df15)

    # Persistência
    if persist and not df15.empty:
        saved = upsert_merged_15m_df(
            plant=plant,
            df15=df15,
            source_oper=str(source_oper),
            source_meteo=str(source_meteo),
            interval_min=int(interval_min),
        )
        stats["saved_rows_15m"] = int(saved)
    else:
        stats["saved_rows_15m"] = 0

    return MergeRunResult(df15=df15, df_hour=df_hour, stats=stats)
