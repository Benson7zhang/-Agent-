from __future__ import annotations

import re
from typing import Any

from .core import parse_report_period
from .llm import LLMClient

INTENT_SYSTEM_PROMPT = """
你是财报问答意图识别器。返回JSON对象，字段:
- intent: single_metric|trend|topn_metric|comparison|reason
- slots: 对应槽位（metric, stock_abbr, report_period, top_n）
- need_clarify: true/false
- clarify_question: 当need_clarify=true时给出追问
只输出JSON。
""".strip()

SUBTASK_SYSTEM_PROMPT = """
你是多意图规划器。将问题拆解为可执行子任务DAG，输出JSON:
{
  "subtasks":[
    {"id":"s1","kind":"sql|retrieval|reason","goal":"...","depends_on":[]}
  ]
}
只输出JSON。
""".strip()


class TaskPlanner:
    def __init__(self, llm_client: LLMClient | None = None) -> None:
        self.llm_client = llm_client

    def parse_intent(self, question: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        context = context or {}
        if self.llm_client:
            payload = self.llm_client.complete_json(
                INTENT_SYSTEM_PROMPT,
                f"上下文:{context}\n问题:{question}",
            )
            if not isinstance(payload, dict) or "intent" not in payload:
                raise RuntimeError("Invalid LLM intent response: missing intent")
            return payload
        return self._fallback_parse_intent(question, context)

    def plan_subtasks(self, question: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        context = context or {}
        if self.llm_client:
            payload = self.llm_client.complete_json(
                SUBTASK_SYSTEM_PROMPT,
                f"上下文:{context}\n问题:{question}",
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("subtasks"), list):
                raise RuntimeError("Invalid LLM task-plan response: missing subtasks")
            return payload
        return self._fallback_plan(question)

    def _fallback_parse_intent(self, question: str, context: dict[str, Any]) -> dict[str, Any]:
        slots = dict(context.get("slots", {}))
        if "利润总额" in question or ("利润" in question and "top" in question.lower()):
            slots["metric"] = "total_profit"
        elif "主营业务收入" in question or "营业总收入" in question:
            slots["metric"] = "total_operating_revenue"
        if "top" in question.lower() or "前" in question and "企业" in question:
            m = re.search(r"top\s*(\d+)", question, flags=re.IGNORECASE)
            slots["top_n"] = int(m.group(1)) if m else 10
            intent = "topn_metric"
        elif "趋势" in question or "变化" in question or "可视化" in question or "绘图" in question:
            intent = "trend"
        elif "原因" in question or "归因" in question:
            intent = "reason"
        else:
            intent = "single_metric"

        for stock in ("金花股份", "华润三九"):
            if stock in question:
                slots["stock_abbr"] = stock
        if match := re.search(r"(\d{4})\s*年", question):
            year = int(match.group(1))
            period, _ = parse_report_period("", question)
            if period.endswith("FY") and re.fullmatch(r"\d{4}FY", period) and str(year) not in period:
                period = f"{year}FY"
            if period.endswith("FY") and "季度" not in question and "半年度" not in question:
                period = f"{year}FY"
            slots["report_period"] = period

        need_clarify = (
            intent in {"single_metric", "trend"} and "report_period" not in slots and intent == "single_metric"
        )
        clarify_question = "请补充明确的年份和报告期" if need_clarify else ""
        return {
            "intent": intent,
            "slots": slots,
            "need_clarify": need_clarify,
            "clarify_question": clarify_question,
        }

    def _fallback_plan(self, question: str) -> dict[str, Any]:
        if "？" in question or "?" in question:
            parts = [p.strip() for p in re.split(r"[？?]", question) if p.strip()]
        else:
            parts = [question]
        subtasks = []
        for idx, part in enumerate(parts, start=1):
            kind = "sql"
            if "原因" in part or "研报" in part or "医保目录" in part or "有哪些" in part:
                kind = "retrieval"
            if "最大" in part or "总结" in part:
                kind = "reason"
            subtasks.append(
                {"id": f"s{idx}", "kind": kind, "goal": part, "depends_on": [f"s{idx - 1}"] if idx > 1 else []}
            )
        if len(subtasks) == 1 and subtasks[0]["kind"] == "retrieval":
            subtasks.append({"id": "s2", "kind": "reason", "goal": "基于检索结果给出总结", "depends_on": ["s1"]})
        return {"subtasks": subtasks}
