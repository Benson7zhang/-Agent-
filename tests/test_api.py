from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from smart_finqa.api import ApiSettings, create_app
from smart_finqa.api_models import (
    ConversationResponse,
    ConversationTurnResponse,
    DocumentRecord,
    FactDecisionResponse,
    FactResponse,
    JobResponse,
    QueryRunResponse,
    ServiceConflictError,
    ServiceNotFoundError,
)
from smart_finqa.application import ApplicationService as QuestionApplicationService
from smart_finqa.application import WebApplicationFacade
from smart_finqa.database import FinanceDatabaseFactory
from smart_finqa.facts import ExtractedFactCandidate, FinancialFact, PeriodType, StatementScope
from smart_finqa.schema import load_schema_from_xlsx
from smart_finqa.task2 import CompanyRecord, CompanyResolver, QuestionAnalyzer
from smart_finqa.web_store import WebStore
from tests.helpers import write_schema_workbook

NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)
PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


class FakeApplicationService:
    def __init__(self, storage_root: Path) -> None:
        self.storage_root = storage_root
        self.documents: dict[str, DocumentRecord] = {}
        self.jobs: dict[str, JobResponse] = {}
        self.conversations: dict[str, ConversationResponse] = {}
        self.facts: dict[str, FactResponse] = {"fact-1": _fact()}
        self.query_runs: dict[str, QueryRunResponse] = {"query-1": _query_run()}
        self.last_turn: dict[str, Any] | None = None
        self.last_turn_request_id: str | None = None
        self.last_decision: dict[str, Any] | None = None

    def register_document(self, document: DocumentRecord) -> DocumentRecord:
        self.documents[document.document_id] = document
        return document

    def list_documents(self, *, cursor: str | None, limit: int) -> tuple[list[DocumentRecord], str | None, int]:
        items = list(self.documents.values())
        offset = int(cursor or 0)
        page = items[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(items) else None
        return page, next_cursor, len(items)

    def get_document(self, document_id: str) -> DocumentRecord:
        try:
            return self.documents[document_id]
        except KeyError as exc:
            raise ServiceNotFoundError("document_not_found", "文档不存在") from exc

    def create_job(self, request: Any) -> JobResponse:
        job = _job(document_id=request.document_id)
        self.jobs[job.job_id] = job
        return job

    def list_jobs(self, *, cursor: str | None, limit: int) -> tuple[list[JobResponse], str | None]:
        return list(self.jobs.values())[:limit], None

    def get_job(self, job_id: str) -> JobResponse:
        try:
            return self.jobs[job_id]
        except KeyError as exc:
            raise ServiceNotFoundError("job_not_found", "任务不存在") from exc

    def retry_job(self, job_id: str, *, idempotency_key: str) -> JobResponse:
        old = self.get_job(job_id)
        retried = _job(document_id=old.document_id, job_id="job-2", attempt=2)
        self.jobs[retried.job_id] = retried
        return retried

    def create_conversation(self, request: Any) -> ConversationResponse:
        conversation = _conversation(title=request.title)
        self.conversations[conversation.conversation_id] = conversation
        return conversation

    def get_conversation(self, conversation_id: str) -> ConversationResponse:
        try:
            return self.conversations[conversation_id]
        except KeyError as exc:
            raise ServiceNotFoundError("conversation_not_found", "会话不存在") from exc

    def create_turn(self, conversation_id: str, request: Any, *, request_id: str) -> ConversationTurnResponse:
        conversation = self.get_conversation(conversation_id)
        self.last_turn = request.model_dump()
        self.last_turn_request_id = request_id
        updated = conversation.model_copy(update={"version": conversation.version + 1})
        turn = {
            "turn_id": "turn-1",
            "conversation_id": conversation_id,
            "sequence": 1,
            "question": request.question,
            "status": "NEEDS_CLARIFICATION",
            "answer": {
                "status": "NEEDS_CLARIFICATION",
                "text": None,
                "clarification": "请补充报告期",
                "values": [],
                "evidence": [],
            },
            "query_run_id": None,
            "created_at": NOW,
        }
        updated = ConversationResponse.model_validate({**updated.model_dump(), "turns": [*updated.turns, turn]})
        self.conversations[conversation_id] = updated
        return ConversationTurnResponse(
            turn=turn,
            conversation=updated,
        )

    def list_facts(
        self,
        *,
        validation_status: str | None,
        company_id: str | None,
        period: str | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[FactResponse], str | None, int]:
        items = list(self.facts.values())
        if validation_status:
            items = [item for item in items if item.validation_status == validation_status]
        offset = int(cursor or 0)
        page = items[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(items) else None
        return page, next_cursor, len(items)

    def get_fact(self, fact_key: str) -> FactResponse:
        try:
            return self.facts[fact_key]
        except KeyError as exc:
            raise ServiceNotFoundError("fact_not_found", "事实不存在") from exc

    def decide_fact(self, fact_key: str, request: Any) -> FactDecisionResponse:
        fact = self.get_fact(fact_key)
        self.last_decision = request.model_dump()
        if request.expected_version != fact.version:
            raise ServiceConflictError(
                "fact_version_conflict",
                "事实已被其他审核操作更新",
                {"expected_version": request.expected_version, "current_version": fact.version},
            )
        updated = fact.model_copy(update={"validation_status": "VALIDATED", "version": fact.version + 1})
        self.facts[fact_key] = updated
        return FactDecisionResponse(fact=updated, event_id="event-1", version=updated.version)

    def get_query_run(self, query_run_id: str) -> QueryRunResponse:
        try:
            return self.query_runs[query_run_id]
        except KeyError as exc:
            raise ServiceNotFoundError("query_run_not_found", "查询记录不存在") from exc

    def export_query_run(self, query_run_id: str) -> tuple[bytes, str]:
        self.get_query_run(query_run_id)
        return b"PK\x03\x04xlsx", "query-result.xlsx"


@pytest.fixture
def api(tmp_path: Path) -> tuple[TestClient, FakeApplicationService]:
    service = FakeApplicationService(tmp_path)
    app = create_app(
        service,
        ApiSettings(storage_root=tmp_path, max_upload_bytes=128, cors_origins=("http://127.0.0.1:5173",)),
    )
    return TestClient(app), service


def test_health_has_request_id(api: tuple[TestClient, FakeApplicationService]) -> None:
    client, _ = api

    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-request-id"]


def test_upload_stores_uuid_name_and_never_exposes_storage_path(
    api: tuple[TestClient, FakeApplicationService],
) -> None:
    client, service = api

    response = client.post(
        "/api/v1/documents",
        data={"kind": "financial_report"},
        files={"file": ("2024年报.pdf", PDF_BYTES, "application/pdf")},
    )

    assert response.status_code == 201
    payload = response.json()
    assert "storage_key" not in payload
    assert "source_file" not in payload
    record = service.documents[payload["document_id"]]
    stored_path = service.storage_root / record.storage_key
    assert stored_path.name == f"{record.document_id}.pdf"
    assert stored_path.read_bytes() == PDF_BYTES
    assert payload["sha256"]


@pytest.mark.parametrize(
    ("filename", "content_type", "content", "code"),
    [
        ("bad.txt", "application/pdf", PDF_BYTES, "invalid_file_extension"),
        ("../bad.pdf", "application/pdf", PDF_BYTES, "invalid_filename"),
        ("bad.pdf", "text/plain", PDF_BYTES, "invalid_mime_type"),
        ("bad.pdf", "application/pdf", b"not a pdf", "invalid_pdf_signature"),
        ("large.pdf", "application/pdf", PDF_BYTES * 10, "upload_too_large"),
    ],
)
def test_upload_rejects_invalid_pdf_explicitly(
    api: tuple[TestClient, FakeApplicationService],
    filename: str,
    content_type: str,
    content: bytes,
    code: str,
) -> None:
    client, _ = api

    response = client.post(
        "/api/v1/documents",
        data={"kind": "financial_report"},
        files={"file": (filename, content, content_type)},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]


def test_request_models_reject_sql_and_path_fields(api: tuple[TestClient, FakeApplicationService]) -> None:
    client, _ = api

    response = client.post(
        "/api/v1/jobs",
        json={
            "document_id": "doc-1",
            "kind": "document_ingestion",
            "idempotency_key": "request-1",
            "sql": "SELECT * FROM financial_fact",
            "path": "C:/secret.pdf",
        },
    )

    assert response.status_code == 422
    fields = {detail["loc"][-1] for detail in response.json()["error"]["details"]}
    assert fields == {"sql", "path"}


def test_document_content_supports_single_range(api: tuple[TestClient, FakeApplicationService]) -> None:
    client, _ = api
    uploaded = client.post(
        "/api/v1/documents",
        data={"kind": "financial_report"},
        files={"file": ("report.pdf", PDF_BYTES, "application/pdf")},
    ).json()

    response = client.get(
        f"/api/v1/documents/{uploaded['document_id']}/content",
        headers={"Range": "bytes=0-7"},
    )

    assert response.status_code == 206
    assert response.content == PDF_BYTES[:8]
    assert response.headers["content-range"] == f"bytes 0-7/{len(PDF_BYTES)}"
    assert response.headers["accept-ranges"] == "bytes"


def test_document_content_rejects_changed_pdf_bytes(
    api: tuple[TestClient, FakeApplicationService], tmp_path: Path
) -> None:
    client, service = api
    uploaded = client.post(
        "/api/v1/documents",
        data={"kind": "financial_report"},
        files={"file": ("report.pdf", PDF_BYTES, "application/pdf")},
    ).json()
    document = service.documents[uploaded["document_id"]]
    (tmp_path / document.storage_key).write_bytes(PDF_BYTES + b"changed")

    response = client.get(f"/api/v1/documents/{uploaded['document_id']}/content")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "source_content_mismatch"


def test_document_content_rejects_multiple_or_unsatisfiable_ranges(
    api: tuple[TestClient, FakeApplicationService],
) -> None:
    client, _ = api
    uploaded = client.post(
        "/api/v1/documents",
        data={"kind": "financial_report"},
        files={"file": ("report.pdf", PDF_BYTES, "application/pdf")},
    ).json()
    url = f"/api/v1/documents/{uploaded['document_id']}/content"

    multiple = client.get(url, headers={"Range": "bytes=0-1,3-4"})
    unsatisfiable = client.get(url, headers={"Range": "bytes=999-"})

    assert multiple.status_code == 416
    assert multiple.json()["error"]["code"] == "invalid_range"
    assert unsatisfiable.status_code == 416
    assert unsatisfiable.headers["content-range"] == f"bytes */{len(PDF_BYTES)}"


def test_document_content_rejects_path_escape_and_symlink_escape(
    api: tuple[TestClient, FakeApplicationService], tmp_path: Path
) -> None:
    client, service = api
    outside = tmp_path.parent / "outside-api-test.pdf"
    outside.write_bytes(PDF_BYTES)
    escaped = _document("escape", "../outside-api-test.pdf")
    service.documents[escaped.document_id] = escaped

    response = client.get(f"/api/v1/documents/{escaped.document_id}/content")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "document_path_forbidden"

    link = tmp_path / "documents" / "linked.pdf"
    link.parent.mkdir(exist_ok=True)
    try:
        link.symlink_to(outside)
    except OSError:
        return
    linked = _document("link", "documents/linked.pdf")
    service.documents[linked.document_id] = linked
    response = client.get(f"/api/v1/documents/{linked.document_id}/content")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "document_path_forbidden"


def test_collections_and_resource_routes_are_wired(api: tuple[TestClient, FakeApplicationService]) -> None:
    client, service = api
    upload = client.post(
        "/api/v1/documents",
        data={"kind": "financial_report"},
        files={"file": ("report.pdf", PDF_BYTES, "application/pdf")},
    ).json()

    job = client.post(
        "/api/v1/jobs",
        json={
            "document_id": upload["document_id"],
            "kind": "document_ingestion",
            "idempotency_key": "job-request-1",
        },
    )
    conversation = client.post("/api/v1/conversations", json={"title": "年报问数"})
    turn = client.post(
        "/api/v1/conversations/conversation-1/turns",
        json={"question": "营收是多少？", "idempotency_key": "turn-request-1", "expected_version": 1},
    )

    assert job.status_code == 202
    assert client.get("/api/v1/jobs").json()["items"][0]["job_id"] == "job-1"
    assert client.get("/api/v1/jobs/job-1").status_code == 200
    assert client.post("/api/v1/jobs/job-1/retry", json={"idempotency_key": "retry-1"}).status_code == 202
    assert conversation.status_code == 201
    assert client.get("/api/v1/conversations/conversation-1").status_code == 200
    assert turn.status_code == 201
    assert turn.json()["turn"]["answer"]["status"] == "NEEDS_CLARIFICATION"
    assert service.last_turn_request_id == turn.headers["x-request-id"]
    history = client.get("/api/v1/conversations/conversation-1").json()["turns"]
    assert [item["question"] for item in history] == ["营收是多少？"]
    facts = client.get("/api/v1/facts").json()
    assert facts["items"][0]["fact_key"] == "fact-1"
    assert facts["total"] == 1
    assert client.get("/api/v1/facts/fact-1").status_code == 200
    assert client.get("/api/v1/query-runs/query-1").status_code == 200
    exported = client.get("/api/v1/query-runs/query-1/export.xlsx")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


def test_document_and_fact_collections_report_total_beyond_current_page(
    api: tuple[TestClient, FakeApplicationService],
) -> None:
    client, service = api
    for name in ("first.pdf", "second.pdf"):
        response = client.post(
            "/api/v1/documents",
            data={"kind": "financial_report"},
            files={"file": (name, PDF_BYTES, "application/pdf")},
        )
        assert response.status_code == 201
    service.facts["fact-2"] = _fact().model_copy(update={"fact_key": "fact-2"})

    documents = client.get("/api/v1/documents", params={"limit": 1}).json()
    facts = client.get("/api/v1/facts", params={"validation_status": "NEEDS_REVIEW", "limit": 1}).json()

    assert len(documents["items"]) == 1
    assert documents["next_cursor"] == "1"
    assert documents["total"] == 2
    assert len(facts["items"]) == 1
    assert facts["next_cursor"] == "1"
    assert facts["total"] == 2


def test_fact_decision_maps_version_conflict_to_409(api: tuple[TestClient, FakeApplicationService]) -> None:
    client, _ = api

    response = client.post(
        "/api/v1/facts/fact-1/decisions",
        json={"action": "VALIDATE", "reason": "已核对原始财报", "expected_version": 99},
    )

    assert response.status_code == 409
    payload = response.json()["error"]
    assert payload["code"] == "fact_version_conflict"
    assert payload["details"]["current_version"] == 1


def test_fact_decision_rejects_unknown_action_and_missing_correction(
    api: tuple[TestClient, FakeApplicationService],
) -> None:
    client, _ = api

    unknown = client.post(
        "/api/v1/facts/fact-1/decisions",
        json={"action": "APPROVE_ALL", "reason": "unsafe", "expected_version": 1},
    )
    missing = client.post(
        "/api/v1/facts/fact-1/decisions",
        json={"action": "CORRECT", "reason": "数值错误", "expected_version": 1},
    )
    incomplete_evidence = client.post(
        "/api/v1/facts/fact-1/decisions",
        json={
            "action": "CORRECT",
            "reason": "补全来源",
            "expected_version": 1,
            "replacement": {
                "raw_value": "100",
                "normalized_value": "100",
                "source_unit": "万元",
                "target_unit": "万元",
                "currency": "CNY",
                "statement_scope": "consolidated",
                "period_type": "duration",
                "page_no": 8,
                "table_name": "income_sheet",
                "row_label": "利润总额",
            },
        },
    )

    assert unknown.status_code == 422
    assert missing.status_code == 422
    assert incomplete_evidence.status_code == 422
    assert incomplete_evidence.json()["error"]["code"] == "request_validation_failed"


def test_unknown_route_and_validation_errors_use_uniform_error_shape(
    api: tuple[TestClient, FakeApplicationService],
) -> None:
    client, _ = api

    missing = client.get("/api/v1/documents/not-found")
    invalid = client.post("/api/v1/conversations", json={"title": "x", "path": "C:/secret"})

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "document_not_found"
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "request_validation_failed"
    assert invalid.json()["error"]["request_id"]


def test_invalid_cursor_fails_at_http_boundary(api: tuple[TestClient, FakeApplicationService]) -> None:
    client, _ = api

    response = client.get("/api/v1/documents", params={"cursor": "../../secret"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_failed"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::"])
def test_unauthenticated_api_refuses_non_loopback_bind(tmp_path: Path, host: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        create_app(FakeApplicationService(tmp_path), ApiSettings(storage_root=tmp_path, host=host))


def test_cors_uses_exact_allowlist_and_never_wildcard(api: tuple[TestClient, FakeApplicationService]) -> None:
    client, _ = api

    allowed = client.options(
        "/api/v1/health",
        headers={"Origin": "http://127.0.0.1:5173", "Access-Control-Request-Method": "GET"},
    )
    denied = client.options(
        "/api/v1/health",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"},
    )
    denied_get = client.get("/api/v1/health", headers={"Origin": "http://evil.example"})

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    assert denied.status_code == 403
    assert "access-control-allow-origin" not in denied.headers
    assert denied.json()["error"]["code"] == "cors_origin_forbidden"
    assert denied_get.status_code == 403
    assert denied_get.json()["error"]["code"] == "cors_origin_forbidden"


def test_disallowed_origin_is_rejected_for_actual_upload_before_parsing(
    api: tuple[TestClient, FakeApplicationService],
) -> None:
    client, service = api

    response = client.post(
        "/api/v1/documents",
        headers={"Origin": "http://evil.example"},
        data={"kind": "financial_report"},
        files={"file": ("report.pdf", PDF_BYTES, "application/pdf")},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "cors_origin_forbidden"
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]
    assert service.documents == {}


def test_local_upload_without_origin_is_allowed(api: tuple[TestClient, FakeApplicationService]) -> None:
    client, _ = api

    response = client.post(
        "/api/v1/documents",
        data={"kind": "financial_report"},
        files={"file": ("report.pdf", PDF_BYTES, "application/pdf")},
    )

    assert response.status_code == 201


def test_allowed_origin_upload_receives_cors_header(api: tuple[TestClient, FakeApplicationService]) -> None:
    client, _ = api

    response = client.post(
        "/api/v1/documents",
        headers={"Origin": "http://127.0.0.1:5173"},
        data={"kind": "financial_report"},
        files={"file": ("report.pdf", PDF_BYTES, "application/pdf")},
    )

    assert response.status_code == 201
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"


def test_upload_rejects_oversized_content_length_before_multipart_parsing(
    api: tuple[TestClient, FakeApplicationService],
) -> None:
    client, service = api

    response = client.post(
        "/api/v1/documents",
        headers={"Content-Type": "multipart/form-data; boundary=unused", "Content-Length": "999999"},
        content=b"this body must not be parsed",
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "upload_request_too_large"
    assert service.documents == {}


def test_upload_requires_content_length(api: tuple[TestClient, FakeApplicationService]) -> None:
    client, service = api
    request = client.build_request(
        "POST",
        "/api/v1/documents",
        headers={"Content-Type": "multipart/form-data; boundary=unused"},
        content=b"unused",
    )
    del request.headers["content-length"]

    response = client.send(request)

    assert response.status_code == 411
    assert response.json()["error"]["code"] == "content_length_required"
    assert service.documents == {}


def test_upload_rejects_conflicting_content_lengths(api: tuple[TestClient, FakeApplicationService]) -> None:
    client, service = api
    request = client.build_request(
        "POST",
        "/api/v1/documents",
        headers=[
            ("Content-Type", "multipart/form-data; boundary=unused"),
            ("Content-Length", "10"),
            ("Content-Length", "11"),
        ],
        content=b"unused",
    )

    response = client.send(request)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "conflicting_content_length"
    assert service.documents == {}


def test_upload_rejects_chunked_transfer_without_content_length(
    api: tuple[TestClient, FakeApplicationService],
) -> None:
    client, service = api
    request = client.build_request(
        "POST",
        "/api/v1/documents",
        headers={
            "Content-Type": "multipart/form-data; boundary=unused",
            "Transfer-Encoding": "chunked",
        },
        content=b"unused",
    )
    if "content-length" in request.headers:
        del request.headers["content-length"]

    response = client.send(request)

    assert response.status_code == 411
    assert response.json()["error"]["code"] == "chunked_upload_forbidden"
    assert service.documents == {}


def test_upload_rejects_content_length_with_transfer_encoding(
    api: tuple[TestClient, FakeApplicationService],
) -> None:
    client, service = api
    request = client.build_request(
        "POST",
        "/api/v1/documents",
        headers={
            "Content-Type": "multipart/form-data; boundary=unused",
            "Content-Length": "6",
            "Transfer-Encoding": "chunked",
        },
        content=b"unused",
    )

    response = client.send(request)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "conflicting_transfer_length"
    assert service.documents == {}


def test_openapi_documents_uniform_errors_and_has_no_sql_execution_route(
    api: tuple[TestClient, FakeApplicationService],
) -> None:
    client, _ = api

    schema = client.get("/api/openapi.json").json()

    assert all("/sql" not in path and "/execute" not in path for path in schema["paths"])
    document_parameters = schema["paths"]["/api/v1/documents"]["get"].get("parameters", [])
    assert {parameter["name"] for parameter in document_parameters} == {"cursor", "limit"}
    decision_responses = schema["paths"]["/api/v1/facts/{fact_key}/decisions"]["post"]["responses"]
    assert decision_responses["409"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorEnvelope"
    }


def test_real_fact_validation_rejects_incomplete_evidence_without_state_change(tmp_path: Path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    database_factory = FinanceDatabaseFactory(tmp_path / "finance.db", schema)
    store = WebStore(database_factory, tmp_path / "storage")
    store.initialize()
    analyzer = QuestionAnalyzer(CompanyResolver([CompanyRecord("600080", "金花股份", "金花股份有限公司")]))
    facade = WebApplicationFacade(store, QuestionApplicationService(store, database_factory, analyzer))
    client = TestClient(create_app(facade, ApiSettings(storage_root=tmp_path / "storage")))
    document = client.post(
        "/api/v1/documents",
        data={"kind": "financial_report"},
        files={"file": ("report.pdf", PDF_BYTES, "application/pdf")},
    ).json()
    fact = FinancialFact.from_candidate(
        ExtractedFactCandidate(
            metric="total_profit",
            raw_value=100.0,
            source_unit="万元",
            page_no=8,
            table_name="income_sheet",
            row_label="利润总额",
            column_label=None,
            confidence=1.0,
        ),
        company_id="600080",
        stock_code="600080",
        period="2025Q3",
        statement_scope=StatementScope.CONSOLIDATED,
        period_type=PeriodType.DURATION,
        target_unit="万元",
        currency="CNY",
        source_file="report.pdf",
        source_content_sha256=hashlib.sha256(PDF_BYTES).hexdigest(),
        extractor_version="api-integration-test-v1",
    )
    with database_factory.unit_of_work() as database:
        database.execute(
            "UPDATE document SET page_count = ? WHERE document_id = ?",
            (8, document["document_id"]),
        )
        database.upsert(
            "income_sheet",
            {
                "serial_number": 1,
                "stock_code": "600080",
                "stock_abbr": "金花股份",
                "report_period": "2025Q3",
                "report_year": 2025,
            },
        )
        database.upsert_financial_facts([fact])
        fact_key = str(database.query("SELECT fact_key FROM financial_fact", use_cache=False)[0]["fact_key"])
        database.execute(
            "UPDATE financial_fact SET document_id = ? WHERE fact_key = ?",
            (document["document_id"], fact_key),
        )
        database.execute(
            "UPDATE financial_source SET document_id = ? WHERE source_key = ?",
            (document["document_id"], fact.source_key),
        )

    response = client.post(
        f"/api/v1/facts/{fact_key}/decisions",
        json={"action": "VALIDATE", "reason": "已核对原文", "expected_version": 1},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "incomplete_fact_evidence"
    with database_factory.unit_of_work() as database:
        row = database.query(
            "SELECT validation_status, review_version FROM financial_fact WHERE fact_key = ?",
            (fact_key,),
            use_cache=False,
        )[0]
        assert row == {"validation_status": "NEEDS_REVIEW", "review_version": 1}
        assert database.query("SELECT * FROM fact_review_event", use_cache=False) == []

        database.execute(
            "UPDATE financial_fact SET column_label = ? WHERE fact_key = ?",
            ("本期金额", fact_key),
        )
    stored_pdf = tmp_path / "storage" / "documents" / f"{document['document_id']}.pdf"
    stored_pdf.write_bytes(PDF_BYTES + b"changed")

    mismatch = client.post(
        f"/api/v1/facts/{fact_key}/decisions",
        json={"action": "VALIDATE", "reason": "已核对原文", "expected_version": 1},
    )

    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "source_content_mismatch"
    with database_factory.unit_of_work() as database:
        row = database.query(
            "SELECT validation_status, review_version FROM financial_fact WHERE fact_key = ?",
            (fact_key,),
            use_cache=False,
        )[0]
        assert row == {"validation_status": "NEEDS_REVIEW", "review_version": 1}
        assert database.query("SELECT * FROM fact_review_event", use_cache=False) == []


def test_real_facade_persists_uploaded_document_turn_request_id_and_query_run(tmp_path: Path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    database_factory = FinanceDatabaseFactory(tmp_path / "finance.db", schema)
    store = WebStore(database_factory, tmp_path / "storage")
    store.initialize()
    analyzer = QuestionAnalyzer(CompanyResolver([CompanyRecord("600080", "金花股份", "金花股份有限公司")]))
    question_service = QuestionApplicationService(store, database_factory, analyzer)
    facade = WebApplicationFacade(store, question_service)
    client = TestClient(create_app(facade, ApiSettings(storage_root=tmp_path / "storage")))

    uploaded = client.post(
        "/api/v1/documents",
        data={"kind": "financial_report"},
        files={"file": ("report.pdf", PDF_BYTES, "application/pdf")},
    )
    assert uploaded.status_code == 201
    document = uploaded.json()
    assert client.get(f"/api/v1/documents/{document['document_id']}").json() == document
    assert client.get(f"/api/v1/documents/{document['document_id']}/content").content == PDF_BYTES

    fact = FinancialFact.from_candidate(
        ExtractedFactCandidate(
            metric="total_profit",
            raw_value=100.0,
            source_unit="万元",
            page_no=8,
            table_name="income_sheet",
            row_label="利润总额",
            column_label="本期金额",
            confidence=1.0,
        ),
        company_id="600080",
        stock_code="600080",
        period="2025Q3",
        statement_scope=StatementScope.CONSOLIDATED,
        period_type=PeriodType.DURATION,
        target_unit="万元",
        currency="CNY",
        source_file="report.pdf",
        source_content_sha256=hashlib.sha256(PDF_BYTES).hexdigest(),
        extractor_version="api-integration-test-v1",
    )
    with database_factory.unit_of_work() as database:
        database.execute(
            "UPDATE document SET page_count = ? WHERE document_id = ?",
            (8, document["document_id"]),
        )
        database.upsert(
            "income_sheet",
            {
                "serial_number": 1,
                "stock_code": "600080",
                "stock_abbr": "金花股份",
                "report_period": "2025Q3",
                "report_year": 2025,
            },
        )
        database.upsert_financial_facts([fact])
        fact_key = str(database.query("SELECT fact_key FROM financial_fact", use_cache=False)[0]["fact_key"])
        database.execute(
            "UPDATE financial_fact SET document_id = ? WHERE fact_key = ?",
            (document["document_id"], fact_key),
        )
        database.execute(
            "UPDATE financial_source SET document_id = ? WHERE source_key = ?",
            (document["document_id"], fact.source_key),
        )
    store.review_fact(
        fact_key,
        action="VALIDATE",
        expected_version=1,
        actor_label="API 集成测试",
        reason="已核对来源页",
    )

    conversation = client.post("/api/v1/conversations", json={"title": "真实 Facade 会话"})
    assert conversation.status_code == 201
    conversation_id = conversation.json()["conversation_id"]
    clarification = client.post(
        f"/api/v1/conversations/{conversation_id}/turns",
        json={"question": "金花股份利润总额是多少？", "idempotency_key": "real-turn-1", "expected_version": 1},
    )
    assert clarification.status_code == 201
    assert clarification.json()["turn"]["answer"]["status"] == "NEEDS_CLARIFICATION"

    answer = client.post(
        f"/api/v1/conversations/{conversation_id}/turns",
        json={"question": "2025年三季度", "idempotency_key": "real-turn-2", "expected_version": 2},
    )
    assert answer.status_code == 201
    answer_payload = answer.json()["turn"]
    assert answer_payload["answer"]["status"] == "VERIFIED"
    query_run_id = answer_payload["query_run_id"]
    assert query_run_id

    query_run = client.get(f"/api/v1/query-runs/{query_run_id}")
    assert query_run.status_code == 200
    assert query_run.json()["status"] == "SUCCEEDED"
    assert query_run.json()["answer_result"]["evidence"][0]["document_id"] == document["document_id"]
    assert store.get_query_run(query_run_id)["request_id"] == answer.headers["x-request-id"]
    history = client.get(f"/api/v1/conversations/{conversation_id}").json()["turns"]
    assert [turn["sequence"] for turn in history] == [1, 2]


def _document(document_id: str, storage_key: str) -> DocumentRecord:
    return DocumentRecord(
        document_id=document_id,
        kind="financial_report",
        original_name="report.pdf",
        storage_key=storage_key,
        sha256="0" * 64,
        size_bytes=len(PDF_BYTES),
        mime_type="application/pdf",
        status="STORED",
        error_code=None,
        error_message=None,
        created_at=NOW,
    )


def _job(*, document_id: str, job_id: str = "job-1", attempt: int = 1) -> JobResponse:
    return JobResponse(
        job_id=job_id,
        document_id=document_id,
        kind="document_ingestion",
        status="QUEUED",
        stage=None,
        progress=0,
        attempt=attempt,
        error_code=None,
        error_message=None,
        created_at=NOW,
        started_at=None,
        finished_at=None,
    )


def _conversation(*, title: str | None = "年报问数") -> ConversationResponse:
    return ConversationResponse(
        conversation_id="conversation-1",
        title=title,
        version=1,
        state={},
        created_at=NOW,
        updated_at=NOW,
    )


def _fact() -> FactResponse:
    return FactResponse(
        fact_key="fact-1",
        company_id="company-1",
        stock_code="600000",
        period="2024FY",
        statement_scope="consolidated",
        period_type="duration",
        metric="operating_revenue",
        raw_value="100",
        normalized_value="100",
        source_unit="万元",
        target_unit="万元",
        currency="CNY",
        document_id="document-1",
        page_no=12,
        table_name="合并利润表",
        row_label="营业收入",
        column_label="本期发生额",
        confidence=0.9,
        validation_status="NEEDS_REVIEW",
        validation_issues=[],
        version=1,
    )


def _query_run() -> QueryRunResponse:
    return QueryRunResponse(
        query_run_id="query-1",
        conversation_id="conversation-1",
        question="2024 年营业收入是多少？",
        query_spec={"metric": "operating_revenue", "period": "2024FY"},
        sql="SELECT operating_revenue FROM profit_sheet WHERE company_id = ?",
        parameters=["company-1"],
        answer_result={
            "status": "VERIFIED",
            "text": "营业收入为 100 万元。",
            "clarification": None,
            "values": [],
            "evidence": [],
        },
        status="SUCCEEDED",
        created_at=NOW,
    )
