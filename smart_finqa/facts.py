from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from enum import Enum

from .core import normalize_numeric

REPORT_PERIOD_PATTERN = re.compile(r"^\d{4}(?:Q[1-3]|FY)$")
STOCK_CODE_PATTERN = re.compile(r"^\d{6}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
UNKNOWN_METADATA_VALUES = {"000000", "unknown", "未知", "未知公司"}
SUPPORTED_UNIT_CONVERSIONS = {("元", "万元"), ("万元", "元")}


class FactValidationError(ValueError):
    """A financial fact violates the trusted fact-layer contract."""


class IncompleteFactEvidenceError(FactValidationError):
    """A fact cannot be trusted because its source coordinates are incomplete."""


class UntrustedFactError(RuntimeError):
    """A query result cannot be backed by reviewed financial facts."""


class ValidationStatus(str, Enum):
    VALIDATED = "VALIDATED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    REJECTED = "REJECTED"


class SourceAuthorityStatus(str, Enum):
    CURRENT = "CURRENT"
    SUPERSEDED = "SUPERSEDED"
    REMOVED = "REMOVED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class SourceOwner(str, Enum):
    BATCH = "BATCH"
    WEB = "WEB"
    LEGACY = "LEGACY"


class StatementScope(str, Enum):
    CONSOLIDATED = "consolidated"
    PARENT = "parent"


class PeriodType(str, Enum):
    INSTANT = "instant"
    DURATION = "duration"


STATEMENT_PERIOD_TYPES = {
    "balance_sheet": PeriodType.INSTANT,
    "income_sheet": PeriodType.DURATION,
    "cash_flow_sheet": PeriodType.DURATION,
    "core_performance_indicators_sheet": PeriodType.DURATION,
}


@dataclass(frozen=True, slots=True)
class AuthoritativeSource:
    """One report source selected to supply facts for a company and period."""

    source_file: str
    source_content_sha256: str
    stock_code: str
    period: str
    source_owner: SourceOwner = SourceOwner.BATCH
    document_id: str | None = None
    selection_version: str = ""

    def __post_init__(self) -> None:
        _require_text("source_file", self.source_file)
        _require_sha256("source_content_sha256", self.source_content_sha256)
        _require_known_metadata("stock_code", self.stock_code)
        if not STOCK_CODE_PATTERN.fullmatch(self.stock_code) or self.stock_code == "000000":
            raise FactValidationError("stock_code must be a recognized six-digit code")
        if not REPORT_PERIOD_PATTERN.fullmatch(self.period):
            raise FactValidationError("period must use YYYYQ1, YYYYQ2, YYYYQ3, or YYYYFY format")
        if self.document_id is not None:
            _require_text("document_id", self.document_id)
        object.__setattr__(self, "source_owner", _coerce_enum("source_owner", self.source_owner, SourceOwner))
        if self.source_owner is SourceOwner.WEB and self.document_id is None:
            raise FactValidationError("WEB authoritative source requires document_id")
        _require_text("selection_version", self.selection_version)

    @property
    def source_key(self) -> str:
        return build_source_key(self.source_file, self.source_content_sha256)


def build_source_key(source_file: str, source_content_sha256: str) -> str:
    _require_text("source_file", source_file)
    _require_sha256("source_content_sha256", source_content_sha256)
    identity = f"smart-finqa:source:v2\x1f{source_file}\x1f{source_content_sha256}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def expected_period_type_for_table(table: str) -> PeriodType:
    try:
        return STATEMENT_PERIOD_TYPES[table]
    except KeyError as exc:
        raise ValueError(f"unknown financial statement table: {table!r}") from exc


def require_validated_evidence(
    *,
    page_no: int | None,
    table_name: str | None,
    row_label: str | None,
    column_label: str | None,
) -> None:
    """Require complete PDF coordinates before a fact can become trusted."""
    _validate_optional_page_no(page_no)
    missing = [
        field_name
        for field_name, value in (
            ("table_name", table_name),
            ("row_label", row_label),
            ("column_label", column_label),
        )
        if not isinstance(value, str) or not value.strip()
    ]
    if page_no is None:
        missing.insert(0, "page_no")
    if missing:
        raise IncompleteFactEvidenceError(
            f"VALIDATED fact requires complete evidence coordinates; missing: {', '.join(missing)}"
        )


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    message: str
    field: str | None = None

    def __post_init__(self) -> None:
        _require_text("validation issue code", self.code)
        _require_text("validation issue message", self.message)


@dataclass(frozen=True, slots=True)
class ExtractedFactCandidate:
    """A value candidate produced by extraction before validation or persistence."""

    metric: str
    raw_value: str | int | float | None
    source_unit: str
    page_no: int | None
    table_name: str | None = None
    row_label: str | None = None
    column_label: str | None = None
    confidence: float = 0.0

    def __post_init__(self) -> None:
        _require_text("metric", self.metric)
        _require_text("source_unit", self.source_unit)
        _validate_optional_page_no(self.page_no)
        _validate_confidence(self.confidence)


@dataclass(frozen=True, slots=True)
class FinancialFact:
    """Canonical financial value with its business context and source provenance."""

    company_id: str
    stock_code: str
    period: str
    statement_scope: StatementScope
    period_type: PeriodType
    metric: str
    raw_value: str | int | float
    normalized_value: float
    source_unit: str
    target_unit: str
    currency: str
    source_file: str
    source_content_sha256: str
    page_no: int | None
    table_name: str | None
    row_label: str | None
    column_label: str | None
    confidence: float
    validation_status: ValidationStatus
    validation_issues: tuple[ValidationIssue, ...]
    extractor_version: str

    def __post_init__(self) -> None:
        _require_known_metadata("company_id", self.company_id)
        _require_known_metadata("stock_code", self.stock_code)
        if not STOCK_CODE_PATTERN.fullmatch(self.stock_code) or self.stock_code == "000000":
            raise FactValidationError("stock_code must be a recognized six-digit code")
        if not REPORT_PERIOD_PATTERN.fullmatch(self.period):
            raise FactValidationError("period must use YYYYQ1, YYYYQ2, YYYYQ3, or YYYYFY format")

        object.__setattr__(
            self, "statement_scope", _coerce_enum("statement_scope", self.statement_scope, StatementScope)
        )
        object.__setattr__(self, "period_type", _coerce_enum("period_type", self.period_type, PeriodType))
        object.__setattr__(
            self,
            "validation_status",
            _coerce_enum("validation_status", self.validation_status, ValidationStatus),
        )

        for field_name in ("metric", "source_unit", "target_unit", "currency", "source_file", "extractor_version"):
            _require_text(field_name, getattr(self, field_name))
        _require_sha256("source_content_sha256", self.source_content_sha256)
        _validate_numeric("raw_value", self.raw_value)
        _validate_numeric("normalized_value", self.normalized_value)
        _validate_optional_page_no(self.page_no)
        _validate_confidence(self.confidence)

        issues = tuple(self.validation_issues)
        if any(not isinstance(issue, ValidationIssue) for issue in issues):
            raise FactValidationError("validation_issues must contain ValidationIssue values")
        object.__setattr__(self, "validation_issues", issues)

        if self.validation_status is ValidationStatus.VALIDATED:
            require_validated_evidence(
                page_no=self.page_no,
                table_name=self.table_name,
                row_label=self.row_label,
                column_label=self.column_label,
            )
            if issues:
                raise FactValidationError("a VALIDATED fact cannot retain validation_issues")
        if self.validation_status is ValidationStatus.REJECTED and not issues:
            raise FactValidationError("a REJECTED fact must include at least one validation issue")

    @classmethod
    def from_candidate(
        cls,
        candidate: ExtractedFactCandidate,
        *,
        company_id: str,
        stock_code: str,
        period: str,
        statement_scope: StatementScope,
        period_type: PeriodType,
        target_unit: str,
        currency: str,
        source_file: str,
        source_content_sha256: str,
        extractor_version: str,
        validation_status: ValidationStatus = ValidationStatus.NEEDS_REVIEW,
        validation_issues: tuple[ValidationIssue, ...] = (),
    ) -> FinancialFact:
        """Build a canonical fact without guessing missing units, values, or provenance."""
        try:
            requested_status = ValidationStatus(validation_status)
        except (TypeError, ValueError) as exc:
            raise FactValidationError(f"unknown candidate validation status: {validation_status!r}") from exc
        if requested_status is not ValidationStatus.NEEDS_REVIEW:
            raise FactValidationError(
                "extracted candidates must start as NEEDS_REVIEW; use review_financial_fact for review decisions"
            )
        if (
            candidate.source_unit != target_unit
            and (candidate.source_unit, target_unit) not in SUPPORTED_UNIT_CONVERSIONS
        ):
            raise FactValidationError(f"unsupported unit conversion: {candidate.source_unit!r} to {target_unit!r}")
        normalized_value = normalize_numeric(
            candidate.raw_value,
            source_unit=candidate.source_unit,
            target_unit=target_unit,
        )
        if normalized_value is None:
            raise FactValidationError("raw_value must be a finite numeric value")

        issues = tuple(validation_issues)
        if candidate.page_no is None and not any(issue.field == "page_no" for issue in issues):
            issues += (
                ValidationIssue(
                    code="missing_page_reference",
                    message="无法定位来源页码",
                    field="page_no",
                ),
            )

        return cls(
            company_id=company_id,
            stock_code=stock_code,
            period=period,
            statement_scope=statement_scope,
            period_type=period_type,
            metric=candidate.metric,
            raw_value=candidate.raw_value,
            normalized_value=normalized_value,
            source_unit=candidate.source_unit,
            target_unit=target_unit,
            currency=currency,
            source_file=source_file,
            source_content_sha256=source_content_sha256,
            page_no=candidate.page_no,
            table_name=candidate.table_name,
            row_label=candidate.row_label,
            column_label=candidate.column_label,
            confidence=candidate.confidence,
            validation_status=ValidationStatus.NEEDS_REVIEW,
            validation_issues=issues,
            extractor_version=extractor_version,
        )

    @property
    def source_key(self) -> str:
        return build_source_key(self.source_file, self.source_content_sha256)


def _require_text(field_name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise FactValidationError(f"{field_name} must be a non-empty string")


def _require_sha256(field_name: str, value: object) -> None:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise FactValidationError(f"{field_name} must be a lowercase SHA-256 hex digest")


def _require_known_metadata(field_name: str, value: object) -> None:
    _require_text(field_name, value)
    if str(value).strip().lower() in UNKNOWN_METADATA_VALUES:
        raise FactValidationError(f"{field_name} cannot be an unknown placeholder")


def _validate_numeric(field_name: str, value: object) -> None:
    if isinstance(value, bool):
        raise FactValidationError(f"{field_name} must be a finite numeric value")
    normalized = normalize_numeric(value) if isinstance(value, str) else value
    if not isinstance(normalized, (int, float)) or not math.isfinite(float(normalized)):
        raise FactValidationError(f"{field_name} must be a finite numeric value")


def _validate_optional_page_no(page_no: int | None) -> None:
    if page_no is not None and (isinstance(page_no, bool) or not isinstance(page_no, int) or page_no < 1):
        raise FactValidationError("page_no must be a positive integer when provided")


def _validate_confidence(confidence: float) -> None:
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise FactValidationError("confidence must be between 0 and 1")
    if not math.isfinite(float(confidence)) or not 0 <= confidence <= 1:
        raise FactValidationError("confidence must be between 0 and 1")


def _coerce_enum(field_name: str, value: object, enum_type: type[Enum]) -> Enum:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(str(item.value) for item in enum_type)
        raise FactValidationError(f"{field_name} must be one of: {allowed}") from exc
