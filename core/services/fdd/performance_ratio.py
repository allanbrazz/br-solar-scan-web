from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction

from core.models import PVPlant, PlantPerformanceRatio
from core.services.fdd.aggregation import aggregate_runtime_series
from core.services.fdd.dashboard_common import DashboardServiceError, as_float
from core.services.fdd.runtime_types import MismatchDashboardParams
from core.services.fdd.source_selection import (
    ensure_plant_configuration,
    group_runtime_rows,
    query_runtime_rows,
)


VALID_PERIODS = {"daily", "monthly", "annual"}
G_REF_WM2 = 1000.0
DEFAULT_MU_PMPP_PER_C = -0.0035


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        number = float(value)
    except Exception:
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _cell_temperature_noct(g_poa_wm2: float, temp_air_c: float, noct_c: float) -> float:
    g = max(0.0, float(g_poa_wm2))
    tcell = float(temp_air_c) + (g / 800.0) * (float(noct_c) - 20.0)
    return max(min(tcell, 95.0), -30.0)


def _module_nominal_power_kwp(details: Any, modules_total: int) -> float:
    module = getattr(details, "module", None)
    pmp_w = _safe_float(getattr(module, "pmp_w", None))
    if pmp_w is None or pmp_w <= 0:
        raise DashboardServiceError(
            "Modulo da planta sem Pmp valido para calcular PR.",
            status_code=400,
        )
    return float(modules_total) * pmp_w / 1000.0


def _mu_pmpp_from_module(details: Any) -> Tuple[float, str]:
    module = getattr(details, "module", None)
    # O cadastro atual nao possui coeficiente Pmpp explicito. Usamos Voc como proxy
    # por ser negativo e representar a sensibilidade termica disponivel do modulo.
    voc_pct_c = _safe_float(getattr(module, "temp_coeff_voc_pct_c", None))
    if voc_pct_c is not None and voc_pct_c < 0:
        return voc_pct_c / 100.0, "temp_coeff_voc_pct_c_proxy"
    return DEFAULT_MU_PMPP_PER_C, "default_pvsyst_like_fallback"


def _period_bounds(local_day: date, period: str) -> Tuple[date, date]:
    if period == "daily":
        return local_day, local_day
    if period == "annual":
        return date(local_day.year, 1, 1), date(local_day.year, 12, 31)
    start = date(local_day.year, local_day.month, 1)
    if local_day.month == 12:
        next_month = date(local_day.year + 1, 1, 1)
    else:
        next_month = date(local_day.year, local_day.month + 1, 1)
    return start, next_month - timedelta(days=1)


def _period_label(start: date, period: str) -> str:
    if period == "daily":
        return start.strftime("%d/%m/%Y")
    if period == "annual":
        return str(start.year)
    return start.strftime("%m/%Y")


def _selected_source_label(params: MismatchDashboardParams, selected_sources: List[str]) -> str:
    raw = str(getattr(params, "source_oper_raw", "") or "").strip()
    if not raw or raw.upper() == "ALL":
        return "ALL"
    return selected_sources[0] if selected_sources else raw


def _append_group(groups: OrderedDict, *, entry: Dict[str, Any], period: str, t_array_weighted_c: float, mu_pmpp_per_c: float) -> None:
    period_start, period_end = _period_bounds(entry["local_date"], period)
    key = period_start.isoformat()
    if key not in groups:
        groups[key] = {
            "period_start": period_start,
            "period_end": period_end,
            "label": _period_label(period_start, period),
            "energy_kwh": 0.0,
            "irradiation_kwh_m2": 0.0,
            "temperature_term_h": 0.0,
            "samples_count": 0,
            "valid_samples_count": 0,
            "first_ts_utc": entry["ts_utc"],
            "last_ts_utc": entry["ts_utc"],
        }

    bucket = groups[key]
    correction = 1.0 + mu_pmpp_per_c * (entry["tcell_c"] - t_array_weighted_c)
    if correction <= 0:
        return

    irradiation_kwh_m2 = (entry["g_poa_wm2"] / G_REF_WM2) * entry["interval_h"]
    bucket["energy_kwh"] += entry["energy_kwh"]
    bucket["irradiation_kwh_m2"] += irradiation_kwh_m2
    bucket["temperature_term_h"] += irradiation_kwh_m2 * correction
    bucket["samples_count"] += 1
    bucket["valid_samples_count"] += 1
    bucket["first_ts_utc"] = min(bucket["first_ts_utc"], entry["ts_utc"])
    bucket["last_ts_utc"] = max(bucket["last_ts_utc"], entry["ts_utc"])


def build_temperature_corrected_pr_payload(
    plant: PVPlant,
    params: MismatchDashboardParams,
    *,
    period: str = "monthly",
    persist: bool = True,
) -> Dict[str, Any]:
    period = str(period or "monthly").strip().lower()
    if period not in VALID_PERIODS:
        raise DashboardServiceError("Periodo de PR invalido. Use daily, monthly ou annual.", status_code=400)

    details, modules_total = ensure_plant_configuration(plant)
    pnom_kwp = _module_nominal_power_kwp(details, modules_total)
    noct_c = _safe_float(getattr(details, "noct_c", None)) or 45.0
    mu_pmpp_per_c, mu_source = _mu_pmpp_from_module(details)

    src_meteo, source_oper_list, selected_sources, rows = query_runtime_rows(plant, params)
    per_ts, times_utc = group_runtime_rows(rows)
    aggregated = aggregate_runtime_series(
        per_ts=per_ts,
        times_utc=times_utc,
        selected_sources=selected_sources,
    )

    entries: List[Dict[str, Any]] = []
    weighted_temp_num = 0.0
    weighted_temp_den = 0.0
    missing_energy = 0
    missing_meteo = 0
    used_gti = 0
    used_ghi_fallback = 0
    interval_h = 15.0 / 60.0

    for i, ts_utc in enumerate(times_utc):
        g_poa = as_float(aggregated.get("gti", [])[i] if i < len(aggregated.get("gti", [])) else None)
        if g_poa is not None and g_poa > 0:
            used_gti += 1
        else:
            ghi = as_float(aggregated.get("ghi", [])[i] if i < len(aggregated.get("ghi", [])) else None)
            if ghi is not None and ghi > 0:
                g_poa = ghi
                used_ghi_fallback += 1

        temp_air = as_float(aggregated.get("temp_air", [])[i] if i < len(aggregated.get("temp_air", [])) else None)
        energy_wh = as_float(aggregated.get("e_ac_wh_15", [])[i] if i < len(aggregated.get("e_ac_wh_15", [])) else None)
        if energy_wh is None:
            pac_w = as_float(aggregated.get("p_ac_w", [])[i] if i < len(aggregated.get("p_ac_w", [])) else None)
            if pac_w is not None and pac_w >= 0:
                energy_wh = pac_w * interval_h

        inv_missing = bool(aggregated.get("flag_inv_missing_all", [False] * len(times_utc))[i])
        meteo_missing = bool(aggregated.get("flag_meteo_missing", [False] * len(times_utc))[i])

        if energy_wh is None or energy_wh < 0 or inv_missing:
            missing_energy += 1
            continue
        if g_poa is None or g_poa <= 0 or temp_air is None or meteo_missing:
            missing_meteo += 1
            continue

        tcell_c = _cell_temperature_noct(g_poa, temp_air, noct_c)
        local_day = ts_utc.astimezone(params.tz).date()
        energy_kwh = energy_wh / 1000.0
        entries.append({
            "ts_utc": ts_utc,
            "local_date": local_day,
            "g_poa_wm2": g_poa,
            "temp_air_c": temp_air,
            "tcell_c": tcell_c,
            "energy_kwh": energy_kwh,
            "interval_h": interval_h,
        })
        weighted_temp_num += g_poa * tcell_c
        weighted_temp_den += g_poa

    t_array_weighted_c = (weighted_temp_num / weighted_temp_den) if weighted_temp_den > 0 else None
    source_oper_label = _selected_source_label(params, selected_sources)
    meta = {
        "definition": "PR_Temp = E_AC / (Pnom_PV * sum(G_POA/G_ref * (1 + mu_Pmpp*(Tcell - Tcell_weighted)) * dt))",
        "definition_source": "PVsyst Performance Ratio PR documentation",
        "g_ref_wm2": G_REF_WM2,
        "g_poa_source": "GTI/POA with GHI fallback when GTI is missing",
        "mu_source": mu_source,
        "temperature_model": "NOCT",
        "interval_minutes": 15,
        "missing_energy_samples": missing_energy,
        "missing_meteo_samples": missing_meteo,
        "used_gti_samples": used_gti,
        "used_ghi_fallback_samples": used_ghi_fallback,
    }

    groups: OrderedDict[str, Dict[str, Any]] = OrderedDict()
    if t_array_weighted_c is not None:
        for entry in entries:
            _append_group(
                groups,
                entry=entry,
                period=period,
                t_array_weighted_c=t_array_weighted_c,
                mu_pmpp_per_c=mu_pmpp_per_c,
            )

    series: List[Dict[str, Any]] = []
    persisted_ids: List[int] = []
    with transaction.atomic():
        for bucket in groups.values():
            denominator_kwh = pnom_kwp * bucket["temperature_term_h"]
            raw_denominator_kwh = pnom_kwp * bucket["irradiation_kwh_m2"]
            performance_ratio = (bucket["energy_kwh"] / denominator_kwh) if denominator_kwh > 0 else None
            raw_performance_ratio = (bucket["energy_kwh"] / raw_denominator_kwh) if raw_denominator_kwh > 0 else None
            ts_end_utc = bucket["last_ts_utc"] + timedelta(minutes=15)
            item_payload = {
                "period_start": bucket["period_start"].isoformat(),
                "period_end": bucket["period_end"].isoformat(),
                "label": bucket["label"],
                "performance_ratio": performance_ratio,
                "raw_performance_ratio": raw_performance_ratio,
                "energy_kwh": bucket["energy_kwh"],
                "irradiation_kwh_m2": bucket["irradiation_kwh_m2"],
                "denominator_kwh": denominator_kwh,
                "pnom_kwp": pnom_kwp,
                "t_array_weighted_c": t_array_weighted_c,
                "mu_pmpp_per_c": mu_pmpp_per_c,
                "samples_count": bucket["samples_count"],
                "valid_samples_count": bucket["valid_samples_count"],
                "ts_start_utc": bucket["first_ts_utc"].isoformat(),
                "ts_end_utc": ts_end_utc.isoformat(),
            }
            series.append(item_payload)

            if persist:
                record, _created = PlantPerformanceRatio.objects.update_or_create(
                    plant=plant,
                    source_oper=source_oper_label,
                    source_meteo=src_meteo,
                    period=period,
                    period_start=bucket["period_start"],
                    defaults={
                        "period_end": bucket["period_end"],
                        "ts_start_utc": bucket["first_ts_utc"],
                        "ts_end_utc": ts_end_utc,
                        "performance_ratio": performance_ratio,
                        "raw_performance_ratio": raw_performance_ratio,
                        "energy_kwh": bucket["energy_kwh"],
                        "irradiation_kwh_m2": bucket["irradiation_kwh_m2"],
                        "denominator_kwh": denominator_kwh,
                        "pnom_kwp": pnom_kwp,
                        "t_array_weighted_c": t_array_weighted_c,
                        "mu_pmpp_per_c": mu_pmpp_per_c,
                        "samples_count": bucket["samples_count"],
                        "valid_samples_count": bucket["valid_samples_count"],
                        "meta": meta,
                    },
                )
                persisted_ids.append(record.id)

    return {
        "ok": True,
        "period": period,
        "plant_id": plant.id,
        "sources": {
            "source_oper": source_oper_label,
            "source_meteo": src_meteo,
            "available_source_oper": source_oper_list,
            "selected_source_oper": selected_sources,
        },
        "range": {
            "start": params.start.isoformat(),
            "end": params.end.isoformat(),
            "start_utc": params.dt0_utc.isoformat(),
            "end_utc": params.dt1_utc.isoformat(),
            "timezone": params.tz_name,
        },
        "summary": {
            "points": len(series),
            "samples_count": len(times_utc),
            "valid_samples_count": len(entries),
            "pnom_kwp": pnom_kwp,
            "t_array_weighted_c": t_array_weighted_c,
            "mu_pmpp_per_c": mu_pmpp_per_c,
            "mu_source": mu_source,
            "persisted": bool(persist),
            "persisted_ids": persisted_ids,
        },
        "meta": meta,
        "series": series,
    }
