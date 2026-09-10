from __future__ import annotations

from dataclasses import replace

import pytest

from smart_finqa.facts import (
    ExtractedFactCandidate,
    FactValidationError,
    FinancialFact,
    IncompleteFactEvidenceError,
    PeriodType,
    StatementScope,
    ValidationIssue,
    ValidationStatus,
)

TEST_SOURCE_SHA256 = "a" * 64


def _candidate(*, page_no: int | None = 12) -> ExtractedFactCandidate:
    return ExtractedFactCandidate(
        metric="total_profit",
        raw_value="12,345.67",
        source_unit="元",
        page_no=page_no,
        table_name="合并利润表",
        row_label="利润总额",
        column_label="本期金额",
        confidence=0.82,
    )


def test_build_fact_from_candidate_normalizes_value_and_preserves_provenance() -> None:
    fact = FinancialFact.from_candidate(
        _candidate(),
        company_id="600080",
        stock_code="600080",
        period="2024Q3",
        statement_scope=StatementScope.CONSOLIDATED,
        period_type=PeriodType.DURATION,
        target_unit="万元",
        currency="CNY",
        source_file="annual-report.pdf",
        source_content_sha256=TEST_SOURCE_SHA256,
        extractor_version="regex-v1",
    )

    assert fact.normalized_value == 1.2346
    assert fact.page_no == 12
    assert fact.validation_status is ValidationStatus.NEEDS_REVIEW
    assert fact.validation_issues == ()


def test_extracted_candidate_cannot_be_declared_validated() -> None:
    with pytest.raises(FactValidationError, match="review_financial_fact"):
        FinancialFact.from_candidate(
            _candidate(),
            company_id="600080",
            stock_code="600080",
            period="2024Q3",
            statement_scope=StatementScope.CONSOLIDATED,
            period_type=PeriodType.DURATION,
            target_unit="万元",
            currency="CNY",
            source_file="annual-report.pdf",
            source_content_sha256=TEST_SOURCE_SHA256,
            extractor_version="regex-v1",
            validation_status=ValidationStatus.VALIDATED,
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("page_no", None),
        ("table_name", None),
        ("table_name", "  "),
        ("row_label", None),
        ("row_label", "  "),
        ("row_label", 123),
        ("column_label", None),
        ("column_label", "  "),
    ],
)
def test_validated_fact_requires_complete_evidence_coordinates(field_name: str, value: object) -> None:
    fact = FinancialFact.from_candidate(
        _candidate(),
        company_id="600080",
        stock_code="600080",
        period="2024Q3",
        statement_scope=StatementScope.CONSOLIDATED,
        period_type=PeriodType.DURATION,
        target_unit="万元",
        currency="CNY",
        source_file="annual-report.pdf",
        source_content_sha256=TEST_SOURCE_SHA256,
        extractor_version="regex-v1",
    )

    with pytest.raises(IncompleteFactEvidenceError, match=field_name):
        replace(
            fact,
            **{field_name: value},
            validation_status=ValidationStatus.VALIDATED,
            validation_issues=(),
        )


def test_missing_page_reference_is_marked_for_review_by_default() -> None:
    fact = FinancialFact.from_candidate(
        _candidate(page_no=None),
        company_id="600080",
        stock_code="600080",
        period="2024Q3",
        statement_scope=StatementScope.CONSOLIDATED,
        period_type=PeriodType.DURATION,
        target_unit="万元",
        currency="CNY",
        source_file="annual-report.pdf",
        source_content_sha256=TEST_SOURCE_SHA256,
        extractor_version="regex-v1",
    )

    assert fact.validation_status is ValidationStatus.NEEDS_REVIEW
    assert fact.validation_issues == (
        ValidationIssue(code="missing_page_reference", message="无法定位来源页码", field="page_no"),
    )


@pytest.mark.parametrize("raw_value", [None, "--", "not-a-number"])
def test_candidate_rejects_non_numeric_values(raw_value) -> None:
    candidate = ExtractedFactCandidate(
        metric="total_profit",
        raw_value=raw_value,
        source_unit="元",
        page_no=1,
        confidence=0.5,
    )

    with pytest.raises(FactValidationError, match="raw_value"):
        FinancialFact.from_candidate(
            candidate,
            company_id="600080",
            stock_code="600080",
            period="2024Q3",
            statement_scope=StatementScope.CONSOLIDATED,
            period_type=PeriodType.DURATION,
            target_unit="万元",
            currency="CNY",
            source_file="annual-report.pdf",
            source_content_sha256=TEST_SOURCE_SHA256,
            extractor_version="regex-v1",
        )


def test_fact_rejects_invalid_metadata() -> None:
    with pytest.raises(FactValidationError, match="stock_code"):
        FinancialFact.from_candidate(
            _candidate(),
            company_id="600080",
            stock_code="000000",
            period="2024Q3",
            statement_scope=StatementScope.CONSOLIDATED,
            period_type=PeriodType.DURATION,
            target_unit="万元",
            currency="CNY",
            source_file="annual-report.pdf",
            source_content_sha256=TEST_SOURCE_SHA256,
            extractor_version="regex-v1",
        )
