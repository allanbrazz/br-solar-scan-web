from __future__ import annotations

import math
from dataclasses import dataclass, field


Q_ELECTRON = 1.60217646e-19
K_BOLTZMANN = 1.3806503e-23
STC_TEMP_C = 25.0
STC_IRRADIANCE = 1000.0


class VillalvaError(ValueError):
    pass


@dataclass(frozen=True)
class VillalvaInput:
    isc_a: float
    voc_v: float
    vmp_v: float
    imp_a: float
    cells_in_series: int
    temp_coeff_voc_pct_c: float
    temp_coeff_isc_pct_c: float
    t_ref_c: float = STC_TEMP_C
    irradiance_ref_w_m2: float = STC_IRRADIANCE


@dataclass(frozen=True)
class VillalvaCandidate:
    diode_a: float
    rs_ohm: float
    rp_ohm: float
    ipv_a: float
    i0_a: float
    pmp_model_w: float
    vmp_model_v: float
    imp_model_a: float
    isc_model_a: float
    voc_model_v: float
    error_w: float
    error_pct: float
    iterations: int
    converged: bool
    warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class VillalvaResult:
    input: VillalvaInput
    pmp_datasheet_w: float
    best: VillalvaCandidate
    candidates: tuple[VillalvaCandidate, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _safe_exp(value: float) -> float:
    if value > 700:
        return math.inf
    if value < -700:
        return 0.0
    return math.exp(value)


def _safe_expm1(value: float) -> float:
    if value > 700:
        return math.inf
    if value < -50:
        return -1.0
    return math.expm1(value)


def _frange(start: float, stop: float, step: float) -> list[float]:
    out: list[float] = []
    value = float(start)
    guard = 0
    while value <= stop + step * 0.5 and guard < 1000:
        out.append(round(value, 6))
        value += step
        guard += 1
    return out


def _validate_input(data: VillalvaInput) -> None:
    if data.isc_a <= 0 or data.imp_a <= 0:
        raise VillalvaError("Isc e Imp devem ser maiores que zero.")
    if data.voc_v <= 0 or data.vmp_v <= 0:
        raise VillalvaError("Voc e Vmp devem ser maiores que zero.")
    if data.isc_a <= data.imp_a:
        raise VillalvaError("Isc deve ser maior que Imp para o metodo iterativo.")
    if data.voc_v <= data.vmp_v:
        raise VillalvaError("Voc deve ser maior que Vmp para o metodo iterativo.")
    if data.cells_in_series < 1:
        raise VillalvaError("O numero de celulas em serie deve ser maior que zero.")
    if data.irradiance_ref_w_m2 <= 0:
        raise VillalvaError("A irradiancia de referencia deve ser maior que zero.")


class _VillalvaSolver:
    def __init__(
        self,
        data: VillalvaInput,
        diode_a: float,
        *,
        rs_step: float,
        max_iterations: int,
        tolerance_w: float,
    ):
        self.data = data
        self.diode_a = float(diode_a)
        self.rs_step = float(rs_step)
        self.max_iterations = int(max_iterations)
        self.tolerance_w = float(tolerance_w)
        self.t_ref_k = data.t_ref_c + 273.15
        self.vt_v = data.cells_in_series * K_BOLTZMANN * self.t_ref_k / Q_ELECTRON
        self.pmp_datasheet_w = data.vmp_v * data.imp_a
        self.ki_a_k = data.isc_a * data.temp_coeff_isc_pct_c / 100.0
        self.kv_v_k = data.voc_v * data.temp_coeff_voc_pct_c / 100.0
        self.i0_a = self._calc_i0()

    def _calc_i0(self) -> float:
        exponent = self.data.voc_v / (self.diode_a * self.vt_v)
        denominator = _safe_expm1(exponent)
        if not math.isfinite(denominator) or denominator <= 0:
            return 0.0
        return self.data.isc_a / denominator

    def rp_min(self) -> float:
        value = (self.data.vmp_v / (self.data.isc_a - self.data.imp_a)) - (
            (self.data.voc_v - self.data.vmp_v) / self.data.imp_a
        )
        if not math.isfinite(value) or value <= 0:
            raise VillalvaError("Rp_min calculado nao e positivo. Verifique Isc, Imp, Voc e Vmp.")
        return value

    def ipv(self, rs_ohm: float, rp_ohm: float) -> float:
        return ((rp_ohm + rs_ohm) / rp_ohm) * self.data.isc_a

    def rp_from_rs(self, rs_ohm: float, ipv_a: float) -> float | None:
        v_m = self.data.vmp_v
        i_m = self.data.imp_a
        exp_term = _safe_exp((v_m + i_m * rs_ohm) / (self.diode_a * self.vt_v))
        if not math.isfinite(exp_term):
            return None
        denominator = v_m * ipv_a - v_m * self.i0_a * exp_term + v_m * self.i0_a - self.pmp_datasheet_w
        numerator = v_m * (v_m + i_m * rs_ohm)
        if denominator <= 0 or not math.isfinite(denominator):
            return None
        value = numerator / denominator
        if not math.isfinite(value) or value <= 0:
            return None
        return value

    def residual_current(self, current_a: float, voltage_v: float, ipv_a: float, rs_ohm: float, rp_ohm: float) -> float:
        exp_arg = (voltage_v + current_a * rs_ohm) / (self.diode_a * self.vt_v)
        diode = self.i0_a * (_safe_exp(exp_arg) - 1.0)
        shunt = (voltage_v + current_a * rs_ohm) / rp_ohm
        return ipv_a - diode - shunt - current_a

    def current_at_voltage(self, voltage_v: float, ipv_a: float, rs_ohm: float, rp_ohm: float) -> float:
        if voltage_v < 0:
            return 0.0
        f0 = self.residual_current(0.0, voltage_v, ipv_a, rs_ohm, rp_ohm)
        if f0 <= 0:
            return 0.0
        high = max(self.data.isc_a * 1.5, ipv_a * 1.2, 1.0)
        f_high = self.residual_current(high, voltage_v, ipv_a, rs_ohm, rp_ohm)
        expand_guard = 0
        while f_high > 0 and expand_guard < 10:
            high *= 2.0
            f_high = self.residual_current(high, voltage_v, ipv_a, rs_ohm, rp_ohm)
            expand_guard += 1
        if f_high > 0:
            return max(0.0, high)

        low = 0.0
        for _ in range(44):
            mid = (low + high) / 2.0
            f_mid = self.residual_current(mid, voltage_v, ipv_a, rs_ohm, rp_ohm)
            if f_mid > 0:
                low = mid
            else:
                high = mid
        return max(0.0, (low + high) / 2.0)

    def voltage_open_circuit(self, ipv_a: float, rs_ohm: float, rp_ohm: float) -> float:
        def f(voltage_v: float) -> float:
            return self.residual_current(0.0, voltage_v, ipv_a, rs_ohm, rp_ohm)

        low = 0.0
        high = max(self.data.voc_v * 1.5, self.data.vmp_v * 1.2)
        while f(high) > 0 and high < self.data.voc_v * 4:
            high *= 1.4
        for _ in range(50):
            mid = (low + high) / 2.0
            if f(mid) > 0:
                low = mid
            else:
                high = mid
        return (low + high) / 2.0

    def power_at_voltage(self, voltage_v: float, ipv_a: float, rs_ohm: float, rp_ohm: float) -> tuple[float, float]:
        current = self.current_at_voltage(voltage_v, ipv_a, rs_ohm, rp_ohm)
        return voltage_v * current, current

    def find_pmax(self, ipv_a: float, rs_ohm: float, rp_ohm: float) -> tuple[float, float, float]:
        v_upper = max(self.data.voc_v * 1.05, self.data.vmp_v * 1.1)
        grid_n = 48
        grid = [v_upper * i / (grid_n - 1) for i in range(grid_n)]
        powers = [self.power_at_voltage(v, ipv_a, rs_ohm, rp_ohm)[0] for v in grid]
        idx = max(range(len(powers)), key=lambda i: powers[i])
        left = grid[max(0, idx - 1)]
        right = grid[min(grid_n - 1, idx + 1)]

        if right <= left:
            voltage = grid[idx]
            power, current = self.power_at_voltage(voltage, ipv_a, rs_ohm, rp_ohm)
            return power, voltage, current

        phi = (math.sqrt(5.0) - 1.0) / 2.0
        c = right - phi * (right - left)
        d = left + phi * (right - left)
        pc = self.power_at_voltage(c, ipv_a, rs_ohm, rp_ohm)[0]
        pd = self.power_at_voltage(d, ipv_a, rs_ohm, rp_ohm)[0]
        for _ in range(32):
            if pc < pd:
                left = c
                c = d
                pc = pd
                d = left + phi * (right - left)
                pd = self.power_at_voltage(d, ipv_a, rs_ohm, rp_ohm)[0]
            else:
                right = d
                d = c
                pd = pc
                c = right - phi * (right - left)
                pc = self.power_at_voltage(c, ipv_a, rs_ohm, rp_ohm)[0]

        voltage = (left + right) / 2.0
        power, current = self.power_at_voltage(voltage, ipv_a, rs_ohm, rp_ohm)
        return power, voltage, current

    def solve(self) -> VillalvaCandidate:
        warnings: list[str] = []
        rp_ohm = self.rp_min()
        best: VillalvaCandidate | None = None
        invalid_streak = 0

        for iteration in range(self.max_iterations + 1):
            rs_ohm = iteration * self.rs_step
            ipv_a = self.ipv(rs_ohm, rp_ohm)
            next_rp = self.rp_from_rs(rs_ohm, ipv_a)
            if next_rp is None:
                invalid_streak += 1
                if len(warnings) < 5:
                    warnings.append(f"Rp invalido na iteracao {iteration}; ponto ignorado.")
                if best is not None and invalid_streak >= 25:
                    break
                continue
            invalid_streak = 0
            rp_ohm = next_rp
            pmp_model_w, vmp_model_v, imp_model_a = self.find_pmax(ipv_a, rs_ohm, rp_ohm)
            error_w = abs(pmp_model_w - self.pmp_datasheet_w)
            error_pct = error_w / self.pmp_datasheet_w * 100.0
            isc_model_a = self.current_at_voltage(0.0, ipv_a, rs_ohm, rp_ohm)
            voc_model_v = self.voltage_open_circuit(ipv_a, rs_ohm, rp_ohm)
            converged = error_w <= self.tolerance_w
            candidate = VillalvaCandidate(
                diode_a=self.diode_a,
                rs_ohm=rs_ohm,
                rp_ohm=rp_ohm,
                ipv_a=ipv_a,
                i0_a=self.i0_a,
                pmp_model_w=pmp_model_w,
                vmp_model_v=vmp_model_v,
                imp_model_a=imp_model_a,
                isc_model_a=isc_model_a,
                voc_model_v=voc_model_v,
                error_w=error_w,
                error_pct=error_pct,
                iterations=iteration,
                converged=converged,
                warnings=tuple(warnings[:5]),
            )
            if best is None or candidate.error_w < best.error_w:
                best = candidate

        if best is None:
            raise VillalvaError("Nao foi possivel encontrar uma combinacao numericamente valida para este valor de a.")

        if best.converged:
            return best

        return VillalvaCandidate(
            **{
                **best.__dict__,
                "warnings": tuple(
                    [
                        *best.warnings,
                        "Nao convergiu dentro do limite; usando o menor erro encontrado.",
                    ]
                ),
            }
        )


def extract_villalva_parameters(
    data: VillalvaInput,
    *,
    alpha_min: float = 1.0,
    alpha_max: float = 1.5,
    alpha_step: float = 0.1,
    rs_step: float = 0.001,
    max_iterations: int = 2500,
    tolerance_w: float | None = None,
) -> VillalvaResult:
    _validate_input(data)
    if alpha_min <= 0 or alpha_max < alpha_min:
        raise VillalvaError("Intervalo do fator de idealidade invalido.")
    if alpha_step <= 0:
        raise VillalvaError("O passo do fator de idealidade deve ser maior que zero.")
    if rs_step <= 0:
        raise VillalvaError("O passo de Rs deve ser maior que zero.")
    if max_iterations < 1:
        raise VillalvaError("O limite de iteracoes deve ser maior que zero.")

    pmp_datasheet_w = data.vmp_v * data.imp_a
    tolerance = tolerance_w if tolerance_w is not None else max(0.05, pmp_datasheet_w * 0.0005)
    candidates: list[VillalvaCandidate] = []
    warnings: list[str] = []

    for alpha in _frange(alpha_min, alpha_max, alpha_step):
        try:
            solver = _VillalvaSolver(
                data,
                alpha,
                rs_step=rs_step,
                max_iterations=max_iterations,
                tolerance_w=tolerance,
            )
            candidates.append(solver.solve())
        except VillalvaError as exc:
            warnings.append(f"a={alpha}: {exc}")
        except (OverflowError, ZeroDivisionError, ValueError) as exc:
            warnings.append(f"a={alpha}: instabilidade numerica ({exc}).")

    if not candidates:
        raise VillalvaError("Nenhum valor de a produziu uma solucao valida.")

    best = min(
        candidates,
        key=lambda c: (
            not c.converged,
            c.error_pct,
            abs(c.diode_a - 1.3),
        ),
    )

    if not best.converged:
        warnings.append("Nenhum candidato atingiu a tolerancia; o sistema selecionou o menor erro relativo.")

    return VillalvaResult(
        input=data,
        pmp_datasheet_w=pmp_datasheet_w,
        best=best,
        candidates=tuple(sorted(candidates, key=lambda c: c.diode_a)),
        warnings=tuple(warnings),
    )


def result_iv_curve(candidate: VillalvaCandidate, data: VillalvaInput, points: int = 40) -> list[dict[str, float]]:
    solver = _VillalvaSolver(
        data,
        candidate.diode_a,
        rs_step=0.001,
        max_iterations=1,
        tolerance_w=max(0.05, data.vmp_v * data.imp_a * 0.0005),
    )
    v_upper = data.voc_v
    curve = []
    for i in range(max(points, 2)):
        voltage = v_upper * i / (max(points, 2) - 1)
        current = solver.current_at_voltage(voltage, candidate.ipv_a, candidate.rs_ohm, candidate.rp_ohm)
        curve.append({"v": voltage, "i": current, "p": voltage * current})
    return curve
