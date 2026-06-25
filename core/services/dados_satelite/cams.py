from __future__ import annotations

import io
import math
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin

import pandas as pd
import requests
from django.conf import settings
from django.db import transaction

from core.models import MeteoDataTypology, MeteoImportBatch, MeteoRecord, MeteoSource, PVPlant
from core.services.dados_satelite.openmeteo import fetch_openmeteo_hourly
from core.services.meteo_qc import MeteoQCConfig, apply_meteo_qc


CAMS_PROCESS_ID = "cams-solar-radiation-timeseries"
CAMS_RETRIEVE_BASE_URL = "https://ads.atmosphere.copernicus.eu/api/retrieve/v1"
CAMS_DATASET_DOI = "10.24381/5cab0912"
SUPPORTED_INTERVALS_MIN = {15, 60}


@dataclass(frozen=True)
class CamsFetchResult:
    df: pd.DataFrame
    meta: Dict[str, Any]


def _to_float_or_none(value: Any) -> Optional[float]:
    try:
        if value is None or pd.isna(value):
            return None
        x = float(value)
        if not math.isfinite(x):
            return None
        return x
    except Exception:
        return None


def _normalize_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return "".join(ch for ch in text if ch.isalnum())


def _time_step_from_interval(interval_min: int) -> str:
    interval = int(interval_min)
    if interval == 15:
        return "15minute"
    if interval == 60:
        return "1hour"
    raise ValueError("CAMS suporta apenas intervalos de 15 ou 60 minutos no fluxo do sistema.")


def _iter_date_chunks(start_date: date, end_date: date, *, chunk_days: int = 31) -> Iterable[Tuple[date, date]]:
    cur = start_date
    while cur <= end_date:
        end = min(cur + timedelta(days=chunk_days - 1), end_date)
        yield cur, end
        cur = end + timedelta(days=1)


def _safe_base_url(base_url: str) -> str:
    raw = (base_url or CAMS_RETRIEVE_BASE_URL).strip()
    return raw.rstrip("/") + "/"


class CamsADSClient:
    """Thin REST client for the ADS OGC retrieve API used by CAMS time series."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = CAMS_RETRIEVE_BASE_URL,
        timeout_s: float = 60.0,
        poll_interval_s: float = 5.0,
        max_wait_s: float = 900.0,
    ) -> None:
        self.api_key = (api_key or "").strip()
        if not self.api_key:
            raise RuntimeError(
                "Token da ADS/Copernicus ausente. Informe o token no formulario ou defina CAMS_ADS_API_KEY."
            )
        self.base_url = _safe_base_url(base_url)
        self.timeout_s = float(timeout_s)
        self.poll_interval_s = max(1.0, float(poll_interval_s))
        self.max_wait_s = max(self.poll_interval_s, float(max_wait_s))

    def _headers(self) -> Dict[str, str]:
        return {
            "PRIVATE-TOKEN": self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "BrazSolarScan CAMS integration",
        }

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    def _request_json(self, method: str, path_or_url: str, **kwargs) -> Dict[str, Any]:
        url = path_or_url if str(path_or_url).startswith("http") else self._url(path_or_url)
        response = requests.request(
            method,
            url,
            headers=self._headers(),
            timeout=self.timeout_s,
            **kwargs,
        )
        if not response.ok:
            body = (response.text or "")[:1500]
            raise RuntimeError(f"ADS/CAMS HTTP {response.status_code}: {body}")
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(f"ADS/CAMS retornou resposta nao JSON em {url}.") from exc
        return data if isinstance(data, dict) else {"value": data}

    def submit(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        return self._request_json(
            "POST",
            f"processes/{CAMS_PROCESS_ID}/execution",
            json={"inputs": inputs, "response": "raw"},
        )

    def wait_for_success(self, job_id: str) -> Dict[str, Any]:
        started = time.monotonic()
        last: Dict[str, Any] = {}
        while True:
            last = self._request_json("GET", f"jobs/{job_id}", params={"request": "false", "log": "true"})
            status = str(last.get("status") or "").lower()
            if status == "successful":
                return last
            if status in {"failed", "rejected", "dismissed"}:
                message = last.get("message") or last.get("error") or ""
                raise RuntimeError(f"ADS/CAMS job {job_id} terminou com status {status}: {message}")
            if (time.monotonic() - started) > self.max_wait_s:
                raise TimeoutError(f"ADS/CAMS job {job_id} excedeu {int(self.max_wait_s)} s de espera.")
            time.sleep(self.poll_interval_s)

    def results(self, job_id: str) -> Dict[str, Any]:
        return self._request_json("GET", f"jobs/{job_id}/results")

    def download_asset(self, href: str) -> bytes:
        response = requests.get(
            href,
            headers={"PRIVATE-TOKEN": self.api_key, "User-Agent": "BrazSolarScan CAMS integration"},
            timeout=max(self.timeout_s, 120.0),
        )
        if not response.ok:
            body = (response.text or "")[:1500]
            raise RuntimeError(f"Download ADS/CAMS HTTP {response.status_code}: {body}")
        return response.content

    def fetch_csv(self, inputs: Dict[str, Any]) -> Tuple[bytes, Dict[str, Any]]:
        submitted = self.submit(inputs)
        job_id = str(submitted.get("jobID") or submitted.get("job_id") or "").strip()
        if not job_id:
            raise RuntimeError(f"ADS/CAMS nao retornou jobID: {submitted}")

        job = submitted
        if str(submitted.get("status") or "").lower() != "successful":
            job = self.wait_for_success(job_id)

        result = self.results(job_id)
        href = _find_asset_href(result)
        if not href:
            raise RuntimeError(f"ADS/CAMS nao retornou asset para download: {result}")

        return self.download_asset(href), {"submitted": submitted, "job": job, "result": _redact_asset_result(result)}


def _find_asset_href(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        href = value.get("href")
        if isinstance(href, str) and href.startswith("http"):
            return href
        if "asset" in value:
            found = _find_asset_href(value.get("asset"))
            if found:
                return found
        if "value" in value:
            found = _find_asset_href(value.get("value"))
            if found:
                return found
        for item in value.values():
            found = _find_asset_href(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_asset_href(item)
            if found:
                return found
    return None


def _redact_asset_result(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key.lower() == "href" and isinstance(item, str) and len(item) > 120:
                out[key] = item[:100] + "...redacted"
            else:
                out[key] = _redact_asset_result(item)
        return out
    if isinstance(value, list):
        return [_redact_asset_result(item) for item in value]
    return value


def build_cams_inputs(
    *,
    lat: float,
    lon: float,
    start_date: date,
    end_date: date,
    interval_min: int = 15,
    sky_type: str = "observed_cloud",
    altitude_m: float = -999.0,
    time_reference: str = "universal_time",
) -> Dict[str, Any]:
    sky = (sky_type or "observed_cloud").strip()
    if sky not in {"clear", "observed_cloud"}:
        raise ValueError("sky_type CAMS invalido. Use clear ou observed_cloud.")

    tref = (time_reference or "universal_time").strip()
    if tref not in {"universal_time", "true_solar_time"}:
        raise ValueError("time_reference CAMS invalido. Use universal_time ou true_solar_time.")

    return {
        "sky_type": sky,
        "location": {"latitude": float(lat), "longitude": float(lon)},
        "altitude": float(altitude_m),
        "date": f"{start_date.isoformat()}/{end_date.isoformat()}",
        "time_step": _time_step_from_interval(interval_min),
        "time_reference": tref,
        "data_format": "csv",
    }


def _decode_csv_bytes(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _split_csv_line(line: str) -> List[str]:
    sep = ";" if line.count(";") >= line.count(",") else ","
    return [part.strip() for part in line.split(sep)]


def _find_header_line(lines: List[str]) -> int:
    for idx, line in enumerate(lines):
        tokens = [_normalize_key(token) for token in _split_csv_line(line)]
        has_time = any(token in {"observationperiod", "period", "time", "timestamp", "date", "datetime", "dateend"} for token in tokens)
        has_radiation = any(token in {"ghi", "bni", "dni", "dhi", "bhi", "ghic", "bnic", "dhic", "bhic"} for token in tokens)
        has_descriptive_radiation = any(("global" in token or "directnormal" in token or "diffuse" in token) for token in tokens)
        if has_time and (has_radiation or has_descriptive_radiation):
            return idx
    raise ValueError("Nao encontrei cabecalho tabular no CSV CAMS.")


def _read_cams_csv(data: bytes) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    text = _decode_csv_bytes(data)
    lines = [line for line in text.splitlines() if line.strip()]
    header_idx = _find_header_line(lines)
    header_line = lines[header_idx]
    sep = ";" if header_line.count(";") >= header_line.count(",") else ","
    df = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])), sep=sep, engine="python")
    return df, {
        "header_line": header_idx + 1,
        "delimiter": sep,
        "metadata_preview": lines[: min(header_idx, 12)],
        "columns": [str(col) for col in df.columns],
    }


ISO_DT_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"
)


def _parse_cams_period_end(value: Any) -> pd.Timestamp:
    text = str(value or "").strip()
    matches = ISO_DT_RE.findall(text)
    candidate = matches[-1] if matches else text
    return pd.to_datetime(candidate, errors="coerce", utc=True)


def _find_col_by_alias(df: pd.DataFrame, aliases: Tuple[str, ...]) -> Optional[str]:
    normalized = {_normalize_key(col): str(col) for col in df.columns}
    for alias in aliases:
        key = _normalize_key(alias)
        if key in normalized:
            return normalized[key]
    for col in df.columns:
        key = _normalize_key(col)
        if any(alias in key for alias in aliases):
            return str(col)
    return None


def _extract_timestamp_series(df: pd.DataFrame) -> Tuple[pd.Series, str]:
    end_col = _find_col_by_alias(
        df,
        (
            "date_end",
            "date end",
            "period_end",
            "end",
            "time",
            "timestamp",
            "observation_period",
            "observation period",
        ),
    )
    if not end_col:
        raise ValueError("CSV CAMS sem coluna de tempo/periodo reconhecida.")
    return df[end_col].map(_parse_cams_period_end), end_col


def _extract_radiation(df: pd.DataFrame, interval_min: int) -> Tuple[pd.DataFrame, Dict[str, str]]:
    factor = 60.0 / float(interval_min)
    aliases = {
        "ghi": ("ghi", "globalhorizontal", "globalhorizontalirradiation", "globalhorizontalirradiance", "ghic", "clearskyghi"),
        "dni": ("bni", "dni", "directnormal", "directnormalirradiation", "directnormalirradiance", "bnic", "clearskybni"),
        "dhi": ("dhi", "diffusehorizontal", "diffusehorizontalirradiation", "diffusehorizontalirradiance", "dhic", "clearskydhi"),
    }
    out = pd.DataFrame(index=df.index)
    used: Dict[str, str] = {}
    for target, target_aliases in aliases.items():
        col = _find_col_by_alias(df, target_aliases)
        if not col:
            out[target] = pd.NA
            continue
        values = pd.to_numeric(df[col], errors="coerce")
        out[target] = values * factor
        used[target] = col
    if not used:
        raise ValueError("CSV CAMS sem colunas GHI/BNI(DNI)/DHI reconhecidas.")
    out["gti"] = pd.NA
    return out, used


def parse_cams_csv_bytes(
    data: bytes,
    *,
    interval_min: int,
    normalize_to_period_start: bool = True,
) -> CamsFetchResult:
    raw, csv_meta = _read_cams_csv(data)
    if raw.empty:
        return CamsFetchResult(df=pd.DataFrame(), meta=csv_meta)

    period_end, ts_col = _extract_timestamp_series(raw)
    ts_utc = period_end
    if normalize_to_period_start:
        ts_utc = ts_utc - pd.Timedelta(minutes=int(interval_min))

    radiation, used_radiation = _extract_radiation(raw, interval_min)
    df = pd.DataFrame(
        {
            "ts_utc": ts_utc,
            "ghi": radiation["ghi"],
            "dni": radiation["dni"],
            "dhi": radiation["dhi"],
            "gti": radiation["gti"],
            "temp_air": pd.NA,
            "wind_speed": pd.NA,
            "rh": pd.NA,
            "pressure": pd.NA,
            "interval_min": int(interval_min),
        }
    )
    df = df.dropna(subset=["ts_utc"]).sort_values("ts_utc").reset_index(drop=True)

    meta = {
        **csv_meta,
        "timestamp_column": ts_col,
        "radiation_columns": used_radiation,
        "radiation_units_original": "Wh/m2 por periodo de integracao",
        "radiation_units_stored": "W/m2 medio do periodo",
        "source_time_label_original": "period_end",
        "stored_time_label": "period_start" if normalize_to_period_start else "period_end",
    }
    return CamsFetchResult(df=df, meta=meta)


def fetch_cams_radiation(
    *,
    lat: float,
    lon: float,
    start_date: date,
    end_date: date,
    interval_min: int = 15,
    sky_type: str = "observed_cloud",
    altitude_m: float = -999.0,
    time_reference: str = "universal_time",
    api_key: str = "",
    timeout_s: float = 60.0,
    poll_interval_s: float = 5.0,
    max_wait_s: float = 900.0,
    base_url: str = CAMS_RETRIEVE_BASE_URL,
) -> CamsFetchResult:
    inputs = build_cams_inputs(
        lat=lat,
        lon=lon,
        start_date=start_date,
        end_date=end_date,
        interval_min=interval_min,
        sky_type=sky_type,
        altitude_m=altitude_m,
        time_reference=time_reference,
    )
    client = CamsADSClient(
        api_key=api_key,
        base_url=base_url,
        timeout_s=timeout_s,
        poll_interval_s=poll_interval_s,
        max_wait_s=max_wait_s,
    )
    data, request_meta = client.fetch_csv(inputs)
    parsed = parse_cams_csv_bytes(data, interval_min=interval_min)
    parsed.meta.update(
        {
            "inputs": inputs,
            "request_meta": request_meta,
            "dataset": CAMS_PROCESS_ID,
            "doi": CAMS_DATASET_DOI,
        }
    )
    return parsed


def _merge_openmeteo_temperature(
    df_cams: pd.DataFrame,
    *,
    plant: PVPlant,
    start_date: date,
    end_date: date,
    interval_min: int,
    model: Optional[str],
    timeout_s: float,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    if df_cams.empty:
        return df_cams, {"rows": 0, "matched_temperature_rows": 0}

    temp = fetch_openmeteo_hourly(
        lat=float(plant.latitude),
        lon=float(plant.longitude),
        start_date=start_date,
        end_date=end_date,
        interval_min=int(interval_min),
        include_gti=False,
        model=model,
        timeout_s=int(timeout_s),
    )
    df_temp = temp.df.copy()
    if df_temp.empty:
        raise RuntimeError("Open-Meteo nao retornou temperatura para complementar a requisicao CAMS.")

    keep = ["ts_utc", "temp_air", "wind_speed", "rh", "pressure"]
    df_temp = df_temp[[col for col in keep if col in df_temp.columns]].copy()
    df_temp["ts_utc"] = pd.to_datetime(df_temp["ts_utc"], errors="coerce", utc=True)

    base = df_cams.copy()
    base["ts_utc"] = pd.to_datetime(base["ts_utc"], errors="coerce", utc=True)
    merged = base.merge(df_temp, on="ts_utc", how="left", suffixes=("", "_om"))
    for col in ("temp_air", "wind_speed", "rh", "pressure"):
        om_col = f"{col}_om"
        if om_col in merged.columns:
            merged[col] = merged[om_col]
            merged = merged.drop(columns=[om_col])

    matched = int(pd.to_numeric(merged.get("temp_air"), errors="coerce").notna().sum())
    if matched == 0:
        raise RuntimeError("A consulta Open-Meteo retornou dados, mas nenhum timestamp coincidiu com a grade CAMS.")

    return merged, {
        "rows": int(len(df_temp)),
        "matched_temperature_rows": matched,
        "openmeteo_meta": temp.meta,
        "openmeteo_model": model or "best_match",
    }


def _dataset_model(*, sky_type: str, interval_min: int, include_openmeteo_temperature: bool, openmeteo_model: Optional[str]) -> str:
    label = f"CAMS_{sky_type}_{interval_min}min"
    if include_openmeteo_temperature:
        suffix = (openmeteo_model or "best_match").strip() or "best_match"
        label = f"{label}+OMTemp_{suffix}"
    return label[:64]


def ingest_cams_range(
    *,
    plant: PVPlant,
    start_date: date,
    end_date: date,
    interval_min: int = 15,
    sky_type: str = "observed_cloud",
    altitude_m: float = -999.0,
    time_reference: str = "universal_time",
    api_key: Optional[str] = None,
    include_openmeteo_temperature: bool = False,
    openmeteo_model: Optional[str] = None,
    timeout_s: Optional[float] = None,
    poll_interval_s: Optional[float] = None,
    max_wait_s: Optional[float] = None,
    base_url: Optional[str] = None,
) -> Tuple[int, Dict[str, Any]]:
    interval_min = int(interval_min)
    if interval_min not in SUPPORTED_INTERVALS_MIN:
        raise ValueError("CAMS suporta 15 ou 60 minutos no fluxo de aquisicao.")

    token = (api_key or getattr(settings, "CAMS_ADS_API_KEY", "") or "").strip()
    if not token:
        raise RuntimeError("Informe o token ADS/Copernicus no formulario ou configure CAMS_ADS_API_KEY.")

    timeout = float(timeout_s if timeout_s is not None else getattr(settings, "CAMS_HTTP_TIMEOUT", 60.0))
    poll_interval = float(
        poll_interval_s if poll_interval_s is not None else getattr(settings, "CAMS_POLL_INTERVAL_SEC", 5.0)
    )
    max_wait = float(max_wait_s if max_wait_s is not None else getattr(settings, "CAMS_MAX_WAIT_SEC", 900.0))
    api_base = str(base_url or getattr(settings, "CAMS_ADS_RETRIEVE_BASE_URL", CAMS_RETRIEVE_BASE_URL))

    dataset_model = _dataset_model(
        sky_type=sky_type,
        interval_min=interval_min,
        include_openmeteo_temperature=include_openmeteo_temperature,
        openmeteo_model=openmeteo_model,
    )

    batch = MeteoImportBatch.objects.create(
        plant=plant,
        source=MeteoSource.CAMS,
        source_endpoint=api_base[:255],
        dataset_model=dataset_model,
        data_typology=MeteoDataTypology.REANALYSIS_MODELED,
        interval_min=interval_min,
        start_date=start_date,
        end_date=end_date,
        request_url=f"{api_base.rstrip('/')}/processes/{CAMS_PROCESS_ID}/execution",
        request_params={},
        response_meta={},
        imported_rows=0,
    )

    total_count = 0
    chunk_metas: List[Dict[str, Any]] = []

    for start_chunk, end_chunk in _iter_date_chunks(start_date, end_date):
        result = fetch_cams_radiation(
            lat=float(plant.latitude),
            lon=float(plant.longitude),
            start_date=start_chunk,
            end_date=end_chunk,
            interval_min=interval_min,
            sky_type=sky_type,
            altitude_m=altitude_m,
            time_reference=time_reference,
            api_key=token,
            timeout_s=timeout,
            poll_interval_s=poll_interval,
            max_wait_s=max_wait,
            base_url=api_base,
        )
        df = result.df.copy()
        meta = dict(result.meta)

        if include_openmeteo_temperature:
            df, temp_meta = _merge_openmeteo_temperature(
                df,
                plant=plant,
                start_date=start_chunk,
                end_date=end_chunk,
                interval_min=interval_min,
                model=(openmeteo_model or None),
                timeout_s=timeout,
            )
            meta["temperature_source"] = "OPENMETEO"
            meta["temperature_meta"] = temp_meta
        else:
            meta["temperature_source"] = ""

        qc_cfg = MeteoQCConfig(interval_min=interval_min, source=MeteoSource.CAMS)
        df, qc_meta = apply_meteo_qc(df, lat=float(plant.latitude), lon=float(plant.longitude), cfg=qc_cfg)
        meta["qc"] = qc_meta
        chunk_metas.append(meta)

        if df.empty:
            continue

        objs: List[MeteoRecord] = []
        for row in df.itertuples(index=False):
            objs.append(
                MeteoRecord(
                    plant=plant,
                    source=MeteoSource.CAMS,
                    import_batch=batch,
                    source_endpoint=api_base[:255],
                    dataset_model=dataset_model,
                    data_typology=MeteoDataTypology.REANALYSIS_MODELED,
                    ts_utc=row.ts_utc.to_pydatetime() if hasattr(row.ts_utc, "to_pydatetime") else row.ts_utc,
                    interval_min=interval_min,
                    ghi=_to_float_or_none(getattr(row, "ghi", None)),
                    dni=_to_float_or_none(getattr(row, "dni", None)),
                    dhi=_to_float_or_none(getattr(row, "dhi", None)),
                    gti=_to_float_or_none(getattr(row, "gti", None)),
                    temp_air=_to_float_or_none(getattr(row, "temp_air", None)),
                    wind_speed=_to_float_or_none(getattr(row, "wind_speed", None)),
                    rh=_to_float_or_none(getattr(row, "rh", None)),
                    pressure=_to_float_or_none(getattr(row, "pressure", None)),
                    meteo_qc_score=_to_float_or_none(getattr(row, "meteo_qc_score", None)),
                    flag_meteo_low_confidence=bool(getattr(row, "flag_meteo_low_confidence", False)),
                    flag_meteo_interpolated=bool(getattr(row, "flag_meteo_interpolated", False)),
                    flag_meteo_outlier=bool(getattr(row, "flag_meteo_outlier", False)),
                    flag_meteo_artifact=bool(getattr(row, "flag_meteo_artifact", False)),
                )
            )

        with transaction.atomic():
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
                    "flag_meteo_low_confidence",
                    "flag_meteo_interpolated",
                    "flag_meteo_outlier",
                    "flag_meteo_artifact",
                ],
            )
        total_count += len(objs)

    first_meta = chunk_metas[0] if chunk_metas else {}
    meta_out = {
        "dataset": CAMS_PROCESS_ID,
        "doi": CAMS_DATASET_DOI,
        "chunks": len(chunk_metas),
        "interval_min": interval_min,
        "sky_type": sky_type,
        "time_reference": time_reference,
        "dataset_model": dataset_model,
        "temperature_source": "OPENMETEO" if include_openmeteo_temperature else "",
        "note": (
            "CAMS fornece irradiacao solar (GHI, DHI, BNI/DNI) em Wh/m2; "
            "o sistema armazena W/m2 medio e normaliza period_end para period_start."
        ),
        "first_chunk": first_meta,
        "last_chunk": chunk_metas[-1] if chunk_metas else None,
    }

    batch.request_params = dict(first_meta.get("inputs") or {})
    batch.response_meta = meta_out
    batch.imported_rows = total_count
    batch.save(update_fields=["request_params", "response_meta", "imported_rows"])

    return total_count, meta_out
