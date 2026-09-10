from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfWriter

from smart_finqa.api_models import ApplicationService as ApiApplicationService
from smart_finqa.api_models import ConversationCreateRequest, ConversationTurnRequest
from smart_finqa.database import FinanceDatabase
from smart_finqa.facts import ExtractedFactCandidate, FinancialFact, PeriodType, StatementScope
from smart_finqa.runtime import (
    _prepare_job_subdirectory,
    _resolve_storage_file,
    create_web_service,
    create_worker,
)
from smart_finqa.schema import load_schema_from_xlsx
from tests.helpers import create_dataset


def _configure_runtime(monkeypatch: pytest.MonkeyPatch, base_dir: Path) -> Path:
    create_dataset(base_dir)
    storage_root = base_dir / "outputs" / "web_storage"
    monkeypatch.setenv("SMART_FINQA_BASE_DIR", str(base_dir))
    monkeypatch.setenv("SMART_FINQA_STORAGE_ROOT", str(storage_root))
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DB_PATH", "outputs/finance.db")
    monkeypatch.delenv("SMART_FINQA_CONFIG", raising=False)
    return storage_root


def _write_pdf(path: Path, *, page_count: int = 1) -> None:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=612, height=792)
    with path.open("wb") as stream:
        writer.write(stream)


def test_real_web_service_restores_persisted_conversation_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_runtime(monkeypatch, tmp_path)
    service = create_web_service()
    assert isinstance(service, ApiApplicationService)
    conversation = service.create_conversation(ConversationCreateRequest(title="问数"))

    result = service.create_turn(
        conversation.conversation_id,
        ConversationTurnRequest(
            question="净利润是多少？",
            expected_version=1,
            idempotency_key="turn-1",
        ),
        request_id="request-1",
    )

    restored = service.get_conversation(conversation.conversation_id)
    assert result.turn.status == "NEEDS_CLARIFICATION"
    assert restored.version == 2
    assert [turn.question for turn in restored.turns] == ["净利润是多少？"]


def test_worker_runs_the_configured_financial_document_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage_root = _configure_runtime(monkeypatch, tmp_path)
    service = create_web_service()
    stored_path = storage_root / "documents" / "document.pdf"
    stored_path.parent.mkdir(parents=True)
    _write_pdf(stored_path)
    source_sha256 = hashlib.sha256(stored_path.read_bytes()).hexdigest()
    document = service.store.create_document(
        document_id="document-1",
        kind="financial_report",
        original_name="600080：2025年第三季度报告.pdf",
        storage_key="documents/document.pdf",
        sha256=source_sha256,
        size_bytes=stored_path.stat().st_size,
        mime_type="application/pdf",
    )
    job = service.store.enqueue_document_job(document.document_id, idempotency_key="job-1")

    class FakePipeline:
        def __init__(self, paths: Any, app_config: Any) -> None:
            self.paths = paths
            self.schema = load_schema_from_xlsx(paths.schema_xlsx)
            self.db = FinanceDatabase(paths.db_path, self.schema, db_config=app_config.db)
            self.db.create_tables()

        def run(self, *, mode: str) -> dict[str, str]:
            assert mode == "ingest"
            self.paths.output_dir.mkdir(parents=True, exist_ok=True)
            log_path = self.paths.output_dir / "run_log.json"
            report_path = self.paths.output_dir / "validation_report.json"
            log_path.write_text("{}", encoding="utf-8")
            report_path.write_text("{}", encoding="utf-8")
            source_file = str(next(self.paths.reports_dir.glob("*.pdf")))
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
                source_file=source_file,
                source_content_sha256=hashlib.sha256(Path(source_file).read_bytes()).hexdigest(),
                extractor_version="test-v1",
            )
            self.db.upsert_financial_facts([fact])
            self.db.refresh_fact_projection(
                "income_sheet",
                {
                    "serial_number": 1,
                    "stock_code": "600080",
                    "stock_abbr": "金花股份",
                    "report_period": "2025Q3",
                    "report_year": 2025,
                },
            )
            return {
                "db_path": str(self.paths.db_path),
                "result_2": "",
                "result_3": "",
                "run_log": str(log_path),
                "validation_report": str(report_path),
            }

    monkeypatch.setattr("smart_finqa.runtime.SmartFinancePipeline", FakePipeline)
    completed = create_worker("worker-1").run_once()

    assert completed is not None
    assert completed.job_id == job.job_id
    assert completed.status == "SUCCEEDED"
    completed_document = service.store.get_document(document.document_id)
    assert completed_document.status == "NEEDS_REVIEW"
    assert completed_document.page_count == 1
    with service.store.database_factory.unit_of_work() as database:
        assert database.query(
            "SELECT content_sha256 FROM financial_source WHERE authority_status = 'CURRENT'",
            use_cache=False,
        ) == [{"content_sha256": source_sha256}]
    assert {item.kind for item in service.store.list_artifacts(job.job_id)} == {
        "run_log",
        "validation_report",
    }


def test_worker_rejects_unconfigured_research_document_ingestion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage_root = _configure_runtime(monkeypatch, tmp_path)
    service = create_web_service()
    stored_path = storage_root / "documents" / "research.pdf"
    stored_path.parent.mkdir(parents=True)
    _write_pdf(stored_path)
    document = service.store.create_document(
        document_id="document-2",
        kind="research_report",
        original_name="research.pdf",
        storage_key="documents/research.pdf",
        sha256="0" * 64,
        size_bytes=stored_path.stat().st_size,
        mime_type="application/pdf",
    )
    service.store.enqueue_document_job(document.document_id, idempotency_key="job-2")

    failed = create_worker("worker-2").run_once()

    assert failed is not None
    assert failed.status == "FAILED"
    assert failed.error_code == "HANDLER_FAILED"
    failed_document = service.store.get_document(document.document_id)
    assert failed_document.status == "FAILED"
    assert failed_document.error_code == "UNSUPPORTED_DOCUMENT_KIND"


def test_storage_resolution_allows_symlink_that_stays_inside_root(tmp_path: Path) -> None:
    storage_root = tmp_path / "storage"
    actual_dir = storage_root / "actual"
    actual_dir.mkdir(parents=True)
    stored_file = actual_dir / "report.pdf"
    stored_file.write_bytes(b"%PDF-1.7\n%%EOF\n")
    linked_dir = storage_root / "documents"
    try:
        linked_dir.symlink_to(actual_dir, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symbolic links are not available: {exc}")

    assert _resolve_storage_file(storage_root, "documents/report.pdf") == stored_file.resolve()


def test_storage_resolution_rejects_symlink_escape(tmp_path: Path) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-1.7\n%%EOF\n")
    link = storage_root / "escape.pdf"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symbolic links are not available: {exc}")

    with pytest.raises(ValueError, match="does not resolve to a stored file"):
        _resolve_storage_file(storage_root, "escape.pdf")


def test_job_subdirectory_rejects_preexisting_file(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "job"
    run_dir.mkdir(parents=True)
    (run_dir / "staging").write_text("not a directory", encoding="utf-8")

    with pytest.raises(FileExistsError):
        _prepare_job_subdirectory(run_dir, "staging")


def test_job_subdirectory_rejects_symlink_escape(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "job"
    run_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (run_dir / "staging").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symbolic links are not available: {exc}")

    with pytest.raises(ValueError, match="symbolic link"):
        _prepare_job_subdirectory(run_dir, "staging")
