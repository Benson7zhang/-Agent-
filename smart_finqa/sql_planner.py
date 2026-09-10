from __future__ import annotations

import re
from typing import Any, Mapping

from .database import (
    FINANCIAL_FACT_COLUMNS,
    FINANCIAL_FACT_TABLE,
    FINANCIAL_SOURCE_COLUMNS,
    FINANCIAL_SOURCE_TABLE,
)
from .facts import SourceAuthorityStatus, expected_period_type_for_table
from .safe_query import (
    CompiledQuery,
    MetricTarget,
    QueryRegistry,
    QuerySpec,
    QueryValidationError,
    placeholder_for_backend,
    validate_select_query,
)
from .task2 import TASK2_METRICS

COMMON_COLUMNS = frozenset({"stock_code", "stock_abbr", "report_period", "report_year"})
EXTRA_FIELD_TO_TABLE = {
    "asset_cash_and_cash_equivalents": "balance_sheet",
    "asset_total_assets": "balance_sheet",
    "liability_total_liabilities": "balance_sheet",
    "liability_short_term_loans": "balance_sheet",
    "equity_total_equity": "balance_sheet",
    "investing_cf_net_amount": "cash_flow_sheet",
    "net_profit_yoy_growth": "core_performance_indicators_sheet",
}
SUPPORTED_ANALYSIS_TYPES = frozenset(
    {"single_metric", "trend", "topn_metric", "comparison", "filter", "intersection_topn", "reason"}
)
REPORT_PERIOD_PATTERN = re.compile(r"^\d{4}(?:Q[1-3]|FY)$")


def _build_default_registry() -> QueryRegistry:
    tables = {
        "income_sheet": COMMON_COLUMNS
        | {
            "total_profit",
            "net_profit",
            "total_operating_revenue",
            "operating_revenue_yoy_growth",
            "operating_expense_rnd_expenses",
            "operating_expense_selling_expenses",
        },
        "balance_sheet": COMMON_COLUMNS
        | {
            "asset_liability_ratio",
            "asset_inventory",
            "asset_accounts_receivable",
            "equity_unappropriated_profit",
            "asset_cash_and_cash_equivalents",
            "asset_total_assets",
            "liability_total_liabilities",
            "liability_short_term_loans",
            "equity_total_equity",
        },
        "cash_flow_sheet": COMMON_COLUMNS | {"operating_cf_net_amount", "investing_cf_net_amount"},
        "core_performance_indicators_sheet": COMMON_COLUMNS
        | {
            "operating_revenue_qoq_growth",
            "gross_profit_margin",
            "net_profit_margin",
            "roe_weighted_excl_non_recurring",
            "roe",
            "net_profit_yoy_growth",
        },
        FINANCIAL_FACT_TABLE: frozenset(FINANCIAL_FACT_COLUMNS),
        FINANCIAL_SOURCE_TABLE: FINANCIAL_SOURCE_COLUMNS,
    }
    metrics = {key: MetricTarget(spec.table, spec.field) for key, spec in TASK2_METRICS.items()}
    metrics.update({field: MetricTarget(table, field) for field, table in EXTRA_FIELD_TO_TABLE.items()})
    return QueryRegistry(tables=tables, metrics=metrics)


DEFAULT_QUERY_REGISTRY = _build_default_registry()


class SQLPlanner:
    def __init__(self, registry: QueryRegistry | None = None) -> None:
        self.registry = registry or DEFAULT_QUERY_REGISTRY

    def compile(self, intent_payload: Mapping[str, Any], *, backend: str = "sqlite") -> CompiledQuery:
        query_spec_payload = intent_payload.get("query_spec")
        if query_spec_payload is None:
            query_spec_payload = self._legacy_query_spec(intent_payload)
        if not isinstance(query_spec_payload, Mapping):
            raise QueryValidationError("intent_payload.query_spec must be a mapping")
        query_spec = QuerySpec.from_mapping(query_spec_payload)
        compiled = self._compile_query_spec(query_spec, backend=backend)
        validate_select_query(compiled, self.registry)
        return compiled

    def build_sql(self, intent_payload: Mapping[str, Any], *, backend: str = "sqlite") -> str:
        """Compatibility wrapper returning placeholder SQL; new callers must use ``compile``."""

        return self.compile(intent_payload, backend=backend).sql

    def build_sql_and_params(
        self,
        intent_payload: Mapping[str, Any],
        *,
        backend: str = "sqlite",
    ) -> tuple[str, tuple[Any, ...]]:
        compiled = self.compile(intent_payload, backend=backend)
        return compiled.sql, compiled.params

    def _compile_query_spec(self, query_spec: QuerySpec, *, backend: str) -> CompiledQuery:
        if query_spec.analysis_type not in SUPPORTED_ANALYSIS_TYPES:
            raise QueryValidationError(f"Unknown analysis type: {query_spec.analysis_type!r}")
        metric_target = self.registry.resolve_metric(query_spec.metric)
        table = query_spec.table or metric_target.table
        self.registry.require_table(table)
        if table in {FINANCIAL_FACT_TABLE, FINANCIAL_SOURCE_TABLE}:
            raise QueryValidationError("internal trust relations are not direct query inputs")
        placeholder = placeholder_for_backend(backend)

        select_fields = query_spec.select_fields or ("stock_abbr", "report_period", metric_target.field)
        required_fields = list(select_fields)
        required_fields.extend(filter_item.field for filter_item in query_spec.filters)
        required_fields.append(metric_target.field)
        field_sources = {
            field_name: self.registry.resolve_field(field_name, base_table=table) for field_name in required_fields
        }

        joined_tables: list[str] = []
        for field_name in required_fields:
            source_table = field_sources[field_name]
            if source_table != table and source_table not in joined_tables:
                joined_tables.append(source_table)
        alias_map = {table: "t0"}
        alias_map.update({joined_table: f"t{index}" for index, joined_table in enumerate(joined_tables, start=1)})

        joins = "".join(
            f" LEFT JOIN {join_table} AS {alias_map[join_table]}"
            f" ON t0.stock_code = {alias_map[join_table]}.stock_code"
            f" AND t0.report_period = {alias_map[join_table]}.report_period"
            for join_table in joined_tables
        )
        trusted_fields = tuple(dict.fromkeys(field for field in required_fields if field not in COMMON_COLUMNS))
        trust_joins: list[str] = []
        params: list[Any] = []
        trusted_expressions: dict[str, str] = {}
        for index, field_name in enumerate(trusted_fields):
            fact_alias = f"vf{index}"
            expected_period_type = expected_period_type_for_table(field_sources[field_name]).value
            trusted_expressions[field_name] = f"{fact_alias}.normalized_value"
            trust_joins.append(
                f" INNER JOIN {FINANCIAL_FACT_TABLE} AS {fact_alias}"
                f" ON {fact_alias}.stock_code = t0.stock_code"
                f" AND {fact_alias}.period = t0.report_period"
                f" AND {fact_alias}.metric = {placeholder}"
                f" AND {fact_alias}.validation_status = {placeholder}"
                f" AND {fact_alias}.statement_scope = {placeholder}"
                f" AND {fact_alias}.period_type = {placeholder}"
            )
            source_alias = f"vs{index}"
            trust_joins.append(
                f" INNER JOIN {FINANCIAL_SOURCE_TABLE} AS {source_alias}"
                f" ON {source_alias}.source_key = {fact_alias}.source_key"
                f" AND {source_alias}.stock_code = {fact_alias}.stock_code"
                f" AND {source_alias}.period = {fact_alias}.period"
                f" AND {source_alias}.authority_status = {placeholder}"
            )
            params.extend(
                (field_name, "VALIDATED", "consolidated", expected_period_type, SourceAuthorityStatus.CURRENT.value)
            )

        field_expressions = {
            field_name: trusted_expressions.get(
                field_name,
                f"{alias_map[field_sources[field_name]]}.{field_name}",
            )
            for field_name in required_fields
        }
        select_sql = ", ".join(f"{field_expressions[field_name]} AS {field_name}" for field_name in select_fields)

        where: list[str] = []
        self._append_equality(where, params, "t0.stock_abbr", query_spec.stock_abbr, placeholder)
        self._append_equality(where, params, "t0.stock_code", query_spec.stock_code, placeholder)
        if query_spec.report_period:
            self._require_period(query_spec.report_period)
            self._append_equality(where, params, "t0.report_period", query_spec.report_period, placeholder)
        if query_spec.periods:
            for period in query_spec.periods:
                self._require_period(period)
            where.append(f"t0.report_period IN ({', '.join(placeholder for _ in query_spec.periods)})")
            params.extend(query_spec.periods)
        if query_spec.start_period:
            self._require_period(query_spec.start_period)
            where.append(f"{self._period_order_sql('t0')} >= {placeholder}")
            params.append(self._period_order_value(query_spec.start_period))
        if query_spec.end_period:
            self._require_period(query_spec.end_period)
            where.append(f"{self._period_order_sql('t0')} <= {placeholder}")
            params.append(self._period_order_value(query_spec.end_period))

        for filter_item in query_spec.filters:
            if filter_item.op not in self.registry.operators:
                raise QueryValidationError(f"Unknown query operator: {filter_item.op!r}")
            where.append(f"{field_expressions[filter_item.field]} {filter_item.op} {placeholder}")
            params.append(filter_item.value)

        where_sql = f" WHERE {' AND '.join(where)}" if where else ""
        order_sql = self._build_order_sql(
            query_spec=query_spec,
            metric_expression=field_expressions[metric_target.field],
        )
        limit_sql = ""
        if query_spec.analysis_type == "topn_metric":
            limit_sql = f" LIMIT {placeholder}"
            params.append(query_spec.top_n)
        elif query_spec.analysis_type == "reason":
            limit_sql = f" LIMIT {placeholder}"
            params.append(20)

        sql = f"SELECT {select_sql} FROM {table} AS t0{joins}{''.join(trust_joins)}{where_sql}{order_sql}{limit_sql}"
        return CompiledQuery(sql=sql, params=tuple(params), dialect=backend)

    def _build_order_sql(self, *, query_spec: QuerySpec, metric_expression: str) -> str:
        if query_spec.analysis_type in {"trend", "comparison"}:
            return f" ORDER BY {self._period_order_sql('t0')}"
        if query_spec.analysis_type == "topn_metric":
            return f" ORDER BY {metric_expression} DESC"
        if query_spec.analysis_type in {"filter", "intersection_topn"}:
            return f" ORDER BY {metric_expression} DESC, t0.stock_code"
        return f" ORDER BY {self._period_order_sql('t0')}"

    def _legacy_query_spec(self, intent_payload: Mapping[str, Any]) -> dict[str, Any]:
        intent = str(intent_payload.get("intent", "single_metric"))
        raw_slots = intent_payload.get("slots", {})
        if not isinstance(raw_slots, Mapping):
            raise QueryValidationError("intent_payload.slots must be a mapping")
        slots = dict(raw_slots)
        metric = slots.get("metric")
        if not isinstance(metric, str) or not metric:
            raise QueryValidationError("slots.metric is required")
        target = self.registry.resolve_metric(metric)
        query_spec: dict[str, Any] = {
            "analysis_type": intent,
            "metric": metric,
            "table": target.table,
            "stock_abbr": slots.get("stock_abbr", ""),
            "stock_code": slots.get("stock_code", ""),
            "report_period": slots.get("report_period", ""),
            "start_period": slots.get("start_period", ""),
            "end_period": slots.get("end_period", ""),
            "top_n": slots.get("top_n", 10),
        }
        if intent == "single_metric":
            query_spec["select_fields"] = [target.field, "stock_abbr", "report_period"]
        elif intent == "trend":
            query_spec["select_fields"] = ["report_period", target.field, "stock_abbr"]
        elif intent == "topn_metric":
            query_spec["select_fields"] = [
                "stock_code",
                "stock_abbr",
                target.field,
                "report_period",
            ]
        elif intent == "comparison":
            query_spec["select_fields"] = ["stock_code", "stock_abbr", target.field]
        elif intent == "reason":
            query_spec["select_fields"] = ["stock_code", "stock_abbr", "report_period", target.field]
        else:
            raise QueryValidationError(f"Unknown analysis type: {intent!r}")
        return query_spec

    @staticmethod
    def _append_equality(
        where: list[str],
        params: list[Any],
        column: str,
        value: str,
        placeholder: str,
    ) -> None:
        if value:
            where.append(f"{column} = {placeholder}")
            params.append(value)

    @staticmethod
    def _period_order_sql(alias: str) -> str:
        return (
            f"({alias}.report_year * 10 + CASE "
            f"WHEN {alias}.report_period LIKE '%Q1' THEN 1 "
            f"WHEN {alias}.report_period LIKE '%Q2' THEN 2 "
            f"WHEN {alias}.report_period LIKE '%Q3' THEN 3 "
            f"WHEN {alias}.report_period LIKE '%FY' THEN 4 "
            f"ELSE 0 END)"
        )

    @staticmethod
    def _period_order_value(period: str) -> int:
        year = int(period[:4])
        order = {"Q1": 1, "Q2": 2, "Q3": 3, "FY": 4}
        return year * 10 + order[period[4:]]

    @staticmethod
    def _require_period(period: str) -> None:
        if REPORT_PERIOD_PATTERN.fullmatch(period) is None:
            raise QueryValidationError(f"Invalid report period: {period!r}")
