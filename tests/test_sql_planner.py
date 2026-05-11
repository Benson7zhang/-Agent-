from __future__ import annotations

from smart_finqa.sql_planner import SQLPlanner


def test_build_single_metric_sql() -> None:
    planner = SQLPlanner()
    sql = planner.build_sql(
        {
            "intent": "single_metric",
            "slots": {"metric": "total_profit", "stock_abbr": "金花股份", "report_period": "2025Q3"},
        }
    )
    assert "SELECT total_profit" in sql
    assert "FROM income_sheet" in sql


def test_build_topn_sql() -> None:
    planner = SQLPlanner()
    sql = planner.build_sql(
        {
            "intent": "topn_metric",
            "slots": {"metric": "total_profit", "report_period": "2024FY", "top_n": 10},
        }
    )
    assert "ORDER BY total_profit DESC" in sql
    assert "LIMIT 10" in sql


def test_build_query_spec_topn_uses_requested_metric_for_order() -> None:
    planner = SQLPlanner()
    sql = planner.build_sql(
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
    assert "ORDER BY" in sql
    assert "roe DESC" in sql
    assert "total_profit" not in sql


def test_build_query_spec_joins_cross_table_fields() -> None:
    planner = SQLPlanner()
    sql = planner.build_sql(
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
    assert "FROM cash_flow_sheet" in sql
    assert "JOIN income_sheet" in sql
    assert "net_profit AS net_profit" in sql
    assert "net_profit > 0" in sql
