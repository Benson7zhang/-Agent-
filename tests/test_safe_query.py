from __future__ import annotations

from typing import Any

import pytest

from smart_finqa.safe_query import (
    CompiledQuery,
    QueryValidationError,
    SafeQueryExecutor,
    SessionState,
    validate_select_query,
)
from smart_finqa.sql_planner import DEFAULT_QUERY_REGISTRY, SQLPlanner


class RecordingDatabase:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, tuple[Any, ...], bool, float | None]] = []

    def query(
        self,
        sql: str,
        params: tuple[Any, ...] | None = None,
        use_cache: bool = True,
        timeout_seconds: float | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, params or (), use_cache, timeout_seconds))
        return self.rows


def test_compile_parameterizes_values_and_malicious_input() -> None:
    compiled = SQLPlanner().compile(
        {
            "query_spec": {
                "analysis_type": "single_metric",
                "metric": "total_profit",
                "table": "income_sheet",
                "stock_abbr": "金花股份'; DROP TABLE income_sheet; --",
                "report_period": "2025Q3",
                "select_fields": ["stock_abbr", "report_period", "total_profit"],
            }
        }
    )

    assert "金花股份" not in compiled.sql
    assert "DROP TABLE" not in compiled.sql
    assert compiled.params == (
        "total_profit",
        "VALIDATED",
        "consolidated",
        "duration",
        "CURRENT",
        "金花股份'; DROP TABLE income_sheet; --",
        "2025Q3",
    )
    validate_select_query(compiled, DEFAULT_QUERY_REGISTRY)


@pytest.mark.parametrize(
    ("query_spec", "message"),
    [
        (
            {
                "metric": "total_profit",
                "table": "income_sheet",
                "select_fields": ["stock_abbr", "secret_column"],
            },
            "Unknown field",
        ),
        (
            {
                "metric": "total_profit",
                "table": "income_sheet",
                "select_fields": ["stock_abbr", "total_profit"],
                "filters": [{"field": "secret_column", "op": ">", "value": 1}],
            },
            "Unknown field",
        ),
        (
            {
                "metric": "total_profit",
                "table": "income_sheet",
                "select_fields": ["stock_abbr", "total_profit"],
                "filters": [{"field": "total_profit", "op": "OR 1=1", "value": 1}],
            },
            "operator",
        ),
    ],
)
def test_planner_rejects_unknown_identifiers_and_operators(
    query_spec: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(QueryValidationError, match=message):
        SQLPlanner().compile({"query_spec": query_spec})


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT unknown_field FROM income_sheet",
        "SELECT stock_abbr FROM income_sheet WHERE unknown_field = ?",
        "SELECT stock_abbr, load_extension(?) FROM income_sheet",
        "SELECT stock_abbr || 'x' FROM income_sheet",
        "SELECT stock_abbr FROM income_sheet WHERE CASE WHEN report_period = 'bad' THEN 1 ELSE 0 END = 1",
        "SELECT stock_abbr FROM income_sheet WHERE EXISTS (SELECT 1 FROM balance_sheet)",
        "SELECT stock_abbr FROM income_sheet; DROP TABLE income_sheet",
        "SELECT stock_abbr FROM income_sheet FOR UPDATE",
        "SELECT stock_abbr FROM income_sheet FOR SHARE",
        "SELECT a.stock_abbr FROM income_sheet AS a JOIN balance_sheet AS b ON a.stock_code = b.stock_code AND 1 = 1",
        "SELECT COUNT(stock_code) FROM income_sheet GROUP BY report_period HAVING COUNT(stock_code) > 1",
        "SELECT stock_abbr FROM income_sheet LIMIT 1",
        "DELETE FROM income_sheet",
    ],
)
def test_ast_validation_rejects_unknown_expressions_and_dangerous_statements(sql: str) -> None:
    with pytest.raises(QueryValidationError):
        validate_select_query(CompiledQuery(sql=sql, params=(1,) if "?" in sql else ()), DEFAULT_QUERY_REGISTRY)


def test_ast_validation_checks_placeholder_count() -> None:
    with pytest.raises(QueryValidationError, match="placeholder"):
        validate_select_query(
            CompiledQuery(sql="SELECT stock_abbr FROM income_sheet WHERE report_period = ?", params=()),
            DEFAULT_QUERY_REGISTRY,
        )


@pytest.mark.parametrize(
    "join_conditions",
    [
        "fs.source_key = ff.source_key",
        ("fs.source_key = ff.source_key AND (fs.stock_code = ff.stock_code OR fs.period = ff.period)"),
    ],
)
def test_ast_validation_rejects_weak_trust_join(join_conditions: str) -> None:
    compiled = CompiledQuery(
        sql=(
            "SELECT ff.normalized_value FROM financial_fact AS ff "
            f"INNER JOIN financial_source AS fs ON {join_conditions} "
            "WHERE ff.validation_status = ? AND fs.authority_status = ?"
        ),
        params=("VALIDATED", "CURRENT"),
    )

    with pytest.raises(QueryValidationError, match="trust join"):
        validate_select_query(compiled, DEFAULT_QUERY_REGISTRY)


def test_ast_validation_rejects_wrong_placeholder_for_mysql() -> None:
    with pytest.raises(QueryValidationError, match="placeholder"):
        validate_select_query(
            CompiledQuery(
                sql="SELECT stock_abbr FROM income_sheet WHERE report_period = ?", params=("2025Q3",), dialect="mysql"
            ),
            DEFAULT_QUERY_REGISTRY,
        )


@pytest.mark.parametrize(
    ("backend", "placeholder"),
    [("sqlite", "?"), ("mysql", "%s")],
)
def test_compile_uses_backend_placeholder(backend: str, placeholder: str) -> None:
    compiled = SQLPlanner().compile(
        {
            "query_spec": {
                "analysis_type": "topn_metric",
                "metric": "roe",
                "table": "core_performance_indicators_sheet",
                "report_period": "2024FY",
                "select_fields": ["stock_code", "stock_abbr", "roe"],
                "top_n": 5,
            }
        },
        backend=backend,
    )

    assert compiled.sql.count(placeholder) == 7
    assert compiled.params == ("roe", "VALIDATED", "consolidated", "duration", "CURRENT", "2024FY", 5)
    validate_select_query(compiled, DEFAULT_QUERY_REGISTRY)


def test_executor_enforces_max_rows_and_records_audit() -> None:
    database = RecordingDatabase(rows=[{"stock_abbr": "金花股份"}])
    executor = SafeQueryExecutor(database, DEFAULT_QUERY_REGISTRY, max_rows=20)
    compiled = CompiledQuery(
        sql="SELECT stock_abbr FROM income_sheet WHERE report_period = ?",
        params=("2025Q3",),
    )

    rows = executor.execute(compiled, use_cache=False)

    assert rows == [{"stock_abbr": "金花股份"}]
    execution_sql, params, use_cache, timeout_seconds = database.calls[0]
    assert "SELECT * FROM (" in execution_sql
    assert "LIMIT ?" in execution_sql
    assert params == ("2025Q3", 21)
    assert use_cache is False
    assert timeout_seconds == 5.0
    assert executor.audit_log[-1].status == "success"
    assert executor.audit_log[-1].row_count == 1


def test_executor_rejects_truncated_results() -> None:
    database = RecordingDatabase(rows=[{"stock_abbr": str(index)} for index in range(3)])
    executor = SafeQueryExecutor(database, DEFAULT_QUERY_REGISTRY, max_rows=2)
    compiled = CompiledQuery(sql="SELECT stock_abbr FROM income_sheet", params=())

    with pytest.raises(QueryValidationError, match="maximum row limit"):
        executor.execute(compiled)

    assert database.calls[0][1] == (3,)
    assert executor.audit_log[-1].status == "rejected"


def test_executor_passes_explicit_timeout_to_database_adapter() -> None:
    database = RecordingDatabase()
    executor = SafeQueryExecutor(database, DEFAULT_QUERY_REGISTRY, timeout_seconds=2.5)
    compiled = CompiledQuery(sql="SELECT stock_abbr FROM income_sheet", params=())

    executor.execute(compiled, timeout_seconds=1)

    assert database.calls[0][3] == 1


def test_session_state_updates_without_mutating_previous_state() -> None:
    state = SessionState(slots={"metric": "total_profit", "periods": ["2024FY"]}, context={"turn": 1})

    updated = state.update(slots={"report_period": "2025Q3"}, context={"turn": 2})

    assert state.to_dict() == {
        "slots": {"metric": "total_profit", "periods": ["2024FY"]},
        "context": {"turn": 1},
    }
    assert updated.to_dict() == {
        "slots": {"metric": "total_profit", "periods": ["2024FY"], "report_period": "2025Q3"},
        "context": {"turn": 2},
    }
    assert state.slots["periods"] == ("2024FY",)
