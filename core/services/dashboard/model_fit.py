from __future__ import annotations

import math
from typing import Any, Dict, List, Optional


def _float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _average_ranks(values: List[float]) -> List[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(indexed):
        end = start + 1
        while end < len(indexed) and indexed[end][1] == indexed[start][1]:
            end += 1
        average_rank = (start + 1 + end) / 2.0
        for position in range(start, end):
            ranks[indexed[position][0]] = average_rank
        start = end
    return ranks


def _pearson_correlation(xs: List[float], ys: List[float]) -> Optional[float]:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    centered_x = [value - mean_x for value in xs]
    centered_y = [value - mean_y for value in ys]
    denominator = math.sqrt(
        sum(value * value for value in centered_x)
        * sum(value * value for value in centered_y)
    )
    if denominator <= 0:
        return None
    return sum(x * y for x, y in zip(centered_x, centered_y)) / denominator


def paired_model_metrics(
    measured: List[Optional[float]] | None,
    modeled: List[Optional[float]] | None,
) -> Dict[str, Any]:
    pairs: List[tuple[float, float]] = []
    for measured_value, modeled_value in zip(measured or [], modeled or []):
        measured_float = _float_or_none(measured_value)
        modeled_float = _float_or_none(modeled_value)
        if measured_float is not None and modeled_float is not None:
            pairs.append((measured_float, modeled_float))

    if not pairs:
        return {"pairs": 0, "rmse": None, "pearson_r": None, "spearman_rho": None}

    measured_values = [pair[0] for pair in pairs]
    modeled_values = [pair[1] for pair in pairs]
    rmse = math.sqrt(
        sum((measured_value - modeled_value) ** 2 for measured_value, modeled_value in pairs)
        / len(pairs)
    )
    return {
        "pairs": len(pairs),
        "rmse": rmse,
        "pearson_r": _pearson_correlation(modeled_values, measured_values),
        "spearman_rho": _pearson_correlation(
            _average_ranks(modeled_values),
            _average_ranks(measured_values),
        ),
    }
