from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from smart_finqa.database import FinanceDatabase, FinanceDatabaseFactory
from smart_finqa.facts import (
    AuthoritativeSource,
    ExtractedFactCandidate,
    FinancialFact,
    IncompleteFactEvidenceError,
    PeriodType,
    StatementScope,
    ValidationStatus,
)
from smart_finqa.promotion import ArtifactManifest, ProjectionSeed, PromotionPlan
from smart_finqa.schema import load_schema_from_xlsx
from smart_finqa.web_store import (
    ConflictError,
    DocumentRecord,
    InvalidStateError,
    JobRecord,
    SourceContentMismatchError,
    WebStore,
)
from smart_finqa.worker import DurableWorker, LeaseLostError
from tests.helpers import write_schema_workbook

REVIEW_PDF_BYTES = b"%PDF-1.7\nreview evidence\n%%EOF\n"
DEFAULT_SOURCE_CONTENT_SHA256 = hashlib.sha256(REVIEW_PDF_BYTES).hexdigest()


def _store(tmp_path: Path) -> tuple[WebStore, FinanceDatabaseFactory]:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    factory = FinanceDatabaseFactory(tmp_path / "finance.db", schema)
    store = WebStore(factory, tmp_path / "outputs")
    store.initialize()
    return store, factory


def _insert_profit_fact(
    factory: FinanceDatabaseFactory,
    *,
    value: float = 100.0,
    page_no: int = 8,
    document_id: str | None = None,
    source_file: str = "report.pdf",
    source_content_sha256: str = DEFAULT_SOURCE_CONTENT_SHA256,
) -> str:
    fact = FinancialFact.from_candidate(
        ExtractedFactCandidate(
            metric="total_profit",
            raw_value=value,
            source_unit="万元",
            page_no=page_no,
            table_name="合并利润表",
            row_label="利润总额",
            column_label="本期金额",
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
        source_content_sha256=source_content_sha256,
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
        fact_key = str(
            db.query(
                f"SELECT fact_key FROM financial_fact WHERE source_key = {db.parameter_marker}",
                (fact.source_key,),
                use_cache=False,
            )[0]["fact_key"]
        )
        if document_id is not None:
            db.execute(
                f"UPDATE financial_fact SET document_id = {db.parameter_marker} WHERE fact_key = {db.parameter_marker}",
                (document_id, fact_key),
            )
        return fact_key


def _review_document(store: WebStore, *, page_count: int | None = 8, status: str = "NEEDS_REVIEW") -> str:
    storage_key = f"documents/{page_count or 'unknown'}-{time.time_ns()}.pdf"
    destination = store.output_root / storage_key
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(REVIEW_PDF_BYTES)
    return store.create_document(
        kind="financial_report",
        original_name="report.pdf",
        storage_key=storage_key,
        sha256=DEFAULT_SOURCE_CONTENT_SHA256,
        size_bytes=len(REVIEW_PDF_BYTES),
        mime_type="application/pdf",
        status=status,
        page_count=page_count,
    ).document_id


def _promotion_context(
    store: WebStore, *, lease_seconds: int = 30
) -> tuple[DocumentRecord, JobRecord, FinancialFact, PromotionPlan]:
    document = store.create_document(
        kind="financial_report",
        original_name="report.pdf",
        storage_key=f"documents/{time.time_ns()}.pdf",
        sha256="c" * 64,
        size_bytes=10,
        mime_type="application/pdf",
        page_count=8,
    )
    job = store.enqueue_document_job(document.document_id, idempotency_key=f"promote-{time.time_ns()}")
    claimed = store.claim_job("promotion-worker", lease_seconds=lease_seconds)
    assert claimed is not None and claimed.job_id == job.job_id and claimed.lease_token
    store.update_document_status_for_job(
        document.document_id,
        "PROCESSING",
        job_id=job.job_id,
        worker_id="promotion-worker",
        lease_token=claimed.lease_token,
        page_count=8,
    )
    fact, plan = _promotion_plan(document)
    return document, claimed, fact, plan


def _promotion_plan(document: DocumentRecord) -> tuple[FinancialFact, PromotionPlan]:
    fact = FinancialFact.from_candidate(
        ExtractedFactCandidate(
            metric="total_profit",
            raw_value=100.0,
            source_unit="万元",
            page_no=1,
            table_name="income_sheet",
            row_label="利润总额",
            column_label="本期金额",
            confidence=0.9,
        ),
        company_id="600080",
        stock_code="600080",
        period="2025Q3",
        statement_scope=StatementScope.CONSOLIDATED,
        period_type=PeriodType.DURATION,
        target_unit="万元",
        currency="CNY",
        source_file=document.storage_key,
        source_content_sha256=document.sha256,
        extractor_version="test-v1",
    )
    return fact, PromotionPlan(
        document_id=document.document_id,
        source_file=document.storage_key,
        source_content_sha256=document.sha256,
        page_count=8,
        facts=(fact,),
        projections=(
            ProjectionSeed(
                table="income_sheet",
                values=(
                    ("serial_number", 1),
                    ("stock_code", "600080"),
                    ("stock_abbr", "金花股份"),
                    ("report_period", "2025Q3"),
                    ("report_year", 2025),
                ),
            ),
        ),
    )


def test_factory_creates_independent_sqlite_connections_with_web_pragmas(tmp_path: Path) -> None:
    _, factory = _store(tmp_path)

    first = factory.create()
    second = factory.create()
    try:
        assert first._conn is not second._conn
        assert first.get_cache_stats()["enabled"] is False
        assert first.query("PRAGMA foreign_keys", use_cache=False)[0]["foreign_keys"] == 1
        assert first.query("PRAGMA journal_mode", use_cache=False)[0]["journal_mode"].lower() == "wal"
        assert first.query("PRAGMA busy_timeout", use_cache=False)[0]["timeout"] >= 1000
    finally:
        first.close()
        second.close()


def test_document_and_filtered_fact_pages_report_total_beyond_page_length(tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    for index in range(3):
        store.create_document(
            kind="financial_report",
            original_name=f"report-{index}.pdf",
            storage_key=f"documents/report-{index}.pdf",
            sha256=f"{index + 1:064x}",
            size_bytes=100 + index,
            mime_type="application/pdf",
        )

    facts = [
        FinancialFact.from_candidate(
            ExtractedFactCandidate(
                metric=metric,
                raw_value=100.0 + index,
                source_unit="万元",
                page_no=8,
                table_name="合并利润表",
                row_label=metric,
                column_label="本期金额",
                confidence=0.9,
            ),
            company_id="600080",
            stock_code="600080",
            period="2025Q3",
            statement_scope=StatementScope.CONSOLIDATED,
            period_type=PeriodType.DURATION,
            target_unit="万元",
            currency="CNY",
            source_file="report.pdf",
            source_content_sha256=DEFAULT_SOURCE_CONTENT_SHA256,
            extractor_version="test-v1",
        )
        for index, metric in enumerate(("total_profit", "net_profit", "total_operating_revenue", "eps"))
    ]
    with factory.unit_of_work() as database:
        database.upsert_financial_facts(facts)
        database.execute(
            "UPDATE financial_fact SET validation_status = ? WHERE metric = ?",
            (ValidationStatus.REJECTED.value, "eps"),
        )

    documents, document_total = store.list_documents(limit=2)
    pending_facts, pending_total = store.list_facts(validation_status="NEEDS_REVIEW", limit=2)

    assert len(documents) == 2
    assert document_total == 3
    assert len(pending_facts) == 2
    assert pending_total == 3


def test_list_facts_only_returns_current_authoritative_source_but_get_preserves_history(tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    selection_version = "report-selection-v1"
    source_a = AuthoritativeSource(
        source_file="report.pdf",
        source_content_sha256="a" * 64,
        stock_code="600080",
        period="2025Q3",
        selection_version=selection_version,
    )
    source_b = AuthoritativeSource(
        source_file="report.pdf",
        source_content_sha256="b" * 64,
        stock_code="600080",
        period="2025Q3",
        selection_version=selection_version,
    )
    with factory.unit_of_work() as db:
        db.reconcile_batch_authoritative_sources([source_a], selection_version=selection_version)
    fact_a_key = _insert_profit_fact(
        factory,
        source_file=source_a.source_file,
        source_content_sha256=source_a.source_content_sha256,
    )
    with factory.unit_of_work() as db:
        db.reconcile_batch_authoritative_sources([source_b], selection_version=selection_version)
    fact_b_key = _insert_profit_fact(
        factory,
        source_file=source_b.source_file,
        source_content_sha256=source_b.source_content_sha256,
    )

    rows, total = store.list_facts()
    assert [row["fact_key"] for row in rows] == [fact_b_key]
    assert total == 1
    assert store.get_fact(fact_a_key)["fact_key"] == fact_a_key


def test_source_business_key_mismatch_is_hidden_and_cannot_be_reviewed(tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    document_id = _review_document(store)
    fact_key = _insert_profit_fact(factory, document_id=document_id)
    with factory.unit_of_work() as database:
        database.execute(
            "UPDATE financial_source SET stock_code = ?, period = ? "
            "WHERE source_key = (SELECT source_key FROM financial_fact WHERE fact_key = ?)",
            ("000001", "2024FY", fact_key),
        )

    rows, total = store.list_facts()
    assert rows == []
    assert total == 0
    with pytest.raises(KeyError, match="Unknown financial fact"):
        store.review_fact(
            fact_key,
            action="VALIDATE",
            expected_version=1,
            actor_label="本地审核员",
            reason="不应信任业务键错配来源",
        )
    with factory.unit_of_work() as database:
        fact = database.query(
            "SELECT validation_status, review_version FROM financial_fact WHERE fact_key = ?",
            (fact_key,),
            use_cache=False,
        )[0]
        assert fact == {"validation_status": "NEEDS_REVIEW", "review_version": 1}
        assert database.query("SELECT * FROM fact_review_event", use_cache=False) == []


def test_migrations_are_versioned_and_idempotent(tmp_path: Path) -> None:
    store, factory = _store(tmp_path)

    first_versions = store.initialize()
    second_versions = store.initialize()

    assert first_versions == second_versions
    assert first_versions
    with factory.unit_of_work() as db:
        tables = {row["name"] for row in db.query("SELECT name FROM sqlite_master WHERE type='table'", use_cache=False)}
        assert {
            "document",
            "job",
            "job_event",
            "artifact",
            "conversation",
            "conversation_turn",
            "query_run",
            "fact_review_event",
        } <= tables
        fact_columns = {row["name"] for row in db.query("PRAGMA table_info(financial_fact)", use_cache=False)}
        assert {"review_version", "document_id"} <= fact_columns
        document_columns = {row["name"] for row in db.query("PRAGMA table_info(document)", use_cache=False)}
        job_columns = {row["name"] for row in db.query("PRAGMA table_info(job)", use_cache=False)}
        assert {"page_count", "active_ingestion_job_id"} <= document_columns
        assert "lease_token" in job_columns


def test_migration_v5_backfills_the_single_legacy_document_attempt(tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    document = store.create_document(
        kind="financial_report",
        original_name="report.pdf",
        storage_key="documents/legacy.pdf",
        sha256="a" * 64,
        size_bytes=10,
        mime_type="application/pdf",
    )
    job = store.enqueue_document_job(document.document_id, idempotency_key="legacy-ingestion")
    with factory.unit_of_work() as database:
        database.execute("ALTER TABLE document DROP COLUMN active_ingestion_job_id")
        database.execute("DELETE FROM schema_migration WHERE version = ?", (5,))

    assert store.initialize() == (1, 2, 3, 4, 5)
    assert store.get_document(document.document_id).active_ingestion_job_id == job.job_id


def test_migration_v5_rejects_multiple_legacy_active_attempts(tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    document = store.create_document(
        kind="financial_report",
        original_name="report.pdf",
        storage_key="documents/legacy-conflict.pdf",
        sha256="b" * 64,
        size_bytes=10,
        mime_type="application/pdf",
    )
    job = store.enqueue_document_job(document.document_id, idempotency_key="legacy-conflict-1")
    with factory.unit_of_work() as database:
        database.execute(
            "INSERT INTO job (job_id, job_type, input_json, status, idempotency_key, parent_job_id, attempt, "
            "run_key, progress, created_at, updated_at) "
            "SELECT ?, job_type, input_json, status, ?, parent_job_id, attempt, ?, progress, created_at, updated_at "
            "FROM job WHERE job_id = ?",
            (
                "11111111-1111-4111-8111-111111111111",
                "legacy-conflict-2",
                "legacy-conflict-run-2",
                job.job_id,
            ),
        )
        database.execute("ALTER TABLE document DROP COLUMN active_ingestion_job_id")
        database.execute("DELETE FROM schema_migration WHERE version = ?", (5,))

    with pytest.raises(RuntimeError, match="multiple active legacy ingestion jobs"):
        store.initialize()


def test_sqlite_concurrent_initialize_applies_each_migration_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema = load_schema_from_xlsx(write_schema_workbook(tmp_path / "schema.xlsx"))
    factory = FinanceDatabaseFactory(tmp_path / "finance.db", schema)
    first_store = WebStore(factory, tmp_path / "outputs")
    second_store = WebStore(factory, tmp_path / "outputs")
    original_migration = WebStore._migration_001
    migration_started = threading.Event()
    migration_calls = 0
    call_lock = threading.Lock()

    def slow_first_migration(database: object, cursor: object) -> None:
        nonlocal migration_calls
        with call_lock:
            migration_calls += 1
        migration_started.set()
        time.sleep(0.15)
        original_migration(database, cursor)

    monkeypatch.setattr(WebStore, "_migration_001", staticmethod(slow_first_migration))
    results: list[tuple[int, ...]] = []
    errors: list[BaseException] = []

    def initialize(store: WebStore) -> None:
        try:
            results.append(store.initialize())
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=initialize, args=(first_store,))
    second = threading.Thread(target=initialize, args=(second_store,))
    first.start()
    assert migration_started.wait(timeout=2)
    second.start()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert results == [(1, 2, 3, 4, 5), (1, 2, 3, 4, 5)]
    assert migration_calls == 1
    with factory.unit_of_work() as database:
        rows = database.query("SELECT version, COUNT(*) AS count FROM schema_migration GROUP BY version")
    assert [(row["version"], row["count"]) for row in rows] == [
        (1, 1),
        (2, 1),
        (3, 1),
        (4, 1),
        (5, 1),
    ]


def test_mysql_initialize_holds_named_lock_on_migration_connection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = _FakeMySQLMigrationDatabase(lock_result=1, release_result=1)
    store = WebStore(_SingleDatabaseFactory(database), tmp_path / "outputs")
    migration_calls: list[int] = []
    for version in range(1, 6):
        monkeypatch.setattr(
            WebStore,
            f"_migration_{version:03d}",
            staticmethod(lambda _database, _cursor, version=version: migration_calls.append(version)),
        )

    versions = store.initialize()

    assert versions == (1, 2, 3, 4, 5)
    assert migration_calls == [1, 2, 3, 4, 5]
    assert database.connection.cursor_count == 1
    sql = [statement for statement, _ in database.events]
    get_lock_index = next(index for index, statement in enumerate(sql) if "GET_LOCK" in statement)
    create_tables_index = sql.index("CREATE BASE TABLES")
    migration_table_index = next(
        index for index, statement in enumerate(sql) if "CREATE TABLE IF NOT EXISTS schema_migration" in statement
    )
    first_check_index = next(index for index, statement in enumerate(sql) if "SELECT version" in statement)
    release_index = next(index for index, statement in enumerate(sql) if "RELEASE_LOCK" in statement)
    assert sum("GET_LOCK" in statement for statement in sql) == 1
    assert sum("RELEASE_LOCK" in statement for statement in sql) == 1
    assert get_lock_index < create_tables_index < migration_table_index < first_check_index < release_index
    assert database.connection.commits == 1
    assert database.connection.rollbacks == 0
    assert database.connection.autocommit_history == [False, True]


def test_mysql_initialize_releases_named_lock_when_base_schema_migration_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = _FakeMySQLMigrationDatabase(lock_result=1, release_result=1)
    store = WebStore(_SingleDatabaseFactory(database), tmp_path / "outputs")

    def fail_create_tables() -> None:
        database.events.append(("CREATE BASE TABLES", ()))
        raise RuntimeError("base schema migration failed explicitly")

    monkeypatch.setattr(database, "create_tables", fail_create_tables)

    with pytest.raises(RuntimeError, match="base schema migration failed explicitly"):
        store.initialize()

    sql = [statement for statement, _ in database.events]
    get_lock_index = next(index for index, statement in enumerate(sql) if "GET_LOCK" in statement)
    create_tables_index = sql.index("CREATE BASE TABLES")
    release_index = next(index for index, statement in enumerate(sql) if "RELEASE_LOCK" in statement)
    assert get_lock_index < create_tables_index < release_index
    assert not any("SELECT version FROM schema_migration" in statement for statement in sql)
    assert database.connection.closed_cursors == 1


def test_mysql_initialize_fails_explicitly_when_named_lock_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = _FakeMySQLMigrationDatabase(lock_result=0, release_result=1)
    store = WebStore(_SingleDatabaseFactory(database), tmp_path / "outputs")
    migration_calls: list[int] = []
    monkeypatch.setattr(
        WebStore,
        "_migration_001",
        staticmethod(lambda _database, _cursor: migration_calls.append(1)),
    )

    with pytest.raises(RuntimeError, match="migration lock"):
        store.initialize()

    assert migration_calls == []
    assert not any(statement == "CREATE BASE TABLES" for statement, _ in database.events)
    assert not any("RELEASE_LOCK" in statement for statement, _ in database.events)


def test_mysql_initialize_fails_explicitly_when_named_lock_cannot_be_released(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = _FakeMySQLMigrationDatabase(lock_result=1, release_result=0)
    store = WebStore(_SingleDatabaseFactory(database), tmp_path / "outputs")
    for version in range(1, 6):
        monkeypatch.setattr(
            WebStore,
            f"_migration_{version:03d}",
            staticmethod(lambda _database, _cursor: None),
        )

    with pytest.raises(RuntimeError, match="release.*migration lock"):
        store.initialize()

    assert database.connection.autocommit is True
    assert database.connection.closed_cursors == 1


def test_mysql_initialize_releases_lock_and_rolls_back_after_migration_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = _FakeMySQLMigrationDatabase(lock_result=1, release_result=1)
    store = WebStore(_SingleDatabaseFactory(database), tmp_path / "outputs")

    def fail_migration(_database: object, _cursor: object) -> None:
        raise RuntimeError("migration failed explicitly")

    monkeypatch.setattr(WebStore, "_migration_001", staticmethod(fail_migration))

    with pytest.raises(RuntimeError, match="migration failed explicitly"):
        store.initialize()

    assert database.connection.commits == 0
    assert database.connection.rollbacks == 1
    assert any("RELEASE_LOCK" in statement for statement, _ in database.events)


def test_mysql_initialize_preserves_migration_error_when_lock_release_also_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = _FakeMySQLMigrationDatabase(lock_result=1, release_result=0)
    store = WebStore(_SingleDatabaseFactory(database), tmp_path / "outputs")

    def fail_migration(_database: object, _cursor: object) -> None:
        raise RuntimeError("primary migration failure")

    monkeypatch.setattr(WebStore, "_migration_001", staticmethod(fail_migration))

    with pytest.raises(RuntimeError, match="primary migration failure") as error:
        store.initialize()

    assert error.value.__cause__ is not None
    assert "release MySQL schema migration lock" in str(error.value.__cause__)
    assert database.connection.rollbacks == 1
    assert database.connection.autocommit is True
    assert database.connection.closed_cursors == 1


def test_mysql_initialize_releases_lock_when_disabling_autocommit_fails(tmp_path: Path) -> None:
    database = _FakeMySQLMigrationDatabase(
        lock_result=1,
        release_result=1,
        autocommit_failures=(False,),
    )
    store = WebStore(_SingleDatabaseFactory(database), tmp_path / "outputs")

    with pytest.raises(RuntimeError, match="autocommit transition failed"):
        store.initialize()

    assert any("RELEASE_LOCK" in statement for statement, _ in database.events)
    assert database.connection.autocommit is True
    assert database.connection.closed_cursors == 1


def test_mysql_initialize_does_not_open_cursor_when_reading_autocommit_fails(tmp_path: Path) -> None:
    database = _FakeMySQLMigrationDatabase(
        lock_result=1,
        release_result=1,
        autocommit_get_error=True,
    )
    store = WebStore(_SingleDatabaseFactory(database), tmp_path / "outputs")

    with pytest.raises(RuntimeError, match="autocommit read failed"):
        store.initialize()

    assert database.connection.cursor_count == 0


def test_mysql_initialize_releases_lock_when_get_lock_result_cannot_be_read(tmp_path: Path) -> None:
    database = _FakeMySQLMigrationDatabase(
        lock_result=1,
        release_result=1,
        get_lock_fetch_error=True,
    )
    store = WebStore(_SingleDatabaseFactory(database), tmp_path / "outputs")

    with pytest.raises(RuntimeError, match="GET_LOCK result read failed"):
        store.initialize()

    assert any("RELEASE_LOCK" in statement for statement, _ in database.events)
    assert database.connection.autocommit_history == []
    assert database.connection.closed_cursors == 1


def test_mysql_initialize_closes_cursor_when_restoring_autocommit_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = _FakeMySQLMigrationDatabase(
        lock_result=1,
        release_result=1,
        autocommit_failures=(True,),
    )
    store = WebStore(_SingleDatabaseFactory(database), tmp_path / "outputs")
    for version in range(1, 6):
        monkeypatch.setattr(
            WebStore,
            f"_migration_{version:03d}",
            staticmethod(lambda _database, _cursor: None),
        )

    with pytest.raises(RuntimeError, match="autocommit transition failed"):
        store.initialize()

    assert any("RELEASE_LOCK" in statement for statement, _ in database.events)
    assert database.connection.closed_cursors == 1


class _SingleDatabaseFactory:
    def __init__(self, database: object) -> None:
        self.database = database

    @contextmanager
    def unit_of_work(self):
        yield self.database


class _FakeMySQLMigrationDatabase:
    backend = "mysql"
    parameter_marker = "%s"

    def __init__(
        self,
        *,
        lock_result: int,
        release_result: int,
        autocommit_failures: tuple[bool, ...] = (),
        autocommit_get_error: bool = False,
        get_lock_fetch_error: bool = False,
    ) -> None:
        self.events: list[tuple[str, tuple[object, ...]]] = []
        self.applied_versions: set[int] = set()
        self.connection = _FakeMySQLMigrationConnection(
            self,
            lock_result,
            release_result,
            autocommit_failures=autocommit_failures,
            autocommit_get_error=autocommit_get_error,
            get_lock_fetch_error=get_lock_fetch_error,
        )
        self._conn = self.connection
        self._schema_migration_lock_depth = 0
        self._schema_migration_lock_cursor = None

    def connect(self) -> None:
        return None

    def create_tables(self) -> None:
        with self.schema_migration_lock():
            self.events.append(("CREATE BASE TABLES", ()))

    @contextmanager
    def schema_migration_lock(self):
        with FinanceDatabase.schema_migration_lock(self) as cursor:
            yield cursor

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
        self.events.append((sql, params or ()))

    def clear_cache(self) -> None:
        return None


class _FakeMySQLMigrationConnection:
    def __init__(
        self,
        database: _FakeMySQLMigrationDatabase,
        lock_result: int,
        release_result: int,
        *,
        autocommit_failures: tuple[bool, ...],
        autocommit_get_error: bool,
        get_lock_fetch_error: bool,
    ) -> None:
        self.database = database
        self.lock_result = lock_result
        self.release_result = release_result
        self.cursor_count = 0
        self.closed_cursors = 0
        self.commits = 0
        self.rollbacks = 0
        self._autocommit = True
        self.autocommit_history: list[bool] = []
        self.autocommit_failures = list(autocommit_failures)
        self.autocommit_get_error = autocommit_get_error
        self.get_lock_fetch_error = get_lock_fetch_error

    @property
    def autocommit(self) -> bool:
        if self.autocommit_get_error:
            raise RuntimeError("autocommit read failed")
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value: bool) -> None:
        self.autocommit_history.append(value)
        if self.autocommit_failures and self.autocommit_failures[0] is value:
            self.autocommit_failures.pop(0)
            raise RuntimeError("autocommit transition failed")
        self._autocommit = value

    def cursor(self, *, dictionary: bool = False):
        assert dictionary is True
        self.cursor_count += 1
        return _FakeMySQLMigrationCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class _FakeMySQLMigrationCursor:
    def __init__(self, connection: _FakeMySQLMigrationConnection) -> None:
        self.connection = connection
        self.next_row: dict[str, object] | None = None
        self.operation: str | None = None

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        database = self.connection.database
        database.events.append((sql, params))
        if "GET_LOCK" in sql:
            self.operation = "GET_LOCK"
            self.next_row = {"acquired": self.connection.lock_result}
        elif "RELEASE_LOCK" in sql:
            self.operation = "RELEASE_LOCK"
            self.next_row = {"released": self.connection.release_result}
        elif "SELECT version FROM schema_migration" in sql:
            self.next_row = {"version": params[0]} if int(params[0]) in database.applied_versions else None
        elif "INSERT INTO schema_migration" in sql:
            database.applied_versions.add(int(params[0]))

    def fetchone(self) -> dict[str, object] | None:
        if self.operation == "GET_LOCK" and self.connection.get_lock_fetch_error:
            self.connection.get_lock_fetch_error = False
            raise RuntimeError("GET_LOCK result read failed")
        row = self.next_row
        self.next_row = None
        self.operation = None
        return row

    def close(self) -> None:
        self.connection.closed_cursors += 1


def test_conversation_append_is_idempotent_and_uses_optimistic_version(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    conversation = store.create_conversation()

    turn = store.append_turn(
        conversation.conversation_id,
        expected_version=1,
        idempotency_key="turn-1",
        question="金花股份 2025 年三季度利润总额是多少？",
        answer_payload={"status": "NEEDS_CLARIFICATION", "content": "test"},
        session_state={"slots": {"stock_code": "600080"}, "context": {}},
    )
    repeated = store.append_turn(
        conversation.conversation_id,
        expected_version=1,
        idempotency_key="turn-1",
        question="金花股份 2025 年三季度利润总额是多少？",
        answer_payload={"status": "NEEDS_CLARIFICATION", "content": "test"},
        session_state={"slots": {"stock_code": "600080"}, "context": {}},
    )

    assert repeated.turn_id == turn.turn_id
    assert store.get_conversation(conversation.conversation_id).version == 2
    with pytest.raises(ConflictError, match="conversation version"):
        store.append_turn(
            conversation.conversation_id,
            expected_version=1,
            idempotency_key="turn-2",
            question="追问",
            answer_payload={"status": "OK"},
            session_state={"slots": {}, "context": {}},
        )


def test_job_idempotency_claim_lease_recovery_and_retry(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    job = store.create_job("INGEST_DOCUMENT", {"document_id": "doc-1"}, idempotency_key="ingest-doc-1")
    repeated = store.create_job("INGEST_DOCUMENT", {"document_id": "doc-1"}, idempotency_key="ingest-doc-1")
    assert repeated.job_id == job.job_id
    assert store.run_directory(job.job_id).parent.name == "runs"

    claimed = store.claim_job("worker-a", lease_seconds=30)
    assert claimed is not None and claimed.job_id == job.job_id and claimed.status == "RUNNING"
    assert claimed.lease_token
    assert store.claim_job("worker-b", lease_seconds=30) is None
    store.heartbeat_job(
        job.job_id,
        "worker-a",
        claimed.lease_token,
        lease_seconds=30,
        stage="EXTRACTING",
        progress=0.5,
    )

    future = datetime.now(timezone.utc) + timedelta(seconds=31)
    assert store.recover_interrupted(now=future) == 1
    interrupted = store.get_job(job.job_id)
    assert interrupted.status == "INTERRUPTED"

    retried = store.retry_job(job.job_id, idempotency_key="retry-1")
    assert retried.job_id != job.job_id
    assert retried.parent_job_id == job.job_id
    assert retried.status == "QUEUED"


def test_enqueue_document_job_updates_document_in_the_same_transaction(tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    document = store.create_document(
        kind="financial_report",
        original_name="report.pdf",
        storage_key="documents/report.pdf",
        sha256="a" * 64,
        size_bytes=10,
        mime_type="application/pdf",
    )
    with factory.unit_of_work() as database:
        database.execute(
            "CREATE TRIGGER reject_document_queue BEFORE UPDATE OF status ON document "
            "WHEN NEW.status = 'QUEUED' BEGIN SELECT RAISE(ABORT, 'queue transition failed'); END"
        )

    with pytest.raises(Exception, match="queue transition failed"):
        store.enqueue_document_job(document.document_id, idempotency_key="enqueue-1")

    assert store.get_document(document.document_id).status == "STORED"
    assert store.list_jobs() == []


def test_enqueue_document_job_is_idempotent_and_queues_stored_document(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    document = store.create_document(
        kind="financial_report",
        original_name="report.pdf",
        storage_key="documents/report.pdf",
        sha256="a" * 64,
        size_bytes=10,
        mime_type="application/pdf",
    )

    created = store.enqueue_document_job(document.document_id, idempotency_key="enqueue-1")
    repeated = store.enqueue_document_job(document.document_id, idempotency_key="enqueue-1")

    assert repeated.job_id == created.job_id
    assert store.get_document(document.document_id).status == "QUEUED"
    assert len(store.list_jobs()) == 1


def test_document_ingestion_job_cannot_bypass_atomic_enqueue(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)

    with pytest.raises(ValueError, match="enqueue_document_job"):
        store.create_job(
            "document_ingestion",
            {"document_id": "orphan-document"},
            idempotency_key="orphan-ingestion",
        )


def test_document_ingestion_cannot_bypass_atomic_promotion(tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    document = store.create_document(
        kind="financial_report",
        original_name="report.pdf",
        storage_key="documents/atomic-promotion.pdf",
        sha256="e" * 64,
        size_bytes=10,
        mime_type="application/pdf",
    )
    job = store.enqueue_document_job(document.document_id, idempotency_key="atomic-promotion")
    claimed = store.claim_job("promotion-worker", lease_seconds=30)
    assert claimed is not None and claimed.job_id == job.job_id and claimed.lease_token

    with pytest.raises(InvalidStateError, match="atomic promotion"):
        store.complete_job(job.job_id, "promotion-worker", claimed.lease_token)
    with pytest.raises(InvalidStateError, match="atomic promotion"):
        store.create_artifact(
            job.job_id,
            "promotion-worker",
            claimed.lease_token,
            kind="run_log",
            storage_key=f"runs/{job.job_id}/run_log.json",
            sha256="f" * 64,
            size_bytes=2,
            mime_type="application/json",
        )

    assert store.get_job(job.job_id).status == "RUNNING"
    assert store.get_document(document.document_id).active_ingestion_job_id == job.job_id
    with factory.unit_of_work() as database:
        assert database.query("SELECT artifact_id FROM artifact", use_cache=False) == []


def test_document_ingestion_cannot_finish_after_losing_current_attempt(tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    document = store.create_document(
        kind="financial_report",
        original_name="report.pdf",
        storage_key="documents/stale-attempt.pdf",
        sha256="1" * 64,
        size_bytes=10,
        mime_type="application/pdf",
    )
    job = store.enqueue_document_job(document.document_id, idempotency_key="stale-attempt")
    claimed = store.claim_job("promotion-worker", lease_seconds=30)
    assert claimed is not None and claimed.job_id == job.job_id and claimed.lease_token
    with factory.unit_of_work() as database:
        database.execute(
            "UPDATE document SET active_ingestion_job_id = NULL WHERE document_id = ?",
            (document.document_id,),
        )

    with pytest.raises(ConflictError, match="active ingestion attempt"):
        store.fail_job(
            job.job_id,
            "promotion-worker",
            claimed.lease_token,
            error_code="STALE_ATTEMPT",
            error_message="must not be persisted",
        )
    _, plan = _promotion_plan(document)
    with pytest.raises(ConflictError, match="active ingestion attempt"):
        store.promote_document_ingestion(
            job.job_id,
            "promotion-worker",
            claimed.lease_token,
            plan=plan,
            artifacts=(),
        )

    assert store.get_job(job.job_id).status == "RUNNING"
    assert store.get_document(document.document_id).status == "QUEUED"
    with factory.unit_of_work() as database:
        assert database.query("SELECT fact_key FROM financial_fact", use_cache=False) == []
        assert database.query("SELECT stock_code FROM income_sheet", use_cache=False) == []
        assert database.query("SELECT artifact_id FROM artifact", use_cache=False) == []


def test_retry_rejects_stale_ancestor_after_child_failure_or_success(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    document = store.create_document(
        kind="financial_report",
        original_name="report.pdf",
        storage_key="documents/retry.pdf",
        sha256="d" * 64,
        size_bytes=10,
        mime_type="application/pdf",
    )
    parent = store.enqueue_document_job(document.document_id, idempotency_key="retry-parent")
    claimed_parent = store.claim_job("retry-worker", lease_seconds=30)
    assert claimed_parent is not None and claimed_parent.lease_token
    store.fail_job(
        parent.job_id,
        "retry-worker",
        claimed_parent.lease_token,
        error_code="TEST_FAILURE",
        error_message="parent failed",
    )
    child = store.retry_job(parent.job_id, idempotency_key="retry-child")
    assert store.retry_job(parent.job_id, idempotency_key="retry-child").job_id == child.job_id
    claimed_child = store.claim_job("retry-worker", lease_seconds=30)
    assert claimed_child is not None and claimed_child.job_id == child.job_id and claimed_child.lease_token
    store.fail_job(
        child.job_id,
        "retry-worker",
        claimed_child.lease_token,
        error_code="TEST_FAILURE",
        error_message="child failed",
    )

    with pytest.raises(ConflictError, match="current ingestion attempt"):
        store.retry_job(parent.job_id, idempotency_key="retry-stale-parent")

    grandchild = store.retry_job(child.job_id, idempotency_key="retry-grandchild")
    claimed_grandchild = store.claim_job("retry-worker", lease_seconds=30)
    assert claimed_grandchild is not None and claimed_grandchild.job_id == grandchild.job_id
    assert claimed_grandchild.lease_token
    current_document = store.update_document_status_for_job(
        document.document_id,
        "PROCESSING",
        job_id=grandchild.job_id,
        worker_id="retry-worker",
        lease_token=claimed_grandchild.lease_token,
        page_count=8,
    )
    _, plan = _promotion_plan(current_document)
    store.promote_document_ingestion(
        grandchild.job_id,
        "retry-worker",
        claimed_grandchild.lease_token,
        plan=plan,
        artifacts=(),
    )
    assert store.get_document(document.document_id).active_ingestion_job_id is None
    with pytest.raises(ConflictError, match="current ingestion attempt"):
        store.retry_job(parent.job_id, idempotency_key="retry-after-success")


def test_promotion_revalidates_fact_and_projection_boundaries(tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    document, claimed, fact, plan = _promotion_context(store)
    lease_token = claimed.lease_token
    assert lease_token
    validated = replace(fact, validation_status=ValidationStatus.VALIDATED, validation_issues=())

    with pytest.raises(ValueError, match="NEEDS_REVIEW"):
        store.promote_document_ingestion(
            claimed.job_id,
            "promotion-worker",
            lease_token,
            plan=replace(plan, facts=(validated,)),
            artifacts=(),
        )
    with pytest.raises(ValueError, match="unknown projection table"):
        store.promote_document_ingestion(
            claimed.job_id,
            "promotion-worker",
            lease_token,
            plan=replace(plan, projections=(ProjectionSeed(table="job", values=()),)),
            artifacts=(),
        )
    with pytest.raises(ConflictError, match="content SHA-256"):
        store.promote_document_ingestion(
            claimed.job_id,
            "promotion-worker",
            lease_token,
            plan=replace(plan, source_content_sha256="d" * 64),
            artifacts=(),
        )

    with factory.unit_of_work() as database:
        assert database.query("SELECT fact_key FROM financial_fact", use_cache=False) == []
    assert store.get_document(document.document_id).status == "PROCESSING"
    assert store.get_job(claimed.job_id).status == "RUNNING"


def test_promotion_commits_all_authority_state_atomically(tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    document, claimed, _, plan = _promotion_context(store)
    lease_token = claimed.lease_token
    assert lease_token
    artifact = ArtifactManifest(
        kind="run_log",
        storage_key=f"runs/{claimed.job_id}/run_log.json",
        sha256="2" * 64,
        size_bytes=2,
        mime_type="application/json",
    )

    result = store.promote_document_ingestion(
        claimed.job_id,
        "promotion-worker",
        lease_token,
        plan=plan,
        artifacts=(artifact,),
    )

    assert result.status == "SUCCEEDED"
    assert result.lease_owner is None and result.lease_token is None and result.lease_expires_at is None
    current_document = store.get_document(document.document_id)
    assert current_document.status == "NEEDS_REVIEW"
    assert current_document.active_ingestion_job_id is None
    with factory.unit_of_work() as database:
        facts = database.query(
            "SELECT fact_key, source_key, document_id, validation_status FROM financial_fact",
            use_cache=False,
        )
        assert len(facts) == 1
        assert facts[0]["fact_key"]
        assert facts[0]["source_key"] == plan.facts[0].source_key
        assert facts[0]["document_id"] == document.document_id
        assert facts[0]["validation_status"] == "NEEDS_REVIEW"
        projections = database.query(
            "SELECT stock_code, report_period, total_profit FROM income_sheet",
            use_cache=False,
        )
        assert projections == [{"stock_code": "600080", "report_period": "2025Q3", "total_profit": None}]
        artifacts = database.query("SELECT job_id, kind, storage_key FROM artifact", use_cache=False)
        assert artifacts == [
            {
                "job_id": claimed.job_id,
                "kind": "run_log",
                "storage_key": artifact.storage_key,
            }
        ]
        events = database.query(
            "SELECT status FROM job_event WHERE job_id = ? ORDER BY event_id",
            (claimed.job_id,),
            use_cache=False,
        )
        assert "SUCCEEDED" in {row["status"] for row in events}


def test_projection_rebuild_ignores_validated_fact_with_mismatched_source_business_key(tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    fact_key = _insert_profit_fact(factory)
    with factory.unit_of_work() as database:
        database.execute(
            "UPDATE financial_fact SET validation_status = 'VALIDATED', validation_issues = '[]' WHERE fact_key = ?",
            (fact_key,),
        )
        database.execute(
            "UPDATE financial_source SET stock_code = ?, period = ? "
            "WHERE source_key = (SELECT source_key FROM financial_fact WHERE fact_key = ?)",
            ("000001", "2024FY", fact_key),
        )

    plan = PromotionPlan(
        document_id="projection-test",
        source_file="report.pdf",
        source_content_sha256=DEFAULT_SOURCE_CONTENT_SHA256,
        page_count=8,
        facts=(),
        projections=(
            ProjectionSeed(
                table="income_sheet",
                values=(
                    ("serial_number", 1),
                    ("stock_code", "600080"),
                    ("stock_abbr", "金花股份"),
                    ("report_period", "2025Q3"),
                    ("report_year", 2025),
                ),
            ),
        ),
    )
    with store._transaction() as (database, cursor):
        WebStore._rebuild_promoted_projections(database, cursor, plan)

    with factory.unit_of_work() as database:
        projection = database.query(
            "SELECT total_profit FROM income_sheet WHERE stock_code = ? AND report_period = ?",
            ("600080", "2025Q3"),
            use_cache=False,
        )[0]
    assert projection["total_profit"] is None


def test_promotion_rolls_back_when_lease_expires_during_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, factory = _store(tmp_path)
    document, claimed, _, plan = _promotion_context(store, lease_seconds=1)
    lease_token = claimed.lease_token
    assert lease_token
    original_insert = WebStore._insert_promoted_facts

    def slow_insert(database: object, cursor: object, promotion_plan: PromotionPlan) -> None:
        original_insert(database, cursor, promotion_plan)
        time.sleep(1.05)

    monkeypatch.setattr(WebStore, "_insert_promoted_facts", staticmethod(slow_insert))
    with pytest.raises(ConflictError, match="expired"):
        store.promote_document_ingestion(
            claimed.job_id,
            "promotion-worker",
            lease_token,
            plan=plan,
            artifacts=(),
        )

    with factory.unit_of_work() as database:
        assert database.query("SELECT fact_key FROM financial_fact", use_cache=False) == []
    assert store.get_document(document.document_id).status == "PROCESSING"
    assert store.get_job(claimed.job_id).status == "RUNNING"


def test_promotion_rolls_back_all_authority_state_when_artifact_insert_fails(tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    document, claimed, _, plan = _promotion_context(store)
    lease_token = claimed.lease_token
    assert lease_token
    with factory.unit_of_work() as database:
        database.execute(
            "CREATE TRIGGER reject_artifact BEFORE INSERT ON artifact "
            "BEGIN SELECT RAISE(ABORT, 'artifact insert failed'); END"
        )

    artifact = ArtifactManifest(
        kind="run_log",
        storage_key=f"runs/{claimed.job_id}/run_log.json",
        sha256="d" * 64,
        size_bytes=2,
        mime_type="application/json",
    )
    with pytest.raises(sqlite3.IntegrityError, match="artifact insert failed"):
        store.promote_document_ingestion(
            claimed.job_id,
            "promotion-worker",
            lease_token,
            plan=plan,
            artifacts=(artifact,),
        )

    with factory.unit_of_work() as database:
        assert database.query("SELECT fact_key FROM financial_fact", use_cache=False) == []
        assert database.query("SELECT stock_code FROM income_sheet", use_cache=False) == []
        assert database.query("SELECT artifact_id FROM artifact", use_cache=False) == []
        succeeded_events = database.query(
            "SELECT event_id FROM job_event WHERE job_id = ? AND status = 'SUCCEEDED'",
            (claimed.job_id,),
            use_cache=False,
        )
        assert succeeded_events == []
    current_document = store.get_document(document.document_id)
    assert current_document.status == "PROCESSING"
    assert current_document.active_ingestion_job_id == claimed.job_id
    assert store.get_job(claimed.job_id).status == "RUNNING"


def test_promotion_rolls_back_all_writes_when_final_attempt_release_fails(tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    document, claimed, _, plan = _promotion_context(store)
    lease_token = claimed.lease_token
    assert lease_token
    with factory.unit_of_work() as database:
        database.execute(
            "CREATE TRIGGER reject_attempt_release BEFORE UPDATE OF active_ingestion_job_id ON document "
            "WHEN OLD.active_ingestion_job_id IS NOT NULL AND NEW.active_ingestion_job_id IS NULL "
            "BEGIN SELECT RAISE(ABORT, 'attempt release failed'); END"
        )
    artifact = ArtifactManifest(
        kind="run_log",
        storage_key=f"runs/{claimed.job_id}/run_log.json",
        sha256="3" * 64,
        size_bytes=2,
        mime_type="application/json",
    )

    with pytest.raises(sqlite3.IntegrityError, match="attempt release failed"):
        store.promote_document_ingestion(
            claimed.job_id,
            "promotion-worker",
            lease_token,
            plan=plan,
            artifacts=(artifact,),
        )

    with factory.unit_of_work() as database:
        assert database.query("SELECT fact_key FROM financial_fact", use_cache=False) == []
        assert database.query("SELECT stock_code FROM income_sheet", use_cache=False) == []
        assert database.query("SELECT artifact_id FROM artifact", use_cache=False) == []
        assert (
            database.query(
                "SELECT event_id FROM job_event WHERE job_id = ? AND status = 'SUCCEEDED'",
                (claimed.job_id,),
                use_cache=False,
            )
            == []
        )
    current_document = store.get_document(document.document_id)
    assert current_document.status == "PROCESSING"
    assert current_document.active_ingestion_job_id == claimed.job_id
    current_job = store.get_job(claimed.job_id)
    assert current_job.status == "RUNNING"
    assert current_job.lease_owner == "promotion-worker"
    assert current_job.lease_token == lease_token


def test_worker_without_handler_fails_instead_of_reporting_success(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    job = store.create_job("unknown_job", {}, idempotency_key="missing-handler")

    finished = DurableWorker(store, "worker-a").run_once()

    assert finished is not None and finished.job_id == job.job_id
    assert finished.status == "FAILED"
    assert finished.error_code == "NOT_CONFIGURED"


def test_worker_renews_lease_while_synchronous_handler_is_running(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    job = store.create_job("slow", {}, idempotency_key="slow-handler")
    started = threading.Event()
    release = threading.Event()
    result: list[object] = []

    def slow_handler(job_record: object, run_dir: Path, heartbeat: object) -> tuple[()]:
        del job_record, run_dir, heartbeat
        started.set()
        assert release.wait(timeout=5)
        return ()

    worker = DurableWorker(store, "worker-a", handlers={"slow": slow_handler}, lease_seconds=1)
    thread = threading.Thread(target=lambda: result.append(worker.run_once()))
    thread.start()
    assert started.wait(timeout=2)
    time.sleep(1.2)

    assert store.recover_interrupted() == 0
    assert store.claim_job("worker-b", lease_seconds=1) is None

    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(result) == 1
    assert result[0] is not None and result[0].status == "SUCCEEDED"
    assert store.get_job(job.job_id).status == "SUCCEEDED"


def test_stale_lease_token_cannot_finish_write_artifact_or_update_document(tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    document_id = _review_document(store, status="STORED")
    job = store.enqueue_document_job(document_id, idempotency_key="fenced-attempt")
    claimed = store.claim_job("shared-worker", lease_seconds=30)
    assert claimed is not None and claimed.lease_token
    with factory.unit_of_work() as database:
        database.execute(
            "UPDATE job SET lease_token = ?, lease_expires_at = ? WHERE job_id = ?",
            ("replacement-token", (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(), job.job_id),
        )

    with pytest.raises(ConflictError, match="lease"):
        store.complete_job(job.job_id, "shared-worker", claimed.lease_token)
    with pytest.raises(ConflictError, match="lease"):
        store.create_artifact(
            job.job_id,
            "shared-worker",
            claimed.lease_token,
            kind="run_log",
            storage_key=f"runs/{job.job_id}/run_log.json",
            sha256="b" * 64,
            size_bytes=2,
            mime_type="application/json",
        )
    with pytest.raises(ConflictError, match="lease"):
        store.update_document_status_for_job(
            document_id,
            "COMPLETED",
            job_id=job.job_id,
            worker_id="shared-worker",
            lease_token=claimed.lease_token,
        )
    assert store.get_document(document_id).status == "QUEUED"


def test_worker_surfaces_lease_loss_instead_of_overwriting_new_attempt(tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    store.create_job("replace-lease", {}, idempotency_key="replace-lease")

    def replace_lease(job: object, run_dir: Path, heartbeat: object) -> tuple[()]:
        del run_dir, heartbeat
        with factory.unit_of_work() as database:
            database.execute(
                "UPDATE job SET lease_token = ? WHERE job_id = ?",
                ("replacement-token", job.job_id),
            )
        return ()

    worker = DurableWorker(store, "worker-a", handlers={"replace-lease": replace_lease}, lease_seconds=30)

    with pytest.raises(LeaseLostError, match="lease"):
        worker.run_once()


def test_review_uses_expected_version_and_correction_is_append_only(tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    document_id = _review_document(store)
    fact_key = _insert_profit_fact(factory, document_id=document_id)

    validated = store.review_fact(
        fact_key,
        action="VALIDATE",
        expected_version=1,
        actor_label="本地审核员",
        reason="已核对原文",
    )
    assert validated.review_version == 2
    with pytest.raises(ConflictError, match="fact review version"):
        store.review_fact(
            fact_key,
            action="REJECT",
            expected_version=1,
            actor_label="本地审核员",
            reason="过期页面",
        )

    corrected = store.review_fact(
        fact_key,
        action="CORRECT",
        expected_version=2,
        actor_label="本地审核员",
        reason="更正数值",
        corrections={"raw_value": 120.0, "normalized_value": 120.0},
    )
    assert corrected.fact_key != fact_key
    with factory.unit_of_work() as db:
        rows = db.query(
            "SELECT fact_key, normalized_value, validation_status FROM financial_fact ORDER BY fact_key",
            use_cache=False,
        )
        by_key = {str(row["fact_key"]): row for row in rows}
        assert by_key[fact_key]["normalized_value"] == 100.0
        assert by_key[fact_key]["validation_status"] == "REJECTED"
        assert by_key[corrected.fact_key]["normalized_value"] == 120.0
        assert by_key[corrected.fact_key]["validation_status"] == "VALIDATED"
        assert db.query("SELECT total_profit FROM income_sheet", use_cache=False)[0]["total_profit"] == 120.0
        events = db.query("SELECT action FROM fact_review_event ORDER BY created_at", use_cache=False)
        assert [row["action"] for row in events] == ["VALIDATE", "CORRECT"]


def test_review_conflict_check_ignores_fact_with_mismatched_source_business_key(tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    document_id = _review_document(store)
    fact_key = _insert_profit_fact(factory, document_id=document_id)
    untrusted_fact_key = _insert_profit_fact(
        factory,
        source_file="untrusted-report.pdf",
        source_content_sha256="b" * 64,
    )
    with factory.unit_of_work() as database:
        database.execute(
            "UPDATE financial_source SET stock_code = ?, period = ?, authority_status = 'CURRENT', "
            "current_slot = 'CURRENT' WHERE source_key = "
            "(SELECT source_key FROM financial_fact WHERE fact_key = ?)",
            ("000001", "2024FY", untrusted_fact_key),
        )
        database.execute(
            "UPDATE financial_fact SET validation_status = 'VALIDATED', validation_issues = '[]' WHERE fact_key = ?",
            (untrusted_fact_key,),
        )

    result = store.review_fact(
        fact_key,
        action="VALIDATE",
        expected_version=1,
        actor_label="本地审核员",
        reason="合法事实不应被错配来源阻断",
    )

    assert result.validation_status == "VALIDATED"
    with factory.unit_of_work() as database:
        projection = database.query("SELECT total_profit FROM income_sheet", use_cache=False)[0]
    assert projection["total_profit"] == 100.0


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("page_no", None),
        ("table_name", None),
        ("table_name", "  "),
        ("row_label", None),
        ("row_label", "  "),
        ("column_label", None),
        ("column_label", "  "),
    ],
)
def test_review_rejects_incomplete_evidence_and_rolls_back(tmp_path: Path, field_name: str, value: object) -> None:
    store, factory = _store(tmp_path)
    document_id = _review_document(store)
    fact_key = _insert_profit_fact(factory, document_id=document_id)
    with factory.unit_of_work() as database:
        database.execute(
            f"UPDATE financial_fact SET {field_name} = {database.parameter_marker} "
            f"WHERE fact_key = {database.parameter_marker}",
            (value, fact_key),
        )

    with pytest.raises(IncompleteFactEvidenceError, match=field_name):
        store.review_fact(
            fact_key,
            action="VALIDATE",
            expected_version=1,
            actor_label="本地审核员",
            reason="证据坐标不完整",
        )

    with factory.unit_of_work() as database:
        fact = database.query(
            "SELECT validation_status, review_version FROM financial_fact WHERE fact_key = ?",
            (fact_key,),
            use_cache=False,
        )[0]
        assert fact == {"validation_status": "NEEDS_REVIEW", "review_version": 1}
        assert database.query("SELECT * FROM fact_review_event", use_cache=False) == []
        assert database.query("SELECT total_profit FROM income_sheet", use_cache=False)[0]["total_profit"] is None


def test_review_rejects_changed_stored_pdf_and_rolls_back(tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    document_id = _review_document(store)
    fact_key = _insert_profit_fact(factory, document_id=document_id)
    document = store.get_document(document_id)
    (store.output_root / document.storage_key).write_bytes(REVIEW_PDF_BYTES + b"changed")

    with pytest.raises(SourceContentMismatchError, match="source_content_mismatch"):
        store.review_fact(
            fact_key,
            action="VALIDATE",
            expected_version=1,
            actor_label="本地审核员",
            reason="已核对原文",
        )

    with factory.unit_of_work() as database:
        fact = database.query(
            "SELECT validation_status, review_version FROM financial_fact WHERE fact_key = ?",
            (fact_key,),
            use_cache=False,
        )[0]
        assert fact == {"validation_status": "NEEDS_REVIEW", "review_version": 1}
        assert database.query("SELECT * FROM fact_review_event", use_cache=False) == []


def test_correction_rejects_unknown_or_invalid_fact_fields(tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    document_id = _review_document(store)
    fact_key = _insert_profit_fact(factory, document_id=document_id)

    with pytest.raises(ValueError, match="correction field"):
        store.review_fact(
            fact_key,
            action="CORRECT",
            expected_version=1,
            actor_label="本地审核员",
            reason="非法字段",
            corrections={"validation_status": "VALIDATED"},
        )
    with pytest.raises(ValueError, match="finite numeric"):
        store.review_fact(
            fact_key,
            action="CORRECT",
            expected_version=1,
            actor_label="本地审核员",
            reason="非法数值",
            corrections={"raw_value": "not-a-number", "normalized_value": "not-a-number"},
        )


@pytest.mark.parametrize("page_count", [None, 1])
def test_review_rejects_unknown_or_out_of_range_pdf_page(tmp_path: Path, page_count: int | None) -> None:
    store, factory = _store(tmp_path)
    document_id = _review_document(store, page_count=page_count)
    fact_key = _insert_profit_fact(factory, page_no=8, document_id=document_id)

    with pytest.raises(ValueError, match="page_count|page_no"):
        store.review_fact(
            fact_key,
            action="VALIDATE",
            expected_version=1,
            actor_label="本地审核员",
            reason="页码必须落在真实 PDF 范围内",
        )

    fact = store.get_fact(fact_key)
    assert fact["validation_status"] == "NEEDS_REVIEW"
    assert fact["review_version"] == 1


def test_review_and_document_status_update_roll_back_together(tmp_path: Path) -> None:
    store, factory = _store(tmp_path)
    document_id = _review_document(store)
    fact_key = _insert_profit_fact(factory, document_id=document_id)
    with factory.unit_of_work() as database:
        database.execute(
            "CREATE TRIGGER reject_review_status BEFORE UPDATE OF status ON document "
            "BEGIN SELECT RAISE(ABORT, 'review status update failed'); END"
        )

    with pytest.raises(Exception, match="review status update failed"):
        store.review_fact(
            fact_key,
            action="VALIDATE",
            expected_version=1,
            actor_label="本地审核员",
            reason="已核对原文",
        )

    with factory.unit_of_work() as database:
        fact = database.query(
            "SELECT validation_status, review_version FROM financial_fact WHERE fact_key = ?",
            (fact_key,),
            use_cache=False,
        )[0]
        assert fact["validation_status"] == "NEEDS_REVIEW"
        assert fact["review_version"] == 1
        assert database.query("SELECT * FROM fact_review_event", use_cache=False) == []
        assert database.query("SELECT total_profit FROM income_sheet", use_cache=False)[0]["total_profit"] is None
