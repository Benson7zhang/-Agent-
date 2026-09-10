from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping, Protocol

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from .database import FINANCIAL_FACT_TABLE, FINANCIAL_SOURCE_TABLE

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SUPPORTED_DIALECTS = frozenset({"sqlite", "mysql"})
DEFAULT_OPERATORS = frozenset({"=", ">", "<", ">=", "<="})
DEFAULT_FUNCTIONS = frozenset({"ABS", "AVG", "CASE", "COALESCE", "COUNT", "IF", "MAX", "MIN", "ROUND", "SUM"})
STRUCTURAL_FUNCTION_NODES = frozenset({"AND", "CASE", "IF"})


class QueryValidationError(ValueError):
    """Raised when a query specification or SQL statement crosses the read-only boundary."""


@dataclass(frozen=True, slots=True)
class MetricTarget:
    table: str
    field: str


@dataclass(frozen=True, slots=True)
class QueryRegistry:
    tables: Mapping[str, frozenset[str]]
    metrics: Mapping[str, MetricTarget]
    operators: frozenset[str] = DEFAULT_OPERATORS
    functions: frozenset[str] = DEFAULT_FUNCTIONS

    def __post_init__(self) -> None:
        tables = {str(table): frozenset(columns) for table, columns in self.tables.items()}
        metrics = dict(self.metrics)
        for table, columns in tables.items():
            _require_identifier(table, kind="table")
            for column in columns:
                _require_identifier(column, kind="field")
        for metric, target in metrics.items():
            _require_identifier(metric, kind="metric")
            if target.table not in tables or target.field not in tables[target.table]:
                raise QueryValidationError(f"Metric {metric!r} points outside the registered schema")
        object.__setattr__(self, "tables", MappingProxyType(tables))
        object.__setattr__(self, "metrics", MappingProxyType(metrics))

    def resolve_metric(self, metric: str) -> MetricTarget:
        try:
            return self.metrics[metric]
        except KeyError as exc:
            raise QueryValidationError(f"Unknown metric: {metric!r}") from exc

    def require_table(self, table: str) -> None:
        _require_identifier(table, kind="table")
        if table not in self.tables:
            raise QueryValidationError(f"Unknown table: {table!r}")

    def resolve_field(self, field: str, *, base_table: str) -> str:
        _require_identifier(field, kind="field")
        self.require_table(base_table)
        if field in self.tables[base_table]:
            return base_table
        sources = [table for table, columns in self.tables.items() if field in columns]
        if not sources:
            raise QueryValidationError(f"Unknown field: {field!r}")
        if len(sources) > 1:
            raise QueryValidationError(f"Ambiguous field {field!r}; qualify it through the query registry")
        return sources[0]


@dataclass(frozen=True, slots=True)
class QueryFilter:
    field: str
    op: str
    value: str | int | float

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> QueryFilter:
        field_name = payload.get("field")
        if not isinstance(field_name, str) or not field_name.strip():
            raise QueryValidationError("Query filter field must be a non-empty string")
        op = payload.get("op", "=")
        if not isinstance(op, str):
            raise QueryValidationError("Query filter operator must be a string")
        value = payload.get("value")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise QueryValidationError("Query filter value must be a string or number")
        return cls(field=field_name.strip(), op=op.strip(), value=value)


@dataclass(frozen=True, slots=True)
class QuerySpec:
    analysis_type: str
    metric: str
    metrics: tuple[str, ...] = ()
    table: str | None = None
    select_fields: tuple[str, ...] = ()
    filters: tuple[QueryFilter, ...] = ()
    stock_abbr: str = ""
    stock_code: str = ""
    report_period: str = ""
    start_period: str = ""
    end_period: str = ""
    periods: tuple[str, ...] = ()
    top_n: int = 10
    post_compute: str = ""
    calculation: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> QuerySpec:
        if not isinstance(payload, Mapping):
            raise QueryValidationError("query_spec must be a mapping")
        metric = payload.get("metric")
        if not isinstance(metric, str) or not metric.strip():
            raise QueryValidationError("query_spec.metric is required")
        metric = metric.strip()
        raw_metrics = payload.get("metrics", (metric,))
        if isinstance(raw_metrics, (str, bytes)) or not isinstance(raw_metrics, (list, tuple)):
            raise QueryValidationError("query_spec.metrics must be a list")
        metrics: list[str] = []
        for metric_name in raw_metrics:
            if not isinstance(metric_name, str) or not metric_name.strip():
                raise QueryValidationError("Each metric must be a non-empty string")
            metrics.append(metric_name.strip())
        if not metrics:
            raise QueryValidationError("query_spec.metrics must contain at least one metric")
        if metric not in metrics:
            raise QueryValidationError("query_spec.metric must be included in query_spec.metrics")

        raw_fields = payload.get("select_fields", ())
        if isinstance(raw_fields, (str, bytes)) or not isinstance(raw_fields, (list, tuple)):
            raise QueryValidationError("query_spec.select_fields must be a list")
        select_fields: list[str] = []
        for field_name in raw_fields:
            if not isinstance(field_name, str) or not field_name.strip():
                raise QueryValidationError("Each selected field must be a non-empty string")
            select_fields.append(field_name.strip())

        raw_filters = payload.get("filters", ())
        if not isinstance(raw_filters, (list, tuple)):
            raise QueryValidationError("query_spec.filters must be a list")
        filters = tuple(QueryFilter.from_mapping(item) for item in raw_filters if isinstance(item, Mapping))
        if len(filters) != len(raw_filters):
            raise QueryValidationError("Each query filter must be a mapping")

        raw_periods = payload.get("periods", ())
        if isinstance(raw_periods, (str, bytes)) or not isinstance(raw_periods, (list, tuple)):
            raise QueryValidationError("query_spec.periods must be a list")
        periods = tuple(_optional_string(item, field_name="periods") for item in raw_periods)

        raw_top_n = payload.get("top_n", 10)
        if isinstance(raw_top_n, bool):
            raise QueryValidationError("query_spec.top_n must be an integer from 1 to 100")
        try:
            top_n = int(raw_top_n)
        except (TypeError, ValueError) as exc:
            raise QueryValidationError("query_spec.top_n must be an integer from 1 to 100") from exc
        if not 1 <= top_n <= 100:
            raise QueryValidationError("query_spec.top_n must be an integer from 1 to 100")

        table = payload.get("table")
        if table is not None and (not isinstance(table, str) or not table.strip()):
            raise QueryValidationError("query_spec.table must be a non-empty string when provided")
        raw_calculation = payload.get("calculation", {})
        if not isinstance(raw_calculation, Mapping):
            raise QueryValidationError("query_spec.calculation must be a mapping")
        return cls(
            analysis_type=_optional_string(payload.get("analysis_type", "single_metric"), field_name="analysis_type"),
            metric=metric,
            metrics=tuple(metrics),
            table=table.strip() if isinstance(table, str) else None,
            select_fields=tuple(select_fields),
            filters=filters,
            stock_abbr=_optional_string(payload.get("stock_abbr", ""), field_name="stock_abbr"),
            stock_code=_optional_string(payload.get("stock_code", ""), field_name="stock_code"),
            report_period=_optional_string(payload.get("report_period", ""), field_name="report_period"),
            start_period=_optional_string(payload.get("start_period", ""), field_name="start_period"),
            end_period=_optional_string(payload.get("end_period", ""), field_name="end_period"),
            periods=periods,
            top_n=top_n,
            post_compute=_optional_string(payload.get("post_compute", ""), field_name="post_compute"),
            calculation=_freeze_mapping(raw_calculation),
        )


@dataclass(frozen=True, slots=True)
class CompiledQuery:
    sql: str
    params: tuple[Any, ...]
    dialect: str = "sqlite"

    def __post_init__(self) -> None:
        if not isinstance(self.sql, str) or not self.sql.strip():
            raise QueryValidationError("Compiled SQL must be a non-empty string")
        if self.dialect not in SUPPORTED_DIALECTS:
            raise QueryValidationError(f"Unsupported SQL dialect: {self.dialect!r}")
        object.__setattr__(self, "params", tuple(self.params))


@dataclass(frozen=True, slots=True)
class SessionState:
    """Immutable conversation state; callers must make updates explicit."""

    slots: Mapping[str, Any] = field(default_factory=dict)
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "slots", _freeze_mapping(self.slots))
        object.__setattr__(self, "context", _freeze_mapping(self.context))

    def update(
        self,
        *,
        slots: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> SessionState:
        next_slots = _thaw_mapping(self.slots)
        next_context = _thaw_mapping(self.context)
        if slots is not None:
            next_slots.update(deepcopy(dict(slots)))
        if context is not None:
            next_context.update(deepcopy(dict(context)))
        return SessionState(slots=next_slots, context=next_context)

    def to_dict(self) -> dict[str, dict[str, Any]]:
        return {
            "slots": _thaw_mapping(self.slots),
            "context": _thaw_mapping(self.context),
        }


@dataclass(frozen=True, slots=True)
class QueryAuditRecord:
    query_id: str
    timestamp: str
    dialect: str
    status: str
    parameter_count: int
    row_count: int | None = None
    error: str | None = None


class QueryDatabase(Protocol):
    def query(
        self,
        sql: str,
        params: tuple[Any, ...] | None = None,
        use_cache: bool = True,
        timeout_seconds: float | None = None,
    ) -> list[dict[str, Any]]: ...


class SafeQueryExecutor:
    """Validate and execute read-only queries with row and database time limits."""

    supports_timeout = True

    def __init__(
        self,
        database: QueryDatabase,
        registry: QueryRegistry,
        *,
        max_rows: int = 100,
        timeout_seconds: float = 5.0,
    ) -> None:
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
            raise ValueError("max_rows must be a positive integer")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int | float) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive number")
        self.database = database
        self.registry = registry
        self.max_rows = max_rows
        self.timeout_seconds = float(timeout_seconds)
        self.audit_log: list[QueryAuditRecord] = []

    def execute(
        self,
        compiled: CompiledQuery,
        *,
        timeout_seconds: float | None = None,
        use_cache: bool = True,
    ) -> list[dict[str, Any]]:
        query_id = hashlib.sha256(f"{compiled.dialect}:{compiled.sql}".encode()).hexdigest()[:16]
        effective_timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        if (
            isinstance(effective_timeout, bool)
            or not isinstance(effective_timeout, int | float)
            or effective_timeout <= 0
        ):
            raise ValueError("timeout_seconds must be a positive number")
        try:
            validate_select_query(compiled, self.registry)
        except QueryValidationError as exc:
            self.audit_log.append(
                self._audit_record(
                    query_id=query_id,
                    compiled=compiled,
                    status="rejected",
                    error=str(exc),
                )
            )
            raise
        placeholder = placeholder_for_backend(compiled.dialect)
        inner_sql = compiled.sql.strip().removesuffix(";").rstrip()
        execution_sql = f"SELECT * FROM ({inner_sql}) AS safe_query LIMIT {placeholder}"
        execution_params = (*compiled.params, self.max_rows + 1)
        try:
            rows = self.database.query(
                execution_sql,
                execution_params,
                use_cache=use_cache,
                timeout_seconds=float(effective_timeout),
            )
        except Exception as exc:
            self.audit_log.append(
                self._audit_record(
                    query_id=query_id,
                    compiled=compiled,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            raise
        if len(rows) > self.max_rows:
            error = f"Query result exceeds the maximum row limit of {self.max_rows}"
            self.audit_log.append(
                self._audit_record(
                    query_id=query_id,
                    compiled=compiled,
                    status="rejected",
                    row_count=len(rows),
                    error=error,
                )
            )
            raise QueryValidationError(error)
        self.audit_log.append(
            self._audit_record(query_id=query_id, compiled=compiled, status="success", row_count=len(rows))
        )
        return rows

    @staticmethod
    def _audit_record(
        *,
        query_id: str,
        compiled: CompiledQuery,
        status: str,
        row_count: int | None = None,
        error: str | None = None,
    ) -> QueryAuditRecord:
        return QueryAuditRecord(
            query_id=query_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            dialect=compiled.dialect,
            status=status,
            parameter_count=len(compiled.params),
            row_count=row_count,
            error=error,
        )


def placeholder_for_backend(backend: str) -> str:
    if backend == "sqlite":
        return "?"
    if backend == "mysql":
        return "%s"
    raise QueryValidationError(f"Unsupported SQL dialect: {backend!r}")


def validate_select_query(compiled: CompiledQuery, registry: QueryRegistry) -> None:
    sql = compiled.sql.strip()
    if "--" in sql or "/*" in sql or "*/" in sql:
        raise QueryValidationError("SQL comments are not allowed")
    if compiled.dialect == "mysql":
        if "?" in sql:
            raise QueryValidationError("MySQL queries must use %s placeholders")
        dialect_placeholder_count = sql.count("%s")
        normalized_sql = sql.replace("%s", "?")
    else:
        if "%s" in sql:
            raise QueryValidationError("SQLite queries must use ? placeholders")
        dialect_placeholder_count = sql.count("?")
        normalized_sql = sql
    if dialect_placeholder_count != len(compiled.params):
        raise QueryValidationError(
            f"SQL placeholder count ({dialect_placeholder_count}) does not match parameter count ({len(compiled.params)})"
        )
    try:
        statements = sqlglot.parse(normalized_sql, read=compiled.dialect)
    except ParseError as exc:
        raise QueryValidationError(f"Invalid SQL: {exc}") from exc
    if len(statements) != 1:
        raise QueryValidationError("Exactly one SQL statement is required")
    statement = statements[0]
    if not isinstance(statement, exp.Select):
        raise QueryValidationError("Only a single SELECT statement is allowed")
    if statement.args.get("locks") or statement.find(exp.Lock) is not None:
        raise QueryValidationError("Locking reads are not allowed")
    if sum(1 for _ in statement.find_all(exp.Select)) != 1 or statement.find(exp.Subquery) is not None:
        raise QueryValidationError("Subqueries and CTEs are not allowed")
    if statement.find(exp.Star) is not None:
        raise QueryValidationError("Wildcard selection is not allowed")

    tables = list(statement.find_all(exp.Table))
    if not tables:
        raise QueryValidationError("SELECT must reference a registered table")
    aliases: dict[str, str] = {}
    referenced_tables: list[str] = []
    for table_expression in tables:
        if table_expression.catalog or table_expression.db:
            raise QueryValidationError("Catalog-qualified and database-qualified tables are not allowed")
        table_name = table_expression.name
        registry.require_table(table_name)
        alias = table_expression.alias or table_name
        if alias in aliases and aliases[alias] != table_name:
            raise QueryValidationError(f"Duplicate table alias: {alias!r}")
        aliases[alias] = table_name
        referenced_tables.append(table_name)

    _require_trust_join_identity(statement, aliases)

    projection_aliases = {projection.alias for projection in statement.expressions if projection.alias}
    for column in statement.find_all(exp.Column):
        column_name = column.name
        if column.table:
            try:
                table_name = aliases[column.table]
            except KeyError as exc:
                raise QueryValidationError(f"Unknown table alias: {column.table!r}") from exc
            if column_name not in registry.tables[table_name]:
                raise QueryValidationError(f"Unknown field {column_name!r} on table {table_name!r}")
            continue
        matching_tables = {table for table in referenced_tables if column_name in registry.tables[table]}
        if len(matching_tables) == 1:
            continue
        if not matching_tables and column_name in projection_aliases and column.find_ancestor(exp.Order, exp.Group):
            continue
        if not matching_tables:
            raise QueryValidationError(f"Unknown field: {column_name!r}")
        raise QueryValidationError(f"Ambiguous unqualified field: {column_name!r}")

    for function in statement.find_all(exp.Func):
        function_name = function.name.upper() if isinstance(function, exp.Anonymous) else function.sql_name().upper()
        if function_name in STRUCTURAL_FUNCTION_NODES:
            continue
        if function_name not in registry.functions:
            raise QueryValidationError(f"Function is not allowed: {function_name or type(function).__name__}")

    for projection in statement.expressions:
        projected_value = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(projected_value, exp.Column):
            continue
        if isinstance(projected_value, exp.Func):
            function_name = (
                projected_value.name.upper()
                if isinstance(projected_value, exp.Anonymous)
                else projected_value.sql_name().upper()
            )
            if function_name in registry.functions and projected_value.find(exp.Column) is not None:
                continue
        raise QueryValidationError("SELECT expressions must be registered fields or allowed functions")

    for literal in statement.find_all(exp.Literal):
        if _is_allowed_internal_period_literal(literal):
            continue
        raise QueryValidationError("SQL values must use bound parameters")

    placeholder_count = sum(1 for _ in statement.find_all(exp.Placeholder))
    if placeholder_count != len(compiled.params):
        raise QueryValidationError(
            f"SQL placeholder count ({placeholder_count}) does not match parameter count ({len(compiled.params)})"
        )


def _require_trust_join_identity(statement: exp.Select, aliases: Mapping[str, str]) -> None:
    source_aliases = {alias for alias, table in aliases.items() if table == FINANCIAL_SOURCE_TABLE}
    fact_aliases = {alias for alias, table in aliases.items() if table == FINANCIAL_FACT_TABLE}
    if not source_aliases or not fact_aliases:
        return

    matched_sources: set[str] = set()
    matched_facts: set[str] = set()
    for join in statement.args.get("joins") or ():
        on_expression = join.args.get("on")
        if on_expression is None:
            continue
        equalities = tuple(
            expression for expression in _flatten_conjunction(on_expression) if isinstance(expression, exp.EQ)
        )
        for source_alias in source_aliases:
            for fact_alias in fact_aliases:
                if not _has_column_equality(equalities, source_alias, "source_key", fact_alias, "source_key"):
                    continue
                if not _has_column_equality(equalities, source_alias, "stock_code", fact_alias, "stock_code"):
                    raise QueryValidationError("financial trust join must match source and fact stock_code")
                if not _has_column_equality(equalities, source_alias, "period", fact_alias, "period"):
                    raise QueryValidationError("financial trust join must match source and fact period")
                matched_sources.add(source_alias)
                matched_facts.add(fact_alias)

    if matched_sources != source_aliases or matched_facts != fact_aliases:
        raise QueryValidationError("financial trust join must pair every source and fact by immutable identity")


def _flatten_conjunction(expression: exp.Expression) -> tuple[exp.Expression, ...]:
    if isinstance(expression, exp.And):
        return (*_flatten_conjunction(expression.this), *_flatten_conjunction(expression.expression))
    return (expression,)


def _has_column_equality(
    equalities: tuple[exp.EQ, ...],
    left_alias: str,
    left_column: str,
    right_alias: str,
    right_column: str,
) -> bool:
    expected = frozenset({(left_alias, left_column), (right_alias, right_column)})
    for equality in equalities:
        left = equality.this
        right = equality.expression
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
            continue
        actual = frozenset({(left.table, left.name), (right.table, right.name)})
        if actual == expected:
            return True
    return False


def _is_allowed_internal_period_literal(literal: exp.Literal) -> bool:
    period_case = literal.find_ancestor(exp.Case)
    if period_case is not None and {column.name for column in period_case.find_all(exp.Column)} == {"report_period"}:
        if literal.is_string:
            return literal.this in {"%Q1", "%Q2", "%Q3", "%FY"} and isinstance(literal.parent, exp.Like)
        return literal.this in {"0", "1", "2", "3", "4"}
    parent = literal.parent
    if literal.this == "10" and isinstance(parent, exp.Mul):
        sibling = parent.left if parent.right is literal else parent.right
        return isinstance(sibling, exp.Column) and sibling.name == "report_year"
    return False


def _require_identifier(value: str, *, kind: str) -> None:
    if not isinstance(value, str) or IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise QueryValidationError(f"Invalid {kind} identifier: {value!r}")


def _optional_string(value: Any, *, field_name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise QueryValidationError(f"query_spec.{field_name} must be a string")
    return value.strip()


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({key: _freeze(item) for key, item in deepcopy(dict(value)).items()})


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_freeze(item) for item in value)
    return value


def _thaw_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _thaw(item) for key, item in value.items()}


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _thaw_mapping(value)
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    if isinstance(value, frozenset):
        return {_thaw(item) for item in value}
    return deepcopy(value)
