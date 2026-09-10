from __future__ import annotations

from pathlib import Path

import pytest

from smart_finqa.database import FinanceDatabase
from smart_finqa.facts import ExtractedFactCandidate, FinancialFact, PeriodType, StatementScope
from smart_finqa.promotion import InvalidStagingDataError, build_promotion_plan
from smart_finqa.schema import FieldSpec, load_schema_from_xlsx
from tests.helpers import write_schema_workbook

SOURCE_CONTENT_SHA256 = "a" * 64


def _staging_database(tmp_path: Path) -> tuple[Path, dict[str, list[FieldSpec]]]:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    path = tmp_path / "staging.db"
    database = FinanceDatabase(path, schema)
    try:
        database.create_tables()
        fact = FinancialFact.from_candidate(
            ExtractedFactCandidate(
                metric="total_profit",
                raw_value=100.0,
                source_unit="万元",
                page_no=1,
                table_name="income_sheet",
                confidence=0.9,
            ),
            company_id="600080",
            stock_code="600080",
            period="2025Q3",
            statement_scope=StatementScope.CONSOLIDATED,
            period_type=PeriodType.DURATION,
            target_unit="万元",
            currency="CNY",
            source_file="temporary/job/input/report.pdf",
            source_content_sha256=SOURCE_CONTENT_SHA256,
            extractor_version="test-v1",
        )
        database.upsert_financial_facts([fact])
        database.refresh_fact_projection(
            "income_sheet",
            {
                "serial_number": 1,
                "stock_code": "600080",
                "stock_abbr": "金花股份",
                "report_period": "2025Q3",
                "report_year": 2025,
            },
        )
    finally:
        database.close()
    return path, schema


def test_build_promotion_plan_rewrites_ephemeral_source_to_stable_storage_key(tmp_path: Path) -> None:
    staging_path, schema = _staging_database(tmp_path)

    plan = build_promotion_plan(
        staging_path,
        schema,
        document_id="document-1",
        source_file="documents/document-1.pdf",
        source_content_sha256=SOURCE_CONTENT_SHA256,
        page_count=2,
    )

    assert plan.source_file == "documents/document-1.pdf"
    assert plan.source_content_sha256 == SOURCE_CONTENT_SHA256
    assert [fact.source_file for fact in plan.facts] == ["documents/document-1.pdf"]
    assert [fact.source_content_sha256 for fact in plan.facts] == [SOURCE_CONTENT_SHA256]
    assert [(seed.table, seed.as_dict()["stock_code"]) for seed in plan.projections] == [("income_sheet", "600080")]


def test_build_promotion_plan_rejects_fractional_page_numbers(tmp_path: Path) -> None:
    staging_path, schema = _staging_database(tmp_path)
    database = FinanceDatabase(staging_path, schema)
    try:
        database.execute("UPDATE financial_fact SET page_no = ?", (1.5,))
    finally:
        database.close()

    with pytest.raises(InvalidStagingDataError, match="positive integer"):
        build_promotion_plan(
            staging_path,
            schema,
            document_id="document-1",
            source_file="documents/document-1.pdf",
            source_content_sha256=SOURCE_CONTENT_SHA256,
            page_count=2,
        )


def test_build_promotion_plan_rejects_non_string_validation_issue_fields(tmp_path: Path) -> None:
    staging_path, schema = _staging_database(tmp_path)
    database = FinanceDatabase(staging_path, schema)
    try:
        database.execute(
            "UPDATE financial_fact SET validation_issues = ?",
            ('[{"code":null,"message":"bad source"}]',),
        )
    finally:
        database.close()

    with pytest.raises(InvalidStagingDataError, match="code must be a non-empty string"):
        build_promotion_plan(
            staging_path,
            schema,
            document_id="document-1",
            source_file="documents/document-1.pdf",
            source_content_sha256=SOURCE_CONTENT_SHA256,
            page_count=2,
        )


def test_build_promotion_plan_rejects_document_hash_that_differs_from_staged_source(tmp_path: Path) -> None:
    staging_path, schema = _staging_database(tmp_path)

    with pytest.raises(InvalidStagingDataError, match="source_key.*SHA-256"):
        build_promotion_plan(
            staging_path,
            schema,
            document_id="document-1",
            source_file="documents/document-1.pdf",
            source_content_sha256="b" * 64,
            page_count=2,
        )
