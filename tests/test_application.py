from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from smart_finqa.application import ApplicationService
from smart_finqa.database import FinanceDatabaseFactory
from smart_finqa.facts import (
    ExtractedFactCandidate,
    FinancialFact,
    PeriodType,
    StatementScope,
    UntrustedFactError,
    ValidationStatus,
)
from smart_finqa.safe_query import QuerySpec
from smart_finqa.schema import load_schema_from_xlsx
from smart_finqa.task2 import CompanyRecord, CompanyResolver, QuestionAnalyzer
from smart_finqa.web_store import SourceContentMismatchError, WebStore
from tests.helpers import write_schema_workbook


class RecordingFactSourceDatabase:
    parameter_marker = "?"

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], bool]] = []

    def query(
        self,
        sql: str,
        params: tuple[object, ...],
        *,
        use_cache: bool,
    ) -> list[dict[str, object]]:
        self.calls.append((sql, params, use_cache))
        return [
            {
                "fact_key": "fact-1",
                "document_id": "document-1",
                "normalized_value": 100.0,
                "target_unit": "万元",
                "currency": "CNY",
                "statement_scope": "consolidated",
                "page_no": 8,
                "table_name": "income_sheet",
                "row_label": "利润总额",
                "column_label": "本期金额",
                "review_version": 2,
            }
        ]


def _register_trusted_profit_fact(
    tmp_path: Path,
    store: WebStore,
    factory: FinanceDatabaseFactory,
) -> tuple[Path, str]:
    pdf_bytes = b"%PDF-1.7\nreview evidence\n%%EOF\n"
    document_path = tmp_path / "outputs" / "documents" / "report.pdf"
    document_path.parent.mkdir(parents=True, exist_ok=True)
    document_path.write_bytes(pdf_bytes)
    document = store.create_document(
        kind="financial_report",
        original_name="report.pdf",
        storage_key="documents/report.pdf",
        sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        size_bytes=len(pdf_bytes),
        mime_type="application/pdf",
        page_count=8,
    )
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
        source_content_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        extractor_version="test-v1",
    )
    with factory.unit_of_work() as database:
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
            (document.document_id, fact_key),
        )
        database.execute(
            "UPDATE financial_source SET document_id = ? WHERE source_key = ?",
            (document.document_id, fact.source_key),
        )
    store.review_fact(
        fact_key,
        action="VALIDATE",
        expected_version=1,
        actor_label="本地审核员",
        reason="已核对",
    )
    return document_path, fact_key


def test_application_source_lookup_uses_content_revision_key(tmp_path: Path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    factory = FinanceDatabaseFactory(tmp_path / "finance.db", schema)
    store = WebStore(factory, tmp_path / "outputs")
    analyzer = QuestionAnalyzer(CompanyResolver([CompanyRecord("600080", "金花股份", "金花股份有限公司")]))
    service = ApplicationService(store, factory, analyzer)
    database = RecordingFactSourceDatabase()
    query_spec = QuerySpec.from_mapping(
        {
            "analysis_type": "single_metric",
            "metric": "total_profit",
            "table": "income_sheet",
            "stock_code": "600080",
            "report_period": "2025Q3",
            "select_fields": ["stock_code", "report_period", "total_profit"],
        }
    )

    sources = service._validated_fact_sources(
        database,
        query_spec,
        [{"stock_code": "600080", "report_period": "2025Q3", "total_profit": 100.0}],
    )

    assert sources[0]["fact_key"] == "fact-1"
    sql, params, use_cache = database.calls[0]
    assert "INNER JOIN financial_source fs ON fs.source_key = ff.source_key" in sql
    assert "fs.stock_code = ff.stock_code" in sql
    assert "fs.period = ff.period" in sql
    assert "fs.source_file = ff.source_file" not in sql
    assert params == ("600080", "2025Q3", "total_profit", "duration", "CURRENT")
    assert use_cache is False


def test_application_source_lookup_rejects_mismatched_source_business_identity(tmp_path: Path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    factory = FinanceDatabaseFactory(tmp_path / "finance.db", schema)
    store = WebStore(factory, tmp_path / "outputs")
    store.initialize()
    analyzer = QuestionAnalyzer(CompanyResolver([CompanyRecord("600080", "金花股份", "金花股份有限公司")]))
    service = ApplicationService(store, factory, analyzer)
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
        source_content_sha256="a" * 64,
        extractor_version="test-v1",
    )
    query_spec = QuerySpec.from_mapping(
        {
            "analysis_type": "single_metric",
            "metric": "total_profit",
            "table": "income_sheet",
            "stock_code": "600080",
            "report_period": "2025Q3",
            "select_fields": ["stock_code", "report_period", "total_profit"],
        }
    )
    result_rows = [{"stock_code": "600080", "report_period": "2025Q3", "total_profit": 100.0}]

    with factory.unit_of_work() as database:
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
            ("document-1", fact_key),
        )
        database.review_financial_fact(fact_key, ValidationStatus.VALIDATED)

        assert service._validated_fact_sources(database, query_spec, result_rows)[0]["fact_key"] == fact_key

        database.execute(
            "UPDATE financial_source SET stock_code = ?, period = ? WHERE source_key = ?",
            ("000001", "2025Q3", fact.source_key),
        )

        with pytest.raises(UntrustedFactError, match="got 0"):
            service._validated_fact_sources(database, query_spec, result_rows)

        database.execute(
            "UPDATE financial_source SET stock_code = ?, period = ? WHERE source_key = ?",
            ("600080", "2024FY", fact.source_key),
        )

        with pytest.raises(UntrustedFactError, match="got 0"):
            service._validated_fact_sources(database, query_spec, result_rows)


def test_application_persists_clarification_and_trusted_query_run(tmp_path: Path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    factory = FinanceDatabaseFactory(tmp_path / "finance.db", schema)
    store = WebStore(factory, tmp_path / "outputs")
    store.initialize()
    analyzer = QuestionAnalyzer(CompanyResolver([CompanyRecord("600080", "金花股份", "金花股份有限公司")]))
    service = ApplicationService(store, factory, analyzer)
    conversation = store.create_conversation()
    pdf_bytes = b"%PDF-1.7\nreview evidence\n%%EOF\n"
    document_path = tmp_path / "outputs" / "documents" / "report.pdf"
    document_path.parent.mkdir(parents=True, exist_ok=True)
    document_path.write_bytes(pdf_bytes)
    document = store.create_document(
        kind="financial_report",
        original_name="report.pdf",
        storage_key="documents/report.pdf",
        sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        size_bytes=len(pdf_bytes),
        mime_type="application/pdf",
        page_count=8,
    )

    clarification = service.ask(
        conversation.conversation_id,
        "金花股份利润总额是多少？",
        expected_version=1,
        idempotency_key="ask-1",
        request_id="request-1",
    )
    assert clarification.answer.status == "NEEDS_CLARIFICATION"
    assert clarification.query_run_id is None

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
        source_content_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        extractor_version="test-v1",
    )
    with factory.unit_of_work() as db:
        db.upsert(
            "income_sheet",
            {
                "serial_number": 1,
                "stock_code": "600080",
                "stock_abbr": "金花股份",
                "report_period": "2025Q3",
                "report_year": 2025,
            },
        )
        db.upsert_financial_facts([fact])
        fact_key = str(db.query("SELECT fact_key FROM financial_fact", use_cache=False)[0]["fact_key"])
        db.execute(
            "UPDATE financial_fact SET document_id = ? WHERE fact_key = ?",
            (document.document_id, fact_key),
        )
        db.execute(
            "UPDATE financial_source SET document_id = ? WHERE source_key = ?",
            (document.document_id, fact.source_key),
        )
    store.review_fact(
        fact_key,
        action="VALIDATE",
        expected_version=1,
        actor_label="本地审核员",
        reason="已核对",
    )

    result = service.ask(
        conversation.conversation_id,
        "2025年三季度",
        expected_version=2,
        idempotency_key="ask-2",
        request_id="request-2",
    )

    assert result.answer.status == "VERIFIED"
    assert result.answer.rows[0]["total_profit"] == 100.0
    assert result.answer.sources[0]["fact_key"] == fact_key
    assert "source_file" not in result.answer.sources[0]
    assert result.query_run_id
    query_run = store.get_query_run(result.query_run_id)
    assert query_run["params"]
    assert query_run["fact_sources"][0]["normalized_value"] == 100.0
    assert query_run["request_id"] == "request-2"
    assert query_run["schema_version"]
    assert query_run["code_version"]


def test_application_rejects_changed_pdf_and_persists_failed_query_run(tmp_path: Path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    factory = FinanceDatabaseFactory(tmp_path / "finance.db", schema)
    store = WebStore(factory, tmp_path / "outputs")
    store.initialize()
    analyzer = QuestionAnalyzer(CompanyResolver([CompanyRecord("600080", "金花股份", "金花股份有限公司")]))
    service = ApplicationService(store, factory, analyzer)
    conversation = store.create_conversation()
    document_path, _ = _register_trusted_profit_fact(tmp_path, store, factory)
    document_path.write_bytes(document_path.read_bytes() + b"changed")

    with pytest.raises(SourceContentMismatchError, match="source_content_mismatch"):
        service.ask(
            conversation.conversation_id,
            "金花股份2025年三季度利润总额是多少？",
            expected_version=1,
            idempotency_key="changed-pdf",
            request_id="request-changed-pdf",
        )

    turns = store.list_turns(conversation.conversation_id)
    assert len(turns) == 1
    assert turns[0].answer_payload["status"] == "FAILED"
    assert turns[0].query_run_id is not None
    query_run = store.get_query_run(turns[0].query_run_id)
    assert query_run["status"] == "FAILED"
    assert "SourceContentMismatchError" in query_run["error_message"]
    assert "source_content_mismatch" in query_run["error_message"]


def test_application_rejects_document_and_current_source_hash_mismatch(tmp_path: Path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    factory = FinanceDatabaseFactory(tmp_path / "finance.db", schema)
    store = WebStore(factory, tmp_path / "outputs")
    store.initialize()
    analyzer = QuestionAnalyzer(CompanyResolver([CompanyRecord("600080", "金花股份", "金花股份有限公司")]))
    service = ApplicationService(store, factory, analyzer)
    conversation = store.create_conversation()
    _, fact_key = _register_trusted_profit_fact(tmp_path, store, factory)
    with factory.unit_of_work() as database:
        database.execute(
            "UPDATE financial_source SET content_sha256 = ? WHERE source_key = "
            "(SELECT source_key FROM financial_fact WHERE fact_key = ?)",
            ("b" * 64, fact_key),
        )

    with pytest.raises(SourceContentMismatchError, match="document SHA-256"):
        service.ask(
            conversation.conversation_id,
            "金花股份2025年三季度利润总额是多少？",
            expected_version=1,
            idempotency_key="changed-source-hash",
            request_id="request-changed-source-hash",
        )

    turn = store.list_turns(conversation.conversation_id)[0]
    assert turn.answer_payload["status"] == "FAILED"
    assert store.get_query_run(str(turn.query_run_id))["status"] == "FAILED"


def test_application_revalidates_sources_before_idempotent_verified_replay(tmp_path: Path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    factory = FinanceDatabaseFactory(tmp_path / "finance.db", schema)
    store = WebStore(factory, tmp_path / "outputs")
    store.initialize()
    analyzer = QuestionAnalyzer(CompanyResolver([CompanyRecord("600080", "金花股份", "金花股份有限公司")]))
    service = ApplicationService(store, factory, analyzer)
    conversation = store.create_conversation()
    document_path, _ = _register_trusted_profit_fact(tmp_path, store, factory)

    original = service.ask(
        conversation.conversation_id,
        "金花股份2025年三季度利润总额是多少？",
        expected_version=1,
        idempotency_key="replayed-answer",
        request_id="request-original",
    )
    assert original.answer.status == "VERIFIED"
    document_path.write_bytes(document_path.read_bytes() + b"changed")

    with pytest.raises(SourceContentMismatchError, match="source_content_mismatch"):
        service.ask(
            conversation.conversation_id,
            "金花股份2025年三季度利润总额是多少？",
            expected_version=2,
            idempotency_key="replayed-answer",
            request_id="request-replay",
        )

    assert len(store.list_turns(conversation.conversation_id)) == 1


def test_application_persists_failed_query_run_and_re_raises_execution_error(tmp_path: Path) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    factory = FinanceDatabaseFactory(tmp_path / "finance.db", schema)
    store = WebStore(factory, tmp_path / "outputs")
    store.initialize()
    analyzer = QuestionAnalyzer(CompanyResolver([CompanyRecord("600080", "金花股份", "金花股份有限公司")]))
    service = ApplicationService(store, factory, analyzer)
    conversation = store.create_conversation()
    with factory.unit_of_work() as database:
        database.execute("DROP TABLE income_sheet")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        service.ask(
            conversation.conversation_id,
            "金花股份2025年三季度利润总额是多少？",
            expected_version=1,
            idempotency_key="failed-query",
            request_id="request-failed",
        )

    restored = store.get_conversation(conversation.conversation_id)
    turns = store.list_turns(conversation.conversation_id)
    assert restored.version == 2
    assert len(turns) == 1
    assert turns[0].answer_payload["status"] == "FAILED"
    assert turns[0].query_run_id is not None
    query_run = store.get_query_run(turns[0].query_run_id)
    assert query_run["status"] == "FAILED"
    assert query_run["request_id"] == "request-failed"
    assert "OperationalError" in query_run["error_message"]
    assert query_run["sql"]

    with pytest.raises(RuntimeError, match="previously failed"):
        service.ask(
            conversation.conversation_id,
            "金花股份2025年三季度利润总额是多少？",
            expected_version=2,
            idempotency_key="failed-query",
            request_id="request-retry",
        )
    assert len(store.list_turns(conversation.conversation_id)) == 1
