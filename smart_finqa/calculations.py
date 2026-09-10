"""Deterministic post-query financial calculation operators."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from .core import report_period_order_value
from .safe_query import QuerySpec

Operator = Callable[[QuerySpec, list[dict[str, Any]]], "CalculationResult"]
REPORT_PERIOD_PATTERN = re.compile(r"^\d{4}(?:Q[1-3]|FY)$")


class CalculationError(ValueError):
    """Raised when a requested calculation cannot produce a trustworthy result."""


@dataclass(frozen=True, slots=True)
class CalculationResult:
    rows: tuple[dict[str, Any], ...]
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"rows": deepcopy(list(self.rows)), "metadata": deepcopy(self.metadata)}


def apply_post_compute(
    query_spec: QuerySpec | Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
) -> CalculationResult:
    """Apply the single declared post-query operator without mutating database rows."""
    spec = query_spec if isinstance(query_spec, QuerySpec) else QuerySpec.from_mapping(query_spec)
    copied_rows = [deepcopy(dict(row)) for row in rows]
    if not spec.post_compute:
        return CalculationResult(rows=tuple(copied_rows), metadata={"operator": None})
    try:
        operator = OPERATORS[spec.post_compute]
    except KeyError as exc:
        raise CalculationError(f"Unknown post-compute operator: {spec.post_compute!r}") from exc
    return operator(spec, copied_rows)


def _above_industry_mean(spec: QuerySpec, rows: list[dict[str, Any]]) -> CalculationResult:
    metric_fields = _metric_fields(spec)
    means: dict[str, float] = {}
    for metric in metric_fields:
        values = [_finite_number(row[metric], field=metric) for row in rows if row.get(metric) is not None]
        if not values:
            raise CalculationError(f"Cannot compute industry mean: metric {metric!r} has no numeric values")
        means[metric] = sum(values) / len(values)

    selected: list[dict[str, Any]] = []
    for row in rows:
        if all(
            row.get(metric) is not None and _finite_number(row[metric], field=metric) > means[metric]
            for metric in metric_fields
        ):
            selected.append(row)
    return CalculationResult(
        rows=tuple(selected),
        metadata={"operator": spec.post_compute, "industry_means": means, "population_size": len(rows)},
    )


def _period_comparison(spec: QuerySpec, rows: list[dict[str, Any]]) -> CalculationResult:
    metric_fields = _metric_fields(spec)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        period = row.get("report_period")
        if not isinstance(period, str) or REPORT_PERIOD_PATTERN.fullmatch(period) is None:
            raise CalculationError("period_comparison requires a valid report_period on every row")
        group_key = (str(row.get("stock_code") or ""), str(row.get("stock_abbr") or ""))
        grouped[group_key].append(row)

    compared_rows: list[dict[str, Any]] = []
    undefined_changes: list[dict[str, str]] = []
    for group_key in sorted(grouped):
        previous_values: dict[str, float] = {}
        for row in sorted(grouped[group_key], key=lambda item: report_period_order_value(str(item["report_period"]))):
            compared = dict(row)
            for metric in metric_fields:
                current = _required_row_number(row, metric)
                previous = previous_values.get(metric)
                compared[f"{metric}_previous_value"] = previous
                compared[f"{metric}_absolute_change"] = None if previous is None else current - previous
                if previous is None:
                    change_pct = None
                elif previous == 0:
                    change_pct = None
                    undefined_changes.append(
                        {
                            "stock_code": group_key[0],
                            "report_period": str(row["report_period"]),
                            "metric": metric,
                            "reason": "zero_base",
                        }
                    )
                else:
                    change_pct = (current - previous) / abs(previous) * 100
                compared[f"{metric}_change_pct"] = change_pct
                previous_values[metric] = current
            compared_rows.append(compared)
    return CalculationResult(
        rows=tuple(compared_rows),
        metadata={
            "operator": spec.post_compute,
            "metric_fields": list(metric_fields),
            "undefined_percentage_changes": undefined_changes,
        },
    )


def _ratio(spec: QuerySpec, rows: list[dict[str, Any]]) -> CalculationResult:
    numerator = _calculation_field(spec, "numerator")
    denominator = _calculation_field(spec, "denominator")
    output_field = _calculation_field(spec, "output_field")
    calculated_rows: list[dict[str, Any]] = []
    for row in rows:
        numerator_value = _required_row_number(row, numerator)
        denominator_value = _required_row_number(row, denominator)
        if denominator_value == 0:
            identity = row.get("stock_code") or row.get("stock_abbr") or "unknown row"
            raise CalculationError(f"Cannot compute ratio for {identity!r}: zero denominator {denominator!r}")
        calculated = dict(row)
        calculated[output_field] = numerator_value / denominator_value
        calculated_rows.append(calculated)
    return CalculationResult(
        rows=tuple(calculated_rows),
        metadata={
            "operator": spec.post_compute,
            "numerator": numerator,
            "denominator": denominator,
            "output_field": output_field,
        },
    )


def _intersection_topn(spec: QuerySpec, rows: list[dict[str, Any]]) -> CalculationResult:
    metric_fields = _metric_fields(spec)
    if len(metric_fields) < 2:
        raise CalculationError("intersection_topn requires at least two metrics")
    identities: dict[str, dict[str, Any]] = {}
    rankings: dict[str, list[str]] = {}
    for row in rows:
        identity = str(row.get("stock_code") or row.get("stock_abbr") or "")
        if not identity:
            raise CalculationError("intersection_topn requires stock_code or stock_abbr on every row")
        if identity in identities:
            raise CalculationError(f"intersection_topn received duplicate company row: {identity!r}")
        identities[identity] = row
    for metric in metric_fields:
        ranked = sorted(
            (
                (identity, _required_row_number(row, metric))
                for identity, row in identities.items()
                if row.get(metric) is not None
            ),
            key=lambda item: (-item[1], item[0]),
        )
        rankings[metric] = [identity for identity, _ in ranked[: spec.top_n]]
    common = set(rankings[metric_fields[0]])
    for metric in metric_fields[1:]:
        common.intersection_update(rankings[metric])
    selected = [row for identity, row in identities.items() if identity in common]
    return CalculationResult(
        rows=tuple(selected),
        metadata={"operator": spec.post_compute, "rankings": rankings, "top_n": spec.top_n},
    )


def _metric_fields(spec: QuerySpec) -> tuple[str, ...]:
    return spec.metrics or (spec.metric,)


def _calculation_field(spec: QuerySpec, name: str) -> str:
    value = spec.calculation.get(name)
    if not isinstance(value, str) or not value.strip():
        raise CalculationError(f"ratio calculation requires a non-empty {name!r}")
    return value.strip()


def _required_row_number(row: Mapping[str, Any], field: str) -> float:
    if field not in row or row[field] is None:
        raise CalculationError(f"Required numeric field {field!r} is missing")
    return _finite_number(row[field], field=field)


def _finite_number(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise CalculationError(f"Field {field!r} must be a finite number")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise CalculationError(f"Field {field!r} must be a finite number")
    return numeric


OPERATORS: Mapping[str, Operator] = {
    "above_industry_mean": _above_industry_mean,
    "intersection_topn": _intersection_topn,
    "period_comparison": _period_comparison,
    "ratio": _ratio,
}
