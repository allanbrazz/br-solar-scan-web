from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from core.services.series_juntar.timeseries_merge import (
    InverterAggregationConfig,
    MeteoPreparationConfig,
    aggregate_inverter_to_15min,
    prepare_meteo_15min,
)


TimeShiftMode = Literal["none", "manual", "auto", "suggest_only"]
TimeShiftTarget = Literal["operational", "meteo"]
ConfidenceLevel = Literal["none", "low", "moderate", "high"]


@dataclass(frozen=True)
class TimeShiftAlignmentConfig:
    """Configuration for physical time-shift calibration between inverter and meteo/model data.

    The automatic calibration estimates a *relative* shift between the measured
    operational DC power and the physical DC power expected from the PV model.
    For each candidate shift, the routine:

      1. shifts the operational timestamps by the candidate value;
      2. aggregates the inverter data to the 15-minute mesh;
      3. prepares the meteorological 15-minute reference;
      4. computes ``P_DC_model`` from the configured PV module/plant model;
      5. compares ``P_DC_measured`` against ``P_DC_model``.

    Candidate selection criterion:
      - primary: lowest RMSE between measured P_DC and modelled P_DC;
      - tie-breaker: highest Pearson correlation between measured P_DC and
        modelled P_DC;
      - final tie-breaker: lowest absolute shift.

    Positive ``selected_shift_minutes`` means that the operational timestamp must
    be moved forward in time to align with the meteorological/model reference.
    Applying the same relative correction to meteo instead requires the opposite
    sign.

    Modes:
      - none: do not estimate/apply shift.
      - manual: apply ``manual_shift_minutes`` according to ``apply_target``.
      - auto: estimate shift and apply it only if confidence is acceptable.
      - suggest_only: estimate shift, report it, but do not apply it.
    """

    mode: TimeShiftMode = "none"
    apply_target: TimeShiftTarget = "operational"

    manual_shift_minutes: float = 0.0

    candidate_minutes: Sequence[int] = field(
        default_factory=lambda: tuple(range(-120, 121, 15))
    )
    max_abs_shift_minutes: int = 120
    step_minutes: int = 15

    min_samples: int = 300
    min_daily_samples: int = 16
    min_days: int = 20

    # Filters used only for temporal calibration.
    min_pdc_w: float = 200.0
    min_pdc_model_w: float = 200.0

    # Kept for backward compatibility with previous forms/scripts.
    min_irradiance_wm2: float = 200.0
    min_pac_w: float = 200.0

    # Automatic-application thresholds. The candidate choice itself is by RMSE;
    # these thresholds only block automatic application when the evidence is weak.
    corr_gain_min: float = 0.0
    corr_drop_max: float = 0.02
    rmse_reduction_min_pct: float = 5.0
    daily_consistency_min_pct: float = 60.0

    apply_if_confidence_at_least: ConfidenceLevel = "moderate"
    apply_even_if_low_confidence: bool = False

    # If G_POA exists, the physical model uses it. Otherwise, expected_and_mismatch
    # is allowed to transpose GHI/DHI/DNI using plant geometry.
    g_poa_preference: Sequence[str] = (
        "g_poa_wm2",
        "g_poa",
        "poa_global_wm2",
        "poa_global",
        "poa",
        "gti_wm2",
        "gti",
    )
    ghi_preference: Sequence[str] = ("ghi", "ghi_wm2")
    dni_preference: Sequence[str] = ("dni", "dni_wm2")
    dhi_preference: Sequence[str] = ("dhi", "dhi_wm2")
    temp_air_preference: Sequence[str] = (
        "temp_air",
        "temp_air_c",
        "tamb_c",
        "t_amb_c",
        "temperature_2m",
    )

    # Operational DC measured series.
    pdc_preference: Sequence[str] = (
        "p_dc_w",
        "pdc_w",
        "dc_power_w",
        "pdc",
    )


@dataclass(frozen=True)
class TimeShiftScore:
    shift_minutes: int
    n: int
    corr: Optional[float]
    rmse_w: Optional[float]
    mae_w: Optional[float]
    bias_w: Optional[float]
    slope_w_per_w: Optional[float]
    intercept_w: Optional[float]
    score: Optional[float]
    measured_pdc_col: str = ""
    model_pdc_col: str = "pdc_model_w"
    g_poa_col: str = ""
    temp_air_col: str = ""
    criterion: str = "min_rmse_pdc_measured_vs_pdc_model_then_max_corr"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TimeShiftAlignmentResult:
    mode: str
    applied: bool
    apply_target: str
    selected_shift_minutes: float
    operational_shift_minutes: float
    meteo_shift_minutes: float
    reference: str
    confidence: ConfidenceLevel
    reason: str

    zero_shift: Dict[str, Any]
    selected_shift: Dict[str, Any]
    corr_gain: Optional[float]
    rmse_reduction_pct: Optional[float]
    daily_median_shift_minutes: Optional[float]
    daily_consistency_pct: Optional[float]
    days_used: int
    scores: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_CONF_ORDER: Dict[str, int] = {
    "none": 0,
    "low": 1,
    "moderate": 2,
    "high": 3,
}


def _as_utc_series(s: pd.Series) -> pd.Series:
    dt = pd.to_datetime(s, errors="coerce")
    if dt.dt.tz is None:
        dt = dt.dt.tz_localize("UTC")
    else:
        dt = dt.dt.tz_convert("UTC")
    return dt


def _normalize_candidates(cfg: TimeShiftAlignmentConfig) -> List[int]:
    if cfg.candidate_minutes:
        vals = sorted({int(round(float(v))) for v in cfg.candidate_minutes})
    else:
        step = max(1, int(cfg.step_minutes or 15))
        lim = max(step, int(abs(cfg.max_abs_shift_minutes or 120)))
        vals = list(range(-lim, lim + 1, step))

    lim = max(0, int(abs(cfg.max_abs_shift_minutes or 0)))
    if lim > 0:
        vals = [v for v in vals if abs(v) <= lim]

    if 0 not in vals:
        vals.append(0)

    return sorted(set(vals))


def _pick_numeric_col(df: pd.DataFrame, preference: Sequence[str]) -> Optional[str]:
    for col in preference:
        if col in df.columns and pd.to_numeric(df[col], errors="coerce").notna().any():
            return col
    return None


def _pick_pdc_col(df: pd.DataFrame, preference: Sequence[str]) -> Optional[str]:
    return _pick_numeric_col(df, preference)


def _empty_score(shift_minutes: int, *, measured_pdc_col: str = "", model_pdc_col: str = "pdc_model_w", g_poa_col: str = "", temp_air_col: str = "") -> TimeShiftScore:
    return TimeShiftScore(
        shift_minutes=int(shift_minutes),
        n=0,
        corr=None,
        rmse_w=None,
        mae_w=None,
        bias_w=None,
        slope_w_per_w=None,
        intercept_w=None,
        score=None,
        measured_pdc_col=measured_pdc_col,
        model_pdc_col=model_pdc_col,
        g_poa_col=g_poa_col,
        temp_air_col=temp_air_col,
    )


def _build_model_plant_with_coordinates(plant: Any, details: Any) -> Tuple[Any, Any]:
    """Return (module, plant_model) for the physical DC model."""
    from dataclasses import asdict, is_dataclass
    from core.services.power_model.power_model import module_from_pvmodule, plant_from_details

    if details is None:
        raise ValueError("detalhes da planta ausentes")
    module_obj = getattr(details, "module", None)
    if module_obj is None:
        raise ValueError("módulo FV não cadastrado nos detalhes da planta")

    mod = module_from_pvmodule(module_obj)
    inv = getattr(details, "inverter", None)
    pl = plant_from_details(details, inverter=inv, use_inverter_eff=False)

    # Completa coordenadas a partir de PVPlant quando PVPlantDetails não contém.
    try:
        pld = asdict(pl) if is_dataclass(pl) else dict(getattr(pl, "__dict__", {}))
        if pld.get("lat_deg") is None:
            lat = getattr(plant, "latitude", None)
            pld["lat_deg"] = None if lat is None else float(lat)
        if pld.get("lon_deg") is None:
            lon = getattr(plant, "longitude", None)
            pld["lon_deg"] = None if lon is None else float(lon)
        if pld.get("tilt_deg") is None:
            tilt = getattr(details, "tilt_deg", None)
            pld["tilt_deg"] = None if tilt is None else float(tilt)
        if pld.get("azimuth_deg") is None:
            az = getattr(details, "azimuth_deg", None)
            pld["azimuth_deg"] = None if az is None else float(az)
        pl = pl.__class__(**pld)
    except Exception:
        pass

    return mod, pl


def _series_to_np(df: pd.DataFrame, col: Optional[str], n: int, default_nan: bool = True) -> np.ndarray:
    if col and col in df.columns:
        return pd.to_numeric(df[col], errors="coerce").to_numpy(dtype=float)
    return np.full(n, np.nan if default_nan else 0.0, dtype=float)


def _compute_pdc_model_15min(
    *,
    plant: Any,
    details: Any,
    met15: pd.DataFrame,
    cfg: TimeShiftAlignmentConfig,
) -> Tuple[pd.DataFrame, Dict[str, str]]:
    """Compute physical ``pdc_model_w`` on the meteorological 15-minute grid.

    The function first tries explicit G_POA/GTI columns. If none exists, it passes
    GHI/DHI/DNI and timestamps to ``expected_and_mismatch`` so that the existing
    physical model performs the transposition using the plant geometry.
    """
    if met15 is None or met15.empty:
        return pd.DataFrame(), {}

    try:
        from core.services.power_model.power_model import expected_and_mismatch
    except Exception as exc:
        raise ValueError(f"não foi possível importar o modelo físico: {exc}")

    mod, pl = _build_model_plant_with_coordinates(plant, details)

    d = met15.copy().sort_index()
    n = len(d)
    if n == 0:
        return pd.DataFrame(), {}

    g_col = _pick_numeric_col(d, cfg.g_poa_preference)
    ghi_col = _pick_numeric_col(d, cfg.ghi_preference)
    dni_col = _pick_numeric_col(d, cfg.dni_preference)
    dhi_col = _pick_numeric_col(d, cfg.dhi_preference)
    t_col = _pick_numeric_col(d, cfg.temp_air_preference)

    if t_col is None:
        raise ValueError("não há coluna de temperatura ambiente para calcular P_DC_model")

    g_poa = _series_to_np(d, g_col, n) if g_col else None
    ghi = _series_to_np(d, ghi_col, n) if ghi_col else None
    dni = _series_to_np(d, dni_col, n) if dni_col else None
    dhi = _series_to_np(d, dhi_col, n) if dhi_col else None
    tamb = _series_to_np(d, t_col, n)

    if g_poa is None and ghi is None:
        raise ValueError("não há G_POA/GTI nem GHI para calcular P_DC_model")

    out = expected_and_mismatch(
        g_poa=g_poa,
        tamb_c=tamb,
        pac_real_w=None,
        module=mod,
        plant=pl,
        ghi=ghi,
        dhi=dhi,
        dni=dni,
        times_utc=d.index,
        use_transposition_if_needed=True,
        g_min_valid=0.0,
        n_points=60,
        eps_w=50.0,
        dt_minutes=15.0,
        window_minutes=60.0,
        force_zero_when_invalid=False,
        auto_time_shift=False,
        meteo_time_shift_minutes=0.0,
        compute_rca=False,
    )

    pdc_model = np.asarray(out.get("pdc_expected_w"), dtype=float)
    g_used = np.asarray(out.get("g_poa_used", out.get("g_poa", np.full(n, np.nan))), dtype=float)

    if pdc_model.size != n:
        raise ValueError("modelo físico retornou P_DC_model com tamanho incompatível")

    model_df = pd.DataFrame(index=d.index)
    model_df["pdc_model_w"] = pdc_model
    model_df["g_poa_model_wm2"] = g_used if g_used.size == n else np.full(n, np.nan)

    meta = {
        "pdc_model_col": "pdc_model_w",
        "g_poa_col": g_col or "transposed_from_ghi_dhi_dni",
        "temp_air_col": t_col,
    }
    return model_df, meta


def _score_measured_pdc_vs_model(
    *,
    df: pd.DataFrame,
    shift_minutes: int,
    pdc_col: str,
    cfg: TimeShiftAlignmentConfig,
    model_pdc_col: str = "pdc_model_w",
    g_poa_col: str = "",
    temp_air_col: str = "",
) -> TimeShiftScore:
    """Score a candidate shift through measured P_DC versus physical P_DC_model."""
    if df.empty or pdc_col not in df.columns or model_pdc_col not in df.columns:
        return _empty_score(
            shift_minutes,
            measured_pdc_col=pdc_col,
            model_pdc_col=model_pdc_col,
            g_poa_col=g_poa_col,
            temp_air_col=temp_air_col,
        )

    y_meas = pd.to_numeric(df[pdc_col], errors="coerce").to_numpy(dtype=float)
    y_model = pd.to_numeric(df[model_pdc_col], errors="coerce").to_numpy(dtype=float)

    mask = (
        np.isfinite(y_meas)
        & np.isfinite(y_model)
        & (y_meas >= float(cfg.min_pdc_w))
        & (y_model >= float(cfg.min_pdc_model_w))
    )
    n = int(mask.sum())
    if n < 3:
        return TimeShiftScore(
            shift_minutes=int(shift_minutes), n=n,
            corr=None, rmse_w=None, mae_w=None, bias_w=None,
            slope_w_per_w=None, intercept_w=None, score=None,
            measured_pdc_col=pdc_col, model_pdc_col=model_pdc_col,
            g_poa_col=g_poa_col, temp_air_col=temp_air_col,
        )

    ym = y_meas[mask]
    yh = y_model[mask]

    if float(np.nanstd(ym)) <= 1e-9 or float(np.nanstd(yh)) <= 1e-9:
        corr = None
    else:
        corr_f = float(np.corrcoef(ym, yh)[0, 1])
        corr = corr_f if np.isfinite(corr_f) else None

    err = ym - yh
    rmse = float(np.sqrt(np.nanmean(err ** 2)))
    mae = float(np.nanmean(np.abs(err)))
    bias = float(np.nanmean(err))

    # Linear fit is not used for selection; it is stored only for audit.
    slope_f = intercept_f = None
    try:
        slope, intercept = np.polyfit(yh, ym, deg=1)
        slope_f = float(slope)
        intercept_f = float(intercept)
    except Exception:
        pass

    return TimeShiftScore(
        shift_minutes=int(shift_minutes),
        n=n,
        corr=corr,
        rmse_w=rmse,
        mae_w=mae,
        bias_w=bias,
        slope_w_per_w=slope_f,
        intercept_w=intercept_f,
        score=-float(rmse),
        measured_pdc_col=pdc_col,
        model_pdc_col=model_pdc_col,
        g_poa_col=g_poa_col,
        temp_air_col=temp_air_col,
    )


def _score_shift_candidates(
    *,
    df_inv: pd.DataFrame,
    df_met: pd.DataFrame,
    candidates: Sequence[int],
    cfg: TimeShiftAlignmentConfig,
    inv_cfg: InverterAggregationConfig,
    met_cfg: MeteoPreparationConfig,
    plant: Any = None,
    details: Any = None,
    dt_start_utc: Optional[Any] = None,
    dt_end_utc: Optional[Any] = None,
) -> Tuple[List[TimeShiftScore], str, str]:
    if df_inv is None or df_inv.empty or df_met is None or df_met.empty:
        return [], "", ""

    met15 = prepare_meteo_15min(df_met, cfg=met_cfg, tz_work="UTC", assume_tz_if_naive="UTC")
    if met15.empty:
        return [], "", ""

    try:
        pdc_model_df, model_meta = _compute_pdc_model_15min(
            plant=plant,
            details=details,
            met15=met15,
            cfg=cfg,
        )
    except Exception:
        return [], "", ""

    if pdc_model_df.empty or "pdc_model_w" not in pdc_model_df.columns:
        return [], "", ""

    # Ensures p_dc_w is aggregated even if a reduced inv_cfg is received.
    mean_cols_base = tuple(dict.fromkeys(tuple(inv_cfg.mean_cols) + tuple(cfg.pdc_preference)))

    scores: List[TimeShiftScore] = []
    for sh in candidates:
        inv_cfg_i = InverterAggregationConfig(
            ts_col=inv_cfg.ts_col,
            freq=inv_cfg.freq,
            mean_cols=mean_cols_base,
            pac_col=inv_cfg.pac_col,
            expected_samples_per_bucket=inv_cfg.expected_samples_per_bucket,
            energy_dt_mode=inv_cfg.energy_dt_mode,
            sampling_minutes=inv_cfg.sampling_minutes,
            max_dt_minutes=inv_cfg.max_dt_minutes,
            coverage_threshold=inv_cfg.coverage_threshold,
            oper_time_shift_minutes=float(sh),
        )
        inv15 = aggregate_inverter_to_15min(df_inv, cfg=inv_cfg_i, tz_work="UTC", assume_tz_if_naive="UTC")
        pdc_col = _pick_pdc_col(inv15, cfg.pdc_preference)
        if inv15.empty or not pdc_col:
            scores.append(
                _empty_score(
                    int(sh),
                    measured_pdc_col="",
                    model_pdc_col="pdc_model_w",
                    g_poa_col=model_meta.get("g_poa_col", ""),
                    temp_air_col=model_meta.get("temp_air_col", ""),
                )
            )
            continue

        joined = inv15[[pdc_col]].join(pdc_model_df[["pdc_model_w", "g_poa_model_wm2"]], how="inner")
        if dt_start_utc is not None and dt_end_utc is not None and not joined.empty:
            t0 = pd.Timestamp(dt_start_utc)
            t1 = pd.Timestamp(dt_end_utc)
            t0 = t0.tz_localize("UTC") if t0.tzinfo is None else t0.tz_convert("UTC")
            t1 = t1.tz_localize("UTC") if t1.tzinfo is None else t1.tz_convert("UTC")
            joined = joined[(joined.index >= t0) & (joined.index < t1)]

        scores.append(
            _score_measured_pdc_vs_model(
                df=joined,
                shift_minutes=int(sh),
                pdc_col=pdc_col,
                model_pdc_col="pdc_model_w",
                g_poa_col=model_meta.get("g_poa_col", ""),
                temp_air_col=model_meta.get("temp_air_col", ""),
                cfg=cfg,
            )
        )

    selected_power_col = ""
    for s in scores:
        if s.measured_pdc_col:
            selected_power_col = s.measured_pdc_col
            break
    return scores, str(model_meta.get("g_poa_col", "")), selected_power_col


def _best_score(scores: Sequence[TimeShiftScore]) -> Optional[TimeShiftScore]:
    valid = [s for s in scores if s.rmse_w is not None and s.n > 0]
    if not valid:
        return None

    # Requested criterion:
    # 1) lowest RMSE between measured P_DC and modelled P_DC;
    # 2) if tied, highest correlation;
    # 3) if still tied, lowest absolute shift.
    return sorted(
        valid,
        key=lambda s: (
            float(s.rmse_w if s.rmse_w is not None else 1e18),
            -float(s.corr if s.corr is not None else -999.0),
            abs(int(s.shift_minutes)),
        ),
    )[0]


def _score_zero(scores: Sequence[TimeShiftScore]) -> Optional[TimeShiftScore]:
    for s in scores:
        if int(s.shift_minutes) == 0:
            return s
    return None


def _estimate_daily_consistency(
    *,
    df_inv: pd.DataFrame,
    df_met: pd.DataFrame,
    candidates: Sequence[int],
    selected_shift: int,
    cfg: TimeShiftAlignmentConfig,
    inv_cfg: InverterAggregationConfig,
    met_cfg: MeteoPreparationConfig,
    plant: Any,
    details: Any,
    dt_start_utc: Optional[Any],
    dt_end_utc: Optional[Any],
) -> Tuple[Optional[float], Optional[float], int]:
    if df_inv is None or df_inv.empty or df_met is None or df_met.empty:
        return None, None, 0
    if inv_cfg.ts_col not in df_inv.columns or met_cfg.ts_col not in df_met.columns:
        return None, None, 0

    dfi = df_inv.copy()
    dfm = df_met.copy()
    dfi[inv_cfg.ts_col] = _as_utc_series(dfi[inv_cfg.ts_col])
    dfm[met_cfg.ts_col] = _as_utc_series(dfm[met_cfg.ts_col])

    if dt_start_utc is not None and dt_end_utc is not None:
        t0 = pd.Timestamp(dt_start_utc)
        t1 = pd.Timestamp(dt_end_utc)
        t0 = t0.tz_localize("UTC") if t0.tzinfo is None else t0.tz_convert("UTC")
        t1 = t1.tz_localize("UTC") if t1.tzinfo is None else t1.tz_convert("UTC")
    else:
        t0 = max(dfi[inv_cfg.ts_col].min(), dfm[met_cfg.ts_col].min())
        t1 = min(dfi[inv_cfg.ts_col].max(), dfm[met_cfg.ts_col].max())

    if pd.isna(t0) or pd.isna(t1) or t1 <= t0:
        return None, None, 0

    pad_min = max(abs(int(x)) for x in candidates) if candidates else 0
    days = pd.date_range(t0.floor("D"), (t1 - pd.Timedelta(minutes=1)).floor("D"), freq="D", tz="UTC")
    daily_best: List[int] = []

    for day0 in days:
        day1 = day0 + pd.Timedelta(days=1)
        sub_i = dfi[
            (dfi[inv_cfg.ts_col] >= day0 - pd.Timedelta(minutes=pad_min))
            & (dfi[inv_cfg.ts_col] < day1 + pd.Timedelta(minutes=pad_min))
        ]
        sub_m = dfm[
            (dfm[met_cfg.ts_col] >= day0 - pd.Timedelta(minutes=pad_min))
            & (dfm[met_cfg.ts_col] < day1 + pd.Timedelta(minutes=pad_min))
        ]
        if sub_i.empty or sub_m.empty:
            continue
        scores, _, _ = _score_shift_candidates(
            df_inv=sub_i,
            df_met=sub_m,
            candidates=candidates,
            cfg=cfg,
            inv_cfg=inv_cfg,
            met_cfg=met_cfg,
            plant=plant,
            details=details,
            dt_start_utc=day0,
            dt_end_utc=day1,
        )
        best = _best_score(scores)
        if not best or int(best.n) < int(cfg.min_daily_samples):
            continue
        daily_best.append(int(best.shift_minutes))

    if not daily_best:
        return None, None, 0

    arr = np.asarray(daily_best, dtype=float)
    med = float(np.median(arr))
    tol = max(15.0, float(cfg.step_minutes or 15))
    consistency = 100.0 * float(np.mean(np.abs(arr - float(selected_shift)) <= tol))
    return med, consistency, int(len(daily_best))


def _confidence_from_metrics(
    *,
    selected: Optional[TimeShiftScore],
    zero: Optional[TimeShiftScore],
    corr_gain: Optional[float],
    rmse_reduction_pct: Optional[float],
    daily_consistency_pct: Optional[float],
    days_used: int,
    cfg: TimeShiftAlignmentConfig,
) -> Tuple[ConfidenceLevel, str]:
    if selected is None:
        return "none", "sem pontuação válida para candidatos de deslocamento"
    if selected.n < int(cfg.min_samples):
        return "low", f"amostra insuficiente para calibração temporal: n={selected.n} < {cfg.min_samples}"
    if int(selected.shift_minutes) == 0:
        return "high", "melhor deslocamento é nulo; não há evidência de timeshift aplicado"

    cg = float(corr_gain or 0.0)
    rr = float(rmse_reduction_pct or 0.0)
    dc = float(daily_consistency_pct or 0.0)

    # Candidate choice is by RMSE; correlation must not degrade significantly.
    pass_rmse = rr >= float(cfg.rmse_reduction_min_pct)
    pass_corr = cg >= float(cfg.corr_gain_min) or cg >= -float(cfg.corr_drop_max)
    pass_days = days_used >= int(cfg.min_days)
    pass_consistency = dc >= float(cfg.daily_consistency_min_pct)

    if pass_rmse and pass_corr and pass_days and pass_consistency:
        return "high", "redução de RMSE P_DC medido × P_DC modelado, correlação compatível e consistência diária suficientes"
    if pass_rmse and pass_corr and (pass_consistency or pass_days):
        return "moderate", "redução de RMSE P_DC medido × P_DC modelado com correlação compatível; consistência diária moderada"
    if pass_rmse:
        return "low", "redução de RMSE detectada, mas consistência ou correlação insuficiente"
    return "low", "redução de RMSE em relação ao deslocamento zero abaixo do limiar"


def estimate_time_shift_alignment(
    *,
    df_inv: pd.DataFrame,
    df_met: pd.DataFrame,
    cfg: TimeShiftAlignmentConfig,
    inv_cfg: InverterAggregationConfig,
    met_cfg: MeteoPreparationConfig,
    plant: Any = None,
    details: Any = None,
    dt_start_utc: Optional[Any] = None,
    dt_end_utc: Optional[Any] = None,
) -> TimeShiftAlignmentResult:
    """Estimate and decide whether to apply a relative operational/meteo time shift.

    The returned ``selected_shift_minutes`` is always the relative correction in the
    operational-reference convention: positive means operational timestamps should be
    moved forward. If ``apply_target='meteo'``, the same relative alignment is applied
    as an opposite meteo shift.
    """
    mode = str(cfg.mode or "none").lower()
    apply_target = str(cfg.apply_target or "operational").lower()
    if apply_target not in ("operational", "meteo"):
        apply_target = "operational"

    if mode == "none":
        return TimeShiftAlignmentResult(
            mode=mode,
            applied=False,
            apply_target=apply_target,
            selected_shift_minutes=0.0,
            operational_shift_minutes=0.0,
            meteo_shift_minutes=0.0,
            reference="none",
            confidence="none",
            reason="calibração temporal desativada",
            zero_shift={},
            selected_shift={},
            corr_gain=None,
            rmse_reduction_pct=None,
            daily_median_shift_minutes=None,
            daily_consistency_pct=None,
            days_used=0,
            scores=[],
        )

    if mode == "manual":
        rel = float(cfg.manual_shift_minutes or 0.0)
        if apply_target == "operational":
            op_shift = rel
            met_shift = 0.0
        else:
            op_shift = 0.0
            met_shift = -rel
        return TimeShiftAlignmentResult(
            mode=mode,
            applied=abs(rel) > 1e-9,
            apply_target=apply_target,
            selected_shift_minutes=rel,
            operational_shift_minutes=op_shift,
            meteo_shift_minutes=met_shift,
            reference="manual",
            confidence="high",
            reason="deslocamento manual aplicado",
            zero_shift={},
            selected_shift={},
            corr_gain=None,
            rmse_reduction_pct=None,
            daily_median_shift_minutes=None,
            daily_consistency_pct=None,
            days_used=0,
            scores=[],
        )

    if details is None:
        return TimeShiftAlignmentResult(
            mode=mode,
            applied=False,
            apply_target=apply_target,
            selected_shift_minutes=0.0,
            operational_shift_minutes=0.0,
            meteo_shift_minutes=0.0,
            reference="pdc_measured_vs_pdc_model",
            confidence="none",
            reason="não foi possível estimar: detalhes físicos da planta ausentes",
            zero_shift={},
            selected_shift={},
            corr_gain=None,
            rmse_reduction_pct=None,
            daily_median_shift_minutes=None,
            daily_consistency_pct=None,
            days_used=0,
            scores=[],
        )

    candidates = _normalize_candidates(cfg)
    scores, g_ref_col, pdc_col = _score_shift_candidates(
        df_inv=df_inv,
        df_met=df_met,
        candidates=candidates,
        cfg=cfg,
        inv_cfg=inv_cfg,
        met_cfg=met_cfg,
        plant=plant,
        details=details,
        dt_start_utc=dt_start_utc,
        dt_end_utc=dt_end_utc,
    )

    selected = _best_score(scores)
    zero = _score_zero(scores)

    if selected is None:
        confidence, reason = "none", "não foi possível estimar deslocamento temporal por P_DC medido × P_DC modelado"
        rel = 0.0
        corr_gain = None
        rmse_red = None
        daily_med = None
        daily_cons = None
        days_used = 0
    else:
        rel = float(selected.shift_minutes)
        if zero and selected.corr is not None and zero.corr is not None:
            corr_gain = float(selected.corr - zero.corr)
        else:
            corr_gain = None
        if zero and selected.rmse_w is not None and zero.rmse_w is not None and zero.rmse_w > 0:
            rmse_red = 100.0 * float((zero.rmse_w - selected.rmse_w) / zero.rmse_w)
        else:
            rmse_red = None

        daily_med, daily_cons, days_used = _estimate_daily_consistency(
            df_inv=df_inv,
            df_met=df_met,
            candidates=candidates,
            selected_shift=int(selected.shift_minutes),
            cfg=cfg,
            inv_cfg=inv_cfg,
            met_cfg=met_cfg,
            plant=plant,
            details=details,
            dt_start_utc=dt_start_utc,
            dt_end_utc=dt_end_utc,
        )
        confidence, reason = _confidence_from_metrics(
            selected=selected,
            zero=zero,
            corr_gain=corr_gain,
            rmse_reduction_pct=rmse_red,
            daily_consistency_pct=daily_cons,
            days_used=days_used,
            cfg=cfg,
        )

    should_apply = False
    if mode == "auto":
        if bool(cfg.apply_even_if_low_confidence):
            should_apply = True
        else:
            should_apply = _CONF_ORDER.get(confidence, 0) >= _CONF_ORDER.get(str(cfg.apply_if_confidence_at_least), 2)
    elif mode == "suggest_only":
        should_apply = False

    if abs(rel) < 1e-9:
        should_apply = False

    if should_apply:
        if apply_target == "operational":
            op_shift = rel
            met_shift = 0.0
        else:
            op_shift = 0.0
            met_shift = -rel
    else:
        op_shift = 0.0
        met_shift = 0.0

    ref = f"{pdc_col or 'p_dc_w'}_vs_pdc_model_w"
    if g_ref_col:
        ref += f"_from_{g_ref_col}"

    return TimeShiftAlignmentResult(
        mode=mode,
        applied=bool(should_apply),
        apply_target=apply_target,
        selected_shift_minutes=float(rel),
        operational_shift_minutes=float(op_shift),
        meteo_shift_minutes=float(met_shift),
        reference=ref,
        confidence=confidence,
        reason=reason,
        zero_shift={} if zero is None else zero.to_dict(),
        selected_shift={} if selected is None else selected.to_dict(),
        corr_gain=corr_gain,
        rmse_reduction_pct=rmse_red,
        daily_median_shift_minutes=daily_med,
        daily_consistency_pct=daily_cons,
        days_used=int(days_used),
        scores=[s.to_dict() for s in scores],
    )
