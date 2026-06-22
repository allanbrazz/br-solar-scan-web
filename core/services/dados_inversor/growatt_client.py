from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable, Dict

from django.conf import settings
from growattServer import GrowattApi, OpenApiV1

class GrowattReadError(RuntimeError):
    """Falha ao consultar dados da Growatt."""


class GrowattAuthError(GrowattReadError):
    """Falha de autenticacao no ShinePhone/Growatt."""


@dataclass(frozen=True)
class GrowattSession:
    user_id: str
    token: str
    timezone_id: str


@dataclass(frozen=True)
class GrowattHistoryEndpoint:
    method: str
    path: str
    serial_param: str


# Tipos retornados por GET /v1/device/list.
HISTORY_ENDPOINTS: dict[str, GrowattHistoryEndpoint] = {
    "1": GrowattHistoryEndpoint("GET", "device/inverter/data", "device_sn"),
    "4": GrowattHistoryEndpoint("POST", "device/max/max_data", "max_sn"),
    "5": GrowattHistoryEndpoint("POST", "device/mix/mix_data", "mix_sn"),
    "6": GrowattHistoryEndpoint("POST", "device/spa/spa_data", "spa_sn"),
    "7": GrowattHistoryEndpoint("POST", "device/tlx/tlx_data", "tlx_sn"),
}


def _base_url(value: str) -> str:
    return f"{str(value or '').rstrip('/')}/"


def _first(mapping: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return default


def _as_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    return []


class GrowattClient:
    """Cliente de leitura para ShinePhone + Growatt Open API v1."""

    def __init__(
        self,
        username: str,
        password: str,
        *,
        login_base_url: str | None = None,
        openapi_base_url: str | None = None,
        api_factory: Callable[..., Any] = GrowattApi,
        openapi_factory: Callable[..., Any] = OpenApiV1,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.username = str(username or "").strip()
        self.password = str(password or "")
        self.login_base_url = _base_url(
            login_base_url or getattr(settings, "GROWATT_LOGIN_BASE_URL", "https://server-api.growatt.com/")
        )
        self.openapi_base_url = _base_url(
            openapi_base_url or getattr(settings, "GROWATT_OPENAPI_BASE_URL", "https://openapi.growatt.com/v1/")
        )
        self.timeout = float(getattr(settings, "GROWATT_HTTP_TIMEOUT", 45.0))
        self.max_retries = max(1, int(getattr(settings, "GROWATT_MAX_RETRIES", 4)))
        self.retry_base_sec = max(0.0, float(getattr(settings, "GROWATT_RETRY_BASE_SEC", 1.0)))
        self._sleeper = sleeper

        try:
            self.api = api_factory(add_random_user_id=True)
        except TypeError:
            self.api = api_factory()
        self.api.server_url = self.login_base_url
        self._openapi_factory = openapi_factory
        self.openapi: Any | None = None
        self.session: GrowattSession | None = None
        self.login_response: dict[str, Any] | None = None

    def login(self) -> GrowattSession:
        if self.session is not None:
            return self.session
        if not self.username or not self.password:
            raise GrowattAuthError("Informe usuario e senha do ShinePhone.")

        try:
            response = self.api.login(self.username, self.password)
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            detail = f" (HTTP {status})" if status else ""
            raise GrowattAuthError(f"Nao foi possivel acessar o servidor Growatt{detail}.") from exc

        if not isinstance(response, dict) or not response.get("success"):
            code = _first(response, "msg", "error_msg", default="") if isinstance(response, dict) else ""
            suffix = f" (codigo {code})" if code else ""
            raise GrowattAuthError(f"Login Growatt recusado{suffix}.")

        user = response.get("user") if isinstance(response.get("user"), dict) else {}
        user_id = _first(response, "userId", "user_id") or _first(user, "id", "uid")
        token = _first(user, "token") or _first(response, "token")
        if not user_id or not token:
            raise GrowattAuthError("Login aceito, mas a Growatt nao retornou usuario/token da Open API.")

        self.login_response = response
        self.session = GrowattSession(
            user_id=str(user_id),
            token=str(token),
            timezone_id=str(_first(user, "timeZone", "timezone", default="")),
        )
        self.openapi = self._openapi_factory(self.session.token)
        self.openapi.api_url = self.openapi_base_url
        return self.session

    def list_plants(self) -> list[dict[str, Any]]:
        session = self.login()
        try:
            raw = self.api.plant_list(session.user_id)
        except Exception as exc:
            raise GrowattReadError("Falha ao listar plantas da conta Growatt.") from exc

        if isinstance(raw, dict):
            plants = _as_list(raw.get("data") or raw.get("plants") or raw.get("plantList"))
        else:
            plants = _as_list(raw)
        if not plants and self.login_response:
            plants = _as_list(self.login_response.get("data"))

        result = []
        for plant in plants:
            plant_id = _first(plant, "plantId", "plant_id", "id")
            if not plant_id:
                continue
            result.append(
                {
                    "plant_id": str(plant_id),
                    "name": str(_first(plant, "plantName", "name", default=f"Planta {plant_id}")),
                    "current_power": _first(plant, "currentPower", "currPower", "power", default=None),
                    "today_energy": _first(plant, "todayEnergy", "today_energy", default=None),
                    "total_energy": _first(plant, "totalEnergy", "total_energy", default=None),
                }
            )
        return result

    def list_devices(self, plant_id: str | int) -> list[dict[str, Any]]:
        self.login()
        try:
            raw = self.api.device_list(str(plant_id))
        except Exception as exc:
            raise GrowattReadError(f"Falha ao listar dispositivos da planta Growatt {plant_id}.") from exc

        devices = _as_list(raw.get("devices") if isinstance(raw, dict) else raw)
        result = []
        for device in devices:
            serial = _first(device, "deviceSn", "device_sn", "serialNum", "sn")
            if not serial:
                continue
            device_type = str(_first(device, "type", "device_type", "invType", default="1"))
            result.append(
                {
                    "device_sn": str(serial),
                    "datalogger_sn": str(_first(device, "datalogSn", "datalogger_sn", default="")),
                    "device_type": device_type,
                    "device_kind": str(_first(device, "deviceType", "kind", default="inverter")),
                    "alias": str(_first(device, "deviceAilas", "alias", "device_alias", default="")),
                    "model": str(_first(device, "model", "invType", default="")),
                    "status": _first(device, "deviceStatus", "status", default=None),
                    "lost": bool(_first(device, "lost", default=False)),
                    "power": _first(device, "power", default=None),
                    "today_energy": _first(device, "eToday", "today_energy", default=None),
                }
            )
        return result

    def _history_page(
        self,
        *,
        endpoint: GrowattHistoryEndpoint,
        device_sn: str,
        start_day: date,
        end_day: date,
        page: int,
    ) -> dict[str, Any]:
        self.login()
        assert self.openapi is not None
        params = {
            endpoint.serial_param: device_sn,
            "start_date": start_day.isoformat(),
            "end_date": end_day.isoformat(),
            "page": page,
            "perpage": 100,
        }

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = self.openapi.session.request(
                    endpoint.method,
                    self.openapi._get_url(endpoint.path),
                    params=params if endpoint.method == "GET" else None,
                    data=params if endpoint.method != "GET" else None,
                    timeout=self.timeout,
                )
                payload = response.json()
            except Exception as exc:
                last_error = exc
                if attempt + 1 >= self.max_retries:
                    break
                self._sleeper(self.retry_base_sec * (2**attempt))
                continue

            if not isinstance(payload, dict):
                raise GrowattReadError("A Growatt retornou um historico em formato invalido.")
            try:
                error_code = int(payload.get("error_code") or 0)
            except (TypeError, ValueError):
                error_code = -1
            if error_code == 0:
                return payload
            if error_code == 10012 and attempt + 1 < self.max_retries:
                self._sleeper(self.retry_base_sec * (2**attempt))
                continue
            message = str(payload.get("error_msg") or "erro nao detalhado")
            raise GrowattReadError(f"Growatt Open API: {message} (codigo {error_code}).")

        status = getattr(getattr(last_error, "response", None), "status_code", None)
        detail = f" HTTP {status}" if status else ""
        raise GrowattReadError(f"Falha de comunicacao com o historico Growatt.{detail}") from last_error

    def fetch_history(
        self,
        *,
        device_sn: str,
        device_type: str | int,
        start_day: date,
        end_day: date,
    ) -> dict[str, Any]:
        if end_day < start_day:
            raise ValueError("A data final deve ser igual ou posterior a data inicial.")
        if (end_day - start_day).days > 400:
            raise ValueError("O periodo Growatt nao pode exceder 400 dias por sincronizacao.")

        type_key = str(device_type or "1")
        endpoint = HISTORY_ENDPOINTS.get(type_key)
        if endpoint is None:
            raise GrowattReadError(
                f"O tipo de dispositivo Growatt {type_key} ainda nao possui endpoint historico mapeado."
            )

        all_rows: list[dict[str, Any]] = []
        chunks = 0
        pages = 0
        cursor = start_day
        datalogger_sn = ""
        while cursor <= end_day:
            chunk_end = min(end_day, cursor + timedelta(days=6))
            chunks += 1
            page = 1
            while True:
                payload = self._history_page(
                    endpoint=endpoint,
                    device_sn=str(device_sn),
                    start_day=cursor,
                    end_day=chunk_end,
                    page=page,
                )
                pages += 1
                data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                rows = _as_list(data.get("datas") or data.get("data") or data.get("records"))
                datalogger_sn = str(data.get("datalogger_sn") or datalogger_sn)
                all_rows.extend(rows)

                total = int(data.get("count") or len(rows))
                if not rows or len(rows) < 100 or page * 100 >= total:
                    break
                page += 1
                if page > 1000:
                    raise GrowattReadError("A paginacao Growatt excedeu o limite de seguranca.")
            cursor = chunk_end + timedelta(days=1)

        unique: dict[str, dict[str, Any]] = {}
        untimed: list[dict[str, Any]] = []
        for row in all_rows:
            timestamp = str(_first(row, "time", "createTime", "timestamp", default="")).strip()
            if timestamp:
                unique[timestamp] = row
            else:
                untimed.append(row)
        rows_out = sorted(unique.values(), key=lambda row: str(_first(row, "time", "createTime", "timestamp")))
        rows_out.extend(untimed)
        return {
            "rows": rows_out,
            "meta": {
                "device_sn": str(device_sn),
                "device_type": type_key,
                "datalogger_sn": datalogger_sn,
                "start_day": start_day.isoformat(),
                "end_day": end_day.isoformat(),
                "chunks": chunks,
                "pages": pages,
                "rows": len(rows_out),
                "timestamp_timezone": "UTC",
            },
        }

    def get_simple_snapshot(self) -> dict[str, Any]:
        plants = self.list_plants()
        if not plants:
            raise GrowattReadError("Nenhuma planta encontrada na conta Growatt.")
        plant = plants[0]
        devices = self.list_devices(plant["plant_id"])
        return {**plant, "devices": devices}


def discover_growatt_account(username: str, password: str) -> dict[str, Any]:
    client = GrowattClient(username, password)
    plants = client.list_plants()
    return {"client": client, "plants": plants}


def fetch_growatt_plant_data(
    username: str,
    password: str,
    *,
    debug: bool = False,
) -> Dict[str, Any]:
    """Compatibilidade com as rotas antigas de snapshot, sem expor token/login bruto."""
    client = GrowattClient(username=username, password=password)
    snapshot = client.get_simple_snapshot()
    if debug:
        snapshot["debug"] = {
            "login_base_url": client.login_base_url,
            "openapi_base_url": client.openapi_base_url,
            "authenticated": client.session is not None,
        }
    return snapshot
