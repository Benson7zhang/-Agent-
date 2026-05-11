from __future__ import annotations

from typing import Any

from .core import report_period_order_sql
from .task2 import TASK2_METRICS


COMMON_COLUMNS = {"stock_code", "stock_abbr", "report_period", "report_year"}
EXTRA_FIELD_TO_TABLE = {
    "asset_cash_and_cash_equivalents": "balance_sheet",
    "asset_total_assets": "balance_sheet",
    "liability_total_liabilities": "balance_sheet",
    "liability_short_term_loans": "balance_sheet",
    "equity_total_equity": "balance_sheet",
    "investing_cf_net_amount": "cash_flow_sheet",
    "net_profit_yoy_growth": "core_performance_indicators_sheet",
}


class SQLPlanner:
    def __init__(self) -> None:
        self.metric_to_table_col = {
            key: (spec.table, spec.field)
            for key, spec in TASK2_METRICS.items()
        }
        self.field_to_table = {
            spec.field: spec.table
            for spec in TASK2_METRICS.values()
        }
        self.field_to_table.update(EXTRA_FIELD_TO_TABLE)
        self.table_columns = {
            "income_sheet": COMMON_COLUMNS | {
                "total_profit",
                "net_profit",
                "total_operating_revenue",
                "operating_revenue_yoy_growth",
                "operating_expense_rnd_expenses",
                "operating_expense_selling_expenses",
            },
            "balance_sheet": COMMON_COLUMNS | {
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
            "cash_flow_sheet": COMMON_COLUMNS | {
                "operating_cf_net_amount",
                "investing_cf_net_amount",
            },
            "core_performance_indicators_sheet": COMMON_COLUMNS | {
                "operating_revenue_qoq_growth",
                "gross_profit_margin",
                "net_profit_margin",
                "roe_weighted_excl_non_recurring",
                "roe",
                "net_profit_yoy_growth",
            },
        }

    def build_sql(self, intent_payload: dict[str, Any]) -> str:
        query_spec = intent_payload.get("query_spec")
        if isinstance(query_spec, dict):
            return self._build_from_query_spec(query_spec)

        """
        Build SQL query from intent payload.
        Note: This still uses string formatting for column/table names (safe as they come from whitelist)
        but should be enhanced to use parameterized queries for user input values.
        """
        intent = str(intent_payload.get("intent", "single_metric"))
        slots = dict(intent_payload.get("slots", {}))
        metric = slots.get("metric", "total_profit")
        table, column = self.metric_to_table_col.get(metric, ("income_sheet", "total_profit"))

        # Validate table and column are from whitelist
        if table not in {"income_sheet", "balance_sheet", "cash_flow_sheet", "core_performance_indicators_sheet"}:
            table = "income_sheet"

        allowed_columns = {
            "total_profit", "net_profit", "total_operating_revenue",
            "operating_revenue_yoy_growth", "asset_liability_ratio",
            "stock_code", "stock_abbr", "report_period", "report_year"
        }
        if column not in allowed_columns:
            column = "total_profit"

        if intent == "single_metric":
            stock = self._sanitize_input(slots.get("stock_abbr", ""))
            period = self._sanitize_input(slots.get("report_period", ""))
            where = []
            if stock:
                where.append(f"stock_abbr = '{stock}'")
            if period:
                where.append(f"report_period = '{period}'")
            where_sql = " AND ".join(where) if where else "1=1"
            return f"SELECT {column}, stock_abbr, report_period FROM {table} WHERE {where_sql} ORDER BY report_year, report_period"

        if intent == "trend":
            stock = self._sanitize_input(slots.get("stock_abbr", ""))
            start_period = self._sanitize_input(slots.get("start_period", "2022FY"))
            where = [f"stock_abbr = '{stock}'"] if stock else ["1=1"]
            if start_period:
                where.append(f"report_period >= '{start_period}'")
            return (
                f"SELECT report_period, {column}, stock_abbr FROM {table} "
                f"WHERE {' AND '.join(where)} ORDER BY report_year, report_period"
            )

        if intent == "topn_metric":
            period = self._sanitize_input(slots.get("report_period", "2024FY"))
            top_n = min(max(1, int(slots.get("top_n", 10))), 100)  # Limit to 1-100
            return (
                f"SELECT stock_code, stock_abbr, {column}, total_operating_revenue, operating_revenue_yoy_growth, report_period FROM {table} "
                f"WHERE report_period = '{period}' ORDER BY {column} DESC LIMIT {top_n}"
            )

        if intent == "comparison":
            period = self._sanitize_input(slots.get("report_period", "2024FY"))
            return (
                f"SELECT stock_code, stock_abbr, {column} FROM {table} "
                f"WHERE report_period = '{period}' ORDER BY {column} DESC"
            )

        # reason intent fetches factual rows first, then joined with retrieval evidence.
        return f"SELECT stock_code, stock_abbr, report_period, {column} FROM {table} ORDER BY report_year DESC LIMIT 20"

    def _build_from_query_spec(self, query_spec: dict[str, Any]) -> str:
        analysis_type = str(query_spec.get("analysis_type", "single_metric"))
        metric = str(query_spec.get("metric") or "total_profit")
        default_table, default_metric_col = self.metric_to_table_col.get(metric, ("income_sheet", "total_profit"))
        table = str(query_spec.get("table") or default_table)
        if table not in self.table_columns:
            table = default_table
        select_fields = [str(field) for field in query_spec.get("select_fields", []) if str(field).strip()]
        if not select_fields:
            select_fields = ["stock_abbr", "report_period", default_metric_col]

        base_alias = "t0"
        required_fields = set(select_fields)
        required_fields.add(default_metric_col)
        for filter_item in query_spec.get("filters", []):
            field = self._sanitize_identifier(filter_item.get("field", ""))
            if field:
                required_fields.add(field)

        field_sources = {
            field: self._resolve_field_table(field, table)
            for field in required_fields
        }
        joined_tables: list[str] = []
        for field in select_fields:
            source_table = field_sources.get(field, table)
            if source_table != table and source_table not in joined_tables:
                joined_tables.append(source_table)
        for filter_item in query_spec.get("filters", []):
            field = self._sanitize_identifier(filter_item.get("field", ""))
            source_table = field_sources.get(field, table)
            if source_table != table and source_table not in joined_tables:
                joined_tables.append(source_table)
        metric_source_table = field_sources.get(default_metric_col, table)
        if metric_source_table != table and metric_source_table not in joined_tables:
            joined_tables.append(metric_source_table)
        alias_map = {table: base_alias}
        for idx, join_table in enumerate(joined_tables, start=1):
            alias_map[join_table] = f"t{idx}"
        joins = "".join(
            f" LEFT JOIN {join_table} {alias_map[join_table]}"
            f" ON {base_alias}.stock_code = {alias_map[join_table]}.stock_code"
            f" AND {base_alias}.report_period = {alias_map[join_table]}.report_period"
            for join_table in joined_tables
        )

        where: list[str] = []
        stock_abbr = self._sanitize_input(str(query_spec.get("stock_abbr", "")))
        stock_code = self._sanitize_input(str(query_spec.get("stock_code", "")))
        report_period = self._sanitize_input(str(query_spec.get("report_period", "")))
        start_period = self._sanitize_input(str(query_spec.get("start_period", "")))
        end_period = self._sanitize_input(str(query_spec.get("end_period", "")))
        periods = [self._sanitize_input(str(item)) for item in query_spec.get("periods", []) if str(item).strip()]

        if stock_abbr:
            where.append(f"{base_alias}.stock_abbr = '{stock_abbr}'")
        if stock_code:
            where.append(f"{base_alias}.stock_code = '{stock_code}'")
        if report_period:
            where.append(f"{base_alias}.report_period = '{report_period}'")
        if periods:
            in_clause = ", ".join(f"'{item}'" for item in periods)
            where.append(f"{base_alias}.report_period IN ({in_clause})")
        if start_period:
            where.append(f"{self._qualified_period_order_sql(base_alias)} >= {self._period_order_literal(start_period)}")
        if end_period:
            where.append(f"{self._qualified_period_order_sql(base_alias)} <= {self._period_order_literal(end_period)}")

        for filter_item in query_spec.get("filters", []):
            field = self._sanitize_identifier(filter_item.get("field", ""))
            op = str(filter_item.get("op", "="))
            value = filter_item.get("value")
            if not field or op not in {"=", ">", "<", ">=", "<="}:
                continue
            if isinstance(value, (int, float)):
                where.append(f"{self._qualified_identifier(alias_map[field_sources.get(field, table)], field)} {op} {value}")

        where_sql = f" WHERE {' AND '.join(where)}" if where else ""
        select_sql = ", ".join(
            f"{self._qualified_identifier(alias_map[field_sources.get(field, table)], field)} AS {self._sanitize_identifier(field)}"
            for field in select_fields
        )
        order_sql = self._build_order_sql(
            query_spec=query_spec,
            base_alias=base_alias,
            metric_col=default_metric_col,
            metric_alias=alias_map[field_sources.get(default_metric_col, table)],
        )
        limit_sql = ""
        if analysis_type == "topn_metric":
            limit_sql = f" LIMIT {min(max(1, int(query_spec.get('top_n', 10))), 100)}"
        return f"SELECT {select_sql} FROM {table} {base_alias}{joins}{where_sql}{order_sql}{limit_sql}"

    def _build_order_sql(
        self,
        *,
        query_spec: dict[str, Any],
        base_alias: str,
        metric_col: str,
        metric_alias: str,
    ) -> str:
        analysis_type = str(query_spec.get("analysis_type", "single_metric"))
        if analysis_type in {"trend", "comparison"}:
            return f" ORDER BY {self._qualified_period_order_sql(base_alias)}"
        if analysis_type == "topn_metric":
            return f" ORDER BY {self._qualified_identifier(metric_alias, metric_col)} DESC"
        if analysis_type == "filter":
            return f" ORDER BY {self._qualified_identifier(metric_alias, metric_col)} DESC, {base_alias}.stock_code"
        return f" ORDER BY {self._qualified_period_order_sql(base_alias)}"

    def _resolve_field_table(self, field: str, base_table: str) -> str:
        if field in COMMON_COLUMNS:
            return base_table
        if field in self.table_columns.get(base_table, set()):
            return base_table
        return self.field_to_table.get(field, base_table)

    @staticmethod
    def _qualified_identifier(alias: str, field: str) -> str:
        safe_alias = SQLPlanner._sanitize_identifier(alias)
        safe_field = SQLPlanner._sanitize_identifier(field)
        return f"{safe_alias}.{safe_field}"

    @staticmethod
    def _qualified_period_order_sql(alias: str) -> str:
        safe_alias = SQLPlanner._sanitize_identifier(alias)
        return (
            f"({safe_alias}.report_year * 10 + CASE "
            f"WHEN {safe_alias}.report_period LIKE '%Q1' THEN 1 "
            f"WHEN {safe_alias}.report_period LIKE '%Q2' THEN 2 "
            f"WHEN {safe_alias}.report_period LIKE '%Q3' THEN 3 "
            f"WHEN {safe_alias}.report_period LIKE '%FY' THEN 4 "
            f"ELSE 0 END)"
        )

    @staticmethod
    def _period_order_literal(period: str) -> int:
        if len(period) < 6:
            return 0
        year = int(period[:4])
        order = {"Q1": 1, "Q2": 2, "Q3": 3, "FY": 4}
        return year * 10 + order.get(period[4:], 0)

    @staticmethod
    def _sanitize_identifier(value: str) -> str:
        if not isinstance(value, str):
            return ""
        cleaned = value.replace("`", "").strip()
        if not cleaned:
            return ""
        return "".join(ch for ch in cleaned if ch.isalnum() or ch == "_")

    @staticmethod
    def _sanitize_input(value: str) -> str:
        """Sanitize user input to prevent SQL injection."""
        if not isinstance(value, str):
            return ""
        # Remove dangerous characters
        sanitized = value.replace("'", "").replace(";", "").replace("--", "").replace("/*", "").replace("*/", "")
        # Only allow alphanumeric, Chinese characters, and safe punctuation
        import re
        sanitized = re.sub(r"[^\w\u4e00-\u9fff\s\-]", "", sanitized)
        return sanitized.strip()[:50]  # Limit length
