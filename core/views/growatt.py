from __future__ import annotations

from datetime import date, timedelta

from django.conf import settings

from core.access import plants_accessible_to
from core.models import PlantMonitoringCredential, PVPlant
from core.services.dados_inversor.growatt_client import (
    GrowattAuthError,
    GrowattClient,
    GrowattReadError,
    fetch_growatt_plant_data,
)
from core.services.dados_inversor.growatt_ingest import sync_growatt_operational_data
from core.views._imports import *  # noqa: F401,F403


class GrowattConsoleView(LoginRequiredMixin, View):
    template_name = "inverters/growatt_console.html"

    def get_cred(self, plant: PVPlant) -> PlantMonitoringCredential | None:
        return PlantMonitoringCredential.objects.filter(plant=plant, provedor="GROWATT").first()

    def _ctx(self, plant: PVPlant, cred: PlantMonitoringCredential, result=None):
        today = date.today()
        return {
            "plant": plant,
            "cred": cred,
            "plants": cred.growatt_plants_cache or [],
            "devices": cred.growatt_devices_cache or [],
            "selected_plant_id": cred.growatt_plant_id,
            "selected_device_sn": cred.growatt_device_sn,
            "login_base_url": getattr(settings, "GROWATT_LOGIN_BASE_URL", ""),
            "openapi_base_url": getattr(settings, "GROWATT_OPENAPI_BASE_URL", ""),
            "default_start_day": (today - timedelta(days=1)).isoformat(),
            "default_end_day": today.isoformat(),
            "result": result,
        }

    def get(self, request, pk):
        plant = get_object_or_404(plants_accessible_to(request.user), pk=pk)
        cred = self.get_cred(plant)
        if not cred:
            messages.error(request, "Salve primeiro as credenciais Growatt para esta planta.")
            return redirect("plants:detail", pk=plant.pk)
        return render(request, self.template_name, self._ctx(plant, cred))

    def post(self, request, pk):
        plant = get_object_or_404(plants_accessible_to(request.user), pk=pk)
        cred = self.get_cred(plant)
        if not cred:
            messages.error(request, "Salve primeiro as credenciais Growatt para esta planta.")
            return redirect("plants:detail", pk=plant.pk)

        action = str(request.POST.get("action") or "").strip()
        username = str(request.POST.get("username") or cred.username or "").strip()
        password = str(request.POST.get("password") or "")
        if not password and request.POST.get("use_saved_password") == "on":
            password = cred.password
        if not username or not password:
            messages.error(request, "Informe usuario e senha, ou use a senha salva.")
            return render(request, self.template_name, self._ctx(plant, cred))

        if username != cred.username:
            cred.username = username
            cred.save(update_fields=["username", "updated_at"])

        try:
            client = GrowattClient(username, password)

            if action == "discover":
                plants = client.list_plants()
                selected_plant_id = str(
                    request.POST.get("plant_id")
                    or cred.growatt_plant_id
                    or (plants[0]["plant_id"] if plants else "")
                )
                devices = client.list_devices(selected_plant_id) if selected_plant_id else []
                cred.growatt_plants_cache = plants
                cred.growatt_devices_cache = devices
                cred.save(update_fields=["growatt_plants_cache", "growatt_devices_cache", "updated_at"])
                messages.success(
                    request,
                    f"Conta consultada: {len(plants)} planta(s) e {len(devices)} dispositivo(s) encontrados.",
                )
                ctx = self._ctx(plant, cred)
                ctx["selected_plant_id"] = selected_plant_id
                return render(request, self.template_name, ctx)

            if action == "bind":
                plant_id = str(request.POST.get("plant_id") or "").strip()
                device_sn = str(request.POST.get("device_sn") or "").strip()
                if not plant_id or not device_sn:
                    raise ValueError("Selecione a planta Growatt e o inversor.")
                devices = client.list_devices(plant_id)
                selected = next((row for row in devices if row.get("device_sn") == device_sn), None)
                if not selected:
                    raise ValueError("O dispositivo selecionado nao pertence a planta Growatt informada.")
                cred.growatt_plant_id = plant_id
                cred.growatt_device_sn = device_sn
                cred.growatt_device_type = str(selected.get("device_type") or "1")
                cred.growatt_datalogger_sn = str(selected.get("datalogger_sn") or "")
                cred.growatt_devices_cache = devices
                cred.save()
                messages.success(request, "Planta e inversor Growatt vinculados com sucesso.")
                return redirect("plants:growatt_console", pk=plant.pk)

            if action in {"fetch", "sync"}:
                start_day = date.fromisoformat(str(request.POST.get("start_day") or ""))
                end_day = date.fromisoformat(str(request.POST.get("end_day") or ""))
                if not cred.growatt_device_sn:
                    raise ValueError("Execute a descoberta e vincule um inversor antes de adquirir dados.")

                if action == "fetch":
                    result = client.fetch_history(
                        device_sn=cred.growatt_device_sn,
                        device_type=cred.growatt_device_type or "1",
                        start_day=start_day,
                        end_day=end_day,
                    )
                    result["preview_rows"] = result.get("rows", [])[:200]
                    messages.success(request, f"Previa carregada: {len(result.get('rows', []))} amostras.")
                else:
                    result = sync_growatt_operational_data(
                        plant=plant,
                        cred=cred,
                        username=username,
                        password=password,
                        start_day=start_day,
                        end_day=end_day,
                        client=client,
                    )
                    messages.success(
                        request,
                        f"Sincronizacao concluida: {result['inserted']} novos e {result['updated']} atualizados.",
                    )
                return render(request, self.template_name, self._ctx(plant, cred, result=result))

            raise ValueError("Acao Growatt invalida.")
        except (ValueError, GrowattAuthError, GrowattReadError) as exc:
            messages.error(request, f"Falha na aquisicao Growatt: {exc}")
            return render(request, self.template_name, self._ctx(plant, cred))
        except Exception:
            logger.exception("Falha inesperada na console Growatt da planta %s", plant.pk)
            messages.error(request, "Falha inesperada na aquisicao Growatt. Consulte os logs do aplicativo.")
            return render(request, self.template_name, self._ctx(plant, cred))


class PlantGrowattDebugView(LoginRequiredMixin, View):
    def get(self, request, pk):
        plant = get_object_or_404(plants_accessible_to(request.user), pk=pk)
        cred = plant.credentials.filter(provedor="GROWATT").first()
        if not cred:
            messages.error(request, "Nenhuma credencial Growatt cadastrada para esta planta.")
            return redirect("plants:detail", pk=plant.pk)
        try:
            data = fetch_growatt_plant_data(cred.username, cred.password, debug=True)
        except (GrowattAuthError, GrowattReadError) as exc:
            return JsonResponse({"error": str(exc)}, status=502)
        return JsonResponse(data, json_dumps_params={"ensure_ascii": False, "indent": 2})


class PlantGrowattDailyJsonView(LoginRequiredMixin, View):
    def get(self, request, pk):
        plant = get_object_or_404(plants_accessible_to(request.user), pk=pk)
        cred = plant.credentials.filter(provedor="GROWATT").first()
        if not cred:
            return JsonResponse({"error": "Nenhuma credencial Growatt cadastrada."}, status=400)
        try:
            data = fetch_growatt_plant_data(cred.username, cred.password)
        except GrowattAuthError as exc:
            return JsonResponse({"error": str(exc)}, status=401)
        except GrowattReadError as exc:
            return JsonResponse({"error": str(exc)}, status=502)
        return JsonResponse(data, json_dumps_params={"ensure_ascii": False})
