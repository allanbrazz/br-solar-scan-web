from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from core.services.power_model.power_model import (
    expected_and_mismatch,
    expected_dc_by_mppt_from_details,
    module_from_pvmodule,
    plant_from_details,
)


def _all_nan_or_missing(arr: Any) -> bool:
    """True quando a série é ausente, vazia ou não possui nenhum valor finito."""
    if arr is None:
        return True
    try:
        a = np.asarray(arr, dtype=float).ravel()
    except Exception:
        return True
    if a.size == 0:
        return True
    return not bool(np.isfinite(a).any())


def _safe_int(v: Any) -> Optional[int]:
    try:
        if v is None:
            return None
        x = int(v)
        return x if x > 0 else None
    except Exception:
        return None


def _string_groups_by_mppt(details: Any) -> Dict[int, List[Tuple[int, int]]]:
    """
    Lê PVPlantStringConfig via related_name string_configs.

    Retorna:
        {mppt: [(strings_qty, modules_per_string), ...]}
    """
    out: Dict[int, List[Tuple[int, int]]] = {}

    qs = getattr(details, "string_configs", None)
    if qs is None:
        return out

    try:
        cfgs = list(qs.all())
    except Exception:
        return out

    for c in cfgs:
        mppt = _safe_int(getattr(c, "mppt", None))
        sq = _safe_int(getattr(c, "strings_qty", None))
        ns = _safe_int(getattr(c, "modules_per_string", None))

        if mppt is None or sq is None or ns is None:
            continue

        out.setdefault(int(mppt), []).append((int(sq), int(ns)))

    return out


def _mppt_expected_from_module_mpp(
    *,
    out_model: Dict[str, Any],
    details: Any,
    k_sys: float,
) -> Dict[int, Dict[str, np.ndarray]]:
    """
    Calcula Vdc/Idc/Pdc esperados por MPPT a partir das curvas por módulo
    já calculadas por expected_and_mismatch.

    Esta é a primeira opção porque preserva o mesmo G_POA, temperatura de célula
    e eventual time-shift aplicados no modelo principal.

    Funciona diretamente para MPPTs formados por grupos em paralelo com o mesmo
    número de módulos em série. Para MPPTs com grupos de Ns diferentes no mesmo
    MPPT, a função sinaliza fallback, pois o ponto MPP do arranjo precisa ser
    resolvido como curva composta.
    """
    vmp_mod = np.asarray(out_model.get("vmp_mod_v"), dtype=float).ravel()
    imp_mod = np.asarray(out_model.get("imp_mod_a"), dtype=float).ravel()
    pmp_mod = np.asarray(out_model.get("pmp_mod_w"), dtype=float).ravel()

    n = vmp_mod.size
    if n == 0 or imp_mod.size != n or pmp_mod.size != n:
        return {}

    groups_by_mppt = _string_groups_by_mppt(details)
    if not groups_by_mppt:
        return {}

    expected: Dict[int, Dict[str, np.ndarray]] = {}

    for mppt, groups in sorted(groups_by_mppt.items()):
        ns_set = {int(ns) for _sq, ns in groups}

        # Caso simples e usual: todas as strings do MPPT têm o mesmo Ns.
        # Ex.: MPPT 1 = 1 string x 8 módulos; MPPT 2 = 1 string x 7 módulos.
        if len(ns_set) == 1:
            ns = float(next(iter(ns_set)))
            np_strings = float(sum(int(sq) for sq, _ns in groups))

            v_exp = vmp_mod * ns
            i_exp = imp_mod * np_strings
            p_exp = pmp_mod * ns * np_strings * float(k_sys)

            expected[int(mppt)] = {
                "v_dc_expected_v": v_exp,
                "i_dc_expected_a": i_exp,
                "pdc_expected_w": p_exp,
                "method": "module_mpp_same_Ns_per_MPPT",
            }
            continue

        # Caso mais complexo: grupos com Ns diferentes dentro do mesmo MPPT.
        # Será tratado por fallback em expected_dc_by_mppt_from_details.
        return {}

    return expected


def _aggregate_mppt_expected(mppt_expected: Dict[int, Dict[str, np.ndarray]], n: int) -> Dict[str, np.ndarray]:
    """
    Agrega referências por MPPT para comparação com telemetria agregada de planta.

    Convenção adotada:
    - Pdc esperado agregado: soma das potências esperadas dos MPPTs.
    - Idc esperado agregado: soma das correntes esperadas dos MPPTs.
    - Vdc esperado agregado: média das tensões esperadas dos MPPTs disponíveis.

    A média de Vdc replica a estratégia já usada em runtime_detection para
    preencher o modelo agregado a partir de modelos por MPPT.
    """
    p_stack: List[np.ndarray] = []
    v_stack: List[np.ndarray] = []
    i_stack: List[np.ndarray] = []

    for _mppt, block in sorted((mppt_expected or {}).items()):
        if not isinstance(block, dict):
            continue

        p = np.asarray(block.get("pdc_expected_w"), dtype=float).ravel()
        v = np.asarray(block.get("v_dc_expected_v"), dtype=float).ravel()
        i = np.asarray(block.get("i_dc_expected_a"), dtype=float).ravel()

        if p.size == n:
            p_stack.append(p)
        if v.size == n:
            v_stack.append(v)
        if i.size == n:
            i_stack.append(i)

    def sum_stack(stacks: List[np.ndarray]) -> np.ndarray:
        if not stacks:
            return np.full(n, np.nan, dtype=float)
        mat = np.vstack(stacks)
        any_valid = np.isfinite(mat).any(axis=0)
        out = np.nansum(mat, axis=0)
        return np.where(any_valid, out, np.nan)

    def mean_stack(stacks: List[np.ndarray]) -> np.ndarray:
        if not stacks:
            return np.full(n, np.nan, dtype=float)
        mat = np.vstack(stacks)
        any_valid = np.isfinite(mat).any(axis=0)
        out = np.nanmean(mat, axis=0)
        return np.where(any_valid, out, np.nan)

    return {
        "pdc_expected_w": sum_stack(p_stack),
        "v_dc_expected_v": mean_stack(v_stack),
        "i_dc_expected_a": sum_stack(i_stack),
    }


def _safe_ratio(observed: Any, expected: Any, eps: float) -> np.ndarray:
    obs = np.asarray(observed, dtype=float).ravel()
    exp = np.asarray(expected, dtype=float).ravel()

    if obs.size != exp.size:
        return np.full(exp.size, np.nan, dtype=float)

    den = np.maximum(np.abs(exp), float(eps))
    return np.where(np.isfinite(obs) & np.isfinite(exp), obs / den, np.nan)


def _fill_dc_expected_from_mppt_if_needed(
    *,
    out: Dict[str, Any],
    details: Any,
    module: Any,
    plant_model: Any,
    g_poa_input: np.ndarray,
    temp_air_input: np.ndarray,
    v_dc_real: Optional[np.ndarray],
    i_dc_real: Optional[np.ndarray],
    replace_pdc_expected: bool = True,
) -> None:
    """
    Preenche Vdc/Idc esperados quando a topologia agregada simples não é suficiente.

    O caso que motivou esta rotina é planta com topologia heterogênea:
    MPPT 1 com 8 módulos e MPPT 2 com 7 módulos. Nessa condição,
    PVPlantDetails.modules_per_string fica propositalmente nulo, mas
    PVPlantStringConfig contém a informação necessária para estimar V/I por MPPT.
    """
    n = len(np.asarray(out.get("valid"), dtype=bool).ravel())

    if n == 0:
        return

    need_v = _all_nan_or_missing(out.get("v_dc_expected_v"))
    need_i = _all_nan_or_missing(out.get("i_dc_expected_a"))
    need_p = _all_nan_or_missing(out.get("pdc_expected_w"))

    # Se já existem V e I esperados, não há nada a corrigir para resíduos V/I.
    if not (need_v or need_i):
        return

    meta = dict(out.get("meta") or {})
    k_sys = float(getattr(plant_model, "k_sys", 1.0) or 1.0)

    mppt_expected: Dict[int, Dict[str, np.ndarray]] = {}

    # 1) Preferência: usa Vmp/Imp/Pmp por módulo já calculados pelo modelo principal.
    # Isso preserva o time-shift automático aplicado dentro de expected_and_mismatch.
    try:
        mppt_expected = _mppt_expected_from_module_mpp(
            out_model=out,
            details=details,
            k_sys=k_sys,
        )
    except Exception as exc:
        meta["dc_expected_module_mpp_error"] = f"{type(exc).__name__}: {exc}"
        mppt_expected = {}

    # 2) Fallback: usa a rotina completa de strings heterogêneas por MPPT.
    # Usa g_poa_used retornado pelo modelo, se disponível, para preservar o
    # alinhamento temporal aplicado à irradiância.
    if not mppt_expected:
        try:
            g_for_mppt = np.asarray(out.get("g_poa_used"), dtype=float).ravel()
            if g_for_mppt.size != n:
                g_for_mppt = np.asarray(g_poa_input, dtype=float).ravel()

            mppt_expected = expected_dc_by_mppt_from_details(
                details=details,
                module=module,
                plant=plant_model,
                g_poa=g_for_mppt,
                tamb_c=temp_air_input,
                g_min_valid=0.0,
                n_points=60,
            )

            if mppt_expected:
                meta["dc_expected_fallback_used"] = "expected_dc_by_mppt_from_details"
        except Exception as exc:
            meta["dc_expected_fallback_error"] = f"{type(exc).__name__}: {exc}"
            mppt_expected = {}

    if not mppt_expected:
        meta.setdefault("dc_expected_source", "unavailable_no_valid_PVPlantStringConfig_by_MPPT")
        out["meta"] = meta
        return

    agg = _aggregate_mppt_expected(mppt_expected, n=n)

    v_exp = agg.get("v_dc_expected_v")
    i_exp = agg.get("i_dc_expected_a")
    p_exp = agg.get("pdc_expected_w")

    if need_v and v_exp is not None and np.asarray(v_exp).size == n:
        out["v_dc_expected_v"] = np.asarray(v_exp, dtype=float)

    if need_i and i_exp is not None and np.asarray(i_exp).size == n:
        out["i_dc_expected_a"] = np.asarray(i_exp, dtype=float)

    # Para o residual Pdc, usa a referência por MPPT quando disponível.
    # Em topologias simples por MPPT, isso coincide com pmp_mod * modules_total * k_sys;
    # em MPPT com grupos heterogêneos, torna o Pdc esperado mais coerente.
    if replace_pdc_expected and p_exp is not None and np.asarray(p_exp).size == n:
        out["pdc_expected_w"] = np.asarray(p_exp, dtype=float)

    out["pdc_expected_w_mppt_sum"] = np.asarray(p_exp, dtype=float) if p_exp is not None else np.full(n, np.nan)

    if v_dc_real is not None:
        v_arr = np.asarray(out.get("v_dc_expected_v"), dtype=float)
        if v_arr.size == n:
            out["v_ratio"] = _safe_ratio(v_dc_real, v_arr, eps=1.0)

    if i_dc_real is not None:
        i_arr = np.asarray(out.get("i_dc_expected_a"), dtype=float)
        if i_arr.size == n:
            out["i_ratio"] = _safe_ratio(i_dc_real, i_arr, eps=0.1)

    meta.update(
        {
            "dc_expected_source": "PVPlantStringConfig_by_MPPT",
            "dc_expected_mppt_count": int(len(mppt_expected)),
            "dc_expected_aggregate_rule": "Vdc=mean(MPPT Vmp), Idc=sum(MPPT Imp), Pdc=sum(MPPT Pmp)",
            "dc_expected_pdc_replaced_by_mppt_sum": bool(replace_pdc_expected),
            "dc_topology_ok_mppt": True,
            "topology_note": None,
        }
    )

    out["meta"] = meta


def compute_expected_state(
    *,
    plant: Any,
    times_utc: List[Any],
    g_poa_used: np.ndarray,
    ghi: np.ndarray,
    dni: np.ndarray,
    dhi: np.ndarray,
    temp_air: np.ndarray,
    p_ac_real: np.ndarray,
    p_dc_real: np.ndarray | None = None,
    v_dc_real: np.ndarray | None = None,
    i_dc_real: np.ndarray | None = None,
) -> Dict[str, Any]:
    details = getattr(plant, "details", None)

    if not details or not getattr(details, "module_id", None):
        n = len(times_utc)
        nan = np.full(n, np.nan, dtype=float)
        return {
            "valid": np.zeros(n, dtype=bool),
            "pac_expected_w": nan,
            "pdc_expected_w": nan,
            "v_dc_expected_v": nan,
            "i_dc_expected_a": nan,
            "mismatch_rel": nan,
            "mismatch_abs_w": nan,
            "tcell_c": nan,
            "v_ratio": nan,
            "i_ratio": nan,
            "meta": {"reason": "missing_module"},
        }

    mod = module_from_pvmodule(details.module)
    inv = getattr(details, "inverter", None)
    pl = plant_from_details(details, inverter=inv, use_inverter_eff=True)

    pld = asdict(pl) if is_dataclass(pl) else dict(getattr(pl, "__dict__", {}))

    if pld.get("lat_deg") is None:
        pld["lat_deg"] = float(getattr(plant, "latitude", 0.0) or 0.0)
    if pld.get("lon_deg") is None:
        pld["lon_deg"] = float(getattr(plant, "longitude", 0.0) or 0.0)
    if pld.get("tilt_deg") is None:
        pld["tilt_deg"] = float(getattr(details, "tilt_deg", 0.0) or 0.0)
    if pld.get("azimuth_deg") is None:
        pld["azimuth_deg"] = float(getattr(details, "azimuth_deg", 0.0) or 0.0)

    pl = pl.__class__(**pld)

    kwargs: Dict[str, Any] = dict(
        g_poa=g_poa_used,
        tamb_c=temp_air,
        pac_real_w=p_ac_real,
        module=mod,
        plant=pl,
        ghi=(ghi if np.isfinite(ghi).any() else None),
        dni=(dni if np.isfinite(dni).any() else None),
        dhi=(dhi if np.isfinite(dhi).any() else None),
        times_utc=np.asarray(times_utc, dtype="datetime64[ns]"),
        v_dc_real_v=v_dc_real,
        i_dc_real_a=i_dc_real,
        g_min_valid=0.0,
        n_points=60,
        eps_w=50.0,
        dt_minutes=15.0,
        window_minutes=60.0,
    )

    out = expected_and_mismatch(**kwargs) or {}

    _fill_dc_expected_from_mppt_if_needed(
        out=out,
        details=details,
        module=mod,
        plant_model=pl,
        g_poa_input=np.asarray(g_poa_used, dtype=float),
        temp_air_input=np.asarray(temp_air, dtype=float),
        v_dc_real=v_dc_real,
        i_dc_real=i_dc_real,
        replace_pdc_expected=True,
    )

    return out
