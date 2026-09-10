"""Validated transfer objects for promoting one staged financial report."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .database import (
    FINANCIAL_FACT_COLUMNS,
    FINANCIAL_FACT_CONTROL_COLUMNS,
    FINANCIAL_FACT_TABLE,
    PROJECTION_KEY_COLUMNS,
)
from .facts import (
    FinancialFact,
    PeriodType,
    StatementScope,
    ValidationIssue,
    ValidationStatus,
    build_source_key,
    expected_period_type_for_table,
)
from .schema import FieldSpec


@dataclass(frozen=True, slots=True)
class ProjectionSeed:
    table: str
    values: tuple[tuple[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return dict(self.values)


@dataclass(frozen=True, slots=True)
class ArtifactManifest:
    kind: str
    storage_key: str
    sha256: str
    size_bytes: int
    mime_type: str


@dataclass(frozen=True, slots=True)
class PromotionPlan:
    document_id: str
    source_file: str
    source_content_sha256: str
    page_count: int
    facts: tuple[FinancialFact, ...]
    projections: tuple[ProjectionSeed, ...]


class InvalidStagingDataError(ValueError):
    """The staging database does not satisfy the promotion contract."""


def build_promotion_plan(
    staging_db_path: Path,
    schema: Mapping[str, list[FieldSpec]],
    *,
    document_id: str,
    source_file: str,
    source_content_sha256: str,
    page_count: int,
) -> PromotionPlan:
    """Read and validate a job-local SQLite database without mutating authority data."""
    if not document_id.strip() or not source_file.strip():
        raise ValueError("document_id and source_file must be non-empty")
    build_source_key(source_file, source_content_sha256)
    if isinstance(page_count, bool) or not isinstance(page_count, int) or page_count < 1:
        raise ValueError("page_count must be a positive integer")
    resolved = staging_db_path.resolve(strict=True)
    if not resolved.is_file():
        raise InvalidStagingDataError("staging database must be a regular file")

    connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        _validate_table_columns(connection, FINANCIAL_FACT_TABLE, _expected_fact_columns())
        facts = _load_facts(
            connection,
            schema,
            source_file=source_file,
            source_content_sha256=source_content_sha256,
            page_count=page_count,
        )
        if not facts:
            raise InvalidStagingDataError("staging database contains no financial fact candidates")
        projections = _load_projection_seeds(connection, schema, facts)
    except sqlite3.DatabaseError as exc:
        raise InvalidStagingDataError(f"invalid staging database: {exc}") from exc
    finally:
        connection.close()
    return PromotionPlan(
        document_id=document_id,
        source_file=source_file,
        source_content_sha256=source_content_sha256,
        page_count=page_count,
        facts=tuple(facts),
        projections=tuple(projections),
    )


def _expected_fact_columns() -> set[str]:
    return set(FINANCIAL_FACT_COLUMNS) | set(FINANCIAL_FACT_CONTROL_COLUMNS)


def _validate_table_columns(connection: sqlite3.Connection, table: str, expected: set[str]) -> None:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    if not rows:
        raise InvalidStagingDataError(f"staging table is missing: {table}")
    actual = {str(row["name"]) for row in rows}
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise InvalidStagingDataError(
            f"staging table {table!r} has an incompatible schema; missing={missing}, unknown={unknown}"
        )


def _load_facts(
    connection: sqlite3.Connection,
    schema: Mapping[str, list[FieldSpec]],
    *,
    source_file: str,
    source_content_sha256: str,
    page_count: int,
) -> list[FinancialFact]:
    selected_columns = [column for column in FINANCIAL_FACT_COLUMNS if column != "fact_key"]
    rows = connection.execute(
        f"SELECT {', '.join(selected_columns)} FROM {FINANCIAL_FACT_TABLE} ORDER BY fact_key"
    ).fetchall()
    metric_tables: dict[str, set[str]] = {}
    for table, fields in schema.items():
        for field in fields:
            metric_tables.setdefault(field.field_name, set()).add(table)

    facts: list[FinancialFact] = []
    for row in rows:
        staged_source_file = str(row["source_file"])
        expected_staged_source_key = build_source_key(staged_source_file, source_content_sha256)
        if str(row["source_key"]) != expected_staged_source_key:
            raise InvalidStagingDataError("staged fact source_key does not match the document content SHA-256")
        status = str(row["validation_status"])
        if status != ValidationStatus.NEEDS_REVIEW.value:
            raise InvalidStagingDataError(f"staged facts must be NEEDS_REVIEW, got {status!r}")
        page_no = row["page_no"]
        if page_no is not None:
            if isinstance(page_no, bool) or not isinstance(page_no, int) or page_no < 1:
                raise InvalidStagingDataError("fact page_no must be a positive integer when provided")
            if page_no > page_count:
                raise InvalidStagingDataError(f"fact page_no {page_no} exceeds document page_count {page_count}")
        table_name = row["table_name"]
        if table_name not in schema:
            raise InvalidStagingDataError(f"fact references an unknown table: {table_name!r}")
        metric = str(row["metric"])
        if table_name not in metric_tables.get(metric, set()):
            raise InvalidStagingDataError(f"metric {metric!r} does not belong to table {table_name!r}")
        period_type = PeriodType(str(row["period_type"]))
        if period_type is not expected_period_type_for_table(str(table_name)):
            raise InvalidStagingDataError(f"fact period_type {period_type.value!r} does not match table {table_name!r}")
        issues = _parse_validation_issues(str(row["validation_issues"]))
        facts.append(
            FinancialFact(
                company_id=str(row["company_id"]),
                stock_code=str(row["stock_code"]),
                period=str(row["period"]),
                statement_scope=StatementScope(str(row["statement_scope"])),
                period_type=period_type,
                metric=metric,
                raw_value=str(row["raw_value"]),
                normalized_value=float(row["normalized_value"]),
                source_unit=str(row["source_unit"]),
                target_unit=str(row["target_unit"]),
                currency=str(row["currency"]),
                source_file=source_file,
                source_content_sha256=source_content_sha256,
                page_no=page_no,
                table_name=str(table_name),
                row_label=row["row_label"],
                column_label=row["column_label"],
                confidence=float(row["confidence"]),
                validation_status=ValidationStatus.NEEDS_REVIEW,
                validation_issues=issues,
                extractor_version=str(row["extractor_version"]),
            )
        )
    return facts


def _parse_validation_issues(raw: str) -> tuple[ValidationIssue, ...]:
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidStagingDataError("fact validation_issues is not valid JSON") from exc
    if not isinstance(values, list):
        raise InvalidStagingDataError("fact validation_issues must be a JSON array")
    issues: list[ValidationIssue] = []
    for value in values:
        if not isinstance(value, dict) or set(value) - {"code", "message", "field"}:
            raise InvalidStagingDataError("fact validation_issues contains an invalid object")
        try:
            code = value["code"]
            message = value["message"]
            field = value.get("field")
            if not isinstance(code, str) or not code.strip():
                raise InvalidStagingDataError("fact validation issue code must be a non-empty string")
            if not isinstance(message, str) or not message.strip():
                raise InvalidStagingDataError("fact validation issue message must be a non-empty string")
            if field is not None and (not isinstance(field, str) or not field.strip()):
                raise InvalidStagingDataError("fact validation issue field must be a non-empty string when provided")
            issues.append(
                ValidationIssue(
                    code=code,
                    message=message,
                    field=field,
                )
            )
        except KeyError as exc:
            raise InvalidStagingDataError("fact validation issue requires code and message") from exc
    return tuple(issues)


def _load_projection_seeds(
    connection: sqlite3.Connection,
    schema: Mapping[str, list[FieldSpec]],
    facts: list[FinancialFact],
) -> list[ProjectionSeed]:
    required_keys = {(str(fact.table_name), fact.stock_code, fact.period) for fact in facts}
    seeds: list[ProjectionSeed] = []
    found_keys: set[tuple[str, str, str]] = set()
    for table, fields in schema.items():
        expected_columns = {field.field_name for field in fields}
        _validate_table_columns(connection, table, expected_columns)
        key_columns = [field.field_name for field in fields if field.field_name in PROJECTION_KEY_COLUMNS]
        if "stock_code" not in key_columns or "report_period" not in key_columns:
            raise InvalidStagingDataError(f"projection table {table!r} is missing its business key")
        rows = connection.execute(f"SELECT {', '.join(key_columns)} FROM {table}").fetchall()
        for row in rows:
            key = (table, str(row["stock_code"]), str(row["report_period"]))
            if key not in required_keys:
                continue
            if key in found_keys:
                raise InvalidStagingDataError(f"duplicate projection seed for {key}")
            found_keys.add(key)
            seeds.append(ProjectionSeed(table=table, values=tuple((column, row[column]) for column in key_columns)))
    missing = sorted(required_keys - found_keys)
    if missing:
        raise InvalidStagingDataError(f"facts have no matching projection seed: {missing}")
    return seeds
