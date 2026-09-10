from __future__ import annotations

from typing import Any

from .core import report_period_sort_key

METRIC_LABELS = {
    "total_profit": "利润总额",
    "net_profit": "净利润",
    "total_operating_revenue": "营业总收入",
    "operating_revenue_yoy_growth": "营业收入同比",
    "asset_liability_ratio": "资产负债率",
    "operating_revenue_qoq_growth": "营业总收入环比增长率",
    "operating_cf_net_amount": "经营性现金流量净额",
    "operating_expense_rnd_expenses": "研发费用",
    "operating_expense_selling_expenses": "销售费用",
    "gross_profit_margin": "销售毛利率",
    "net_profit_margin": "销售净利率",
    "asset_inventory": "存货",
    "asset_accounts_receivable": "应收账款",
    "equity_unappropriated_profit": "未分配利润",
    "roe_weighted_excl_non_recurring": "加权平均净资产收益率（扣非）",
    "roe": "净资产收益率",
}

METRIC_UNITS = {
    "total_profit": "万元",
    "net_profit": "万元",
    "total_operating_revenue": "万元",
    "operating_revenue_yoy_growth": "%",
    "asset_liability_ratio": "%",
    "operating_revenue_qoq_growth": "%",
    "operating_cf_net_amount": "万元",
    "operating_expense_rnd_expenses": "万元",
    "operating_expense_selling_expenses": "万元",
    "gross_profit_margin": "%",
    "net_profit_margin": "%",
    "asset_inventory": "万元",
    "asset_accounts_receivable": "万元",
    "equity_unappropriated_profit": "万元",
    "roe_weighted_excl_non_recurring": "%",
    "roe": "%",
}


def _fmt_num(value: Any, ndigits: int = 2) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, (int, float)):
        return f"{value:.{ndigits}f}"
    return str(value)


def format_single_metric_answer(slots: dict[str, Any], row: dict[str, Any]) -> str:
    metric = str(slots.get("metric", "total_profit"))
    label = METRIC_LABELS.get(metric, metric)
    unit = METRIC_UNITS.get(metric, "")
    stock = str(slots.get("stock_abbr", "目标公司"))
    period = str(slots.get("report_period", "指定报告期"))
    value = row.get(metric)
    if value is None:
        fallback_col = next(
            (
                k
                for k in row.keys()
                if k not in {"stock_abbr", "stock_code", "report_period", "report_year"} and row.get(k) is not None
            ),
            None,
        )
        if fallback_col:
            metric = fallback_col
            label = METRIC_LABELS.get(metric, metric)
            unit = METRIC_UNITS.get(metric, "")
            value = row.get(metric)
    suffix = unit if unit else ""
    return f"{stock} 在 {period} 的{label}为 {_fmt_num(value)}{suffix}。"


def format_trend_analysis_answer(rows: list[dict[str, Any]], metric_col: str, metric_label: str) -> str:
    clean = [r for r in rows if isinstance(r.get(metric_col), (int, float))]
    if not clean:
        return f"未检索到可用于分析{metric_label}趋势的数据。"
    ordered = sorted(clean, key=lambda r: report_period_sort_key(str(r.get("report_period", ""))))
    periods = [str(r.get("report_period", "")) for r in ordered]
    values = [float(r.get(metric_col, 0.0)) for r in ordered]
    start, end = values[0], values[-1]
    trend = "上升" if end >= start else "下降"
    min_idx = values.index(min(values))
    max_idx = values.index(max(values))
    head = f"{metric_label}在 {periods[0]} 至 {periods[-1]} 整体呈{trend}趋势。"
    range_part = (
        f"区间最小值为 {values[min_idx]:.2f}（{periods[min_idx]}），"
        f"最大值为 {values[max_idx]:.2f}（{periods[max_idx]}）。"
    )
    tail_points = "；".join(f"{p}:{v:.2f}" for p, v in zip(periods[-3:], values[-3:]))
    return f"{head}{range_part}最近区间数据点为 {tail_points}。"


def format_topn_analysis_answer(rows: list[dict[str, Any]], metric_col: str, metric_label: str) -> str:
    if not rows:
        return f"未检索到{metric_label}TOP数据。"
    dedup: dict[str, dict[str, Any]] = {}
    for item in rows:
        name = str(item.get("stock_abbr") or item.get("stock_code") or "未知公司")
        old = dedup.get(name)
        if old is None:
            dedup[name] = item
            continue
        old_metric = float(old.get(metric_col, 0) or 0)
        new_metric = float(item.get(metric_col, 0) or 0)
        if new_metric > old_metric:
            dedup[name] = item
            continue
        # If metric equals, keep richer yoy.
        if old.get("operating_revenue_yoy_growth") is None and item.get("operating_revenue_yoy_growth") is not None:
            dedup[name] = item

    ranked = sorted(dedup.values(), key=lambda r: float(r.get(metric_col, 0) or 0), reverse=True)
    lines: list[str] = []
    best_yoy_name = None
    best_yoy_val = None
    for item in ranked[:10]:
        name = str(item.get("stock_abbr") or item.get("stock_code") or "未知公司")
        metric_val = _fmt_num(item.get(metric_col), 2)
        yoy = item.get("operating_revenue_yoy_growth")
        if isinstance(yoy, (int, float)):
            yoy_text = f"{yoy:.2f}%"
            if best_yoy_val is None or yoy > best_yoy_val:
                best_yoy_val = float(yoy)
                best_yoy_name = name
        else:
            yoy_text = "N/A"
        lines.append(f"{name}：{metric_label} {metric_val}，销售额同比 {yoy_text}")
    summary = "；".join(lines)
    if best_yoy_name is not None:
        summary += f"。其中销售额同比涨幅最大的是 {best_yoy_name}（{best_yoy_val:.2f}%）。"
    return summary


def format_reason_with_references(question: str, sql_rows_count: int, references: list[dict[str, Any]]) -> str:
    refs = references[:3]
    if not refs:
        return f"基于结构化数据分析，围绕“{question}”共检索到 {sql_rows_count} 条财务记录，但缺少研报证据支撑。"
    snippet = ""
    source = ""
    for item in refs:
        text = str(item.get("quote") or item.get("text") or "").strip().replace("\n", " ")
        if len(text) > 90:
            text = text[:90] + "..."
        if text:
            snippet = text
            path = str(item.get("paper_path") or "")
            page_no = item.get("page_no")
            source = f"（来源：{path}，第{page_no}页）" if path and page_no else ""
            break
    return (
        f"围绕“{question}”，共检索到 {sql_rows_count} 条结构化财务记录，"
        f"并匹配到 {len(refs)} 条研报证据。"
        f"核心证据显示：{snippet}{source}"
    )
