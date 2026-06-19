from __future__ import annotations

from collections import OrderedDict
from typing import Any, Dict, List

DEFAULT_CONFIG_NAME = "C18_estabilidade_restritiva"
DEFAULT_DETECTOR_VERSION = "mismatch_runtime_v1"
DEFAULT_SOURCE_OPER = "ALL"
DEFAULT_SOURCE_METEO = "OPENMETEO"
DEFAULT_DISPLAY_MODE = "tipologia"
DEFAULT_PERSIST = True

BASIC_PARAM_DEFAULTS: Dict[str, float] = {
    "warn_abs": 0.47,
    "fault_abs": 0.95,
    "gpoa_min": 250.0,
    "pmin_w": 300.0,
}

BASIC_PARAM_HELP: Dict[str, str] = {
    "plant_id": "Planta cujos dados casados de inversor e meteorologia serao analisados.",
    "start": "Primeiro dia local incluido na analise.",
    "end": "Ultimo dia local incluido na analise.",
    "dt_minutes": "Resolucao visual do heatmap; nao altera a base 15 min persistida.",
    "warn_abs": "Desvio relativo absoluto a partir do qual o ponto entra em atencao.",
    "display_mode": "Escolhe se as celulas priorizam o desvio numerico ou a tipologia diagnosticada.",
    "fault_abs": "Desvio negativo relativo a partir do qual o ponto e tratado como falha severa.",
    "gpoa_min": "Irradiancia minima no plano do modulo para considerar que ha sol suficiente.",
    "pmin_w": "Potencia minima usada para filtrar pontos sem geracao util.",
    "savedConfigurationSelect": "Conjunto de parametros salvo para reutilizar em execucoes futuras.",
    "configurationName": "Nome que identifica a configuracao da planta.",
    "rs_trials": "Quantidade de combinacoes testadas na busca aleatoria de parametros.",
    "rs_seed": "Semente numerica que torna a busca aleatoria reprodutivel.",
}

ADVANCED_PARAM_HELP: Dict[str, str] = {
    "meteo_pos_abs": "Tolerancia para desvio positivo associado a possivel erro meteorologico.",
    "shading_std_abs": "Limite de variabilidade usado para diferenciar sombreamento de falha persistente.",
    "shading_window_points": "Quantidade de pontos na janela movel usada para avaliar variacao de sombreamento.",
    "max_gap_minutes": "Intervalo maximo para unir pontos proximos no mesmo evento.",
    "gpoa_plot_min": "Irradiancia minima usada para destacar pontos nos graficos auxiliares.",
    "pmodel_plot_min": "Potencia modelada minima para incluir pontos na leitura visual.",
    "mismatch_clip_abs": "Limite aplicado ao desvio mostrado para evitar escala visual dominada por outliers.",
    "sun_available_gpoa_wm2": "Irradiancia minima para classificar sol disponivel para geracao.",
    "coarse_diag_gpoa_wm2": "Irradiancia minima para liberar diagnostico preliminar.",
    "fine_diag_gpoa_wm2": "Irradiancia minima para liberar diagnostico mais rigoroso.",
    "stable_cv_max": "Coeficiente de variacao maximo para considerar o ceu estavel.",
    "stable_ramp_max_wm2": "Variacao maxima de irradiancia na janela para manter estabilidade.",
    "stable_window_points": "Tamanho da janela de avaliacao de estabilidade.",
    "min_baseline_points": "Numero minimo de amostras para formar referencia estatistica.",
    "inv_cov_min": "Cobertura minima esperada das amostras do inversor no periodo.",
    "ewma_lambda": "Peso da memoria curta no controle EWMA do residual.",
    "ewma_L": "Multiplicador do limite de controle EWMA.",
    "cusum_k": "Referencia de desvio minimo acumulado pelo CUSUM.",
    "cusum_h": "Limite acumulado que dispara sinal estatistico no CUSUM.",
    "zero_abs_w": "Potencia AC abaixo da qual a injecao e considerada praticamente nula.",
    "zero_rel_model": "Fração do modelo abaixo da qual a injecao e tratada como nula.",
    "degraded_rel": "Perda relativa usada para marcar geracao degradada.",
    "severe_rel": "Perda relativa usada para marcar anomalia severa.",
    "low_i_ratio_warn": "Razao de corrente DC que indica alerta de corrente baixa.",
    "low_i_ratio_crit": "Razao de corrente DC que indica corrente criticamente baixa.",
    "low_v_ratio_warn": "Razao de tensao DC que indica alerta de tensao baixa.",
    "low_v_ratio_crit": "Razao de tensao DC que indica tensao criticamente baixa.",
    "vac_low_ratio": "Limite relativo inferior de tensao CA comparado ao nominal.",
    "vac_high_ratio": "Limite relativo superior de tensao CA comparado ao nominal.",
    "vac_abs_margin_v": "Margem absoluta de tensao CA aceita antes de sinalizar rede.",
    "freq_abs_tol_hz": "Desvio absoluto de frequencia CA tolerado.",
    "clip_margin": "Margem medida para identificar limitacao ou clipping.",
    "clip_model_margin": "Margem modelada usada para confirmar clipping esperado.",
    "rca_min_baseline_points": "Numero minimo de pontos de referencia para diagnostico da causa.",
}

ADVANCED_PARAM_META: List[Dict[str, Any]] = [
    {"key": "meteo_pos_abs", "label": "Erro meteorológico positivo", "default": 0.25, "auto_text": "0.25", "step": "0.01", "min": "0", "group": "Modelo e desvio"},
    {"key": "shading_std_abs", "label": "Variabilidade de sombreamento", "default": 0.22, "auto_text": "0.22", "step": "0.01", "min": "0", "group": "Modelo e desvio"},
    {"key": "shading_window_points", "label": "Janela de sombreamento [pontos]", "default": 8, "auto_text": "8", "step": "1", "min": "1", "group": "Modelo e desvio"},
    {"key": "max_gap_minutes", "label": "Intervalo máximo entre eventos [min]", "default": 60.0, "auto_text": "60", "step": "1", "min": "0", "group": "Modelo e desvio"},
    {"key": "gpoa_plot_min", "label": "Irradiância mínima no gráfico [W/m²]", "default": 700.0, "auto_text": "700", "step": "1", "min": "0", "group": "Modelo e desvio"},
    {"key": "pmodel_plot_min", "label": "Potência modelada mínima [W]", "default": 200.0, "auto_text": "200", "step": "1", "min": "0", "group": "Modelo e desvio"},
    {"key": "mismatch_clip_abs", "label": "Limite visual do desvio", "default": 2.0, "auto_text": "2.0", "step": "0.1", "min": "0", "group": "Modelo e desvio"},
    {"key": "sun_available_gpoa_wm2", "label": "Irradiância com sol disponível [W/m²]", "default": 300.0, "auto_text": "300", "step": "1", "min": "0", "group": "Detecção"},
    {"key": "coarse_diag_gpoa_wm2", "label": "Diagnóstico preliminar [W/m²]", "default": 650.0, "auto_text": "650", "step": "1", "min": "0", "group": "Detecção"},
    {"key": "fine_diag_gpoa_wm2", "label": "Diagnóstico refinado [W/m²]", "default": 850.0, "auto_text": "850", "step": "1", "min": "0", "group": "Detecção"},
    {"key": "stable_cv_max", "label": "CV máximo de estabilidade", "default": 0.06, "auto_text": "0.06", "step": "0.01", "min": "0", "group": "Detecção"},
    {"key": "stable_ramp_max_wm2", "label": "Rampa máxima estável [W/m²]", "default": 90.0, "auto_text": "90", "step": "1", "min": "0", "group": "Detecção"},
    {"key": "stable_window_points", "label": "Janela de estabilidade [pontos]", "default": 8, "auto_text": "8", "step": "1", "min": "2", "group": "Detecção"},
    {"key": "min_baseline_points", "label": "Referência mínima [pontos]", "default": 48, "auto_text": "48", "step": "1", "min": "4", "group": "Detecção"},
    {"key": "inv_cov_min", "label": "Cobertura mínima do inversor", "default": 0.70, "auto_text": "0.70", "step": "0.01", "min": "0", "max": "1", "group": "Detecção"},
    {"key": "ewma_lambda", "label": "Suavização EWMA (lambda)", "default": 0.15, "auto_text": "0.15", "step": "0.01", "min": "0.01", "max": "1", "group": "Detecção"},
    {"key": "ewma_L", "label": "Limite de controle EWMA", "default": 3.5, "auto_text": "3.5", "step": "0.1", "min": "0", "group": "Detecção"},
    {"key": "cusum_k", "label": "Referência CUSUM", "default": 0.60, "auto_text": "0.60", "step": "0.01", "min": "0", "group": "Detecção"},
    {"key": "cusum_h", "label": "Limite acumulado CUSUM", "default": 10.0, "auto_text": "10.0", "step": "0.1", "min": "0", "group": "Detecção"},
    {"key": "zero_abs_w", "label": "Potência de injeção nula [W]", "default": 75.0, "auto_text": "75", "step": "1", "min": "0", "group": "Diagnóstico da causa"},
    {"key": "zero_rel_model", "label": "Injeção nula relativa ao modelo", "default": 0.03, "auto_text": "0.03", "step": "0.01", "min": "0", "group": "Diagnóstico da causa"},
    {"key": "degraded_rel", "label": "Perda relativa moderada", "default": 0.42, "auto_text": "0.42", "step": "0.01", "min": "0", "max": "1", "group": "Diagnóstico da causa"},
    {"key": "severe_rel", "label": "Perda relativa severa", "default": 0.80, "auto_text": "0.80", "step": "0.01", "min": "0", "max": "1", "group": "Diagnóstico da causa"},
    {"key": "low_i_ratio_warn", "label": "Corrente baixa - atenção", "default": 0.60, "auto_text": "0.60", "step": "0.01", "min": "0", "max": "1", "group": "Diagnóstico da causa"},
    {"key": "low_i_ratio_crit", "label": "Corrente baixa - crítico", "default": 0.40, "auto_text": "0.40", "step": "0.01", "min": "0", "max": "1", "group": "Diagnóstico da causa"},
    {"key": "low_v_ratio_warn", "label": "Tensão baixa - atenção", "default": 0.70, "auto_text": "0.70", "step": "0.01", "min": "0", "max": "1", "group": "Diagnóstico da causa"},
    {"key": "low_v_ratio_crit", "label": "Tensão baixa - crítico", "default": 0.50, "auto_text": "0.50", "step": "0.01", "min": "0", "max": "1", "group": "Diagnóstico da causa"},
    {"key": "vac_low_ratio", "label": "Limite inferior de tensão CA", "default": 0.90, "auto_text": "0.90", "step": "0.01", "min": "0", "max": "2", "group": "Diagnóstico da causa"},
    {"key": "vac_high_ratio", "label": "Limite superior de tensão CA", "default": 1.10, "auto_text": "1.10", "step": "0.01", "min": "0", "max": "2", "group": "Diagnóstico da causa"},
    {"key": "vac_abs_margin_v", "label": "Margem absoluta de tensão CA [V]", "default": 10.0, "auto_text": "10", "step": "1", "min": "0", "group": "Diagnóstico da causa"},
    {"key": "freq_abs_tol_hz", "label": "Tolerância de frequência [Hz]", "default": 1.0, "auto_text": "1.0", "step": "0.1", "min": "0", "group": "Diagnóstico da causa"},
    {"key": "clip_margin", "label": "Margem de limitação medida", "default": 0.98, "auto_text": "0.98", "step": "0.01", "min": "0", "max": "2", "group": "Diagnóstico da causa"},
    {"key": "clip_model_margin", "label": "Margem de limitação modelada", "default": 1.02, "auto_text": "1.02", "step": "0.01", "min": "0", "max": "2", "group": "Diagnóstico da causa"},
    {"key": "rca_min_baseline_points", "label": "Referência mínima da causa [pontos]", "default": 48, "auto_text": "48", "step": "1", "min": "4", "group": "Diagnóstico da causa"},
]

ADVANCED_PARAM_DEFAULTS: Dict[str, Any] = {item["key"]: item["default"] for item in ADVANCED_PARAM_META}
ADVANCED_PARAM_KEYS: List[str] = [item["key"] for item in ADVANCED_PARAM_META]

TIPOLOGY_RANDOM_SEARCH_SPACE = OrderedDict([
    ("gpoa_min", [180.0, 220.0, 250.0, 300.0]),
    ("coarse_diag_gpoa_wm2", [500.0, 600.0, 650.0, 700.0]),
    ("fine_diag_gpoa_wm2", [750.0, 800.0, 850.0, 900.0]),
    ("stable_cv_max", [0.04, 0.06, 0.08]),
    ("stable_ramp_max_wm2", [70.0, 90.0, 120.0]),
    ("zero_abs_w", [50.0, 75.0, 100.0]),
    ("zero_rel_model", [0.02, 0.03, 0.05]),
    ("degraded_rel", [0.35, 0.42, 0.50]),
    ("severe_rel", [0.70, 0.80, 0.90]),
])

RANDOM_SEARCH_DEFAULT_TRIALS = 24
RANDOM_SEARCH_MAX_TRIALS = 120
RANDOM_SEARCH_DEFAULT_SEED = 42


def advanced_groups() -> List[Dict[str, Any]]:
    groups: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    for item in ADVANCED_PARAM_META:
        control = dict(item)
        control["help"] = ADVANCED_PARAM_HELP.get(str(item["key"]), "Parametro avancado do detector FDD mismatch.")
        groups.setdefault(str(item["group"]), []).append(control)
    return [{"title": title, "controls": controls} for title, controls in groups.items()]
