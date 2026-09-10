from __future__ import annotations

import re

from .core import report_period_sort_key

# Pre-compiled regex patterns for better performance
NUMBER_RE = re.compile(r"-?\d{1,3}(?:,\d{3})*(?:\.\d+)?|-?\d+\.\d+|-?\d+")
WHITESPACE_RE = re.compile(r"\s+")


def _to_float(number: str) -> float:
    return float(number.replace(",", ""))


def _extract_numbers_near_keyword(text: str, keyword: str, window: int = 140) -> list[float]:
    idx = text.find(keyword)
    if idx < 0:
        return []
    snippet = text[idx : idx + len(keyword) + window]
    return [_to_float(match.group(0)) for match in NUMBER_RE.finditer(snippet)]


def _extract_metric_candidates(text: str, keyword: str, window: int = 180) -> list[dict[str, float | int | None]]:
    normalized = WHITESPACE_RE.sub(" ", text)
    candidates: list[dict[str, float | int | None]] = []
    escaped_keyword = re.escape(keyword)
    for match in re.finditer(escaped_keyword, normalized):
        start = match.start()
        snippet = normalized[start : start + len(keyword) + window]
        line_context = normalized[max(0, start - 120) : start + len(keyword) + window]
        number_tokens = [token.group(0) for token in NUMBER_RE.finditer(snippet)]
        if not number_tokens:
            continue
        value_token = number_tokens[0]
        value = _to_float(value_token)
        yoy = None
        secondary = None
        if len(number_tokens) > 1:
            second = _to_float(number_tokens[1])
            secondary = second
            if abs(second) <= 1000:
                yoy = second
        score = 0
        if "," in value_token:
            score += 3
        if any(flag in line_context for flag in ("本报告期", "合并利润表", "项目", "前三季度", "年度")):
            score += 2
        if "归属于上市公司股东的净利润" in line_context:
            score += 1
        if any(flag in line_context for flag in ("全国规模以上", "行业", "国家统计局", "亿元")):
            score -= 2
        candidates.append(
            {
                "value": value,
                "yoy": yoy,
                "secondary": secondary,
                "score": score,
                "abs_value": abs(value),
            }
        )
    return candidates


def _pick_best_candidate(
    candidates: list[dict[str, float | int | None]],
    min_abs_value: float,
) -> tuple[float | None, float | None, float | None]:
    valid = [item for item in candidates if item["abs_value"] >= min_abs_value]
    if not valid:
        valid = candidates
    if not valid:
        return None, None, None
    valid.sort(key=lambda item: (item["score"], item["abs_value"]), reverse=True)
    best = valid[0]
    yoy = None if best["yoy"] is None else float(best["yoy"])
    secondary = None if best["secondary"] is None else float(best["secondary"])
    return float(best["value"]), yoy, secondary


def extract_metric_snapshot_from_text(text: str) -> dict[str, float | None]:
    """
    Extract key financial values from compact report lines.
    Values are kept in yuan for storage consistency and converted later as needed.
    """
    normalized = WHITESPACE_RE.sub(" ", text)

    revenue_candidates = _extract_metric_candidates(normalized, "营业收入") + _extract_metric_candidates(
        normalized, "营业总收入"
    )
    total_profit_candidates = _extract_metric_candidates(normalized, "利润总额")
    net_profit_candidates = _extract_metric_candidates(normalized, "归属于上市公司股东的净利润")
    if not net_profit_candidates:
        net_profit_candidates = _extract_metric_candidates(normalized, "净利润")
    operating_cf_candidates = _extract_metric_candidates(normalized, "经营活动产生的现金流量净额")
    asset_liability_candidates = _extract_metric_candidates(normalized, "资产负债率")
    eps_candidates = _extract_metric_candidates(normalized, "基本每股收益")

    revenue_value, revenue_yoy, revenue_secondary = _pick_best_candidate(revenue_candidates, min_abs_value=1000000)
    if (
        revenue_yoy is None
        and revenue_value
        and revenue_secondary
        and abs(revenue_secondary) > 1000
        and revenue_secondary != 0
    ):
        revenue_yoy = round((revenue_value - revenue_secondary) / revenue_secondary * 100.0, 4)

    profit_value, profit_yoy, profit_secondary = _pick_best_candidate(total_profit_candidates, min_abs_value=100000)
    if (
        profit_yoy is None
        and profit_value
        and profit_secondary
        and abs(profit_secondary) > 1000
        and profit_secondary != 0
    ):
        profit_yoy = round((profit_value - profit_secondary) / profit_secondary * 100.0, 4)

    net_profit_value, net_profit_yoy, net_profit_secondary = _pick_best_candidate(
        net_profit_candidates, min_abs_value=100000
    )
    if (
        net_profit_yoy is None
        and net_profit_value
        and net_profit_secondary
        and abs(net_profit_secondary) > 1000
        and net_profit_secondary != 0
    ):
        net_profit_yoy = round((net_profit_value - net_profit_secondary) / net_profit_secondary * 100.0, 4)

    operating_cf_value, operating_cf_yoy, _ = _pick_best_candidate(operating_cf_candidates, min_abs_value=100000)
    asset_liability_value, _, _ = _pick_best_candidate(asset_liability_candidates, min_abs_value=0)
    eps_value, _, _ = _pick_best_candidate(eps_candidates, min_abs_value=0)

    return {
        "total_operating_revenue_yuan": revenue_value,
        "operating_revenue_yoy": revenue_yoy,
        "total_profit_yuan": profit_value,
        "total_profit_yoy": profit_yoy,
        "net_profit_yuan": net_profit_value,
        "net_profit_yoy": net_profit_yoy,
        "operating_cf_net_yuan": operating_cf_value,
        "operating_cf_yoy": operating_cf_yoy,
        "asset_liability_ratio": asset_liability_value,
        "eps": eps_value,
    }


def period_sort_key(period: str) -> tuple[int, int]:
    return report_period_sort_key(period)
