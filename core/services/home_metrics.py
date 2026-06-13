# core/services/home_metrics.py
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from statistics import median
from typing import Any, Optional
from zoneinfo import ZoneInfo
import math
import unicodedata

from django.db.models import Max, Min, Q
from django.utils import timezone

from core.models import (
    DataIngestState,
    FaultEvent,
    InverterOperationalData,
    MeteoImportBatch,
    MeteoRecord,
    PlantDiagnostic15m,
    PVPlant,
    PVPlantMergedRecord15m,
)

UTC = dt_timezone.utc


def _fmt_dt(value: Optional[datetime]) -> str:
    if not value:
        return "—"
    try:
        if timezone.is_naive(value):
            value = timezone.make_aware(value, UTC)
        value = timezone.localtime(value)
        return value.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return str(value)


def _fmt_date(value: Optional[datetime]) -> str:
    if not value:
        return "—"
    try:
        if timezone.is_naive(value):
            value = timezone.make_aware(value, UTC)
        value = timezone.localtime(value)
        return value.strftime("%d/%m/%Y")
    except Exception:
        return str(value)


def _max_dt(*values: Optional[datetime]) -> Optional[datetime]:
    vals = [v for v in values if v is not None]
    return max(vals) if vals else None


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        if isinstance(value, Decimal):
            value = float(value)
        if isinstance(value, str):
            s = value.strip()
            if not s:
                return None
            allowed = "0123456789-. ,"
            s = "".join(ch for ch in s if ch in allowed).strip().replace(" ", "")
            if not s or s in ("-", ".", ","):
                return None
            if "," in s and "." in s:
                if s.rfind(",") > s.rfind("."):
                    s = s.replace(".", "").replace(",", ".")
                else:
                    s = s.replace(",", "")
            elif "," in s:
                s = s.replace(",", ".")
            value = s
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _safe_zoneinfo(tz_name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def _month_range_utc_for_plant(latest_ts_utc: datetime, tz_name: str | None) -> tuple[datetime, datetime, str]:
    """
    Retorna o mês local da planta correspondente ao timestamp de referência.
    O filtro no banco segue em UTC, mas o recorte mensal respeita o timezone da planta.
    """
    tz = _safe_zoneinfo(tz_name)
    if timezone.is_naive(latest_ts_utc):
        latest_ts_utc = timezone.make_aware(latest_ts_utc, UTC)

    latest_local = latest_ts_utc.astimezone(tz)
    start_local = datetime(latest_local.year, latest_local.month, 1, 0, 0, 0, tzinfo=tz)
    if latest_local.month == 12:
        end_local = datetime(latest_local.year + 1, 1, 1, 0, 0, 0, tzinfo=tz)
    else:
        end_local = datetime(latest_local.year, latest_local.month + 1, 1, 0, 0, 0, tzinfo=tz)

    label = start_local.strftime("%m/%Y")
    return start_local.astimezone(UTC), end_local.astimezone(UTC), label


def _round_or_none(value: Optional[float], ndigits: int = 1) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), ndigits)
    except Exception:
        return None


# -----------------------------------------------------------------------------
# Energia real mensal
# -----------------------------------------------------------------------------

def _is_mppt_source(source_oper: str | None) -> bool:
    return "|MPPT" in str(source_oper or "").upper()


def _source_family(source_oper: str | None) -> str:
    src = str(source_oper or "").strip()
    return src.split("|", 1)[0] if "|" in src else src


def _format_source_label(source_oper: str | None, source_meteo: str | None, suffix: str = "") -> str:
    base = _source_family(source_oper) or str(source_oper or "").strip() or "—"
    if source_meteo:
        base = f"{base} + {source_meteo}"
    if _is_mppt_source(source_oper):
        base = f"{base} (MPPT)"
    if suffix:
        base = f"{base} {suffix}"
    return base


def _merged_row_energy_wh(row: dict[str, Any]) -> Optional[float]:
    """
    Energia do bucket consolidado.
    Usa e_ac_wh_15 quando existir; caso contrário, integra p_ac_w pelo intervalo do bucket.
    """
    e_wh = _to_float(row.get("e_ac_wh_15"))
    if e_wh is None:
        p_ac = _to_float(row.get("p_ac_w"))
        interval_min = _to_float(row.get("interval_min")) or 15.0
        if p_ac is not None:
            e_wh = p_ac * (interval_min / 60.0)
    if e_wh is None:
        return None
    return max(float(e_wh), 0.0)


def _actual_energy_from_merged(
    *,
    plant_id: int,
    start_utc: datetime,
    end_utc: datetime,
) -> dict[str, Any]:
    """
    Soma a energia mensal a partir da tabela consolidada, sem depender da fonte do último registro.

    A lógica evita dupla contagem quando há mais de uma fonte meteorológica ou mais de uma versão
    do mesmo bucket: para cada timestamp, usa um bucket agregado com energia/potência válida; se
    não houver agregado, soma os buckets por MPPT da mesma família operacional.
    """
    qs = (
        PVPlantMergedRecord15m.objects
        .filter(
            plant_id=plant_id,
            interval_min=15,
            ts_utc__gte=start_utc,
            ts_utc__lt=end_utc,
        )
        .filter(Q(e_ac_wh_15__isnull=False) | Q(p_ac_w__isnull=False))
        .values("ts_utc", "source_oper", "source_meteo", "e_ac_wh_15", "p_ac_w", "interval_min")
        .order_by("ts_utc", "source_oper", "source_meteo")
    )

    by_ts: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for row in qs.iterator(chunk_size=4000):
        by_ts[row["ts_utc"]].append(row)

    total_wh = 0.0
    used_buckets = 0
    latest_ts: Optional[datetime] = None
    source_counter: Counter[str] = Counter()

    for ts, rows in by_ts.items():
        agg_rows = [r for r in rows if not _is_mppt_source(r.get("source_oper"))]
        agg_with_energy = [(wh, r) for r in agg_rows if (wh := _merged_row_energy_wh(r)) is not None]

        chosen_wh: Optional[float] = None
        chosen_row: Optional[dict[str, Any]] = None

        if agg_with_energy:
            # Se houver duplicidade por fonte meteorológica, não somar duplicado.
            chosen_wh, chosen_row = max(agg_with_energy, key=lambda item: item[0])
        else:
            mppt_groups: dict[tuple[str, str], list[tuple[float, dict[str, Any]]]] = defaultdict(list)
            other_with_energy: list[tuple[float, dict[str, Any]]] = []

            for r in rows:
                wh = _merged_row_energy_wh(r)
                if wh is None:
                    continue
                if _is_mppt_source(r.get("source_oper")):
                    key = (_source_family(r.get("source_oper")), str(r.get("source_meteo") or ""))
                    mppt_groups[key].append((wh, r))
                else:
                    other_with_energy.append((wh, r))

            if mppt_groups:
                # Escolhe a família operacional/meteorológica com maior soma; evita duplicar fontes.
                candidates = []
                for _key, vals in mppt_groups.items():
                    candidates.append((sum(v[0] for v in vals), vals[0][1]))
                chosen_wh, chosen_row = max(candidates, key=lambda item: item[0])
            elif other_with_energy:
                chosen_wh, chosen_row = max(other_with_energy, key=lambda item: item[0])

        if chosen_wh is None or chosen_row is None:
            continue

        total_wh += chosen_wh
        used_buckets += 1
        latest_ts = ts if latest_ts is None else max(latest_ts, ts)
        source_counter[_format_source_label(chosen_row.get("source_oper"), chosen_row.get("source_meteo"))] += 1

    if not used_buckets:
        return {"kwh": None, "points": 0, "latest_ts": None, "source_label": None}

    source_label = source_counter.most_common(1)[0][0] if source_counter else "merge 15m"
    return {
        "kwh": total_wh / 1000.0,
        "points": used_buckets,
        "latest_ts": latest_ts,
        "source_label": source_label,
    }


# -----------------------------------------------------------------------------
# Fallback: energia real a partir da tabela operacional bruta
# -----------------------------------------------------------------------------

_PAC_ALIASES = (
    "p_ac_w",
    "pac_w",
    "pac",
    "p_ac",
    "potência ativa total",
    "potencia ativa total",
    "potência activa total",
    "potencia activa total",
    "total active power",
    "active power total",
    "active_power",
    "ac_power",
    "ac power",
    "power_ac",
    "potência ca",
    "potencia ca",
    "potência de saída ca",
    "potencia de saida ca",
    "potência de saída ac",
    "potencia de saida ac",
    "output active power",
    "output ac power",
)


def _normalize_payload_key(value: Any) -> str:
    text = str(value or "")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("_", " ").replace("-", " ")
    text = " ".join(text.split())
    return text


def _payload_item(payload: dict[str, Any], aliases: tuple[str, ...]) -> tuple[str, Any] | tuple[None, None]:
    if not isinstance(payload, dict):
        return None, None

    normalized_aliases = [_normalize_payload_key(a) for a in aliases]
    normalized_items = [(_normalize_payload_key(k), k, v) for k, v in payload.items()]

    # Primeiro: correspondência exata normalizada.
    for alias in normalized_aliases:
        for nk, original_key, value in normalized_items:
            if nk == alias:
                return str(original_key), value

    # Depois: alias contido no nome da coluna, útil para chaves com unidade: "Potência ativa total(kW)".
    for alias in normalized_aliases:
        for nk, original_key, value in normalized_items:
            if alias and alias in nk:
                return str(original_key), value

    return None, None


def _extract_p_ac_w_from_payload(payload: dict[str, Any]) -> Optional[float]:
    key, value = _payload_item(payload, _PAC_ALIASES)
    p = _to_float(value)
    if p is None:
        return None

    unit_text = f"{key or ''} {value or ''}".lower()
    if "kw" in unit_text and "kwh" not in unit_text:
        p *= 1000.0
    elif "mw" in unit_text and "mwh" not in unit_text:
        p *= 1_000_000.0

    return max(float(p), 0.0)


def _actual_energy_from_raw_oper(
    *,
    plant_id: int,
    start_utc: datetime,
    end_utc: datetime,
) -> dict[str, Any]:
    """
    Fallback para quando o merge existe, mas p_ac_w/e_ac_wh_15 ficou vazio.
    Integra a potência AC extraída do payload bruto por dispositivo.
    """
    qs = (
        InverterOperationalData.objects
        .filter(plant_id=plant_id, ts_utc__gte=start_utc, ts_utc__lt=end_utc)
        .values("ts_utc", "provedor", "pn", "devcode", "devaddr", "sn", "payload")
        .order_by("provedor", "pn", "devcode", "devaddr", "sn", "ts_utc")
    )

    by_device: dict[tuple[Any, ...], list[tuple[datetime, float, str]]] = defaultdict(list)
    latest_ts: Optional[datetime] = None
    provider_counter: Counter[str] = Counter()

    for row in qs.iterator(chunk_size=4000):
        p_ac = _extract_p_ac_w_from_payload(row.get("payload") or {})
        if p_ac is None:
            continue

        ts = row.get("ts_utc")
        if not ts:
            continue
        if timezone.is_naive(ts):
            ts = timezone.make_aware(ts, UTC)

        provider = str(row.get("provedor") or "operativo").upper()
        key = (
            row.get("provedor"),
            row.get("pn"),
            row.get("devcode"),
            row.get("devaddr"),
            row.get("sn"),
        )
        by_device[key].append((ts, p_ac, provider))
        latest_ts = ts if latest_ts is None else max(latest_ts, ts)
        provider_counter[provider] += 1

    total_wh = 0.0
    used_points = 0

    for samples in by_device.values():
        samples = sorted(samples, key=lambda x: x[0])
        if not samples:
            continue

        diffs_h = []
        for i in range(len(samples) - 1):
            dt_h = (samples[i + 1][0] - samples[i][0]).total_seconds() / 3600.0
            if 0 < dt_h <= 0.5:
                diffs_h.append(dt_h)
        fallback_h = median(diffs_h) if diffs_h else (5.0 / 60.0)
        fallback_h = min(max(float(fallback_h), 1.0 / 60.0), 15.0 / 60.0)

        for i, (ts, p_ac, _provider) in enumerate(samples):
            if i < len(samples) - 1:
                dt_h = (samples[i + 1][0] - ts).total_seconds() / 3600.0
                if not (0 < dt_h <= 0.5):
                    dt_h = fallback_h
                dt_h = min(dt_h, 15.0 / 60.0)
            else:
                dt_h = fallback_h

            total_wh += p_ac * dt_h
            used_points += 1

    if not used_points:
        return {"kwh": None, "points": 0, "latest_ts": None, "source_label": None}

    provider = provider_counter.most_common(1)[0][0] if provider_counter else "operativo"
    return {
        "kwh": total_wh / 1000.0,
        "points": used_points,
        "latest_ts": latest_ts,
        "source_label": f"{provider} bruto",
    }


# -----------------------------------------------------------------------------
# Energia prevista pelo modelo
# -----------------------------------------------------------------------------

def _model_energy_kwh(
    *,
    plant_id: int,
    start_utc: datetime,
    end_utc: datetime,
) -> dict[str, Any]:
    """
    Energia AC prevista pelo modelo a partir de PlantDiagnostic15m.pac_model_w.

    A soma é feita por timestamp para evitar dupla contagem quando houver mais de uma versão,
    fonte ou execução diagnóstica persistida para o mesmo bucket.
    """
    base_qs = PlantDiagnostic15m.objects.filter(
        plant_id=plant_id,
        ts_utc__gte=start_utc,
        ts_utc__lt=end_utc,
        pac_model_w__isnull=False,
    )

    # Preferir amostras válidas, mas não zerar a Home se o diagnóstico tiver sido persistido
    # com valid=False e pac_model_w preenchido.
    qs = base_qs.filter(valid=True)
    if not qs.exists():
        qs = base_qs

    rows = qs.values("ts_utc", "pac_model_w").order_by("ts_utc")

    by_ts: dict[datetime, float] = {}
    latest_ts: Optional[datetime] = None
    for row in rows.iterator(chunk_size=4000):
        p = _to_float(row.get("pac_model_w"))
        ts = row.get("ts_utc")
        if p is None or ts is None:
            continue
        by_ts[ts] = max(by_ts.get(ts, 0.0), max(float(p), 0.0))
        latest_ts = ts if latest_ts is None else max(latest_ts, ts)

    if not by_ts:
        return {"kwh": None, "points": 0, "latest_ts": None}

    total_wh = sum(p * 0.25 for p in by_ts.values())  # 15 min = 0,25 h
    return {"kwh": total_wh / 1000.0, "points": len(by_ts), "latest_ts": latest_ts}


def _latest_merged_source(plant_id: int) -> dict[str, Any]:
    row = (
        PVPlantMergedRecord15m.objects
        .filter(plant_id=plant_id, interval_min=15)
        .order_by("-ts_utc", "-created_at")
        .values("source_oper", "source_meteo", "ts_utc")
        .first()
    )
    if not row:
        return {"source_oper": "", "source_meteo": "", "latest_ts": None}
    return {
        "source_oper": str(row.get("source_oper") or ""),
        "source_meteo": str(row.get("source_meteo") or ""),
        "latest_ts": row.get("ts_utc"),
    }


def _build_monthly_energy_summary(plant_objs: list[PVPlant], *, limit: int = 8) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    latest_merged_by_plant = {
        r["plant_id"]: r["latest_ts"]
        for r in (
            PVPlantMergedRecord15m.objects
            .filter(plant__in=plant_objs, interval_min=15)
            .values("plant_id")
            .annotate(latest_ts=Max("ts_utc"))
        )
    }
    latest_raw_by_plant = {
        r["plant_id"]: r["latest_ts"]
        for r in (
            InverterOperationalData.objects
            .filter(plant__in=plant_objs)
            .values("plant_id")
            .annotate(latest_ts=Max("ts_utc"))
        )
    }
    latest_diag_by_plant = {
        r["plant_id"]: r["latest_ts"]
        for r in (
            PlantDiagnostic15m.objects
            .filter(plant__in=plant_objs, pac_model_w__isnull=False)
            .values("plant_id")
            .annotate(latest_ts=Max("ts_utc"))
        )
    }

    ordered = sorted(
        plant_objs,
        key=lambda p: _max_dt(
            latest_merged_by_plant.get(p.id),
            latest_raw_by_plant.get(p.id),
            latest_diag_by_plant.get(p.id),
        ) or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )[:limit]

    for plant in ordered:
        source_info = _latest_merged_source(plant.id)
        latest_ts = _max_dt(
            latest_merged_by_plant.get(plant.id),
            latest_raw_by_plant.get(plant.id),
            latest_diag_by_plant.get(plant.id),
        )

        if not latest_ts:
            rows.append({
                "plant_id": plant.id,
                "plant_name": plant.nome,
                "month_label": "—",
                "actual_kwh": None,
                "model_kwh": None,
                "delta_kwh": None,
                "delta_pct": None,
                "last_data": "—",
                "source_label": "sem dados",
                "real_points": 0,
                "model_points": 0,
            })
            continue

        start_utc, end_utc, month_label = _month_range_utc_for_plant(latest_ts, getattr(plant, "timezone", None))

        actual = _actual_energy_from_merged(
            plant_id=plant.id,
            start_utc=start_utc,
            end_utc=end_utc,
        )
        source_suffix = ""
        if actual.get("kwh") is None:
            actual = _actual_energy_from_raw_oper(
                plant_id=plant.id,
                start_utc=start_utc,
                end_utc=end_utc,
            )
            if actual.get("kwh") is not None:
                source_suffix = "(fallback)"

        model = _model_energy_kwh(
            plant_id=plant.id,
            start_utc=start_utc,
            end_utc=end_utc,
        )

        actual_kwh = actual.get("kwh")
        model_kwh = model.get("kwh")

        delta_kwh = None
        delta_pct = None
        if actual_kwh is not None and model_kwh not in (None, 0):
            delta_kwh = float(actual_kwh) - float(model_kwh)
            delta_pct = 100.0 * delta_kwh / float(model_kwh)

        source_label = actual.get("source_label")
        if source_label and source_suffix:
            source_label = f"{source_label} {source_suffix}"
        if not source_label:
            source_label = _format_source_label(source_info.get("source_oper"), source_info.get("source_meteo")) if source_info.get("latest_ts") else "sem merge"

        last_data_dt = _max_dt(actual.get("latest_ts"), model.get("latest_ts"), latest_ts)

        rows.append({
            "plant_id": plant.id,
            "plant_name": plant.nome,
            "month_label": month_label,
            "actual_kwh": _round_or_none(actual_kwh, 1),
            "model_kwh": _round_or_none(model_kwh, 1),
            "delta_kwh": _round_or_none(delta_kwh, 1),
            "delta_pct": _round_or_none(delta_pct, 1),
            "last_data": _fmt_dt(last_data_dt),
            "source_label": source_label,
            "real_points": int(actual.get("points") or 0),
            "model_points": int(model.get("points") or 0),
        })

    return rows


# -----------------------------------------------------------------------------
# Contexto geral da Home
# -----------------------------------------------------------------------------

def build_home_context(user) -> dict[str, Any]:
    plants_qs = PVPlant.objects.all().order_by("nome")
    if not user.is_superuser:
        plants_qs = plants_qs.filter(owner=user)

    plant_objs = list(plants_qs)
    plant_ids = [p.id for p in plant_objs]

    plants = [
        {
            "id": p.id,
            "nome": p.nome,
            "latitude": p.latitude,
            "longitude": p.longitude,
            "timezone": p.timezone,
            "created_at": p.created_at,
        }
        for p in plant_objs
    ]

    if not plant_ids:
        return {
            "plants": plants,
            "plant_count": 0,
            "latest_plant": None,
            "latest_plant_created": "—",
            "health_pct": 0,
            "last_refresh": "—",
            "data_start": "—",
            "data_end": "—",
            "oper_points": 0,
            "meteo_points": 0,
            "merged_points": 0,
            "oper_last_sync": "—",
            "oper_last_sample": "—",
            "meteo_last_sync": "—",
            "meteo_last_sample": "—",
            "merged_last_sync": "—",
            "merged_last_sample": "—",
            "critical_alerts": 0,
            "energy_month_rows": [],
        }

    latest_plant = max(plant_objs, key=lambda p: p.created_at or datetime.min.replace(tzinfo=UTC))

    op_qs = InverterOperationalData.objects.filter(plant_id__in=plant_ids)
    met_qs = MeteoRecord.objects.filter(plant_id__in=plant_ids)
    merged_qs = PVPlantMergedRecord15m.objects.filter(plant_id__in=plant_ids, interval_min=15)

    op_stats = op_qs.aggregate(first_ts=Min("ts_utc"), last_ts=Max("ts_utc"), last_created=Max("created_at"))
    met_stats = met_qs.aggregate(first_ts=Min("ts_utc"), last_ts=Max("ts_utc"), last_created=Max("created_at"))
    merged_stats = merged_qs.aggregate(first_ts=Min("ts_utc"), last_ts=Max("ts_utc"), last_created=Max("created_at"))

    ingest_last_run = DataIngestState.objects.filter(plant_id__in=plant_ids).aggregate(v=Max("last_run_at"))["v"]
    meteo_batch_last = MeteoImportBatch.objects.filter(plant_id__in=plant_ids).aggregate(v=Max("created_at"))["v"]

    oper_last_sync_dt = _max_dt(ingest_last_run, op_stats.get("last_created"))
    meteo_last_sync_dt = _max_dt(meteo_batch_last, met_stats.get("last_created"))
    merged_last_sync_dt = merged_stats.get("last_created")
    last_refresh_dt = _max_dt(oper_last_sync_dt, meteo_last_sync_dt, merged_last_sync_dt)

    all_first = [op_stats.get("first_ts"), met_stats.get("first_ts"), merged_stats.get("first_ts")]
    all_last = [op_stats.get("last_ts"), met_stats.get("last_ts"), merged_stats.get("last_ts")]
    data_start_dt = min([v for v in all_first if v is not None], default=None)
    data_end_dt = max([v for v in all_last if v is not None], default=None)

    plants_with_op = set(op_qs.values_list("plant_id", flat=True).distinct())
    plants_with_meteo = set(met_qs.values_list("plant_id", flat=True).distinct())
    plants_with_merged = set(merged_qs.values_list("plant_id", flat=True).distinct())
    plants_ready = plants_with_op & plants_with_meteo & plants_with_merged
    health_pct = round(100.0 * len(plants_ready) / max(len(plant_ids), 1))

    critical_alerts = FaultEvent.objects.filter(plant_id__in=plant_ids, status=FaultEvent.STATUS_OPEN).count()

    return {
        "plants": plants,
        "plant_count": len(plant_ids),
        "latest_plant": latest_plant,
        "latest_plant_created": _fmt_dt(latest_plant.created_at),
        "health_pct": health_pct,
        "last_refresh": _fmt_dt(last_refresh_dt),
        "data_start": _fmt_date(data_start_dt),
        "data_end": _fmt_date(data_end_dt),
        "oper_points": op_qs.count(),
        "meteo_points": met_qs.count(),
        "merged_points": merged_qs.count(),
        "oper_last_sync": _fmt_dt(oper_last_sync_dt),
        "oper_last_sample": _fmt_dt(op_stats.get("last_ts")),
        "meteo_last_sync": _fmt_dt(meteo_last_sync_dt),
        "meteo_last_sample": _fmt_dt(met_stats.get("last_ts")),
        "merged_last_sync": _fmt_dt(merged_last_sync_dt),
        "merged_last_sample": _fmt_dt(merged_stats.get("last_ts")),
        "critical_alerts": critical_alerts,
        "energy_month_rows": _build_monthly_energy_summary(plant_objs, limit=8),
    }
