#core/views/meteo
from __future__ import annotations
from core.views._imports import *
from datetime import datetime, timedelta, timezone as dt_timezone
from core.services.dados_satelite.openmeteo import ingest_openmeteo_range
from core.services.dados_satelite.csv_import import ingest_user_meteo_csv
from django.utils.timezone import make_aware
from core.services.coverage import compute_time_coverage
from zoneinfo import ZoneInfo
from django.db.models import Count, Max, Min
from django.urls import reverse
# Forms
from core.forms import (
    MeteoCSVUploadForm,
    MeteoRequestForm,

)

# Models
from core.models import (
    MeteoRecord,
    MeteoSource,

)

#---------------------------
#---------------------------  M E T E O
#---------------------------

UTC = ZoneInfo("UTC")


def _safe_zoneinfo(tzname: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tzname) if tzname else ZoneInfo("UTC")
    except Exception:
        return ZoneInfo("UTC")


def _local_dates_to_utc_range(*, plant_tz: str | None, start_date, end_date):
    """
    Converte datas (date) em intervalo UTC semiaberto:
      [start_local 00:00, (end_date+1) 00:00) em tz local
      -> retorna (start_utc, end_utc_exclusive, tz_local)
    Isso evita off-by-one e funciona bem com __gte / __lt e com grades (15min/5min).
    """
    tz_local = _safe_zoneinfo(plant_tz)

    start_local = make_aware(datetime.combine(start_date, time.min), timezone=tz_local)

    # end EXCLUSIVO: começo do dia seguinte
    end_local_excl = make_aware(
        datetime.combine(end_date + timedelta(days=1), time.min),
        timezone=tz_local,
    )

    return start_local.astimezone(dt_timezone.utc), end_local_excl.astimezone(dt_timezone.utc), tz_local


def _align_utc_range_to_interval(*, start_utc, end_utc, interval_min: int):
    """
    Opcional: alinha start/end para a grade do intervalo.
    - start: floor
    - end: ceil (mantém end exclusivo)
    """
    import pandas as pd

    freq = f"{int(interval_min)}min"
    s = pd.Timestamp(start_utc).floor(freq)
    e = pd.Timestamp(end_utc).ceil(freq)

    # garante tz
    if s.tzinfo is None:
        s = s.tz_localize("UTC")
    else:
        s = s.tz_convert("UTC")

    if e.tzinfo is None:
        e = e.tz_localize("UTC")
    else:
        e = e.tz_convert("UTC")

    return s.to_pydatetime(), e.to_pydatetime()


@require_http_methods(["GET", "POST"])
@login_required
def _open_meteo_view_legacy(request):
    """
    Mantive o nome da view/URL para não quebrar a rota.
    Opera com Open-Meteo (ingest).
    """
    form = MeteoRequestForm(request.POST or None, user=request.user)

    if request.method == "POST" and form.is_valid():
        plant = form.cleaned_data["plant"]
        start_date = form.cleaned_data["start_date"]
        end_date = form.cleaned_data["end_date"]
        include_gti = form.cleaned_data["include_gti"]
        model = (form.cleaned_data.get("model") or "").strip() or None

        try:
            count, meta = ingest_openmeteo_range(
                plant=plant,
                start_date=start_date,
                end_date=end_date,
                include_gti=include_gti,
                model=model,
            )
            model_label = model or "best_match"
            messages.success(
                request,
                (
                    f"Open-Meteo: {count} registros ingeridos/atualizados. "
                    f"Modelo: {model_label}. "
                    f"Intervalo: {start_date} a {end_date}."
                )
            )
        except Exception as e:
            messages.error(request, f"Falha ao ingerir dados meteorológicos da Open-Meteo: {e}")

    return render(request, "meteo/open_meteo_request.html", {"form": form})

@require_http_methods(["GET", "POST"])
@login_required
def open_meteo_view(request):
    visible_plants = list(MeteoRequestForm(user=request.user).fields["plant"].queryset)
    selected_id = request.POST.get("plant") if request.method == "POST" else request.GET.get("plant")
    selected_plant = next((plant for plant in visible_plants if str(plant.pk) == str(selected_id)), None)
    if selected_plant is None and visible_plants:
        selected_plant = visible_plants[0]

    today = date.today()
    initial = {
        "plant": selected_plant,
        "start_date": today - timedelta(days=30),
        "end_date": today,
        "interval_min": "60",
        "include_gti": True,
        "model": "",
    }
    action = (request.POST.get("action") or "import").strip().lower() if request.method == "POST" else ""
    if request.method == "POST" and action != "upload_csv":
        form = MeteoRequestForm(request.POST, user=request.user)
    elif request.GET.get("plant"):
        query_data = request.GET.copy()
        query_data.setdefault("start_date", initial["start_date"].isoformat())
        query_data.setdefault("end_date", initial["end_date"].isoformat())
        query_data.setdefault("interval_min", "60")
        form = MeteoRequestForm(query_data, user=request.user)
    else:
        form = MeteoRequestForm(initial=initial, user=request.user)

    csv_initial = {"plant": selected_plant, "interval_min": "15", "timestamp_timezone": "UTC"}
    csv_form = (
        MeteoCSVUploadForm(request.POST, request.FILES, user=request.user)
        if request.method == "POST" and action == "upload_csv"
        else MeteoCSVUploadForm(initial=csv_initial, user=request.user)
    )

    if request.method == "POST" and action == "upload_csv":
        if csv_form.is_valid():
            plant = csv_form.cleaned_data["plant"]
            column_map = {
                "ghi": csv_form.cleaned_data.get("ghi_col") or "",
                "dni": csv_form.cleaned_data.get("dni_col") or "",
                "dhi": csv_form.cleaned_data.get("dhi_col") or "",
                "gti": csv_form.cleaned_data.get("gti_col") or "",
                "temp_air": csv_form.cleaned_data.get("temp_air_col") or "",
                "wind_speed": csv_form.cleaned_data.get("wind_speed_col") or "",
                "rh": csv_form.cleaned_data.get("rh_col") or "",
                "pressure": csv_form.cleaned_data.get("pressure_col") or "",
            }
            try:
                result = ingest_user_meteo_csv(
                    plant=plant,
                    uploaded_file=csv_form.cleaned_data["arquivo"],
                    interval_min=int(csv_form.cleaned_data["interval_min"]),
                    delimiter=csv_form.cleaned_data["delimiter"],
                    decimal_separator=csv_form.cleaned_data["decimal_separator"],
                    timestamp_col=csv_form.cleaned_data["timestamp_col"],
                    timestamp_timezone=csv_form.cleaned_data["timestamp_timezone"],
                    dayfirst=bool(csv_form.cleaned_data.get("dayfirst")),
                    dataset_model=csv_form.cleaned_data.get("dataset_model") or "USER_CSV",
                    data_typology=csv_form.cleaned_data.get("data_typology"),
                    column_map=column_map,
                    update_existing=bool(csv_form.cleaned_data.get("update_existing")),
                )
                messages.success(
                    request,
                    (
                        f"CSV meteorologico importado: {result.rows_imported} registros "
                        f"({result.rows_skipped} linhas ignoradas), fonte USER_CSV."
                    ),
                )
                return redirect(
                    f"{reverse('open_meteo_view')}?plant={plant.pk}"
                    f"&start_date={initial['start_date']}&end_date={initial['end_date']}"
                    f"&interval_min={csv_form.cleaned_data['interval_min']}"
                )
            except Exception as exc:
                messages.error(request, f"Falha ao importar CSV meteorologico: {exc}")

    if request.method == "POST" and form.is_valid():
        plant = form.cleaned_data["plant"]
        start_date = form.cleaned_data["start_date"]
        end_date = form.cleaned_data["end_date"]
        interval_min = form.cleaned_data["interval_min"]

        if action == "delete_range":
            start_utc, end_utc, _ = _local_dates_to_utc_range(
                plant_tz=getattr(plant, "timezone", None),
                start_date=start_date,
                end_date=end_date,
            )
            deleted, _ = MeteoRecord.objects.filter(
                plant=plant,
                source=MeteoSource.OPENMETEO,
                ts_utc__gte=start_utc,
                ts_utc__lt=end_utc,
            ).delete()
            messages.success(request, f"Foram excluídos {deleted} registros meteorológicos do período selecionado.")
            return redirect(f"{reverse('open_meteo_view')}?plant={plant.pk}&start_date={start_date}&end_date={end_date}&interval_min={interval_min}")

        include_gti = form.cleaned_data["include_gti"]
        model = (form.cleaned_data.get("model") or "").strip() or None
        try:
            count, _meta = ingest_openmeteo_range(
                plant=plant,
                start_date=start_date,
                end_date=end_date,
                include_gti=include_gti,
                model=model,
            )
            messages.success(
                request,
                f"Open-Meteo: {count} registros importados ou atualizados. Modelo: {model or 'melhor correspondência'}.",
            )
            return redirect(f"{reverse('open_meteo_view')}?plant={plant.pk}&start_date={start_date}&end_date={end_date}&interval_min={interval_min}")
        except Exception as exc:
            messages.error(request, f"Falha ao importar dados meteorológicos da Open-Meteo: {exc}")

    meteo_summary = None
    coverage = None
    missing_ranges_local = []
    if form.is_bound and form.is_valid():
        selected_plant = form.cleaned_data["plant"]

    if selected_plant is not None:
        qs = MeteoRecord.objects.filter(plant=selected_plant, source=MeteoSource.OPENMETEO)
        stats = qs.aggregate(total=Count("id"), first_ts=Min("ts_utc"), last_ts=Max("ts_utc"))
        meteo_summary = {
            **stats,
            "plant": selected_plant,
            "models": list(qs.exclude(dataset_model="").values_list("dataset_model", flat=True).distinct().order_by("dataset_model")),
            "intervals": list(qs.values_list("interval_min", flat=True).distinct().order_by("interval_min")),
        }
        if form.is_bound and form.is_valid():
            start_utc, end_utc, tz_local = _local_dates_to_utc_range(
                plant_tz=getattr(selected_plant, "timezone", None),
                start_date=form.cleaned_data["start_date"],
                end_date=form.cleaned_data["end_date"],
            )
            coverage = compute_time_coverage(
                queryset=qs,
                start_utc=start_utc,
                end_utc=end_utc,
                interval_min=int(form.cleaned_data["interval_min"]),
            )
            missing_ranges_local = [
                (a.astimezone(tz_local), b.astimezone(tz_local))
                for a, b in coverage.missing_ranges_utc[:50]
            ]

    return render(request, "meteo/open_meteo_request.html", {
        "form": form,
        "meteo_summary": meteo_summary,
        "coverage": coverage,
        "missing_ranges_local": missing_ranges_local,
        "csv_form": csv_form,
    })


@require_GET
@login_required
def open_meteo_view_api_json(request):
    """
    Endpoint reutilizável para cobertura/consistência no banco.
    GET params via MeteoRequestForm: plant, start_date, end_date, interval_min
    """
    form = MeteoRequestForm(request.GET, user=request.user)
    if not form.is_valid():
        return JsonResponse({"ok": False, "errors": form.errors}, status=400)

    plant = form.cleaned_data["plant"]
    start_date = form.cleaned_data["start_date"]
    end_date = form.cleaned_data["end_date"]
    interval_min = int(form.cleaned_data["interval_min"])

    start_utc, end_utc, tz_local = _local_dates_to_utc_range(
        plant_tz=getattr(plant, "timezone", None),
        start_date=start_date,
        end_date=end_date,
    )

    # Opcional, mas ajuda muito a bater contagens com “expected_count”
    start_utc, end_utc = _align_utc_range_to_interval(
        start_utc=start_utc, end_utc=end_utc, interval_min=interval_min
    )

    # >>> CORREÇÃO PRINCIPAL AQUI: source do Open-Meteo <<<
    qs = MeteoRecord.objects.filter(plant=plant, source=MeteoSource.OPENMETEO)

    cov = compute_time_coverage(
        queryset=qs,
        start_utc=start_utc,
        end_utc=end_utc,
        interval_min=interval_min,
    )

    ranges_local = [
        {"start": a.astimezone(tz_local).isoformat(), "end": b.astimezone(tz_local).isoformat()}
        for (a, b) in cov.missing_ranges_utc[:50]
    ]

    return JsonResponse({
        "ok": True,
        "plant_id": plant.id,
        "plant_tz": str(tz_local),
        "interval_min": cov.interval_min,
        "start_utc": cov.start_utc.isoformat(),
        "end_utc": cov.end_utc.isoformat(),
        "expected_count": cov.expected_count,
        "existing_count": cov.existing_count,
        "missing_count": cov.missing_count,
        "coverage_pct": round(cov.coverage_pct, 2),
        "missing_ranges_local": ranges_local,
        "missing_ranges_truncated": len(cov.missing_ranges_utc) > 50,
    })

