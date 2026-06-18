from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from django.db import transaction

from core.models import MeteoDataTypology, MeteoImportBatch, MeteoRecord, MeteoSource, PVPlant


CANONICAL_COLUMNS = ("ghi", "dni", "dhi", "gti", "temp_air", "wind_speed", "rh", "pressure")

ALIASES: dict[str, tuple[str, ...]] = {
    "ghi": ("ghi", "global_horizontal_irradiance", "global_horizontal", "g_horizontal"),
    "dni": ("dni", "direct_normal_irradiance", "direct_normal"),
    "dhi": ("dhi", "diffuse_horizontal_irradiance", "diffuse_horizontal"),
    "gti": ("gti", "poa", "g_poa", "poa_irradiance", "plane_of_array"),
    "temp_air": ("temp_air", "temperature", "temperatura", "t_amb", "tamb", "air_temperature"),
    "wind_speed": ("wind_speed", "vento", "wind", "wind_speed_10m"),
    "rh": ("rh", "humidity", "relative_humidity", "umidade"),
    "pressure": ("pressure", "pressao", "surface_pressure"),
}


@dataclass(frozen=True)
class MeteoCSVIngestResult:
    rows_seen: int
    rows_imported: int
    rows_skipped: int
    first_ts_utc: Any
    last_ts_utc: Any
    dataset_model: str
    used_columns: dict[str, str]


def _decode_upload(uploaded_file) -> str:
    raw = uploaded_file.read()
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _read_csv(uploaded_file, *, delimiter: str, decimal_separator: str) -> pd.DataFrame:
    text = _decode_upload(uploaded_file)
    sep = None if delimiter == "auto" else delimiter
    return pd.read_csv(
        io.StringIO(text),
        sep=sep,
        engine="python",
        decimal=decimal_separator or ".",
    )


def _normalized_columns(df: pd.DataFrame) -> dict[str, str]:
    return {str(c).strip().lower(): c for c in df.columns}


def _find_column(df: pd.DataFrame, requested: str, aliases: tuple[str, ...] = ()) -> str | None:
    columns = _normalized_columns(df)
    requested = (requested or "").strip()
    if requested:
        if requested in df.columns:
            return requested
        match = columns.get(requested.lower())
        if match is not None:
            return str(match)
    for alias in aliases:
        match = columns.get(alias.lower())
        if match is not None:
            return str(match)
    return None


def _to_float(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        if "," in value and "." in value:
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", ".")
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(out):
        return None
    return out


def _parse_timestamps(df: pd.DataFrame, *, timestamp_col: str, plant_tz: str, timezone_mode: str, dayfirst: bool) -> pd.Series:
    raw = df[timestamp_col]
    if timezone_mode == "PLANT_TZ":
        parsed = pd.to_datetime(raw, errors="coerce", dayfirst=dayfirst)
        try:
            if parsed.dt.tz is None:
                parsed = parsed.dt.tz_localize(
                    ZoneInfo(plant_tz or "UTC"),
                    ambiguous="NaT",
                    nonexistent="shift_forward",
                )
            parsed = parsed.dt.tz_convert("UTC")
        except Exception:
            parsed = pd.to_datetime(raw, errors="coerce", dayfirst=dayfirst, utc=True)
    else:
        parsed = pd.to_datetime(raw, errors="coerce", dayfirst=dayfirst, utc=True)
    return parsed


def ingest_user_meteo_csv(
    *,
    plant: PVPlant,
    uploaded_file,
    interval_min: int,
    delimiter: str = "auto",
    decimal_separator: str = ".",
    timestamp_col: str = "ts_utc",
    timestamp_timezone: str = "UTC",
    dayfirst: bool = True,
    dataset_model: str = "USER_CSV",
    data_typology: str = MeteoDataTypology.MEASURED,
    column_map: dict[str, str] | None = None,
    update_existing: bool = True,
) -> MeteoCSVIngestResult:
    interval_min = int(interval_min)
    if interval_min not in {5, 15}:
        raise ValueError("A malha temporal do CSV deve ser de 5 ou 15 minutos.")

    df = _read_csv(uploaded_file, delimiter=delimiter, decimal_separator=decimal_separator)
    if df.empty:
        raise ValueError("O CSV esta vazio.")

    column_map = column_map or {}
    ts_col = _find_column(df, timestamp_col, aliases=("ts_utc", "timestamp", "datetime", "data_hora", "date_time"))
    if not ts_col:
        raise ValueError(f"Coluna de timestamp nao encontrada: {timestamp_col}.")

    used_columns: dict[str, str] = {"ts_utc": ts_col}
    for canonical in CANONICAL_COLUMNS:
        col = _find_column(df, column_map.get(canonical, ""), aliases=ALIASES.get(canonical, ()))
        if col:
            used_columns[canonical] = col

    value_columns = [c for c in CANONICAL_COLUMNS if c in used_columns]
    if not value_columns:
        raise ValueError("Mapeie pelo menos uma coluna meteorologica, como GHI, GTI/POA ou temperatura.")

    ts_utc = _parse_timestamps(
        df,
        timestamp_col=ts_col,
        plant_tz=getattr(plant, "timezone", "UTC") or "UTC",
        timezone_mode=timestamp_timezone,
        dayfirst=dayfirst,
    )

    dataset_model = (dataset_model or "USER_CSV").strip()[:64]
    data_typology = data_typology or MeteoDataTypology.MEASURED

    objs: list[MeteoRecord] = []
    rows_skipped = 0
    source_endpoint = f"upload:{getattr(uploaded_file, 'name', 'csv')}"[:255]
    for idx, row in df.iterrows():
        ts = ts_utc.iloc[idx]
        if pd.isna(ts):
            rows_skipped += 1
            continue
        values = {
            canonical: _to_float(row.get(used_columns[canonical]))
            for canonical in value_columns
        }
        if all(values.get(canonical) is None for canonical in value_columns):
            rows_skipped += 1
            continue
        objs.append(
            MeteoRecord(
                plant=plant,
                source=MeteoSource.USER_CSV,
                source_endpoint=source_endpoint,
                dataset_model=dataset_model,
                data_typology=data_typology,
                ts_utc=ts.to_pydatetime(),
                interval_min=interval_min,
                ghi=values.get("ghi"),
                dni=values.get("dni"),
                dhi=values.get("dhi"),
                gti=values.get("gti"),
                temp_air=values.get("temp_air"),
                wind_speed=values.get("wind_speed"),
                rh=values.get("rh"),
                pressure=values.get("pressure"),
                meteo_qc_score=1.0 if data_typology == MeteoDataTypology.MEASURED else None,
            )
        )

    if not objs:
        raise ValueError("Nenhuma linha valida foi encontrada no CSV.")

    first_ts = min(obj.ts_utc for obj in objs)
    last_ts = max(obj.ts_utc for obj in objs)
    with transaction.atomic():
        batch = MeteoImportBatch.objects.create(
            plant=plant,
            source=MeteoSource.USER_CSV,
            source_endpoint=source_endpoint,
            dataset_model=dataset_model,
            data_typology=data_typology,
            interval_min=interval_min,
            start_date=first_ts.date() if hasattr(first_ts, "date") else date.today(),
            end_date=last_ts.date() if hasattr(last_ts, "date") else date.today(),
            request_params={
                "delimiter": delimiter,
                "decimal_separator": decimal_separator,
                "timestamp_col": ts_col,
                "timestamp_timezone": timestamp_timezone,
                "dayfirst": bool(dayfirst),
                "used_columns": used_columns,
            },
            response_meta={"source": "USER_CSV"},
            imported_rows=len(objs),
        )
        for obj in objs:
            obj.import_batch = batch

        if update_existing:
            MeteoRecord.objects.bulk_create(
                objs,
                batch_size=2000,
                update_conflicts=True,
                unique_fields=["plant", "source", "ts_utc"],
                update_fields=[
                    "import_batch",
                    "source_endpoint",
                    "dataset_model",
                    "data_typology",
                    "interval_min",
                    "ghi",
                    "dni",
                    "dhi",
                    "gti",
                    "temp_air",
                    "wind_speed",
                    "rh",
                    "pressure",
                    "meteo_qc_score",
                ],
            )
        else:
            MeteoRecord.objects.bulk_create(objs, batch_size=2000, ignore_conflicts=True)

    return MeteoCSVIngestResult(
        rows_seen=int(len(df)),
        rows_imported=len(objs),
        rows_skipped=rows_skipped,
        first_ts_utc=first_ts,
        last_ts_utc=last_ts,
        dataset_model=dataset_model,
        used_columns=used_columns,
    )
