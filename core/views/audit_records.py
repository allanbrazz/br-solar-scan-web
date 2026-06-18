from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, time, timezone as dt_timezone
from typing import Any

from django import forms
from django.core.paginator import Paginator
from django.db import IntegrityError, models, transaction
from django.db.models import Q
from django.utils.dateparse import parse_date

from core.access import plants_accessible_to
from core.models import InverterOperationalData, MeteoRecord, PVPlantMergedRecord15m
from core.views._imports import *


UTC = dt_timezone.utc


def _utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_dt_local_input(value: datetime | None) -> str:
    value = _utc_datetime(value)
    if value is None:
        return ""
    return value.strftime("%Y-%m-%dT%H:%M:%S")


class BaseAuditModelForm(forms.ModelForm):
    timestamp_field = "ts_utc"

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if "plant" in self.fields:
            self.fields["plant"].queryset = plants_accessible_to(user).order_by("nome")

        for name, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "form-check-input")
            else:
                widget.attrs.setdefault("class", "form-control")

            if isinstance(widget, forms.Textarea):
                widget.attrs.setdefault("rows", "7")
                widget.attrs.setdefault("spellcheck", "false")

        ts_field = self.fields.get(self.timestamp_field)
        if ts_field is not None:
            ts_field.input_formats = [
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M",
            ]
            ts_field.help_text = "Informe em UTC."
            current_value = getattr(self.instance, self.timestamp_field, None)
            if current_value:
                self.initial[self.timestamp_field] = _format_dt_local_input(current_value)

    def clean(self):
        cleaned = super().clean()
        value = cleaned.get(self.timestamp_field)
        if value is not None:
            cleaned[self.timestamp_field] = _utc_datetime(value)
        return cleaned


class AuditMeteoRecordForm(BaseAuditModelForm):
    class Meta:
        model = MeteoRecord
        fields = [
            "plant",
            "source",
            "source_endpoint",
            "dataset_model",
            "data_typology",
            "ts_utc",
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
        ]
        widgets = {
            "ts_utc": forms.DateTimeInput(attrs={"type": "datetime-local", "step": "1"}, format="%Y-%m-%dT%H:%M:%S"),
            "source_endpoint": forms.TextInput(attrs={"placeholder": "Opcional"}),
            "dataset_model": forms.TextInput(attrs={"placeholder": "Ex.: best_match, USER_CSV"}),
        }


class AuditInverterOperationalDataForm(BaseAuditModelForm):
    payload = forms.JSONField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 10, "spellcheck": "false"}),
        help_text="JSON bruto da leitura.",
    )

    class Meta:
        model = InverterOperationalData
        fields = [
            "plant",
            "provedor",
            "pn",
            "devcode",
            "devaddr",
            "sn",
            "ts_utc",
            "payload",
        ]
        widgets = {
            "ts_utc": forms.DateTimeInput(attrs={"type": "datetime-local", "step": "1"}, format="%Y-%m-%dT%H:%M:%S"),
        }


class AuditMergedRecordForm(BaseAuditModelForm):
    interval_min = forms.ChoiceField(
        label="Intervalo (min)",
        choices=[("15", "15 minutos")],
        initial="15",
        widget=forms.Select(attrs={"class": "form-control"}),
    )

    class Meta:
        model = PVPlantMergedRecord15m
        fields = [
            "plant",
            "source_oper",
            "source_meteo",
            "ts_utc",
            "interval_min",
            "p_dc_w",
            "p_ac_w",
            "v_dc_v",
            "i_dc_a",
            "v_ac_v",
            "i_ac_a",
            "freq_hz",
            "mppt1_vdc_v",
            "mppt2_vdc_v",
            "mppt3_vdc_v",
            "mppt4_vdc_v",
            "mppt1_idc_a",
            "mppt2_idc_a",
            "mppt3_idc_a",
            "mppt4_idc_a",
            "alarm_code",
            "alarm_sev",
            "e_ac_wh_15",
            "inv_n",
            "inv_coverage",
            "flag_low_coverage",
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
            "flag_meteo_missing",
            "flag_inv_missing",
        ]
        widgets = {
            "ts_utc": forms.DateTimeInput(attrs={"type": "datetime-local", "step": "1"}, format="%Y-%m-%dT%H:%M:%S"),
        }

    def clean_interval_min(self):
        return 15


@dataclass(frozen=True)
class AuditConfig:
    key: str
    label: str
    singular_label: str
    model: type[models.Model]
    form_class: type[BaseAuditModelForm]
    timestamp_field: str
    display_fields: tuple[str, ...]
    detail_fields: tuple[str, ...]
    search_fields: tuple[str, ...]
    source_filter_fields: tuple[str, ...]
    export_fields: tuple[str, ...]


METEO_FIELDS = (
    "id",
    "plant_id",
    "plant",
    "source",
    "source_endpoint",
    "dataset_model",
    "data_typology",
    "ts_utc",
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
    "created_at",
)

INVERTER_FIELDS = (
    "id",
    "plant_id",
    "plant",
    "provedor",
    "pn",
    "devcode",
    "devaddr",
    "sn",
    "ts_utc",
    "payload",
    "created_at",
    "updated_at",
)

MERGED_FIELDS = (
    "id",
    "plant_id",
    "plant",
    "source_oper",
    "source_meteo",
    "ts_utc",
    "interval_min",
    "p_dc_w",
    "p_ac_w",
    "v_dc_v",
    "i_dc_a",
    "v_ac_v",
    "i_ac_a",
    "freq_hz",
    "mppt1_vdc_v",
    "mppt2_vdc_v",
    "mppt3_vdc_v",
    "mppt4_vdc_v",
    "mppt1_idc_a",
    "mppt2_idc_a",
    "mppt3_idc_a",
    "mppt4_idc_a",
    "alarm_code",
    "alarm_sev",
    "e_ac_wh_15",
    "inv_n",
    "inv_coverage",
    "flag_low_coverage",
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
    "flag_meteo_missing",
    "flag_inv_missing",
    "created_at",
)

AUDIT_CONFIGS: dict[str, AuditConfig] = {
    "meteo": AuditConfig(
        key="meteo",
        label="Meteorologia",
        singular_label="registro meteorologico",
        model=MeteoRecord,
        form_class=AuditMeteoRecordForm,
        timestamp_field="ts_utc",
        display_fields=("ts_utc", "plant", "source", "interval_min", "ghi", "gti", "temp_air", "meteo_qc_score"),
        detail_fields=METEO_FIELDS,
        search_fields=("source", "dataset_model", "source_endpoint", "plant__nome"),
        source_filter_fields=("source",),
        export_fields=METEO_FIELDS,
    ),
    "inverter": AuditConfig(
        key="inverter",
        label="Inversor",
        singular_label="registro do inversor",
        model=InverterOperationalData,
        form_class=AuditInverterOperationalDataForm,
        timestamp_field="ts_utc",
        display_fields=("ts_utc", "plant", "provedor", "pn", "devcode", "sn", "payload"),
        detail_fields=INVERTER_FIELDS,
        search_fields=("provedor", "pn", "devcode", "sn", "plant__nome"),
        source_filter_fields=("provedor",),
        export_fields=INVERTER_FIELDS,
    ),
    "merged": AuditConfig(
        key="merged",
        label="Merge 15 min",
        singular_label="registro de merge",
        model=PVPlantMergedRecord15m,
        form_class=AuditMergedRecordForm,
        timestamp_field="ts_utc",
        display_fields=("ts_utc", "plant", "source_oper", "source_meteo", "p_ac_w", "gti", "flag_inv_missing", "flag_meteo_missing"),
        detail_fields=MERGED_FIELDS,
        search_fields=("source_oper", "source_meteo", "plant__nome"),
        source_filter_fields=("source_oper", "source_meteo"),
        export_fields=MERGED_FIELDS,
    ),
}


def _get_config(dataset: str | None) -> AuditConfig:
    return AUDIT_CONFIGS.get((dataset or "").strip().lower()) or AUDIT_CONFIGS["meteo"]


def _field_label(model: type[models.Model], field_name: str) -> str:
    if field_name == "id":
        return "ID"
    if field_name == "plant_id":
        return "ID da planta"
    if field_name == "plant":
        return "Planta"
    try:
        return str(model._meta.get_field(field_name).verbose_name).capitalize()
    except Exception:
        return field_name.replace("_", " ").capitalize()


def _value_for_field(obj: models.Model, field_name: str) -> Any:
    if field_name == "plant_id":
        return getattr(obj, "plant_id", "")
    if field_name == "plant":
        return getattr(getattr(obj, "plant", None), "nome", "")
    return getattr(obj, field_name, "")


def _stringify(value: Any, *, short: bool = False) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return _utc_datetime(value).strftime("%Y-%m-%d %H:%M:%S UTC")
    if isinstance(value, bool):
        return "Sim" if value else "Nao"
    if isinstance(value, (dict, list)):
        if short:
            if isinstance(value, dict):
                return f"JSON ({len(value)} chaves)"
            return f"Lista ({len(value)} itens)"
        return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    return str(value)


def _base_queryset(config: AuditConfig, user) -> models.QuerySet:
    return config.model.objects.filter(plant__in=plants_accessible_to(user))


def _filter_queryset(config: AuditConfig, user, params) -> tuple[models.QuerySet, dict[str, str]]:
    qs = _base_queryset(config, user).select_related("plant")
    filters = {
        "plant": (params.get("plant") or "").strip(),
        "start_date": (params.get("start_date") or "").strip(),
        "end_date": (params.get("end_date") or "").strip(),
        "q": (params.get("q") or "").strip(),
    }
    for field in config.source_filter_fields:
        filters[field] = (params.get(field) or "").strip()

    if filters["plant"]:
        qs = qs.filter(plant_id=filters["plant"])

    start_d = parse_date(filters["start_date"]) if filters["start_date"] else None
    end_d = parse_date(filters["end_date"]) if filters["end_date"] else None
    ts_field = config.timestamp_field
    if start_d:
        qs = qs.filter(**{f"{ts_field}__gte": datetime.combine(start_d, time.min, tzinfo=UTC)})
    if end_d:
        qs = qs.filter(**{f"{ts_field}__lt": datetime.combine(end_d, time.max, tzinfo=UTC)})

    for field in config.source_filter_fields:
        if filters[field]:
            qs = qs.filter(**{field: filters[field]})

    if filters["q"]:
        query = Q()
        for field in config.search_fields:
            query |= Q(**{f"{field}__icontains": filters["q"]})
        qs = qs.filter(query)

    return qs.order_by(f"-{config.timestamp_field}", "-id"), filters


def _source_options(config: AuditConfig, user) -> dict[str, list[str]]:
    base = _base_queryset(config, user)
    options: dict[str, list[str]] = {}
    for field in config.source_filter_fields:
        options[field] = [
            str(v)
            for v in base.exclude(**{field: ""})
            .values_list(field, flat=True)
            .distinct()
            .order_by(field)[:100]
        ]
    return options


def _csv_response(config: AuditConfig, qs: models.QuerySet) -> HttpResponse:
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="auditoria_{config.key}.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow([_field_label(config.model, field) for field in config.export_fields])
    for obj in qs.iterator(chunk_size=1000):
        writer.writerow([_stringify(_value_for_field(obj, field), short=False) for field in config.export_fields])
    return response


def _build_rows(config: AuditConfig, objects: list[models.Model]) -> list[dict[str, Any]]:
    rows = []
    for obj in objects:
        rows.append({
            "obj": obj,
            "display_values": [
                {
                    "field": field,
                    "label": _field_label(config.model, field),
                    "value": _stringify(_value_for_field(obj, field), short=True),
                    "is_json": isinstance(_value_for_field(obj, field), (dict, list)),
                    "full_value": _stringify(_value_for_field(obj, field), short=False),
                }
                for field in config.display_fields
            ],
            "detail_values": [
                {
                    "field": field,
                    "label": _field_label(config.model, field),
                    "value": _stringify(_value_for_field(obj, field), short=False),
                }
                for field in config.detail_fields
            ],
        })
    return rows


@login_required
@require_http_methods(["GET", "POST"])
def audit_records_view(request: HttpRequest) -> HttpResponse:
    config = _get_config(request.GET.get("dataset"))
    qs, filters = _filter_queryset(config, request.user, request.GET)

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        selected_ids = [pk for pk in request.POST.getlist("selected") if str(pk).isdigit()]
        selected_qs = qs.filter(pk__in=selected_ids)
        if not selected_ids:
            messages.warning(request, "Selecione pelo menos um registro.")
            return redirect(f"{reverse('audit_records')}?{request.GET.urlencode()}")

        if action == "delete_selected":
            deleted, _ = selected_qs.delete()
            messages.success(request, f"{deleted} registro(s) excluido(s).")
            return redirect(f"{reverse('audit_records')}?{request.GET.urlencode()}")

        if action == "export_selected":
            return _csv_response(config, selected_qs.order_by(f"-{config.timestamp_field}", "-id"))

    if request.GET.get("action") == "export":
        return _csv_response(config, qs)

    try:
        page_size = int(request.GET.get("page_size") or 50)
    except (TypeError, ValueError):
        page_size = 50
    page_size = max(20, min(page_size, 500))

    paginator = Paginator(qs, page_size)
    page_obj = paginator.get_page(request.GET.get("page") or 1)

    export_params = request.GET.copy()
    export_params["dataset"] = config.key
    export_params["action"] = "export"
    export_params.pop("page", None)
    page_params = request.GET.copy()
    page_params["dataset"] = config.key
    page_params.pop("page", None)
    page_params.pop("action", None)

    dataset_tabs = []
    for key, item in AUDIT_CONFIGS.items():
        tab_params = request.GET.copy()
        tab_params["dataset"] = key
        tab_params.pop("page", None)
        tab_params.pop("action", None)
        dataset_tabs.append({
            "key": key,
            "label": item.label,
            "url": f"{reverse('audit_records')}?{tab_params.urlencode()}",
            "active": key == config.key,
        })

    source_options = _source_options(config, request.user)
    context = {
        "config": config,
        "dataset_tabs": dataset_tabs,
        "plants": plants_accessible_to(request.user).order_by("nome"),
        "filters": filters,
        "source_filters": [
            {
                "field": field,
                "label": _field_label(config.model, field),
                "value": filters.get(field, ""),
                "options": source_options.get(field, []),
            }
            for field in config.source_filter_fields
        ],
        "display_headers": [_field_label(config.model, field) for field in config.display_fields],
        "page_obj": page_obj,
        "rows": _build_rows(config, list(page_obj.object_list)),
        "page_size": page_size,
        "filtered_count": paginator.count,
        "export_url": f"{reverse('audit_records')}?{export_params.urlencode()}",
        "pagination_query": page_params.urlencode(),
    }
    return render(request, "audit/records.html", context)


@login_required
@require_http_methods(["GET", "POST"])
def audit_record_form_view(request: HttpRequest, dataset: str, pk: int | None = None) -> HttpResponse:
    config = _get_config(dataset)
    obj = None
    if pk is not None:
        obj = get_object_or_404(_base_queryset(config, request.user), pk=pk)

    form_class = config.form_class
    if request.method == "POST" and request.POST.get("action") == "delete" and obj is not None:
        obj.delete()
        messages.success(request, "Registro excluido.")
        return redirect(f"{reverse('audit_records')}?dataset={config.key}")

    form = form_class(request.POST or None, instance=obj, user=request.user)
    if request.method == "POST" and request.POST.get("action") != "delete":
        if form.is_valid():
            try:
                with transaction.atomic():
                    saved = form.save()
                messages.success(request, "Registro salvo com sucesso.")
                return redirect(f"{reverse('audit_records')}?dataset={config.key}&plant={saved.plant_id}")
            except IntegrityError:
                form.add_error(None, "Ja existe um registro com a mesma chave unica. Ajuste a fonte, dispositivo ou timestamp.")

    return render(request, "audit/form.html", {
        "config": config,
        "form": form,
        "obj": obj,
        "is_create": obj is None,
    })
