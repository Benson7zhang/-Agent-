from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

DocumentKind = Literal["financial_report", "research_report"]
DocumentStatus = Literal["STORED", "QUEUED", "PROCESSING", "NEEDS_REVIEW", "COMPLETED", "FAILED"]
JobKind = Literal["document_ingestion"]
JobStatus = Literal["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "INTERRUPTED"]
AnswerStatus = Literal["VERIFIED", "NEEDS_CLARIFICATION", "INSUFFICIENT_EVIDENCE", "FAILED"]
TurnStatus = Literal["COMPLETED", "NEEDS_CLARIFICATION", "FAILED"]
FactStatus = Literal["VALIDATED", "NEEDS_REVIEW", "REJECTED"]
FactDecisionAction = Literal["VALIDATE", "REJECT", "CORRECT"]
Scalar = str | int | float | bool | None

NonEmptyText = Annotated[str, Field(min_length=1, max_length=500)]
EvidenceCoordinateText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
IdempotencyKey = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")]
PositiveVersion = Annotated[int, Field(ge=1)]


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class ErrorDetail(ApiModel):
    code: str
    message: str
    details: Any = None
    request_id: str


class ErrorEnvelope(ApiModel):
    error: ErrorDetail


class HealthResponse(ApiModel):
    status: Literal["ok"] = "ok"


class DocumentResponse(ApiModel):
    document_id: str
    kind: DocumentKind
    original_name: str
    sha256: str
    size_bytes: int
    mime_type: Literal["application/pdf"] = "application/pdf"
    status: DocumentStatus
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime

    @field_validator("original_name")
    @classmethod
    def validate_original_name(cls, value: str) -> str:
        if (
            not value
            or len(value) > 255
            or "/" in value
            or "\\" in value
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError("original_name must be a path-free display filename")
        return value


class DocumentRecord(DocumentResponse):
    """Internal service contract. `storage_key` must never use an HTTP response model."""

    storage_key: str

    def to_response(self) -> DocumentResponse:
        return DocumentResponse.model_validate(self.model_dump(exclude={"storage_key"}))


class DocumentListResponse(ApiModel):
    items: list[DocumentResponse]
    next_cursor: str | None = None
    total: Annotated[int, Field(ge=0)]


class JobCreateRequest(ApiModel):
    document_id: NonEmptyText
    kind: JobKind = "document_ingestion"
    idempotency_key: IdempotencyKey


class JobRetryRequest(ApiModel):
    idempotency_key: IdempotencyKey


class JobResponse(ApiModel):
    job_id: str
    document_id: str
    kind: JobKind
    status: JobStatus
    stage: str | None = None
    progress: Annotated[int, Field(ge=0, le=100)]
    attempt: Annotated[int, Field(ge=1)]
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class JobListResponse(ApiModel):
    items: list[JobResponse]
    next_cursor: str | None = None


class ConversationCreateRequest(ApiModel):
    title: Annotated[str, Field(min_length=1, max_length=100)] | None = None


class ConversationResponse(ApiModel):
    conversation_id: str
    title: str | None = None
    version: PositiveVersion
    state: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    turns: list[TurnResponse] = Field(default_factory=list)


class ConversationTurnRequest(ApiModel):
    question: Annotated[str, Field(min_length=1, max_length=4000)]
    idempotency_key: IdempotencyKey
    expected_version: PositiveVersion


class BoundingBox(ApiModel):
    x: Annotated[float, Field(ge=0, le=1)]
    y: Annotated[float, Field(ge=0, le=1)]
    width: Annotated[float, Field(gt=0, le=1)]
    height: Annotated[float, Field(gt=0, le=1)]
    coordinate_space: Literal["normalized_top_left"] = "normalized_top_left"

    @model_validator(mode="after")
    def validate_bounds(self) -> BoundingBox:
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("bbox must remain within the normalized page bounds")
        return self


class EvidenceResponse(ApiModel):
    document_id: str
    page_no: Annotated[int, Field(ge=1)]
    snippet: str
    table_name: str | None = None
    row_label: str | None = None
    column_label: str | None = None
    bbox: BoundingBox | None = None


class AnswerValue(ApiModel):
    label: str
    value: str
    unit: str
    currency: str
    company: str
    period: str
    statement_scope: Literal["consolidated", "parent"]
    fact_keys: list[str]


class ChartAxis(ApiModel):
    label: str | None = None
    categories: list[str]


class ChartSeries(ApiModel):
    name: str
    values: list[float | None]
    unit: str | None = None


class ChartSpec(ApiModel):
    type: Literal["bar", "line", "horizontal_bar"]
    title: str | None = None
    x_axis: ChartAxis
    series: list[ChartSeries]
    source_query_run_id: str

    @model_validator(mode="after")
    def validate_series_lengths(self) -> ChartSpec:
        expected = len(self.x_axis.categories)
        if any(len(series.values) != expected for series in self.series):
            raise ValueError("each chart series must match the x-axis category count")
        return self


class TableColumn(ApiModel):
    key: str
    label: str
    unit: str | None = None


class TableSpec(ApiModel):
    columns: list[TableColumn]
    rows: list[dict[str, Scalar]]


class AnswerResult(ApiModel):
    status: AnswerStatus
    text: str | None = None
    clarification: str | None = None
    company: str | None = None
    period: str | None = None
    unit: str | None = None
    currency: str | None = None
    statement_scope: Literal["consolidated", "parent"] | None = None
    formula: str | None = None
    values: list[AnswerValue] = Field(default_factory=list)
    chart_spec: ChartSpec | None = None
    table: TableSpec | None = None
    evidence: list[EvidenceResponse] = Field(default_factory=list)


class TurnResponse(ApiModel):
    turn_id: str
    conversation_id: str
    sequence: Annotated[int, Field(ge=1)]
    question: str
    status: TurnStatus
    answer: AnswerResult | None = None
    query_run_id: str | None = None
    created_at: datetime


ConversationResponse.model_rebuild()


class ConversationTurnResponse(ApiModel):
    turn: TurnResponse
    conversation: ConversationResponse


class ValidationIssueResponse(ApiModel):
    code: str
    message: str
    field: str | None = None


class FactResponse(ApiModel):
    fact_key: str
    company_id: str
    stock_code: str
    period: str
    statement_scope: Literal["consolidated", "parent"]
    period_type: Literal["instant", "duration"]
    metric: str
    raw_value: str
    normalized_value: str
    source_unit: str
    target_unit: str
    currency: str
    document_id: str
    page_no: Annotated[int, Field(ge=1)] | None = None
    table_name: str | None = None
    row_label: str | None = None
    column_label: str | None = None
    confidence: Annotated[float, Field(ge=0, le=1)]
    validation_status: FactStatus
    validation_issues: list[ValidationIssueResponse]
    version: PositiveVersion


class FactListResponse(ApiModel):
    items: list[FactResponse]
    next_cursor: str | None = None
    total: Annotated[int, Field(ge=0)]


class FactReplacement(ApiModel):
    raw_value: str
    normalized_value: str
    source_unit: NonEmptyText
    target_unit: NonEmptyText
    currency: NonEmptyText
    statement_scope: Literal["consolidated", "parent"]
    period_type: Literal["instant", "duration"]
    page_no: Annotated[int, Field(ge=1)]
    table_name: EvidenceCoordinateText
    row_label: EvidenceCoordinateText
    column_label: EvidenceCoordinateText


class FactDecisionRequest(ApiModel):
    action: FactDecisionAction
    reason: Annotated[str, Field(min_length=1, max_length=1000)]
    note: Annotated[str, Field(max_length=4000)] | None = None
    expected_version: PositiveVersion
    replacement: FactReplacement | None = None

    @model_validator(mode="after")
    def validate_replacement(self) -> FactDecisionRequest:
        if self.action == "CORRECT" and self.replacement is None:
            raise ValueError("replacement is required when action is CORRECT")
        if self.action != "CORRECT" and self.replacement is not None:
            raise ValueError("replacement is only allowed when action is CORRECT")
        return self


class FactDecisionResponse(ApiModel):
    fact: FactResponse
    event_id: str
    version: PositiveVersion


class QueryRunResponse(ApiModel):
    query_run_id: str
    conversation_id: str | None = None
    question: str
    query_spec: dict[str, Any]
    sql: str
    parameters: list[Scalar]
    answer_result: AnswerResult
    status: Literal["SUCCEEDED", "FAILED"]
    created_at: datetime


class ServiceError(RuntimeError):
    status_code = 500

    def __init__(self, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class ServiceNotFoundError(ServiceError):
    status_code = 404


class ServiceConflictError(ServiceError):
    status_code = 409


class ServiceValidationError(ServiceError):
    status_code = 422


class ServiceUnavailableError(ServiceError):
    status_code = 503


@runtime_checkable
class ApplicationService(Protocol):
    def register_document(self, document: DocumentRecord) -> DocumentRecord: ...

    def list_documents(self, *, cursor: str | None, limit: int) -> tuple[list[DocumentRecord], str | None, int]: ...

    def get_document(self, document_id: str) -> DocumentRecord: ...

    def create_job(self, request: JobCreateRequest) -> JobResponse: ...

    def list_jobs(self, *, cursor: str | None, limit: int) -> tuple[list[JobResponse], str | None]: ...

    def get_job(self, job_id: str) -> JobResponse: ...

    def retry_job(self, job_id: str, *, idempotency_key: str) -> JobResponse: ...

    def create_conversation(self, request: ConversationCreateRequest) -> ConversationResponse: ...

    def get_conversation(self, conversation_id: str) -> ConversationResponse: ...

    def create_turn(
        self,
        conversation_id: str,
        request: ConversationTurnRequest,
        *,
        request_id: str,
    ) -> ConversationTurnResponse: ...

    def list_facts(
        self,
        *,
        validation_status: str | None,
        company_id: str | None,
        period: str | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[FactResponse], str | None, int]: ...

    def get_fact(self, fact_key: str) -> FactResponse: ...

    def decide_fact(self, fact_key: str, request: FactDecisionRequest) -> FactDecisionResponse: ...

    def get_query_run(self, query_run_id: str) -> QueryRunResponse: ...

    def export_query_run(self, query_run_id: str) -> tuple[bytes, str]: ...
