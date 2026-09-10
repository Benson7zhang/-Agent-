from __future__ import annotations

from smart_finqa.sql_planner import SQLPlanner


def test_build_single_metric_sql() -> None:
    planner = SQLPlanner()
    compiled = planner.compile(
        {
            "intent": "single_metric",
            "slots": {"metric": "total_profit", "stock_abbr": "金花股份", "report_period": "2025Q3"},
        }
    )
    assert "vf0.normalized_value AS total_profit" in compiled.sql
    assert "FROM income_sheet" in compiled.sql
    assert "vs0.source_key = vf0.source_key" in compiled.sql
    assert "vs0.stock_code = vf0.stock_code" in compiled.sql
    assert "vs0.period = vf0.period" in compiled.sql
    assert "vs0.source_file = vf0.source_file" not in compiled.sql
    assert compiled.params == ("total_profit", "VALIDATED", "consolidated", "duration", "CURRENT", "金花股份", "2025Q3")


def test_build_topn_sql() -> None:
    planner = SQLPlanner()
    compiled = planner.compile(
        {
            "intent": "topn_metric",
            "slots": {"metric": "total_profit", "report_period": "2024FY", "top_n": 10},
        }
    )
    assert "ORDER BY vf0.normalized_value DESC" in compiled.sql
    assert "LIMIT ?" in compiled.sql
    assert compiled.params == ("total_profit", "VALIDATED", "consolidated", "duration", "CURRENT", "2024FY", 10)


def test_build_query_spec_topn_uses_requested_metric_for_order() -> None:
    planner = SQLPlanner()
    compiled = planner.compile(
        {
            "intent": "topn_metric",
            "query_spec": {
                "analysis_type": "topn_metric",
                "table": "core_performance_indicators_sheet",
                "metric": "roe",
                "report_period": "2024FY",
                "select_fields": ["stock_code", "stock_abbr", "roe"],
                "top_n": 10,
            },
        }
    )
    assert "ORDER BY" in compiled.sql
    assert "vf0.normalized_value DESC" in compiled.sql
    assert "total_profit" not in compiled.sql
    assert compiled.params == ("roe", "VALIDATED", "consolidated", "duration", "CURRENT", "2024FY", 10)


def test_build_query_spec_joins_cross_table_fields() -> None:
    planner = SQLPlanner()
    compiled = planner.compile(
        {
            "intent": "filter",
            "query_spec": {
                "analysis_type": "filter",
                "table": "cash_flow_sheet",
                "metric": "operating_cf_net_amount",
                "report_period": "2025Q3",
                "select_fields": ["stock_code", "stock_abbr", "operating_cf_net_amount", "net_profit"],
                "filters": [
                    {"field": "operating_cf_net_amount", "op": "<", "value": 0},
                    {"field": "net_profit", "op": ">", "value": 0},
                ],
            },
        }
    )
    assert "FROM cash_flow_sheet" in compiled.sql
    assert "JOIN income_sheet" in compiled.sql
    assert "vf1.normalized_value AS net_profit" in compiled.sql
    assert "vf1.normalized_value > ?" in compiled.sql
    assert compiled.params == (
        "operating_cf_net_amount",
        "VALIDATED",
        "consolidated",
        "duration",
        "CURRENT",
        "net_profit",
        "VALIDATED",
        "consolidated",
        "duration",
        "CURRENT",
        "2025Q3",
        0,
        0,
    )
