#core/views/modulos
from __future__ import annotations
from core.views._imports import *

# Forms
from core.forms import (
    PVModuleForm, CSVUploadForm, VillalvaModuleForm,
)

from core.services.pvmodule.villalva import (
    VillalvaError,
    VillalvaInput,
    extract_villalva_parameters,
    result_iv_curve,
)

# Models
from core.models import (
    PVModule,
)

#---------------------------
#---------------------------  MÓDULOS
#---------------------------

class ModuleListView(LoginRequiredMixin, ListView):
    model = PVModule
    paginate_by = 20
    template_name = "pvmodules/list.html"
    context_object_name = "modulos"

    def get_queryset(self):
        qs = super().get_queryset()
        q = self.request.GET.get("q", "").strip()
        fab = self.request.GET.get("fab", "").strip()
        sort = self.request.GET.get("sort", "").strip()
        order = self.request.GET.get("order", "asc")

        if q:
            qs = qs.filter(Q(nome__icontains=q) | Q(fabricante__icontains=q))
        if fab:
            qs = qs.filter(fabricante__iexact=fab)

        allowed = {
            "fabricante": "fabricante",
            "nome": "nome",
            "pmp": "pmp_w",
            "vmp": "vmp_v",
            "imp": "imp_a",
            "voc": "voc_v",
            "isc": "isc_a",
            "eficiencia": "eficiencia_pct",
            "celulas": "num_celulas",
            "rs": "rs_ohm",
            "rp": "rp_ohm",
            "a": "diode_a",
        }
        if sort in allowed:
            key = allowed[sort]
            if order == "desc":
                key = f"-{key}"
            qs = qs.order_by(key)

        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["q"] = self.request.GET.get("q", "").strip()
        ctx["fab"] = self.request.GET.get("fab", "").strip()
        ctx["order"] = self.request.GET.get("order", "asc")
        ctx["sort"] = self.request.GET.get("sort", "")
        ctx["fabricantes"] = (
            PVModule.objects.values_list("fabricante", flat=True).distinct().order_by("fabricante")
        )
        return ctx
    
class ModuleDetailView(LoginRequiredMixin, DetailView):
    model = PVModule
    template_name = "pvmodules/detail.html"
    context_object_name = "m"

class ModuleCreateView(LoginRequiredMixin, CreateView):
    model = PVModule
    form_class = PVModuleForm
    template_name = "pvmodules/form.html"

    def get_success_url(self):
        messages.success(self.request, "Módulo criado com sucesso.")
        return reverse("pvmodules:list")

class ModuleUpdateView(LoginRequiredMixin, UpdateView):
    model = PVModule
    form_class = PVModuleForm
    template_name = "pvmodules/form.html"

    def get_success_url(self):
        messages.success(self.request, "Módulo atualizado com sucesso.")
        return reverse("pvmodules:detail", args=[self.object.pk])

def _quantize_decimal(value: object, pattern: str) -> Decimal:
    return Decimal(str(value)).quantize(Decimal(pattern))


def _villalva_input_from_cleaned(cleaned: dict[str, Any]) -> VillalvaInput:
    return VillalvaInput(
        isc_a=float(cleaned["isc_a"]),
        voc_v=float(cleaned["voc_v"]),
        vmp_v=float(cleaned["vmp_v"]),
        imp_a=float(cleaned["imp_a"]),
        cells_in_series=int(cleaned["num_celulas"]),
        temp_coeff_voc_pct_c=float(cleaned["temp_coeff_voc_pct_c"]),
        temp_coeff_isc_pct_c=float(cleaned["temp_coeff_isc_pct_c"]),
    )


def _villalva_module_defaults(cleaned: dict[str, Any], result) -> dict[str, Any]:
    best = result.best
    eficiencia = cleaned.get("eficiencia_pct")
    if eficiencia in (None, ""):
        eficiencia = Decimal("0")
    return {
        "pmp_w": _quantize_decimal(cleaned["pmp_w"], "0.01"),
        "vmp_v": _quantize_decimal(cleaned["vmp_v"], "0.001"),
        "imp_a": _quantize_decimal(cleaned["imp_a"], "0.001"),
        "voc_v": _quantize_decimal(cleaned["voc_v"], "0.001"),
        "isc_a": _quantize_decimal(cleaned["isc_a"], "0.001"),
        "eficiencia_pct": _quantize_decimal(eficiencia, "0.01"),
        "power_tolerance": (cleaned.get("power_tolerance") or "").strip(),
        "num_celulas": int(cleaned["num_celulas"]),
        "temp_coeff_voc_pct_c": _quantize_decimal(cleaned["temp_coeff_voc_pct_c"], "0.001"),
        "temp_coeff_isc_pct_c": _quantize_decimal(cleaned["temp_coeff_isc_pct_c"], "0.001"),
        "rs_ohm": _quantize_decimal(best.rs_ohm, "0.0001"),
        "rp_ohm": _quantize_decimal(best.rp_ohm, "0.001"),
        "diode_a": _quantize_decimal(best.diode_a, "0.001"),
    }


class ModuleVillalvaEstimateView(LoginRequiredMixin, View):
    template_name = "pvmodules/villalva.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        return render(request, self.template_name, {"form": VillalvaModuleForm()})

    def post(self, request: HttpRequest) -> HttpResponse:
        form = VillalvaModuleForm(request.POST)
        context: dict[str, Any] = {"form": form}
        if not form.is_valid():
            return render(request, self.template_name, context)

        try:
            result = self._calculate(form.cleaned_data)
        except VillalvaError as exc:
            form.add_error(None, str(exc))
            return render(request, self.template_name, context)

        data = _villalva_input_from_cleaned(form.cleaned_data)
        context.update(
            {
                "result": result,
                "curve": result_iv_curve(result.best, data, points=28),
                "method_warnings": self._warnings(form.cleaned_data, result),
            }
        )

        if request.POST.get("action") == "save":
            saved = self._save_module(form, result)
            if saved is not None:
                obj, created = saved
                action = "criado" if created else "atualizado"
                messages.success(
                    request,
                    (
                        f"Modulo {action} com parametros de Villalva: "
                        f"Rs={obj.rs_ohm} ohm, Rp={obj.rp_ohm} ohm, a={obj.diode_a}."
                    ),
                )
                return redirect("pvmodules:detail", pk=obj.pk)

        return render(request, self.template_name, context)

    def _calculate(self, cleaned: dict[str, Any]):
        return extract_villalva_parameters(
            _villalva_input_from_cleaned(cleaned),
            alpha_min=float(cleaned["alpha_min"]),
            alpha_max=float(cleaned["alpha_max"]),
            alpha_step=float(cleaned["alpha_step"]),
            rs_step=float(cleaned["rs_step"]),
            max_iterations=int(cleaned["max_iterations"]),
        )

    def _save_module(self, form: VillalvaModuleForm, result):
        cleaned = form.cleaned_data
        nome = cleaned["nome"].strip()
        fabricante = cleaned["fabricante"].strip()
        defaults = _villalva_module_defaults(cleaned, result)
        obj = PVModule.objects.filter(nome=nome, fabricante=fabricante).first()
        created = obj is None

        if obj is not None and not cleaned.get("atualizar_existente"):
            form.add_error(
                None,
                "Ja existe um modulo com este nome e fabricante. Marque a opcao de atualizar para sobrescrever.",
            )
            return None

        if obj is None:
            obj = PVModule(nome=nome, fabricante=fabricante)
        for field, value in defaults.items():
            setattr(obj, field, value)

        try:
            obj.full_clean()
            obj.save()
        except ValidationError as exc:
            form.add_error(None, exc)
            return None
        return obj, created

    def _warnings(self, cleaned: dict[str, Any], result) -> list[str]:
        warnings = list(result.warnings)
        pmp_user = float(cleaned["pmp_w"])
        pmp_mpp = result.pmp_datasheet_w
        if pmp_user > 0:
            mismatch_pct = abs(pmp_user - pmp_mpp) / pmp_user * 100.0
            if mismatch_pct > 1.0:
                warnings.append(
                    (
                        "Pmp nominal difere de Vmp x Imp em "
                        f"{mismatch_pct:.2f}%. O metodo usa Vmp x Imp como ponto de maxima potencia."
                    )
                )
        for candidate in result.candidates:
            warnings.extend(candidate.warnings)
        return list(dict.fromkeys(warnings))


class CSVUploadView(LoginRequiredMixin, FormView):
    template_name = "pvmodules/upload.html"
    form_class = CSVUploadForm
    success_url = reverse_lazy("pvmodules:list")

    expected_headers = [
        "nome","fabricante","pmp_w","vmp_v","imp_a","voc_v","isc_a",
        "eficiencia_pct","power_tolerance","num_celulas",
        "temp_coeff_voc_pct_c","temp_coeff_isc_pct_c",
        "rs_ohm","rp_ohm","diode_a"
    ]

    def form_valid(self, form):
        f = form.cleaned_data["arquivo"]
        atualizar = form.cleaned_data["atualizar_existentes"]
        decoded = f.read().decode("utf-8", errors="ignore").splitlines()
        reader = csv.DictReader(decoded)

        missing = [h for h in self.expected_headers if h not in reader.fieldnames]
        if missing:
            messages.error(self.request, f"CSV faltando cabeçalhos: {', '.join(missing)}")
            return HttpResponseRedirect(self.get_success_url())

        criados, atualizados, erros = 0, 0, 0
        for i, row in enumerate(reader, start=2):
            try:
                nome = row["nome"].strip()
                fabricante = row["fabricante"].strip()
                if not nome or not fabricante:
                    raise ValueError("Nome e Fabricante são obrigatórios.")

                defaults = {
                    "pmp_w": _to_decimal(row["pmp_w"]),
                    "vmp_v": _to_decimal(row["vmp_v"]),
                    "imp_a": _to_decimal(row["imp_a"]),
                    "voc_v": _to_decimal(row["voc_v"]),
                    "isc_a": _to_decimal(row["isc_a"]),
                    "eficiencia_pct": _to_decimal(row["eficiencia_pct"]),
                    "power_tolerance": row.get("power_tolerance", "").strip(),
                    "num_celulas": int(row["num_celulas"]),
                    "temp_coeff_voc_pct_c": _to_decimal(row["temp_coeff_voc_pct_c"]),
                    "temp_coeff_isc_pct_c": _to_decimal(row["temp_coeff_isc_pct_c"]),
                    "rs_ohm": _to_decimal(row["rs_ohm"]),
                    "rp_ohm": _to_decimal(row["rp_ohm"]),
                    "diode_a": _to_decimal(row["diode_a"]),
                }

                obj = PVModule.objects.filter(nome=nome, fabricante=fabricante).first()
                if obj:
                    if atualizar:
                        for k, v in defaults.items():
                            setattr(obj, k, v)
                        obj.full_clean()
                        obj.save()
                        atualizados += 1
                else:
                    obj = PVModule(nome=nome, fabricante=fabricante, **defaults)
                    obj.full_clean()
                    obj.save()
                    criados += 1

            except Exception as e:
                erros += 1
                messages.error(self.request, f"Linha {i}: {e}")

        if criados:
            messages.success(self.request, f"{criados} módulo(s) criado(s).")
        if atualizados:
            messages.info(self.request, f"{atualizados} módulo(s) atualizado(s).")
        if erros:
            messages.warning(self.request, f"{erros} linha(s) com erro no CSV.")
        return super().form_valid(form)

