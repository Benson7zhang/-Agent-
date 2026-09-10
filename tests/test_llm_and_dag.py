from __future__ import annotations

import pytest

from smart_finqa.llm import LLMClient
from smart_finqa.planner import TaskPlanner


class _FakeLLM(LLMClient):
    def __init__(self) -> None:
        pass

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict:
        if "subtasks" in system_prompt:
            return {
                "subtasks": [
                    {"id": "s1", "kind": "sql", "goal": "查询2024利润TOP10", "depends_on": []},
                    {"id": "s2", "kind": "reason", "goal": "汇总同比最大企业", "depends_on": ["s1"]},
                ]
            }
        return {
            "intent": "single_metric",
            "slots": {"metric": "total_profit", "stock_abbr": "金花股份", "report_period": "2025Q3"},
            "need_clarify": False,
            "clarify_question": "",
        }


def test_plan_subtasks_via_llm() -> None:
    planner = TaskPlanner(llm_client=_FakeLLM())
    plan = planner.plan_subtasks("2024年利润最高top10以及同比最大企业")
    assert len(plan["subtasks"]) == 2
    assert plan["subtasks"][1]["depends_on"] == ["s1"]


def test_parse_intent_via_llm() -> None:
    planner = TaskPlanner(llm_client=_FakeLLM())
    intent = planner.parse_intent("金花股份2025年三季度利润总额是多少")
    assert intent["intent"] == "single_metric"
    assert intent["slots"]["metric"] == "total_profit"


class _FailingLLM:
    def complete_json(self, system_prompt: str, user_prompt: str) -> dict:
        raise RuntimeError("configured service unavailable")


def test_explicit_llm_failure_is_not_silently_replaced_by_rules() -> None:
    planner = TaskPlanner(llm_client=_FailingLLM())

    with pytest.raises(RuntimeError, match="configured service unavailable"):
        planner.parse_intent("金花股份利润总额")
