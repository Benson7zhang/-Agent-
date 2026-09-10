from __future__ import annotations

from pathlib import Path

import pytest

from smart_finqa.calculations import CalculationError, apply_post_compute
from smart_finqa.safe_query import QuerySpec, SessionState
from smart_finqa.task2 import CompanyResolver, QuestionAnalyzer
from tests.helpers import write_company_workbook


def test_above_industry_mean_requires_every_metric_to_exceed_its_mean() -> None:
    query_spec = QuerySpec.from_mapping(
        {
            "analysis_type": "filter",
            "metric": "total_profit",
            "metrics": ["total_profit", "net_profit"],
            "post_compute": "above_industry_mean",
        }
    )
    rows = [
        {"stock_code": "001", "total_profit": 10.0, "net_profit": 3.0},
        {"stock_code": "002", "total_profit": 30.0, "net_profit": 4.0},
        {"stock_code": "003", "total_profit": 40.0, "net_profit": None},
    ]

    result = apply_post_compute(query_spec, rows)

    assert [row["stock_code"] for row in result.rows] == ["002"]
    assert result.metadata["operator"] == "above_industry_mean"
    assert result.metadata["industry_means"] == {"total_profit": pytest.approx(80 / 3), "net_profit": 3.5}
    assert rows[1] == {"stock_code": "002", "total_profit": 30.0, "net_profit": 4.0}


def test_period_comparison_is_chronological_and_grouped_by_company() -> None:
    query_spec = QuerySpec.from_mapping(
        {
            "analysis_type": "comparison",
            "metric": "total_profit",
            "metrics": ["total_profit"],
            "post_compute": "period_comparison",
            "periods": ["2023FY", "2024FY"],
        }
    )
    rows = [
        {"stock_code": "002", "report_period": "2024FY", "total_profit": 50.0},
        {"stock_code": "001", "report_period": "2024FY", "total_profit": 120.0},
        {"stock_code": "001", "report_period": "2023FY", "total_profit": 100.0},
        {"stock_code": "002", "report_period": "2023FY", "total_profit": 40.0},
    ]

    result = apply_post_compute(query_spec, rows)

    assert [(row["stock_code"], row["report_period"]) for row in result.rows] == [
        ("001", "2023FY"),
        ("001", "2024FY"),
        ("002", "2023FY"),
        ("002", "2024FY"),
    ]
    assert result.rows[1]["total_profit_previous_value"] == 100.0
    assert result.rows[1]["total_profit_absolute_change"] == 20.0
    assert result.rows[1]["total_profit_change_pct"] == 20.0
    assert result.rows[3]["total_profit_change_pct"] == 25.0


def test_period_comparison_exposes_undefined_percentage_for_zero_base() -> None:
    query_spec = QuerySpec.from_mapping(
        {
            "analysis_type": "comparison",
            "metric": "net_profit",
            "post_compute": "period_comparison",
        }
    )

    result = apply_post_compute(
        query_spec,
        [
            {"stock_code": "001", "report_period": "2023FY", "net_profit": 0.0},
            {"stock_code": "001", "report_period": "2024FY", "net_profit": 5.0},
        ],
    )

    assert result.rows[1]["net_profit_absolute_change"] == 5.0
    assert result.rows[1]["net_profit_change_pct"] is None
    assert result.metadata["undefined_percentage_changes"] == [
        {"stock_code": "001", "report_period": "2024FY", "metric": "net_profit", "reason": "zero_base"}
    ]


def test_ratio_uses_explicit_operands_and_fails_on_zero_denominator() -> None:
    query_spec = QuerySpec.from_mapping(
        {
            "analysis_type": "topn_metric",
            "metric": "asset_inventory",
            "metrics": ["asset_inventory", "total_operating_revenue"],
            "post_compute": "ratio",
            "calculation": {
                "numerator": "total_operating_revenue",
                "denominator": "asset_inventory",
                "output_field": "total_operating_revenue_to_asset_inventory_ratio",
            },
        }
    )
    result = apply_post_compute(
        query_spec,
        [{"stock_code": "001", "asset_inventory": 20.0, "total_operating_revenue": 100.0}],
    )

    assert result.rows[0]["total_operating_revenue_to_asset_inventory_ratio"] == 5.0

    with pytest.raises(CalculationError, match="zero denominator"):
        apply_post_compute(
            query_spec,
            [{"stock_code": "001", "asset_inventory": 0.0, "total_operating_revenue": 100.0}],
        )


def test_intersection_topn_uses_the_same_operator_entry() -> None:
    query_spec = QuerySpec.from_mapping(
        {
            "analysis_type": "intersection_topn",
            "metric": "total_profit",
            "metrics": ["total_profit", "net_profit"],
            "post_compute": "intersection_topn",
            "top_n": 2,
        }
    )
    rows = [
        {"stock_code": "001", "total_profit": 100.0, "net_profit": 20.0},
        {"stock_code": "002", "total_profit": 90.0, "net_profit": 80.0},
        {"stock_code": "003", "total_profit": 80.0, "net_profit": 70.0},
    ]

    result = apply_post_compute(query_spec, rows)

    assert [row["stock_code"] for row in result.rows] == ["002"]


def test_unknown_operator_and_invalid_numeric_value_fail_explicitly() -> None:
    with pytest.raises(CalculationError, match="Unknown post-compute operator"):
        apply_post_compute(
            QuerySpec.from_mapping(
                {"analysis_type": "filter", "metric": "total_profit", "post_compute": "does_not_exist"}
            ),
            [],
        )

    with pytest.raises(CalculationError, match="must be a finite number"):
        apply_post_compute(
            QuerySpec.from_mapping(
                {
                    "analysis_type": "comparison",
                    "metric": "total_profit",
                    "post_compute": "period_comparison",
                }
            ),
            [{"stock_code": "001", "report_period": "2024FY", "total_profit": "100"}],
        )


def test_question_analyzer_declares_ratio_operands(tmp_path: Path) -> None:
    analyzer = _build_analyzer(tmp_path)

    analysis = analyzer.analyze_turn(
        "2025年第三季度，存货排名前三的公司，其营业总收入与存货金额的比值分别是多少？",
        {},
    )

    query_spec = analysis["query_spec"]
    assert query_spec["post_compute"] == "ratio"
    assert query_spec["calculation"] == {
        "numerator": "total_operating_revenue",
        "denominator": "asset_inventory",
        "output_field": "total_operating_revenue_to_asset_inventory_ratio",
    }


def test_question_analyzer_accepts_and_returns_session_state(tmp_path: Path) -> None:
    analyzer = _build_analyzer(tmp_path)

    first = analyzer.analyze_turn("金花股份利润总额是多少", SessionState())
    second = analyzer.analyze_turn("2025年第三季度的", first["session_state"])

    assert first["need_clarify"] is True
    assert second["need_clarify"] is False
    assert second["query_spec"]["report_period"] == "2025Q3"
    assert second["session_state"].slots["metric"] == "total_profit"
    assert second["session_state"].context["last_intent"] == "single_metric"


def test_relative_period_requires_an_explicit_session_anchor(tmp_path: Path) -> None:
    analyzer = _build_analyzer(tmp_path)

    missing_anchor = analyzer.analyze_turn("金花股份近三年利润总额趋势", SessionState())
    anchored = analyzer.analyze_turn(
        "近三年利润总额趋势",
        SessionState(slots={"stock_abbr": "金花股份", "stock_code": "600080", "report_period": "2024FY"}),
    )

    assert missing_anchor["need_clarify"] is True
    assert "年份" in missing_anchor["clarify_question"]
    assert anchored["query_spec"]["start_period"] == "2022Q1"
    assert anchored["query_spec"]["end_period"] == "2024FY"


def test_topic_switch_clears_conflicting_company_context(tmp_path: Path) -> None:
    analyzer = _build_analyzer(tmp_path)
    first = analyzer.analyze_turn("金花股份2024年利润总额是多少", SessionState())

    switched = analyzer.analyze_turn("华润三九的净利润是多少", first["session_state"])

    assert switched["need_clarify"] is True
    assert switched["session_state"].slots["stock_code"] == "000999"
    assert "report_period" not in switched["session_state"].slots


def _build_analyzer(tmp_path: Path) -> QuestionAnalyzer:
    fixture_path = tmp_path / "calculation_companies.xlsx"
    write_company_workbook(fixture_path)
    return QuestionAnalyzer(company_resolver=CompanyResolver.from_xlsx(fixture_path))
