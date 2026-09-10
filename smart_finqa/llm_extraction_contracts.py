from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, model_validator

PromptStage = Literal[
    "document-routing",
    "layout-recovery",
    "financial-fact-extraction",
    "cross-page-reconciliation",
    "fact-normalization-plan",
    "validation-explanation",
    "candidate-arbitration",
    "review-task-generation",
    "research-evidence-extraction",
    "schema-repair",
]
DocumentType = Literal["financial_report", "research_report", "other"]
StatementTable = Literal[
    "balance_sheet",
    "income_sheet",
    "cash_flow_sheet",
    "core_performance_indicators_sheet",
]
StatementScope = Literal["consolidated", "parent"]
PeriodType = Literal["instant", "duration"]
LocatorType = Literal["pdf_page", "docx_paragraph", "docx_table"]
CandidateDecision = Literal["NEEDS_REVIEW"]

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SemVer = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")]
ReportPeriod = Annotated[str, Field(pattern=r"^[0-9]{4}(?:Q[1-3]|FY)$")]
IsoDate = Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")]


def _reject_placeholder_stock_code(value: str) -> str:
    if value == "000000":
        raise ValueError("stock code cannot be the unknown placeholder 000000")
    return value


StockCode = Annotated[str, Field(pattern=r"^[0-9]{6}$"), AfterValidator(_reject_placeholder_stock_code)]
DecimalString = Annotated[str, Field(pattern=r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")]
PositiveIndex = Annotated[int, Field(ge=1)]
Confidence = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class ContractModel(BaseModel):
    """Base for untrusted model output: unknown fields always fail validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class IssueCode(str, Enum):
    DOCUMENT_TYPE_AMBIGUOUS = "DOCUMENT_TYPE_AMBIGUOUS"
    PROMPT_INJECTION_CONTENT = "PROMPT_INJECTION_CONTENT"
    DOCUMENT_IDENTITY_MISMATCH = "DOCUMENT_IDENTITY_MISMATCH"
    UNREADABLE_CONTENT = "UNREADABLE_CONTENT"
    OCR_CONFLICT = "OCR_CONFLICT"
    MISSING_COMPANY = "MISSING_COMPANY"
    COMPANY_CONFLICT = "COMPANY_CONFLICT"
    MISSING_STOCK_CODE = "MISSING_STOCK_CODE"
    STOCK_CODE_CONFLICT = "STOCK_CODE_CONFLICT"
    MISSING_REPORT_PERIOD = "MISSING_REPORT_PERIOD"
    REPORT_PERIOD_CONFLICT = "REPORT_PERIOD_CONFLICT"
    MISSING_STATEMENT_SCOPE = "MISSING_STATEMENT_SCOPE"
    STATEMENT_SCOPE_CONFLICT = "STATEMENT_SCOPE_CONFLICT"
    MISSING_PERIOD_TYPE = "MISSING_PERIOD_TYPE"
    PERIOD_SEMANTICS_UNSUPPORTED = "PERIOD_SEMANTICS_UNSUPPORTED"
    COLUMN_ROLE_AMBIGUOUS = "COLUMN_ROLE_AMBIGUOUS"
    MISSING_UNIT = "MISSING_UNIT"
    UNIT_CONFLICT = "UNIT_CONFLICT"
    MISSING_CURRENCY = "MISSING_CURRENCY"
    CURRENCY_CONFLICT = "CURRENCY_CONFLICT"
    TABLE_TITLE_AMBIGUOUS = "TABLE_TITLE_AMBIGUOUS"
    TABLE_HEADER_AMBIGUOUS = "TABLE_HEADER_AMBIGUOUS"
    CROSS_PAGE_AMBIGUOUS = "CROSS_PAGE_AMBIGUOUS"
    ROW_COLUMN_MISALIGNMENT = "ROW_COLUMN_MISALIGNMENT"
    VALUE_UNREADABLE = "VALUE_UNREADABLE"
    VALUE_NOT_IN_EVIDENCE = "VALUE_NOT_IN_EVIDENCE"
    VALUE_CONFLICT = "VALUE_CONFLICT"
    METRIC_UNMAPPED = "METRIC_UNMAPPED"
    DUPLICATE_CANDIDATE = "DUPLICATE_CANDIDATE"
    INCOMPLETE_EVIDENCE = "INCOMPLETE_EVIDENCE"
    DOCX_LOCATION_UNSTABLE = "DOCX_LOCATION_UNSTABLE"
    REVISION_CONFLICT = "REVISION_CONFLICT"
    BALANCE_EQUATION_FAILED = "BALANCE_EQUATION_FAILED"
    CASH_FLOW_EQUATION_FAILED = "CASH_FLOW_EQUATION_FAILED"
    CASH_ROLL_FORWARD_FAILED = "CASH_ROLL_FORWARD_FAILED"
    PERIOD_CONTINUITY_FAILED = "PERIOD_CONTINUITY_FAILED"
    REPORTED_GROWTH_MISMATCH = "REPORTED_GROWTH_MISMATCH"
    DETERMINISTIC_CHECK_UNAVAILABLE = "DETERMINISTIC_CHECK_UNAVAILABLE"
    SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"
    SCHEMA_REPAIR_APPLIED = "SCHEMA_REPAIR_APPLIED"


class Issue(ContractModel):
    code: IssueCode
    severity: Literal["error", "warning", "info"]
    field: ShortText | None
    message: NonEmptyText
    evidence_locator: ShortText | None


class BoundingBox(ContractModel):
    x: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    y: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    width: Annotated[float, Field(gt=0, le=1, allow_inf_nan=False)]
    height: Annotated[float, Field(gt=0, le=1, allow_inf_nan=False)]
    coordinate_space: Literal["normalized_top_left"]

    @model_validator(mode="after")
    def require_box_inside_page(self) -> BoundingBox:
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("bounding box must remain inside the normalized page")
        return self


class RequestManifest(ContractModel):
    request_id: Identifier
    document_id: Identifier
    source_file_name: ShortText
    source_content_sha256: Sha256
    stage: PromptStage
    prompt_version: SemVer
    input_scope_id: Identifier
    allowed_region_ids: list[Identifier]
    allowed_candidate_ids: list[Identifier]
    extractor_version: Identifier
    input_locator_type: Literal["pdf_page", "docx_block"]
    metric_catalog_version: Identifier
    payload: dict[str, Any]


PayloadT = TypeVar("PayloadT", bound=BaseModel)


class StageEnvelope(ContractModel, Generic[PayloadT]):
    schema_version: Literal["1.0.0"]
    request_id: Identifier
    document_id: Identifier
    source_content_sha256: Sha256
    stage: PromptStage
    prompt_version: SemVer
    input_scope_id: Identifier
    status: Literal["OK", "PARTIAL", "REFUSED"]
    payload: PayloadT
    issues: list[Issue]


class IdentityMismatchError(ValueError):
    """A model response did not echo the immutable request identity."""


def assert_envelope_identity(envelope: StageEnvelope[Any], manifest: RequestManifest) -> None:
    compared_fields = (
        "request_id",
        "document_id",
        "source_content_sha256",
        "stage",
        "prompt_version",
        "input_scope_id",
    )
    mismatches = [name for name in compared_fields if getattr(envelope, name) != getattr(manifest, name)]
    if mismatches:
        raise IdentityMismatchError(f"model response identity mismatch: {', '.join(mismatches)}")


class LocatedTextEvidence(ContractModel):
    locator: ShortText
    text: NonEmptyText


class CompanyCandidate(ContractModel):
    company_name: ShortText
    stock_code: StockCode | None
    locator: ShortText
    evidence_text: NonEmptyText


class SelectedCompany(ContractModel):
    company_name: ShortText
    stock_code: StockCode
    selection_reason: NonEmptyText


class ReportPeriodCandidate(ContractModel):
    report_period: ReportPeriod
    locator: ShortText
    evidence_text: NonEmptyText


class DocumentSection(ContractModel):
    kind: Literal["financial_statements", "research_content", "other"]
    start_locator: ShortText
    end_locator: ShortText


class DocumentRoutingPayload(ContractModel):
    document_id: Identifier
    document_type: DocumentType | None
    report_kind: Literal["annual", "semiannual", "q1", "q3", "other", "unknown"]
    document_type_evidence: list[LocatedTextEvidence]
    company_candidates: list[CompanyCandidate]
    selected_company: SelectedCompany | None
    report_period_candidates: list[ReportPeriodCandidate]
    selected_report_period: ReportPeriod | None
    publication_date: IsoDate | None
    sections: list[DocumentSection]
    route: Literal["EXTRACT_FINANCIAL_FACTS", "EXTRACT_RESEARCH_EVIDENCE", "STOP_NEEDS_REVIEW"]
    confidence: Confidence
    issues: list[Issue]

    @model_validator(mode="after")
    def require_route_metadata(self) -> DocumentRoutingPayload:
        if self.route == "EXTRACT_FINANCIAL_FACTS":
            if self.document_type != "financial_report":
                raise ValueError("financial extraction route requires document_type=financial_report")
            if self.selected_company is None or self.selected_report_period is None:
                raise ValueError("financial extraction route requires selected company and report period")
        if self.route == "EXTRACT_RESEARCH_EVIDENCE" and self.document_type != "research_report":
            raise ValueError("research extraction route requires document_type=research_report")
        return self


class HeaderRow(ContractModel):
    row_index: PositiveIndex
    cells: list[str]


class DataColumn(ContractModel):
    column_index: PositiveIndex
    column_label: ShortText
    column_role: Literal[
        "current_period",
        "comparative_prior_period",
        "opening_balance",
        "closing_balance",
        "other",
        "unknown",
    ]
    period_hint: ReportPeriod | None


class RowRange(ContractModel):
    start: PositiveIndex
    end: PositiveIndex

    @model_validator(mode="after")
    def require_ordered_range(self) -> RowRange:
        if self.end < self.start:
            raise ValueError("row range end must be greater than or equal to start")
        return self


class ContinuationCandidate(ContractModel):
    continues_from_fragment_id: Identifier | None
    may_continue_to_next: bool
    evidence: list[NonEmptyText]


class TableCell(ContractModel):
    column_index: PositiveIndex
    raw_text: Annotated[str, StringConstraints(max_length=4000)]
    value_kind: Literal["row_label", "data_value", "note_reference", "text", "empty", "unreadable"]
    region_id: Identifier
    bbox: BoundingBox | None

    @model_validator(mode="after")
    def require_empty_cell_consistency(self) -> TableCell:
        if self.value_kind == "empty" and self.raw_text:
            raise ValueError("empty table cells cannot contain raw_text")
        if self.value_kind != "empty" and not self.raw_text.strip():
            raise ValueError("non-empty table cells require raw_text")
        return self


class TableDataRow(ContractModel):
    row_index: PositiveIndex
    row_path: list[ShortText]
    cells: list[TableCell]


class TableFragment(ContractModel):
    fragment_id: Identifier
    locator_type: LocatorType
    start_locator: ShortText
    end_locator: ShortText
    physical_page_numbers: list[PositiveIndex]
    table_index: PositiveIndex | None
    table_name: ShortText | None
    statement_table: StatementTable | None
    statement_scope: StatementScope | None
    source_unit: ShortText | None
    currency: ShortText | None
    header_rows: list[HeaderRow]
    data_columns: list[DataColumn]
    row_range: RowRange | None
    data_rows: list[TableDataRow]
    repeated_header_rows: list[PositiveIndex]
    excluded_regions: list[NonEmptyText]
    continuation: ContinuationCandidate
    confidence: Confidence
    issues: list[Issue]

    @model_validator(mode="after")
    def require_locator_consistency(self) -> TableFragment:
        if self.locator_type == "pdf_page" and not self.physical_page_numbers:
            raise ValueError("PDF fragments require physical_page_numbers")
        if self.locator_type != "pdf_page" and self.physical_page_numbers:
            raise ValueError("DOCX fragments cannot claim physical PDF page numbers")
        return self


class UnreadableRegion(ContractModel):
    locator: ShortText
    reason: NonEmptyText


class LayoutRecoveryPayload(ContractModel):
    document_id: Identifier
    fragments: list[TableFragment]
    unreadable_regions: list[UnreadableRegion]
    issues: list[Issue]


class PeriodSemantics(ContractModel):
    as_of_date: IsoDate | None
    period_start: IsoDate | None
    period_end: IsoDate | None
    basis: Literal["instant", "quarter", "ytd", "annual", "unknown"]
    column_role: Literal[
        "current_period",
        "comparative_prior_period",
        "opening_balance",
        "closing_balance",
        "other",
        "unknown",
    ]


class FactSource(ContractModel):
    source_file_name: ShortText
    source_content_sha256: Sha256
    locator_type: LocatorType
    region_id: Identifier
    page_no: PositiveIndex | None
    paragraph_no: PositiveIndex | None
    table_name: ShortText | None
    table_index: PositiveIndex | None
    row_index: PositiveIndex | None
    column_index: PositiveIndex | None
    bbox: BoundingBox | None
    row_label: ShortText | None
    column_label: ShortText | None
    evidence_text: NonEmptyText

    @model_validator(mode="after")
    def require_source_coordinates(self) -> FactSource:
        if self.locator_type == "pdf_page" and self.page_no is None:
            raise ValueError("pdf_page evidence requires page_no")
        if self.locator_type == "docx_paragraph" and self.paragraph_no is None:
            raise ValueError("docx_paragraph evidence requires paragraph_no")
        if self.locator_type == "docx_table" and None in (self.table_index, self.row_index, self.column_index):
            raise ValueError("docx_table evidence requires table, row, and column indices")
        if self.locator_type != "pdf_page" and self.page_no is not None:
            raise ValueError("DOCX evidence cannot claim a physical PDF page")
        return self


class P2FactCandidate(ContractModel):
    document_id: Identifier
    document_type: Literal["financial_report"]
    company_id: Identifier | None
    company_name: ShortText | None
    stock_code: StockCode | None
    report_period: ReportPeriod | None
    statement_table: StatementTable
    statement_scope: StatementScope | None
    period_type: PeriodType | None
    period_semantics: PeriodSemantics
    metric: Identifier
    raw_value: ShortText
    normalized_value: None
    source_unit: ShortText | None
    target_unit: ShortText | None
    currency: ShortText | None
    conversion: None
    source: FactSource
    row_path: list[ShortText]
    negative_interpretation: bool
    confidence: Confidence
    candidate_decision: CandidateDecision
    issues: list[Issue]


class ComparisonValue(ContractModel):
    metric: Identifier
    raw_value: ShortText
    column_role: Literal["comparative_prior_period", "opening_balance", "other"]
    period_hint: ReportPeriod | None
    source_unit: ShortText | None
    region_id: Identifier
    locator: ShortText
    evidence_text: NonEmptyText


class UnresolvedRow(ContractModel):
    row_label: ShortText
    locator: ShortText
    reason: NonEmptyText
    issue_code: Literal[IssueCode.METRIC_UNMAPPED]


class FinancialFactExtractionPayload(ContractModel):
    document_id: Identifier
    fragment_id: Identifier
    facts: list[P2FactCandidate]
    comparison_values: list[ComparisonValue]
    unresolved_rows: list[UnresolvedRow]
    issues: list[Issue]


class FragmentGroup(ContractModel):
    group_id: Identifier
    fragment_ids: list[Identifier]
    action: Literal["MERGE", "KEEP_SEPARATE"]
    evidence: list[NonEmptyText]
    confidence: Confidence
    issues: list[Issue]


class CandidateAssessment(ContractModel):
    candidate_ids: list[Identifier]
    assessment: Literal["POSSIBLE_DUPLICATE", "POSSIBLE_CONFLICT", "DISTINCT"]
    reason: NonEmptyText
    issues: list[Issue]


class CrossPageReconciliationPayload(ContractModel):
    document_id: Identifier
    fragment_groups: list[FragmentGroup]
    candidate_assessments: list[CandidateAssessment]
    issues: list[Issue]


class ConversionPlan(ContractModel):
    parsed_decimal_proposal: DecimalString
    operation: Literal["multiply"]
    factor: DecimalString
    rule_id: Identifier


class NormalizationFactPlan(ContractModel):
    candidate_id: Identifier
    raw_value: ShortText
    normalized_value: None
    source_unit: ShortText | None
    target_unit: ShortText | None
    currency: ShortText | None
    report_period: ReportPeriod | None
    statement_scope: StatementScope | None
    period_type: PeriodType | None
    conversion: ConversionPlan | None
    candidate_decision: CandidateDecision
    issues: list[Issue]


class FactNormalizationPayload(ContractModel):
    document_id: Identifier
    facts: list[NormalizationFactPlan]
    issues: list[Issue]


class ValidationExplanation(ContractModel):
    check_id: Identifier
    status: Literal["passed", "failed", "unavailable"]
    issue_code: IssueCode | None
    summary: NonEmptyText
    related_candidate_ids: list[Identifier]
    review_steps: list[NonEmptyText]
    candidate_decision: CandidateDecision


class ValidationExplanationPayload(ContractModel):
    document_id: Identifier
    checks: list[ValidationExplanation]
    issues: list[Issue]


class FactBusinessKey(ContractModel):
    stock_code: StockCode
    report_period: ReportPeriod
    statement_scope: StatementScope
    period_type: PeriodType
    metric: Identifier


class ArbitrationGroup(ContractModel):
    business_key: FactBusinessKey
    candidate_ids: list[Identifier]
    selected_candidate_id: Identifier | None
    action: Literal["SELECT_FOR_REVIEW", "REVIEW_CONFLICT", "REJECT_ALL_UNSUPPORTED"]
    reason_codes: list[Identifier]
    reason: NonEmptyText
    candidate_decision: CandidateDecision
    issues: list[Issue]

    @model_validator(mode="after")
    def require_valid_selection(self) -> ArbitrationGroup:
        if self.action == "SELECT_FOR_REVIEW":
            if self.selected_candidate_id is None or self.selected_candidate_id not in self.candidate_ids:
                raise ValueError("selected candidate must be one of candidate_ids")
        elif self.selected_candidate_id is not None:
            raise ValueError("non-selection arbitration actions require selected_candidate_id=null")
        return self


class CandidateArbitrationPayload(ContractModel):
    document_id: Identifier
    groups: list[ArbitrationGroup]
    issues: list[Issue]


class ReviewReason(ContractModel):
    code: IssueCode
    field: ShortText | None
    message: NonEmptyText


class ReviewAction(ContractModel):
    locator: ShortText
    action: NonEmptyText


class FactReviewRecommendation(ContractModel):
    candidate_id: Identifier
    persistence_status: Literal["NEEDS_REVIEW", "DO_NOT_PERSIST"]
    review_priority: Literal["P0", "P1", "P2"]
    review_reasons: list[ReviewReason]
    review_actions: list[ReviewAction]


class ReviewSummary(ContractModel):
    candidate_count: Annotated[int, Field(ge=0)]
    persist_needs_review_count: Annotated[int, Field(ge=0)]
    do_not_persist_count: Annotated[int, Field(ge=0)]
    error_count: Annotated[int, Field(ge=0)]


class ReviewTaskGenerationPayload(ContractModel):
    document_id: Identifier
    document_recommendation: Literal["NEEDS_REVIEW", "FAILED"]
    facts: list[FactReviewRecommendation]
    summary: ReviewSummary
    issues: list[Issue]

    @model_validator(mode="after")
    def require_consistent_counts(self) -> ReviewTaskGenerationPayload:
        needs_review = sum(item.persistence_status == "NEEDS_REVIEW" for item in self.facts)
        do_not_persist = sum(item.persistence_status == "DO_NOT_PERSIST" for item in self.facts)
        if self.summary.candidate_count != len(self.facts):
            raise ValueError("candidate_count must equal the number of fact recommendations")
        if self.summary.persist_needs_review_count != needs_review:
            raise ValueError("persist_needs_review_count does not match fact recommendations")
        if self.summary.do_not_persist_count != do_not_persist:
            raise ValueError("do_not_persist_count does not match fact recommendations")
        return self


class ResearchClaim(ContractModel):
    claim_id: Identifier
    claim_type: Literal["reported_fact", "forecast", "opinion", "causal_claim", "risk_statement"]
    subject_company: ShortText | None
    subject_stock_code: StockCode | None
    industry: ShortText | None
    claim_text: NonEmptyText
    forecast_period: ReportPeriod | None
    locator_type: LocatorType
    page_no: PositiveIndex | None
    paragraph_no: PositiveIndex | None
    table_index: PositiveIndex | None
    row_index: PositiveIndex | None
    column_index: PositiveIndex | None
    evidence_text: NonEmptyText
    confidence: Confidence
    abstained: bool
    issues: list[Issue]

    @model_validator(mode="after")
    def require_claim_coordinates(self) -> ResearchClaim:
        if self.locator_type == "pdf_page" and self.page_no is None:
            raise ValueError("PDF research evidence requires page_no")
        if self.locator_type == "docx_paragraph" and self.paragraph_no is None:
            raise ValueError("DOCX paragraph evidence requires paragraph_no")
        if self.locator_type == "docx_table" and None in (self.table_index, self.row_index, self.column_index):
            raise ValueError("DOCX table evidence requires table, row, and column indices")
        if self.locator_type != "pdf_page" and self.page_no is not None:
            raise ValueError("DOCX research evidence cannot claim a physical PDF page")
        return self


class ResearchEvidenceExtractionPayload(ContractModel):
    document_id: Identifier
    institution: ShortText | None
    publication_date: IsoDate | None
    claims: list[ResearchClaim]
    issues: list[Issue]


class ModifiedField(ContractModel):
    json_pointer: ShortText
    operation: Identifier
    reason: NonEmptyText


class RejectedRecord(ContractModel):
    json_pointer: ShortText
    reason: NonEmptyText


class SchemaRepairPayload(ContractModel):
    repaired_response: dict[str, Any]
    modified_fields: list[ModifiedField]
    rejected_records: list[RejectedRecord]
    issues: list[Issue]
