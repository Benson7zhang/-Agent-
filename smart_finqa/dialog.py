from __future__ import annotations

import re
from typing import Any

from .core import parse_report_period


def _extract_period(text: str) -> str | None:
    period, _ = parse_report_period("", text)
    if re.search(r"\d{4}\s*年", text):
        return period
    return None


def _extract_stock(text: str) -> str | None:
    for stock in ("金花股份", "华润三九"):
        if stock in text:
            return stock
    return None


def _extract_metric(text: str) -> str | None:
    if "利润总额" in text:
        return "total_profit"
    if "营业总收入" in text or "主营业务收入" in text:
        return "total_operating_revenue"
    return None


def next_turn_action(question: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    context = dict(context or {})
    stock = _extract_stock(question)
    metric = _extract_metric(question)
    period = _extract_period(question)

    if stock:
        context["stock_abbr"] = stock
    if metric:
        context["metric"] = metric
    if period:
        context["report_period"] = period

    needs_period = context.get("metric") in {"total_profit", "total_operating_revenue"}
    if needs_period and not context.get("report_period"):
        return {
            "state": "CLARIFY",
            "content": "请问你查询哪一个报告期的数据？",
            "context": context,
        }

    if context.get("stock_abbr") and context.get("metric") and context.get("report_period"):
        return {"state": "QUERY", "content": "", "context": context}

    return {
        "state": "UNDERSTAND",
        "content": "请补充公司名称和查询指标。",
        "context": context,
    }

