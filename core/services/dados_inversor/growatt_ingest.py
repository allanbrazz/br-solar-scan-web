from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from django.db import transaction

from core.models import InverterOperationalData, PVPlant, PlantMonitoringCredential
from core.services.dados_inversor.growatt_client import GrowattClient


def _parse_growatt_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _payload_with_canonical_time(row: dict[str, Any], timestamp: datetime) -> dict[str, Any]:
    payload = dict(row)
    payload.setdefault("growatt_time_original", row.get("time") or row.get("createTime"))
    payload["Data E Hora"] = timestamp.isoformat()
    return payload


@transaction.atomic
def sync_growatt_operational_data(
    *,
    plant: PVPlant,
    cred: PlantMonitoringCredential,
    username: str,
    password: str,
    start_day: date,
    end_day: date,
    client: GrowattClient | None = None,
) -> dict[str, Any]:
    device_sn = str(cred.growatt_device_sn or "").strip()
    device_type = str(cred.growatt_device_type or "1").strip()
    if not cred.growatt_plant_id or not device_sn:
        raise ValueError("Descubra e vincule uma planta/dispositivo Growatt antes da sincronizacao.")

    client = client or GrowattClient(username, password)
    result = client.fetch_history(
        device_sn=device_sn,
        device_type=device_type,
        start_day=start_day,
        end_day=end_day,
    )
    rows = result.get("rows") if isinstance(result, dict) else []
    rows = rows if isinstance(rows, list) else []
    meta = result.get("meta") if isinstance(result, dict) and isinstance(result.get("meta"), dict) else {}

    datalogger_sn = str(cred.growatt_datalogger_sn or meta.get("datalogger_sn") or "GROWATT")
    if not cred.growatt_datalogger_sn and datalogger_sn != "GROWATT":
        cred.growatt_datalogger_sn = datalogger_sn
        cred.save(update_fields=["growatt_datalogger_sn", "updated_at"])

    devcode = f"TYPE_{device_type}"
    devaddr = 1
    parsed_rows: dict[datetime, dict[str, Any]] = {}
    bad_ts = 0
    for row in rows:
        if not isinstance(row, dict):
            bad_ts += 1
            continue
        timestamp = _parse_growatt_utc(row.get("time") or row.get("createTime") or row.get("timestamp"))
        if timestamp is None:
            bad_ts += 1
            continue
        parsed_rows[timestamp] = _payload_with_canonical_time(row, timestamp)

    timestamps = list(parsed_rows)
    existing = set(
        InverterOperationalData.objects.filter(
            plant=plant,
            provedor="GROWATT",
            pn=datalogger_sn,
            devcode=devcode,
            devaddr=devaddr,
            sn=device_sn,
            ts_utc__in=timestamps,
        ).values_list("ts_utc", flat=True)
    )

    objects = [
        InverterOperationalData(
            plant=plant,
            provedor="GROWATT",
            pn=datalogger_sn,
            devcode=devcode,
            devaddr=devaddr,
            sn=device_sn,
            ts_utc=timestamp,
            payload=payload,
        )
        for timestamp, payload in parsed_rows.items()
    ]
    if objects:
        InverterOperationalData.objects.bulk_create(
            objects,
            batch_size=1000,
            update_conflicts=True,
            unique_fields=["plant", "provedor", "pn", "devcode", "devaddr", "sn", "ts_utc"],
            update_fields=["payload", "updated_at"],
        )

    inserted = sum(1 for timestamp in timestamps if timestamp not in existing)
    return {
        "inserted": inserted,
        "updated": len(timestamps) - inserted,
        "requested_rows": len(rows),
        "valid_rows": len(timestamps),
        "bad_ts": bad_ts,
        "range": {
            "start": start_day.isoformat(),
            "end": end_day.isoformat(),
        },
        "meta": meta,
    }
