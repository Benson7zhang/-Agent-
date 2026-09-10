"""Durable control-plane storage for the local Web application."""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Mapping, Sequence
from uuid import UUID, uuid4

from .database import (
    FINANCIAL_FACT_COLUMNS,
    FINANCIAL_FACT_TABLE,
    PROJECTION_KEY_COLUMNS,
    FinanceDatabase,
    FinanceDatabaseFactory,
)
from .facts import (
    AuthoritativeSource,
    ExtractedFactCandidate,
    FinancialFact,
    PeriodType,
    SourceAuthorityStatus,
    SourceOwner,
    StatementScope,
    ValidationIssue,
    ValidationStatus,
    expected_period_type_for_table,
)
from .ingestion_state import SourceIdentityError, sha256_file
from .promotion import ArtifactManifest, PromotionPlan

MIGRATION_VERSION = 5
DOCUMENT_STATUSES = frozenset({"STORED", "QUEUED", "PROCESSING", "NEEDS_REVIEW", "COMPLETED", "FAILED"})
JOB_STATUSES = frozenset({"QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "INTERRUPTED"})
TERMINAL_JOB_STATUSES = frozenset({"SUCCEEDED", "FAILED", "INTERRUPTED"})
REVIEW_ACTIONS = frozenset({"VALIDATE", "REJECT", "CORRECT"})
CORRECTABLE_FACT_FIELDS = frozenset(
    {
        "metric",
        "raw_value",
        "normalized_value",
        "source_unit",
        "target_unit",
        "currency",
        "page_no",
        "table_name",
        "row_label",
        "column_label",
        "statement_scope",
        "period_type",
        "confidence",
    }
)
_UUID_PATTERN = re.compile(r"^[0-9a-fA-F-]{36}$")


class ConflictError(RuntimeError):
    """A persisted resource changed after the caller loaded it."""


class SourceContentMismatchError(ConflictError):
    """Stored PDF bytes no longer match the registered authoritative source."""


class InvalidStateError(RuntimeError):
    """A requested state transition is not allowed."""


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    document_id: str
    kind: str
    original_name: str
    storage_key: str
    sha256: str
    size_bytes: int
    mime_type: str
    page_count: int | None
    active_ingestion_job_id: str | None
    status: str
    error_code: str | None
    error_message: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_id: str
    job_type: str
    input_payload: dict[str, Any]
    status: str
    idempotency_key: str
    parent_job_id: str | None
    attempt: int
    run_key: str
    lease_owner: str | None
    lease_token: str | None
    lease_expires_at: str | None
    heartbeat_at: str | None
    stage: str | None
    progress: float
    error_code: str | None
    error_message: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class ConversationRecord:
    conversation_id: str
    title: str
    session_state: dict[str, Any]
    version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class TurnRecord:
    turn_id: str
    conversation_id: str
    sequence_no: int
    idempotency_key: str
    question: str
    answer_payload: dict[str, Any]
    query_run_id: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class FactReviewResult:
    fact_key: str
    validation_status: str
    review_version: int
    replaced_fact_key: str | None = None
    event_id: str = ""


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    artifact_id: str
    job_id: str
    kind: str
    storage_key: str
    sha256: str
    size_bytes: int
    mime_type: str
    created_at: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_timestamp(value: datetime | None) -> str:
    moment = value or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("timestamps must include timezone information")
    return moment.astimezone(timezone.utc).isoformat()


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _json_load(value: str | None, *, default: Any) -> Any:
    if value in (None, ""):
        return default
    return json.loads(value)


class WebStore:
    """Persistence API whose public methods each own one database unit of work."""

    def __init__(self, database_factory: FinanceDatabaseFactory, output_root: Path) -> None:
        self.database_factory = database_factory
        self.output_root = output_root.resolve()

    def initialize(self) -> tuple[int, ...]:
        with self.database_factory.unit_of_work() as database:
            if database.backend == "mysql":
                return self._initialize_mysql(database)
            database.create_tables()
            self._ensure_migration_table(database)
            return self._initialize_sqlite(database)

    def _migration_plan(self) -> tuple[tuple[int, str, Callable[[FinanceDatabase, Any], None]], ...]:
        return (
            (1, "create_web_control_plane", self._migration_001),
            (2, "version_financial_fact_review", self._migration_002),
            (3, "add_job_attempt", self._migration_003),
            (4, "fence_jobs_and_record_pdf_page_count", self._migration_004),
            (5, "serialize_document_ingestion_attempts", self._migration_005),
        )

    def _initialize_sqlite(self, database: FinanceDatabase) -> tuple[int, ...]:
        database._begin_transaction()
        cursor = database._conn.cursor()
        try:
            versions = self._apply_pending_migrations(database, cursor)
            database._conn.commit()
        except Exception:
            database._conn.rollback()
            raise
        finally:
            cursor.close()
        database.clear_cache()
        return versions

    def _initialize_mysql(self, database: FinanceDatabase) -> tuple[int, ...]:
        database.connect()
        previous_autocommit = bool(getattr(database._conn, "autocommit", True))
        versions: tuple[int, ...] = ()
        with database.schema_migration_lock() as cursor:
            if cursor is None:  # pragma: no cover - guarded by the backend branch
                raise RuntimeError("MySQL schema migration lock did not provide a cursor")
            autocommit_change_attempted = False
            primary_error: BaseException | None = None
            primary_traceback: Any = None
            cleanup_errors: list[BaseException] = []
            try:
                database.create_tables()
                self._ensure_migration_table(database)
                autocommit_change_attempted = True
                database._conn.autocommit = False
                try:
                    versions = self._apply_pending_migrations(database, cursor)
                    database._conn.commit()
                except BaseException:
                    try:
                        database._conn.rollback()
                    except BaseException as rollback_error:
                        cleanup_errors.append(rollback_error)
                    raise
            except BaseException as error:
                primary_error = error
                primary_traceback = error.__traceback__
            finally:
                if autocommit_change_attempted:
                    try:
                        database._conn.autocommit = previous_autocommit
                    except BaseException as autocommit_error:
                        cleanup_errors.append(autocommit_error)

            cleanup_error: BaseException | None = None
            if len(cleanup_errors) == 1:
                cleanup_error = cleanup_errors[0]
            elif cleanup_errors:
                details = "; ".join(f"{type(error).__name__}: {error}" for error in cleanup_errors)
                cleanup_error = RuntimeError(f"multiple MySQL migration cleanup failures: {details}")
                cleanup_error.__cause__ = cleanup_errors[0]
            if primary_error is not None:
                if cleanup_error is not None:
                    raise primary_error.with_traceback(primary_traceback) from cleanup_error
                raise primary_error.with_traceback(primary_traceback)
            if cleanup_error is not None:
                raise cleanup_error
        database.clear_cache()
        return versions

    def _apply_pending_migrations(self, database: FinanceDatabase, cursor: Any) -> tuple[int, ...]:
        versions: list[int] = []
        for version, name, migration in self._migration_plan():
            cursor.execute(
                f"SELECT version FROM schema_migration WHERE version = {database.parameter_marker}",
                (version,),
            )
            if self._one(cursor) is None:
                migration(database, cursor)
                cursor.execute(
                    f"INSERT INTO schema_migration (version, name, applied_at) VALUES "
                    f"({database.parameter_marker}, {database.parameter_marker}, {database.parameter_marker})",
                    (version, name, _utc_now()),
                )
            versions.append(version)
        return tuple(versions)

    def _ensure_migration_table(self, database: FinanceDatabase) -> None:
        if database.backend == "mysql":
            ddl = (
                "CREATE TABLE IF NOT EXISTS schema_migration (version BIGINT PRIMARY KEY, "
                "name VARCHAR(128) NOT NULL, applied_at VARCHAR(40) NOT NULL)"
            )
        else:
            ddl = (
                "CREATE TABLE IF NOT EXISTS schema_migration (version INTEGER PRIMARY KEY, "
                "name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
        database.execute(ddl)

    @staticmethod
    def _migration_001(database: FinanceDatabase, cursor: Any) -> None:
        for statement in _control_plane_ddl(database.backend):
            cursor.execute(statement)

    @staticmethod
    def _migration_002(database: FinanceDatabase, cursor: Any) -> None:
        existing = WebStore._table_columns(database, cursor, FINANCIAL_FACT_TABLE)
        if "review_version" not in existing:
            cursor.execute(
                "ALTER TABLE financial_fact ADD COLUMN review_version BIGINT NOT NULL DEFAULT 1"
                if database.backend == "mysql"
                else "ALTER TABLE financial_fact ADD COLUMN review_version INTEGER NOT NULL DEFAULT 1"
            )
        if "document_id" not in existing:
            cursor.execute(
                "ALTER TABLE financial_fact ADD COLUMN document_id VARCHAR(36) NULL"
                if database.backend == "mysql"
                else "ALTER TABLE financial_fact ADD COLUMN document_id TEXT"
            )

    @staticmethod
    def _migration_003(database: FinanceDatabase, cursor: Any) -> None:
        existing = WebStore._table_columns(database, cursor, "job")
        if "attempt" not in existing:
            cursor.execute(
                "ALTER TABLE job ADD COLUMN attempt BIGINT NOT NULL DEFAULT 1"
                if database.backend == "mysql"
                else "ALTER TABLE job ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1"
            )

    @staticmethod
    def _migration_004(database: FinanceDatabase, cursor: Any) -> None:
        document_columns = WebStore._table_columns(database, cursor, "document")
        if "page_count" not in document_columns:
            cursor.execute(
                "ALTER TABLE document ADD COLUMN page_count BIGINT NULL"
                if database.backend == "mysql"
                else "ALTER TABLE document ADD COLUMN page_count INTEGER"
            )
        job_columns = WebStore._table_columns(database, cursor, "job")
        if "lease_token" not in job_columns:
            cursor.execute(
                "ALTER TABLE job ADD COLUMN lease_token VARCHAR(36) NULL"
                if database.backend == "mysql"
                else "ALTER TABLE job ADD COLUMN lease_token TEXT"
            )

    @staticmethod
    def _migration_005(database: FinanceDatabase, cursor: Any) -> None:
        document_columns = WebStore._table_columns(database, cursor, "document")
        if "active_ingestion_job_id" not in document_columns:
            cursor.execute(
                "ALTER TABLE document ADD COLUMN active_ingestion_job_id VARCHAR(36) NULL"
                if database.backend == "mysql"
                else "ALTER TABLE document ADD COLUMN active_ingestion_job_id TEXT"
            )
        cursor.execute("SELECT document_id, status, active_ingestion_job_id FROM document")
        documents = {str(row["document_id"]): dict(row) for row in cursor.fetchall()}
        cursor.execute(
            "SELECT job_id, input_json, status, attempt, created_at FROM job "
            f"WHERE job_type = {database.parameter_marker} AND status IN "
            "('QUEUED', 'RUNNING', 'FAILED', 'INTERRUPTED')",
            ("document_ingestion",),
        )
        jobs_by_document: dict[str, list[dict[str, Any]]] = {}
        for raw_job in cursor.fetchall():
            job = dict(raw_job)
            try:
                payload = _json_load(str(job["input_json"]), default={})
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"legacy ingestion job {job['job_id']} has invalid input_json") from exc
            if not isinstance(payload, dict) or not str(payload.get("document_id") or "").strip():
                raise RuntimeError(f"legacy ingestion job {job['job_id']} has no document_id")
            document_id = str(payload["document_id"])
            if document_id not in documents:
                raise RuntimeError(f"legacy ingestion job {job['job_id']} references an unknown document")
            jobs_by_document.setdefault(document_id, []).append(job)

        backfill: list[tuple[str, str]] = []
        for document_id, jobs in jobs_by_document.items():
            document = documents[document_id]
            running = [job for job in jobs if job["status"] in {"QUEUED", "RUNNING"}]
            if len(running) > 1:
                raise RuntimeError(f"document {document_id} has multiple active legacy ingestion jobs")
            selected: dict[str, Any] | None = None
            if running:
                if document["status"] not in {"QUEUED", "PROCESSING", "FAILED"}:
                    raise RuntimeError(
                        f"document {document_id} status {document['status']!r} conflicts with an active ingestion job"
                    )
                selected = running[0]
            elif document["status"] == "FAILED":
                terminal = [job for job in jobs if job["status"] in {"FAILED", "INTERRUPTED"}]
                if terminal:
                    latest_attempt = max(int(job.get("attempt") or 1) for job in terminal)
                    latest = [job for job in terminal if int(job.get("attempt") or 1) == latest_attempt]
                    if len(latest) > 1:
                        raise RuntimeError(
                            f"document {document_id} has ambiguous legacy ingestion attempt {latest_attempt}"
                        )
                    selected = latest[0]

            current_job_id = document.get("active_ingestion_job_id")
            selected_job_id = None if selected is None else str(selected["job_id"])
            if current_job_id not in {None, selected_job_id}:
                raise RuntimeError(f"document {document_id} has an inconsistent ingestion attempt pointer")
            if selected_job_id is not None:
                backfill.append((selected_job_id, document_id))

        for document_id, document in documents.items():
            if document["status"] in {"QUEUED", "PROCESSING"} and document_id not in jobs_by_document:
                raise RuntimeError(f"document {document_id} has no active legacy ingestion job")

        for selected_job_id, document_id in backfill:
            cursor.execute(
                f"UPDATE document SET active_ingestion_job_id = {database.parameter_marker} "
                f"WHERE document_id = {database.parameter_marker}",
                (selected_job_id, document_id),
            )

    @staticmethod
    def _table_columns(database: FinanceDatabase, cursor: Any, table: str) -> set[str]:
        if database.backend == "mysql":
            cursor.execute(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
                (table,),
            )
            return {str(dict(row)["COLUMN_NAME"]) for row in cursor.fetchall()}
        cursor.execute(f"PRAGMA table_info({table})")
        return {str(dict(row)["name"]) for row in cursor.fetchall()}

    @contextmanager
    def _transaction(self) -> Iterator[tuple[FinanceDatabase, Any]]:
        with self.database_factory.unit_of_work() as database:
            database._begin_transaction()
            cursor = database._conn.cursor(dictionary=True) if database.backend == "mysql" else database._conn.cursor()
            try:
                yield database, cursor
                database._conn.commit()
            except Exception:
                database._conn.rollback()
                raise
            finally:
                cursor.close()
            database.clear_cache()

    @staticmethod
    def _one(cursor: Any) -> dict[str, Any] | None:
        row = cursor.fetchone()
        return None if row is None else dict(row)

    def create_document(
        self,
        *,
        kind: str,
        original_name: str,
        storage_key: str,
        sha256: str,
        size_bytes: int,
        mime_type: str,
        status: str = "STORED",
        page_count: int | None = None,
        document_id: str | None = None,
    ) -> DocumentRecord:
        if not all(str(value).strip() for value in (kind, original_name, storage_key, sha256, mime_type, status)):
            raise ValueError("document metadata fields must be non-empty")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 1:
            raise ValueError("size_bytes must be a positive integer")
        self._validate_page_count(page_count)
        self._validate_document_status(status, error_code=None, error_message=None)
        document_id = document_id or str(uuid4())
        now = _utc_now()
        with self._transaction() as (database, cursor):
            markers = database.parameter_markers(12)
            cursor.execute(
                "INSERT INTO document (document_id, kind, original_name, storage_key, sha256, size_bytes, "
                f"mime_type, page_count, active_ingestion_job_id, status, created_at, updated_at) VALUES ({markers})",
                (
                    document_id,
                    kind,
                    original_name,
                    storage_key,
                    sha256,
                    size_bytes,
                    mime_type,
                    page_count,
                    None,
                    status,
                    now,
                    now,
                ),
            )
        return self.get_document(document_id)

    def update_document_status(
        self,
        document_id: str,
        status: str,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> DocumentRecord:
        self._validate_document_status(status, error_code=error_code, error_message=error_message)
        with self._transaction() as (database, cursor):
            self._update_document_status_in_transaction(
                database,
                cursor,
                document_id,
                status,
                error_code=error_code,
                error_message=error_message,
            )
        return self.get_document(document_id)

    def update_document_status_for_job(
        self,
        document_id: str,
        status: str,
        *,
        job_id: str,
        worker_id: str,
        lease_token: str,
        error_code: str | None = None,
        error_message: str | None = None,
        page_count: int | None = None,
    ) -> DocumentRecord:
        self._validate_document_status(status, error_code=error_code, error_message=error_message)
        self._validate_page_count(page_count)
        with self._transaction() as (database, cursor):
            job = self._require_active_job_lease(database, cursor, job_id, worker_id, lease_token)
            self._require_active_document_attempt(database, cursor, job, document_id)
            self._update_document_status_in_transaction(
                database,
                cursor,
                document_id,
                status,
                error_code=error_code,
                error_message=error_message,
                page_count=page_count,
            )
        return self.get_document(document_id)

    def promote_document_ingestion(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        plan: PromotionPlan,
        artifacts: tuple[ArtifactManifest, ...],
    ) -> JobRecord:
        with self._transaction() as (database, cursor):
            job = self._require_active_job_lease(database, cursor, job_id, worker_id, lease_token)
            document = self._require_active_document_attempt(database, cursor, job, plan.document_id)
            if str(document["storage_key"]) != plan.source_file:
                raise ConflictError("promotion source is not the document's stable storage key")
            if str(document["sha256"]) != plan.source_content_sha256:
                raise ConflictError("promotion source content SHA-256 does not match the active document")
            if document.get("page_count") != plan.page_count:
                raise ConflictError("promotion page_count does not match the active document")
            self._validate_promotion_plan(database, plan)
            first_fact = plan.facts[0]
            database.activate_web_authoritative_source_in_transaction(
                cursor,
                AuthoritativeSource(
                    source_file=plan.source_file,
                    source_content_sha256=plan.source_content_sha256,
                    stock_code=first_fact.stock_code,
                    period=first_fact.period,
                    source_owner=SourceOwner.WEB,
                    document_id=plan.document_id,
                    selection_version="web-document-v1",
                ),
            )
            self._insert_promoted_facts(database, cursor, plan)
            self._rebuild_promoted_projections(database, cursor, plan)
            for artifact in artifacts:
                self._insert_artifact(database, cursor, job_id, artifact)
            self._refresh_document_review_status_in_transaction(database, cursor, plan.document_id)
            job = self._require_active_job_lease(database, cursor, job_id, worker_id, lease_token)
            self._require_active_document_attempt(database, cursor, job, plan.document_id)
            now = _utc_now()
            cursor.execute(
                "UPDATE job SET status = 'SUCCEEDED', progress = 1, error_code = NULL, error_message = NULL, "
                "lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, finished_at = {m}, updated_at = {m} "
                "WHERE job_id = {m}".format(m=database.parameter_marker),
                (now, now, job_id),
            )
            self._insert_job_event(database, cursor, job_id, "SUCCEEDED", stage=None, message=None, created_at=now)
            self._release_document_attempt(database, cursor, job, "SUCCEEDED", None, None, now)
        return self.get_job(job_id)

    @staticmethod
    def _validate_promotion_plan(database: FinanceDatabase, plan: PromotionPlan) -> None:
        if not plan.facts:
            raise ValueError("promotion requires at least one financial fact")
        required_projection_keys: set[tuple[str, str, str]] = set()
        report_keys = {(fact.stock_code, fact.period) for fact in plan.facts}
        if len(report_keys) != 1:
            raise ValueError("one promoted document must contain exactly one company and report period")
        for fact in plan.facts:
            if fact.validation_status is not ValidationStatus.NEEDS_REVIEW:
                raise ValueError("promotion accepts NEEDS_REVIEW facts only")
            if fact.source_file != plan.source_file:
                raise ValueError("promoted fact source_file must match the stable document source")
            if fact.source_content_sha256 != plan.source_content_sha256:
                raise ValueError("promoted fact content SHA-256 must match the promotion source")
            if fact.page_no is not None and fact.page_no > plan.page_count:
                raise ValueError(f"fact page_no {fact.page_no} exceeds document page_count {plan.page_count}")
            table = fact.table_name
            if table not in database.schema:
                raise ValueError(f"promoted fact references an unknown table: {table!r}")
            table_metrics = {field.field_name for field in database.schema[table]}
            if fact.metric not in table_metrics:
                raise ValueError(f"metric {fact.metric!r} does not belong to table {table!r}")
            if fact.period_type is not expected_period_type_for_table(table):
                raise ValueError(f"fact period_type does not match table {table!r}")
            required_projection_keys.add((table, fact.stock_code, fact.period))

        actual_projection_keys: set[tuple[str, str, str]] = set()
        for seed in plan.projections:
            if seed.table not in database.schema:
                raise ValueError(f"unknown projection table: {seed.table!r}")
            row = seed.as_dict()
            if len(row) != len(seed.values):
                raise ValueError(f"projection seed for {seed.table!r} contains duplicate columns")
            allowed_columns = {
                field.field_name for field in database.schema[seed.table] if field.field_name in PROJECTION_KEY_COLUMNS
            }
            if set(row) != allowed_columns:
                raise ValueError(f"projection seed for {seed.table!r} must contain exactly the schema base columns")
            key = (seed.table, str(row["stock_code"]), str(row["report_period"]))
            if key in actual_projection_keys:
                raise ValueError(f"duplicate projection seed for {key}")
            actual_projection_keys.add(key)
        if actual_projection_keys != required_projection_keys:
            raise ValueError("promotion projections must match the promoted fact business keys")

    @staticmethod
    def _insert_promoted_facts(database: FinanceDatabase, cursor: Any, plan: PromotionPlan) -> None:
        columns = (*FINANCIAL_FACT_COLUMNS, "review_version", "document_id")
        quoted_columns = ", ".join(f"`{column}`" if database.backend == "mysql" else column for column in columns)
        insert_sql = (
            f"INSERT INTO financial_fact ({quoted_columns}) VALUES ({database.parameter_markers(len(columns))})"
        )
        lock = " FOR UPDATE" if database.backend == "mysql" else ""
        for fact in plan.facts:
            params = database._financial_fact_params(fact)
            fact_key = str(params[0])
            cursor.execute(
                f"SELECT document_id FROM financial_fact WHERE fact_key = {database.parameter_marker}{lock}",
                (fact_key,),
            )
            existing = cursor.fetchone()
            if existing is not None:
                if dict(existing).get("document_id") != plan.document_id:
                    raise ConflictError("promoted fact identity is already bound to another document")
                continue
            cursor.execute(insert_sql, (*params, 1, plan.document_id))

    @staticmethod
    def _rebuild_promoted_projections(database: FinanceDatabase, cursor: Any, plan: PromotionPlan) -> None:
        for seed in plan.projections:
            row = seed.as_dict()
            columns = list(row)
            cursor.execute(database.build_upsert_sql(seed.table, columns), tuple(row[column] for column in columns))
            metric_columns = [field.field_name for field in database.schema[seed.table] if field.field_name not in row]
            values: dict[str, Any] = {}
            if metric_columns:
                cursor.execute(
                    "SELECT ff.metric, ff.normalized_value FROM financial_fact ff "
                    "INNER JOIN financial_source fs ON fs.source_key = ff.source_key "
                    "AND fs.stock_code = ff.stock_code AND fs.period = ff.period "
                    "WHERE ff.stock_code = {m} AND ff.period = {m} AND ff.table_name = {m} "
                    "AND ff.validation_status = 'VALIDATED' AND ff.statement_scope = {m} AND ff.period_type = {m} "
                    "AND fs.authority_status = {m} AND ff.metric IN ({markers})".format(
                        m=database.parameter_marker,
                        markers=database.parameter_markers(len(metric_columns)),
                    ),
                    (
                        row["stock_code"],
                        row["report_period"],
                        seed.table,
                        StatementScope.CONSOLIDATED.value,
                        expected_period_type_for_table(seed.table).value,
                        SourceAuthorityStatus.CURRENT.value,
                        *metric_columns,
                    ),
                )
                for fact_row in cursor.fetchall():
                    fact = dict(fact_row)
                    metric = str(fact["metric"])
                    if metric in values:
                        raise ValueError(
                            f"multiple VALIDATED facts conflict for {row['stock_code']}/{row['report_period']}/{metric}"
                        )
                    values[metric] = fact["normalized_value"]
            assignments = ", ".join(
                f"{database._quote_identifier(metric)} = {database.parameter_marker}" for metric in metric_columns
            )
            if assignments:
                cursor.execute(
                    f"UPDATE {database._quote_identifier(seed.table)} SET {assignments} "
                    f"WHERE stock_code = {database.parameter_marker} AND report_period = {database.parameter_marker}",
                    (*[values.get(metric) for metric in metric_columns], row["stock_code"], row["report_period"]),
                )

    @staticmethod
    def _insert_artifact(
        database: FinanceDatabase,
        cursor: Any,
        job_id: str,
        artifact: ArtifactManifest,
    ) -> None:
        if not all(
            str(value).strip() for value in (artifact.kind, artifact.storage_key, artifact.sha256, artifact.mime_type)
        ):
            raise ValueError("artifact metadata fields must be non-empty")
        if artifact.size_bytes < 0:
            raise ValueError("artifact size_bytes must be non-negative")
        storage_path = Path(artifact.storage_key)
        expected_prefix = Path("runs") / job_id
        if storage_path.is_absolute() or ".." in storage_path.parts or not storage_path.is_relative_to(expected_prefix):
            raise ValueError("artifact storage_key must stay inside the active job run directory")
        cursor.execute(
            "INSERT INTO artifact (artifact_id, job_id, kind, storage_key, sha256, size_bytes, mime_type, "
            f"created_at) VALUES ({database.parameter_markers(8)})",
            (
                str(uuid4()),
                job_id,
                artifact.kind,
                artifact.storage_key,
                artifact.sha256,
                artifact.size_bytes,
                artifact.mime_type,
                _utc_now(),
            ),
        )

    def refresh_document_review_status(self, document_id: str) -> DocumentRecord:
        with self._transaction() as (database, cursor):
            self._refresh_document_review_status_in_transaction(database, cursor, document_id)
        return self.get_document(document_id)

    def _update_document_status_in_transaction(
        self,
        database: FinanceDatabase,
        cursor: Any,
        document_id: str,
        status: str,
        *,
        error_code: str | None,
        error_message: str | None,
        page_count: int | None = None,
    ) -> None:
        assignments = [
            f"status = {database.parameter_marker}",
            f"error_code = {database.parameter_marker}",
            f"error_message = {database.parameter_marker}",
            f"updated_at = {database.parameter_marker}",
        ]
        params: list[Any] = [status, error_code, error_message, _utc_now()]
        if page_count is not None:
            assignments.append(f"page_count = {database.parameter_marker}")
            params.append(page_count)
        params.append(document_id)
        cursor.execute(
            f"UPDATE document SET {', '.join(assignments)} WHERE document_id = {database.parameter_marker}",
            tuple(params),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"Unknown document: {document_id}")

    def _refresh_document_review_status_in_transaction(
        self,
        database: FinanceDatabase,
        cursor: Any,
        document_id: str,
    ) -> None:
        lock = " FOR UPDATE" if database.backend == "mysql" else ""
        cursor.execute(
            f"SELECT status FROM document WHERE document_id = {database.parameter_marker}{lock}",
            (document_id,),
        )
        document = self._one(cursor)
        if document is None:
            raise KeyError(f"Unknown document: {document_id}")
        if document["status"] == "FAILED":
            return
        cursor.execute(
            "SELECT COUNT(*) AS total_count, "
            "SUM(CASE WHEN validation_status = 'NEEDS_REVIEW' THEN 1 ELSE 0 END) AS pending_count "
            f"FROM financial_fact WHERE document_id = {database.parameter_marker}",
            (document_id,),
        )
        counts = self._one(cursor) or {}
        total_count = int(counts.get("total_count") or 0)
        pending_count = int(counts.get("pending_count") or 0)
        next_status = "NEEDS_REVIEW" if total_count == 0 or pending_count > 0 else "COMPLETED"
        self._update_document_status_in_transaction(
            database,
            cursor,
            document_id,
            next_status,
            error_code=None,
            error_message=None,
        )

    def get_document(self, document_id: str) -> DocumentRecord:
        with self.database_factory.unit_of_work() as database:
            rows = database.query(
                f"SELECT * FROM document WHERE document_id = {database.parameter_marker}",
                (document_id,),
                use_cache=False,
            )
        if not rows:
            raise KeyError(f"Unknown document: {document_id}")
        return DocumentRecord(**rows[0])

    def require_current_document_content(self, document_ids: Sequence[str]) -> None:
        unique_ids = tuple(dict.fromkeys(str(document_id).strip() for document_id in document_ids))
        if not unique_ids or any(not document_id for document_id in unique_ids):
            raise SourceContentMismatchError("source_content_mismatch: verified answer has no document evidence")
        with self.database_factory.unit_of_work() as database:
            rows = database.query(
                "SELECT d.document_id, d.storage_key, d.sha256, d.size_bytes, "
                "fs.content_sha256 AS source_content_sha256 FROM document d "
                "INNER JOIN financial_source fs ON fs.document_id = d.document_id "
                f"WHERE d.document_id IN ({database.parameter_markers(len(unique_ids))}) "
                f"AND fs.authority_status = {database.parameter_marker}",
                (*unique_ids, SourceAuthorityStatus.CURRENT.value),
                use_cache=False,
            )
        rows_by_document: dict[str, list[dict[str, Any]]] = {document_id: [] for document_id in unique_ids}
        for row in rows:
            document_id = str(row["document_id"])
            if document_id in rows_by_document:
                rows_by_document[document_id].append(row)
        for document_id, matches in rows_by_document.items():
            if len(matches) != 1:
                raise SourceContentMismatchError(
                    "source_content_mismatch: "
                    f"document {document_id} must have exactly one CURRENT financial source, got {len(matches)}"
                )
            self._require_document_source_content_match(matches[0])

    def list_documents(self, *, limit: int = 100, offset: int = 0) -> tuple[list[DocumentRecord], int]:
        self._validate_limit(limit)
        self._validate_offset(offset)
        with self.database_factory.unit_of_work() as database:
            total = int(database.query("SELECT COUNT(*) AS total FROM document", use_cache=False)[0]["total"])
            rows = database.query(
                f"SELECT * FROM document ORDER BY created_at DESC LIMIT {database.parameter_marker} "
                f"OFFSET {database.parameter_marker}",
                (limit, offset),
                use_cache=False,
            )
        return [DocumentRecord(**row) for row in rows], total

    def create_job(
        self,
        job_type: str,
        input_payload: Mapping[str, Any],
        *,
        idempotency_key: str,
        parent_job_id: str | None = None,
        attempt: int = 1,
    ) -> JobRecord:
        if job_type == "document_ingestion":
            raise ValueError("document_ingestion jobs must be created with enqueue_document_job")
        self._validate_new_job(job_type, idempotency_key, attempt)
        with self._transaction() as (database, cursor):
            row, _ = self._create_job_in_transaction(
                database,
                cursor,
                job_type=job_type,
                input_payload=input_payload,
                idempotency_key=idempotency_key,
                parent_job_id=parent_job_id,
                attempt=attempt,
            )
        return self._job_record(row)

    def enqueue_document_job(self, document_id: str, *, idempotency_key: str) -> JobRecord:
        job_type = "document_ingestion"
        input_payload = {"document_id": document_id}
        self._validate_new_job(job_type, idempotency_key, 1)
        with self._transaction() as (database, cursor):
            lock = " FOR UPDATE" if database.backend == "mysql" else ""
            cursor.execute(
                f"SELECT status, active_ingestion_job_id FROM document "
                f"WHERE document_id = {database.parameter_marker}{lock}",
                (document_id,),
            )
            document = self._one(cursor)
            if document is None:
                raise KeyError(f"Unknown document: {document_id}")

            def require_stored_document() -> None:
                if document.get("active_ingestion_job_id"):
                    raise ConflictError("document already has an active ingestion job")
                if document["status"] != "STORED":
                    raise InvalidStateError(
                        f"document must be STORED before queueing; current status is {document['status']!r}"
                    )

            row, created = self._create_job_in_transaction(
                database,
                cursor,
                job_type=job_type,
                input_payload=input_payload,
                idempotency_key=idempotency_key,
                parent_job_id=None,
                attempt=1,
                before_insert=require_stored_document,
            )
            if created:
                now = _utc_now()
                cursor.execute(
                    "UPDATE document SET status = 'QUEUED', active_ingestion_job_id = {m}, updated_at = {m} "
                    "WHERE document_id = {m} AND status = 'STORED' AND active_ingestion_job_id IS NULL".format(
                        m=database.parameter_marker
                    ),
                    (str(row["job_id"]), now, document_id),
                )
                if cursor.rowcount != 1:
                    raise ConflictError("document status changed while queueing its ingestion job")
        return self._job_record(row)

    def _create_job_in_transaction(
        self,
        database: FinanceDatabase,
        cursor: Any,
        *,
        job_type: str,
        input_payload: Mapping[str, Any],
        idempotency_key: str,
        parent_job_id: str | None,
        attempt: int,
        before_insert: Callable[[], None] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        payload_json = _json_dump(dict(input_payload))
        cursor.execute(
            f"SELECT * FROM job WHERE idempotency_key = {database.parameter_marker}",
            (idempotency_key,),
        )
        existing = self._one(cursor)
        if existing is not None:
            if (
                existing["job_type"] != job_type
                or existing["input_json"] != payload_json
                or existing.get("parent_job_id") != parent_job_id
                or int(existing.get("attempt") or 1) != attempt
            ):
                raise ConflictError("idempotency key is already bound to a different job request")
            return existing, False
        if before_insert is not None:
            before_insert()
        if parent_job_id is not None:
            cursor.execute(
                f"SELECT status FROM job WHERE job_id = {database.parameter_marker}",
                (parent_job_id,),
            )
            if self._one(cursor) is None:
                raise KeyError(f"Unknown parent job: {parent_job_id}")
        job_id = str(uuid4())
        now = _utc_now()
        run_key = f"runs/{job_id}"
        cursor.execute(
            "INSERT INTO job (job_id, job_type, input_json, status, idempotency_key, parent_job_id, run_key, "
            f"progress, attempt, created_at, updated_at) VALUES ({database.parameter_markers(11)})",
            (
                job_id,
                job_type,
                payload_json,
                "QUEUED",
                idempotency_key,
                parent_job_id,
                run_key,
                0.0,
                attempt,
                now,
                now,
            ),
        )
        self._insert_job_event(database, cursor, job_id, "QUEUED", stage=None, message=None, created_at=now)
        cursor.execute(
            f"SELECT * FROM job WHERE job_id = {database.parameter_marker}",
            (job_id,),
        )
        created = self._one(cursor)
        if created is None:
            raise RuntimeError("job insert did not produce a readable row")
        return created, True

    @staticmethod
    def _validate_new_job(job_type: str, idempotency_key: str, attempt: int) -> None:
        if not job_type.strip() or not idempotency_key.strip():
            raise ValueError("job_type and idempotency_key must be non-empty")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("job attempt must be a positive integer")

    def get_job(self, job_id: str) -> JobRecord:
        with self.database_factory.unit_of_work() as database:
            rows = database.query(
                f"SELECT * FROM job WHERE job_id = {database.parameter_marker}",
                (job_id,),
                use_cache=False,
            )
        if not rows:
            raise KeyError(f"Unknown job: {job_id}")
        return self._job_record(rows[0])

    def list_jobs(self, *, limit: int = 100, offset: int = 0) -> list[JobRecord]:
        self._validate_limit(limit)
        self._validate_offset(offset)
        with self.database_factory.unit_of_work() as database:
            rows = database.query(
                f"SELECT * FROM job ORDER BY created_at DESC LIMIT {database.parameter_marker} "
                f"OFFSET {database.parameter_marker}",
                (limit, offset),
                use_cache=False,
            )
        return [self._job_record(row) for row in rows]

    def _require_active_job_lease(
        self,
        database: FinanceDatabase,
        cursor: Any,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not lease_token.strip():
            raise ValueError("lease_token must be non-empty")
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            raise ValueError("lease validation time must include timezone information")
        lock = " FOR UPDATE" if database.backend == "mysql" else ""
        cursor.execute(
            f"SELECT * FROM job WHERE job_id = {database.parameter_marker}{lock}",
            (job_id,),
        )
        row = self._one(cursor)
        if row is None:
            raise KeyError(f"Unknown job: {job_id}")
        if row["status"] != "RUNNING" or row.get("lease_owner") != worker_id or row.get("lease_token") != lease_token:
            raise ConflictError("job lease is not owned by this worker")
        expires_at = row.get("lease_expires_at")
        if not expires_at or datetime.fromisoformat(str(expires_at)) <= moment.astimezone(timezone.utc):
            raise ConflictError("job lease has expired")
        return row

    def _require_active_document_attempt(
        self,
        database: FinanceDatabase,
        cursor: Any,
        job: Mapping[str, Any],
        document_id: str,
    ) -> dict[str, Any]:
        payload = _json_load(str(job["input_json"]), default={})
        if payload.get("document_id") != document_id:
            raise ConflictError("job lease is not bound to this document")
        lock = " FOR UPDATE" if database.backend == "mysql" else ""
        cursor.execute(
            f"SELECT * FROM document WHERE document_id = {database.parameter_marker}{lock}",
            (document_id,),
        )
        document = self._one(cursor)
        if document is None:
            raise KeyError(f"Unknown document: {document_id}")
        if document.get("active_ingestion_job_id") != job["job_id"]:
            raise ConflictError("job is not the document's active ingestion attempt")
        return document

    def claim_job(self, worker_id: str, *, lease_seconds: int = 60) -> JobRecord | None:
        self._validate_lease(worker_id, lease_seconds)
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        lease_token = str(uuid4())
        with self._transaction() as (database, cursor):
            lock = " FOR UPDATE SKIP LOCKED" if database.backend == "mysql" else ""
            cursor.execute(f"SELECT job_id FROM job WHERE status = 'QUEUED' ORDER BY created_at LIMIT 1{lock}")
            selected = self._one(cursor)
            if selected is None:
                return None
            job_id = str(selected["job_id"])
            cursor.execute(
                "UPDATE job SET status = 'RUNNING', lease_owner = {m}, lease_token = {m}, lease_expires_at = {m}, "
                "heartbeat_at = {m}, started_at = COALESCE(started_at, {m}), updated_at = {m} "
                "WHERE job_id = {m} AND status = 'QUEUED'".format(m=database.parameter_marker),
                (worker_id, lease_token, expires, now_text, now_text, now_text, job_id),
            )
            if cursor.rowcount != 1:
                return None
            self._insert_job_event(database, cursor, job_id, "RUNNING", stage=None, message=None, created_at=now_text)
        return self.get_job(job_id)

    def heartbeat_job(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        lease_seconds: int = 60,
        stage: str | None = None,
        progress: float | None = None,
    ) -> JobRecord:
        self._validate_lease(worker_id, lease_seconds)
        if progress is not None and (
            isinstance(progress, bool) or not isinstance(progress, int | float) or not 0 <= float(progress) <= 1
        ):
            raise ValueError("progress must be between 0 and 1")
        now = datetime.now(timezone.utc)
        with self._transaction() as (database, cursor):
            row = self._require_active_job_lease(database, cursor, job_id, worker_id, lease_token, now=now)
            next_progress = float(row["progress"]) if progress is None else float(progress)
            next_stage = row["stage"] if stage is None else stage
            cursor.execute(
                "UPDATE job SET lease_expires_at = {m}, heartbeat_at = {m}, stage = {m}, progress = {m}, "
                "updated_at = {m} WHERE job_id = {m}".format(m=database.parameter_marker),
                (
                    (now + timedelta(seconds=lease_seconds)).isoformat(),
                    now.isoformat(),
                    next_stage,
                    next_progress,
                    now.isoformat(),
                    job_id,
                ),
            )
        return self.get_job(job_id)

    def complete_job(self, job_id: str, worker_id: str, lease_token: str) -> JobRecord:
        return self._finish_job(
            job_id,
            worker_id,
            lease_token,
            status="SUCCEEDED",
            error_code=None,
            error_message=None,
        )

    def fail_job(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        error_code: str,
        error_message: str,
    ) -> JobRecord:
        if not error_code.strip() or not error_message.strip():
            raise ValueError("failed jobs require an error code and message")
        return self._finish_job(
            job_id,
            worker_id,
            lease_token,
            status="FAILED",
            error_code=error_code,
            error_message=error_message,
        )

    def _finish_job(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        status: str,
        error_code: str | None,
        error_message: str | None,
    ) -> JobRecord:
        now = _utc_now()
        with self._transaction() as (database, cursor):
            job = self._require_active_job_lease(database, cursor, job_id, worker_id, lease_token)
            if job.get("job_type") == "document_ingestion":
                payload = _json_load(str(job.get("input_json") or ""), default={})
                document_id = str(payload.get("document_id") or "") if isinstance(payload, dict) else ""
                if not document_id:
                    raise InvalidStateError("document ingestion job has no document_id")
                self._require_active_document_attempt(database, cursor, job, document_id)
                if status == "SUCCEEDED":
                    raise InvalidStateError("document ingestion must succeed through atomic promotion")
            cursor.execute(
                "UPDATE job SET status = {m}, progress = {m}, error_code = {m}, error_message = {m}, "
                "lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, finished_at = {m}, updated_at = {m} "
                "WHERE job_id = {m}".format(m=database.parameter_marker),
                (status, 1.0 if status == "SUCCEEDED" else 0.0, error_code, error_message, now, now, job_id),
            )
            self._insert_job_event(database, cursor, job_id, status, stage=None, message=error_message, created_at=now)
            self._release_document_attempt(database, cursor, job, status, error_code, error_message, now)
        return self.get_job(job_id)

    def recover_interrupted(self, *, now: datetime | None = None) -> int:
        current = _to_timestamp(now)
        with self._transaction() as (database, cursor):
            lock = " FOR UPDATE" if database.backend == "mysql" else ""
            cursor.execute(
                "SELECT job_id, job_type, input_json FROM job WHERE status = 'RUNNING' AND lease_expires_at IS NOT NULL "
                f"AND lease_expires_at < {database.parameter_marker}{lock}",
                (current,),
            )
            jobs = [dict(row) for row in cursor.fetchall()]
            recovered = 0
            for job in jobs:
                job_id = str(job["job_id"])
                cursor.execute(
                    "UPDATE job SET status = 'INTERRUPTED', lease_owner = NULL, lease_token = NULL, "
                    "lease_expires_at = NULL, "
                    f"error_code = 'LEASE_EXPIRED', error_message = 'Worker lease expired', finished_at = {database.parameter_marker}, "
                    f"updated_at = {database.parameter_marker} WHERE job_id = {database.parameter_marker} "
                    f"AND status = 'RUNNING' AND lease_expires_at < {database.parameter_marker}",
                    (current, current, job_id, current),
                )
                if cursor.rowcount != 1:
                    continue
                recovered += 1
                self._insert_job_event(
                    database,
                    cursor,
                    job_id,
                    "INTERRUPTED",
                    stage=None,
                    message="Worker lease expired",
                    created_at=current,
                )
                self._release_document_attempt(
                    database,
                    cursor,
                    job,
                    "INTERRUPTED",
                    "LEASE_EXPIRED",
                    "Worker lease expired",
                    current,
                )
        return recovered

    @staticmethod
    def _release_document_attempt(
        database: FinanceDatabase,
        cursor: Any,
        job: Mapping[str, Any],
        status: str,
        error_code: str | None,
        error_message: str | None,
        now: str,
    ) -> None:
        if job.get("job_type") != "document_ingestion":
            return
        payload = _json_load(str(job.get("input_json") or ""), default={})
        document_id = payload.get("document_id")
        if not document_id:
            return
        if status == "SUCCEEDED":
            cursor.execute(
                f"UPDATE document SET active_ingestion_job_id = NULL, updated_at = {database.parameter_marker} "
                f"WHERE document_id = {database.parameter_marker} "
                f"AND active_ingestion_job_id = {database.parameter_marker}",
                (now, document_id, job["job_id"]),
            )
            return
        cursor.execute(
            "UPDATE document SET status = 'FAILED', error_code = COALESCE(error_code, {m}), "
            "error_message = COALESCE(error_message, {m}), "
            "active_ingestion_job_id = {m}, updated_at = {m} WHERE document_id = {m} "
            "AND active_ingestion_job_id = {m}".format(m=database.parameter_marker),
            (error_code, error_message, job["job_id"], now, document_id, job["job_id"]),
        )

    def retry_job(self, job_id: str, *, idempotency_key: str) -> JobRecord:
        with self._transaction() as (database, cursor):
            lock = " FOR UPDATE" if database.backend == "mysql" else ""
            cursor.execute(
                f"SELECT * FROM job WHERE job_id = {database.parameter_marker}{lock}",
                (job_id,),
            )
            source = self._one(cursor)
            if source is None:
                raise KeyError(f"Unknown job: {job_id}")
            if source["status"] not in {"FAILED", "INTERRUPTED"}:
                raise InvalidStateError("only FAILED or INTERRUPTED jobs can be retried")
            payload = _json_load(str(source["input_json"]), default={})
            document_id = str(payload.get("document_id") or "")
            if source["job_type"] == "document_ingestion" and not document_id:
                raise InvalidStateError("document ingestion job has no document_id")
            document = None
            if source["job_type"] == "document_ingestion" and document_id:
                cursor.execute(
                    f"SELECT * FROM document WHERE document_id = {database.parameter_marker}{lock}",
                    (document_id,),
                )
                document = self._one(cursor)
                if document is None:
                    raise KeyError(f"Unknown document: {document_id}")
                cursor.execute(
                    f"SELECT * FROM job WHERE idempotency_key = {database.parameter_marker}",
                    (idempotency_key,),
                )
                existing = self._one(cursor)
                if existing is not None:
                    expected_attempt = int(source.get("attempt") or 1) + 1
                    if (
                        existing["job_type"] != source["job_type"]
                        or existing["input_json"] != source["input_json"]
                        or existing.get("parent_job_id") != job_id
                        or int(existing.get("attempt") or 1) != expected_attempt
                    ):
                        raise ConflictError("idempotency key is already bound to a different job request")
                    return self._job_record(existing)
                if document.get("active_ingestion_job_id") != job_id:
                    raise ConflictError("only the document's current ingestion attempt may be retried")
                if document.get("status") != "FAILED":
                    raise ConflictError("document must be FAILED before retrying ingestion")
            row, created = self._create_job_in_transaction(
                database,
                cursor,
                job_type=str(source["job_type"]),
                input_payload=payload,
                idempotency_key=idempotency_key,
                parent_job_id=job_id,
                attempt=int(source.get("attempt") or 1) + 1,
            )
            if created and document is not None:
                cursor.execute(
                    "UPDATE document SET status = 'QUEUED', active_ingestion_job_id = {m}, "
                    "error_code = NULL, error_message = NULL, updated_at = {m} "
                    "WHERE document_id = {m} AND active_ingestion_job_id = {m} AND status = 'FAILED'".format(
                        m=database.parameter_marker
                    ),
                    (str(row["job_id"]), _utc_now(), document_id, job_id),
                )
                if cursor.rowcount != 1:
                    raise ConflictError("document gained another active ingestion job during retry")
        return self._job_record(row)

    def create_artifact(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        kind: str,
        storage_key: str,
        sha256: str,
        size_bytes: int,
        mime_type: str,
    ) -> ArtifactRecord:
        if not all(str(value).strip() for value in (kind, storage_key, sha256, mime_type)):
            raise ValueError("artifact metadata fields must be non-empty")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
            raise ValueError("artifact size_bytes must be a non-negative integer")
        artifact_id = str(uuid4())
        now = _utc_now()
        with self._transaction() as (database, cursor):
            job = self._require_active_job_lease(database, cursor, job_id, worker_id, lease_token)
            if job.get("job_type") == "document_ingestion":
                raise InvalidStateError("document ingestion artifacts must be recorded through atomic promotion")
            cursor.execute(
                "INSERT INTO artifact (artifact_id, job_id, kind, storage_key, sha256, size_bytes, mime_type, "
                f"created_at) VALUES ({database.parameter_markers(8)})",
                (artifact_id, job_id, kind, storage_key, sha256, size_bytes, mime_type, now),
            )
        return ArtifactRecord(artifact_id, job_id, kind, storage_key, sha256, size_bytes, mime_type, now)

    def list_artifacts(self, job_id: str) -> list[ArtifactRecord]:
        with self.database_factory.unit_of_work() as database:
            rows = database.query(
                f"SELECT * FROM artifact WHERE job_id = {database.parameter_marker} ORDER BY created_at",
                (job_id,),
                use_cache=False,
            )
        return [ArtifactRecord(**row) for row in rows]

    def run_directory(self, job_id: str) -> Path:
        if _UUID_PATTERN.fullmatch(job_id) is None:
            raise ValueError("job_id must be a UUID")
        UUID(job_id)
        run_dir = (self.output_root / "runs" / job_id).resolve()
        allowed_root = (self.output_root / "runs").resolve()
        if run_dir.parent != allowed_root:
            raise ValueError("job run directory escapes the configured output root")
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def create_conversation(self, *, title: str = "新会话") -> ConversationRecord:
        conversation_id = str(uuid4())
        now = _utc_now()
        state = {"slots": {}, "context": {}}
        with self._transaction() as (database, cursor):
            cursor.execute(
                "INSERT INTO conversation (conversation_id, title, session_state_json, version, created_at, updated_at) "
                f"VALUES ({database.parameter_markers(6)})",
                (conversation_id, title.strip() or "新会话", _json_dump(state), 1, now, now),
            )
        return self.get_conversation(conversation_id)

    def get_conversation(self, conversation_id: str) -> ConversationRecord:
        with self.database_factory.unit_of_work() as database:
            rows = database.query(
                f"SELECT * FROM conversation WHERE conversation_id = {database.parameter_marker}",
                (conversation_id,),
                use_cache=False,
            )
        if not rows:
            raise KeyError(f"Unknown conversation: {conversation_id}")
        row = rows[0]
        return ConversationRecord(
            conversation_id=str(row["conversation_id"]),
            title=str(row["title"]),
            session_state=_json_load(row["session_state_json"], default={"slots": {}, "context": {}}),
            version=int(row["version"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def list_conversations(self, *, limit: int = 100) -> list[ConversationRecord]:
        self._validate_limit(limit)
        with self.database_factory.unit_of_work() as database:
            rows = database.query(
                f"SELECT conversation_id FROM conversation ORDER BY updated_at DESC LIMIT {database.parameter_marker}",
                (limit,),
                use_cache=False,
            )
        return [self.get_conversation(str(row["conversation_id"])) for row in rows]

    def append_turn(
        self,
        conversation_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
        question: str,
        answer_payload: Mapping[str, Any],
        session_state: Mapping[str, Any],
        query_run: Mapping[str, Any] | None = None,
    ) -> TurnRecord:
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
            raise ValueError("expected_version must be a non-negative integer")
        if not idempotency_key.strip() or not question.strip():
            raise ValueError("idempotency_key and question must be non-empty")
        with self._transaction() as (database, cursor):
            cursor.execute(
                "SELECT * FROM conversation_turn WHERE conversation_id = {m} AND idempotency_key = {m}".format(
                    m=database.parameter_marker
                ),
                (conversation_id, idempotency_key),
            )
            existing = self._one(cursor)
            if existing is not None:
                if existing["question"] != question:
                    raise ConflictError("idempotency key is already bound to a different conversation turn")
                return self._turn_record(existing)
            lock = " FOR UPDATE" if database.backend == "mysql" else ""
            cursor.execute(
                f"SELECT version FROM conversation WHERE conversation_id = {database.parameter_marker}{lock}",
                (conversation_id,),
            )
            conversation = self._one(cursor)
            if conversation is None:
                raise KeyError(f"Unknown conversation: {conversation_id}")
            actual_version = int(conversation["version"])
            if actual_version != expected_version:
                raise ConflictError(
                    f"conversation version conflict: expected {expected_version}, current {actual_version}"
                )
            turn_id = str(uuid4())
            cursor.execute(
                f"SELECT COALESCE(MAX(sequence_no), 0) AS last_sequence FROM conversation_turn "
                f"WHERE conversation_id = {database.parameter_marker}",
                (conversation_id,),
            )
            sequence_no = int(self._one(cursor)["last_sequence"]) + 1
            query_run_id = None
            if query_run is not None:
                query_run_id = self._insert_query_run(
                    database,
                    cursor,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    payload=query_run,
                )
            now = _utc_now()
            cursor.execute(
                "INSERT INTO conversation_turn (turn_id, conversation_id, sequence_no, idempotency_key, question, "
                f"answer_payload_json, query_run_id, created_at) VALUES ({database.parameter_markers(8)})",
                (
                    turn_id,
                    conversation_id,
                    sequence_no,
                    idempotency_key,
                    question,
                    _json_dump(dict(answer_payload)),
                    query_run_id,
                    now,
                ),
            )
            cursor.execute(
                f"UPDATE conversation SET session_state_json = {database.parameter_marker}, "
                f"version = {database.parameter_marker}, updated_at = {database.parameter_marker} "
                f"WHERE conversation_id = {database.parameter_marker} AND version = {database.parameter_marker}",
                (_json_dump(dict(session_state)), actual_version + 1, now, conversation_id, actual_version),
            )
            if cursor.rowcount != 1:
                raise ConflictError("conversation version changed while appending the turn")
        return self.get_turn(turn_id)

    def get_turn(self, turn_id: str) -> TurnRecord:
        with self.database_factory.unit_of_work() as database:
            rows = database.query(
                f"SELECT * FROM conversation_turn WHERE turn_id = {database.parameter_marker}",
                (turn_id,),
                use_cache=False,
            )
        if not rows:
            raise KeyError(f"Unknown conversation turn: {turn_id}")
        return self._turn_record(rows[0])

    def find_turn_by_idempotency(self, conversation_id: str, idempotency_key: str) -> TurnRecord | None:
        with self.database_factory.unit_of_work() as database:
            rows = database.query(
                "SELECT * FROM conversation_turn WHERE conversation_id = {m} AND idempotency_key = {m}".format(
                    m=database.parameter_marker
                ),
                (conversation_id, idempotency_key),
                use_cache=False,
            )
        return None if not rows else self._turn_record(rows[0])

    def list_turns(self, conversation_id: str) -> list[TurnRecord]:
        with self.database_factory.unit_of_work() as database:
            rows = database.query(
                f"SELECT * FROM conversation_turn WHERE conversation_id = {database.parameter_marker} "
                "ORDER BY sequence_no",
                (conversation_id,),
                use_cache=False,
            )
        return [self._turn_record(row) for row in rows]

    def _insert_query_run(
        self,
        database: FinanceDatabase,
        cursor: Any,
        *,
        conversation_id: str,
        turn_id: str,
        payload: Mapping[str, Any],
    ) -> str:
        required = {
            "normalized_question",
            "query_spec",
            "sql",
            "params",
            "fact_sources",
            "calculation",
            "answer_payload",
            "request_id",
            "schema_version",
            "code_version",
            "status",
        }
        missing = sorted(required - set(payload))
        if missing:
            raise ValueError(f"query run is missing required fields: {missing}")
        query_run_id = str(uuid4())
        cursor.execute(
            "INSERT INTO query_run (query_run_id, conversation_id, turn_id, normalized_question, query_spec_json, "
            "sql_text, params_json, fact_snapshot_json, calculation_json, answer_payload_json, request_id, "
            f"schema_version, code_version, status, error_message, created_at) VALUES ({database.parameter_markers(16)})",
            (
                query_run_id,
                conversation_id,
                turn_id,
                str(payload["normalized_question"]),
                _json_dump(payload["query_spec"]),
                str(payload["sql"]),
                _json_dump(payload["params"]),
                _json_dump(payload["fact_sources"]),
                _json_dump(payload["calculation"]),
                _json_dump(payload["answer_payload"]),
                str(payload["request_id"]),
                str(payload["schema_version"]),
                str(payload["code_version"]),
                str(payload["status"]),
                payload.get("error_message"),
                _utc_now(),
            ),
        )
        return query_run_id

    def get_query_run(self, query_run_id: str) -> dict[str, Any]:
        with self.database_factory.unit_of_work() as database:
            rows = database.query(
                f"SELECT * FROM query_run WHERE query_run_id = {database.parameter_marker}",
                (query_run_id,),
                use_cache=False,
            )
        if not rows:
            raise KeyError(f"Unknown query run: {query_run_id}")
        row = rows[0]
        return {
            "query_run_id": row["query_run_id"],
            "conversation_id": row["conversation_id"],
            "turn_id": row["turn_id"],
            "normalized_question": row["normalized_question"],
            "query_spec": _json_load(row["query_spec_json"], default={}),
            "sql": row["sql_text"],
            "params": _json_load(row["params_json"], default=[]),
            "fact_sources": _json_load(row["fact_snapshot_json"], default=[]),
            "calculation": _json_load(row["calculation_json"], default={}),
            "answer_payload": _json_load(row["answer_payload_json"], default={}),
            "request_id": row["request_id"],
            "schema_version": row["schema_version"],
            "code_version": row["code_version"],
            "status": row["status"],
            "error_message": row["error_message"],
            "created_at": row["created_at"],
        }

    def review_fact(
        self,
        fact_key: str,
        *,
        action: str,
        expected_version: int,
        actor_label: str,
        reason: str,
        corrections: Mapping[str, Any] | None = None,
        note: str = "",
    ) -> FactReviewResult:
        normalized_action = action.strip().upper()
        if normalized_action not in REVIEW_ACTIONS:
            raise ValueError(f"review action must be one of: {sorted(REVIEW_ACTIONS)}")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
            raise ValueError("expected_version must be a non-negative integer")
        if not actor_label.strip() or not reason.strip():
            raise ValueError("actor_label and reason must be non-empty")
        correction_payload = dict(corrections or {})
        unknown = sorted(set(correction_payload) - CORRECTABLE_FACT_FIELDS)
        if unknown:
            raise ValueError(f"unknown correction field(s): {unknown}")
        if normalized_action != "CORRECT" and correction_payload:
            raise ValueError("corrections are only allowed for CORRECT review actions")
        if normalized_action == "CORRECT" and not correction_payload:
            raise ValueError("CORRECT requires at least one corrected field")

        with self._transaction() as (database, cursor):
            lock = " FOR UPDATE" if database.backend == "mysql" else ""
            cursor.execute(
                "SELECT ff.*, fs.content_sha256 AS source_content_sha256 FROM financial_fact ff "
                "INNER JOIN financial_source fs ON fs.source_key = ff.source_key "
                "AND fs.stock_code = ff.stock_code AND fs.period = ff.period "
                f"WHERE ff.fact_key = {database.parameter_marker}{lock}",
                (fact_key,),
            )
            row = self._one(cursor)
            if row is None:
                raise KeyError(f"Unknown financial fact: {fact_key}")
            cursor.execute(
                "SELECT source_key FROM financial_source WHERE source_key = {m} "
                "AND authority_status = {m}{lock}".format(
                    m=database.parameter_marker,
                    lock=lock,
                ),
                (
                    row["source_key"],
                    SourceAuthorityStatus.CURRENT.value,
                ),
            )
            if cursor.fetchone() is None:
                raise ConflictError("fact source is not CURRENT and cannot be reviewed")
            actual_version = int(row.get("review_version") or 1)
            if actual_version != expected_version:
                raise ConflictError(
                    f"fact review version conflict: expected {expected_version}, current {actual_version}"
                )
            self._require_fact_source_content_match(database, cursor, row)
            before = self._public_fact_snapshot(row)
            replaced_fact_key = None
            if normalized_action == "VALIDATE":
                validated = self._fact_from_row(row, validation_status=ValidationStatus.VALIDATED)
                self._require_fact_page_within_document(database, cursor, row, validated.page_no)
                self._require_projection_and_no_conflict(database, cursor, row, fact_key)
                self._update_review_state(
                    database,
                    cursor,
                    fact_key,
                    status=ValidationStatus.VALIDATED,
                    issues=(),
                    version=actual_version + 1,
                )
                self._update_projection(database, cursor, row, validated.normalized_value)
                result_key = fact_key
                result_version = actual_version + 1
                after = {**before, "validation_status": "VALIDATED", "review_version": result_version}
            elif normalized_action == "REJECT":
                issue = ValidationIssue(code="human_rejected", message=reason)
                self._fact_from_row(row, validation_status=ValidationStatus.REJECTED, issues=(issue,))
                self._update_review_state(
                    database,
                    cursor,
                    fact_key,
                    status=ValidationStatus.REJECTED,
                    issues=(issue,),
                    version=actual_version + 1,
                )
                if row["validation_status"] == ValidationStatus.VALIDATED.value:
                    self._update_projection(database, cursor, row, None)
                result_key = fact_key
                result_version = actual_version + 1
                after = {**before, "validation_status": "REJECTED", "review_version": result_version}
            else:
                corrected_fact = self._corrected_fact(row, correction_payload)
                self._require_fact_page_within_document(database, cursor, row, corrected_fact.page_no)
                corrected_params = database._financial_fact_params(corrected_fact)
                replaced_fact_key = str(corrected_params[0])
                self._require_projection_and_no_conflict(database, cursor, asdict(corrected_fact), fact_key)
                issue = ValidationIssue(code="superseded_by_correction", message=reason)
                self._update_review_state(
                    database,
                    cursor,
                    fact_key,
                    status=ValidationStatus.REJECTED,
                    issues=(issue,),
                    version=actual_version + 1,
                )
                corrected_values = list(corrected_params)
                corrected_values[FINANCIAL_FACT_COLUMNS.index("validation_status")] = ValidationStatus.VALIDATED.value
                corrected_values[FINANCIAL_FACT_COLUMNS.index("validation_issues")] = "[]"
                columns = (*FINANCIAL_FACT_COLUMNS, "review_version", "document_id")
                cursor.execute(
                    f"INSERT INTO financial_fact ({', '.join(columns)}) VALUES ({database.parameter_markers(len(columns))})",
                    (*corrected_values, 1, row.get("document_id")),
                )
                corrected_row = {column: value for column, value in zip(FINANCIAL_FACT_COLUMNS, corrected_values)}
                self._update_projection(database, cursor, corrected_row, corrected_fact.normalized_value)
                result_key = replaced_fact_key
                result_version = 1
                after = self._public_fact_snapshot({**corrected_row, "review_version": 1})
            event_id = str(uuid4())
            cursor.execute(
                "INSERT INTO fact_review_event (event_id, fact_key, replacement_fact_key, action, before_json, "
                f"after_json, reason, note, actor_label, expected_version, created_at) VALUES ({database.parameter_markers(11)})",
                (
                    event_id,
                    fact_key,
                    replaced_fact_key,
                    normalized_action,
                    _json_dump(before),
                    _json_dump(after),
                    reason,
                    note,
                    actor_label,
                    expected_version,
                    _utc_now(),
                ),
            )
            document_id = row.get("document_id")
            if document_id:
                self._refresh_document_review_status_in_transaction(database, cursor, str(document_id))
        return FactReviewResult(
            fact_key=result_key,
            validation_status=ValidationStatus.VALIDATED.value if normalized_action != "REJECT" else "REJECTED",
            review_version=result_version,
            replaced_fact_key=replaced_fact_key,
            event_id=event_id,
        )

    def _require_fact_page_within_document(
        self,
        database: FinanceDatabase,
        cursor: Any,
        fact: Mapping[str, Any],
        page_no: int | None,
    ) -> None:
        document_id = fact.get("document_id")
        if not document_id:
            raise ValueError("fact must be linked to a document before validation")
        if isinstance(page_no, bool) or not isinstance(page_no, int) or page_no < 1:
            raise ValueError("fact page_no must be a positive integer")
        lock = " FOR UPDATE" if database.backend == "mysql" else ""
        cursor.execute(
            f"SELECT page_count FROM document WHERE document_id = {database.parameter_marker}{lock}",
            (document_id,),
        )
        document = self._one(cursor)
        if document is None:
            raise ValueError("fact document does not exist")
        page_count = document.get("page_count")
        if page_count is None:
            raise ValueError("document page_count is unknown; re-ingest the PDF before validation")
        if page_no > int(page_count):
            raise ValueError(f"fact page_no {page_no} exceeds document page_count {page_count}")

    def _require_fact_source_content_match(
        self,
        database: FinanceDatabase,
        cursor: Any,
        fact: Mapping[str, Any],
    ) -> None:
        document_id = fact.get("document_id")
        if not document_id:
            raise ValueError("fact must be linked to a document before review")
        lock = " FOR UPDATE" if database.backend == "mysql" else ""
        cursor.execute(
            f"SELECT storage_key, sha256, size_bytes FROM document "
            f"WHERE document_id = {database.parameter_marker}{lock}",
            (document_id,),
        )
        document = self._one(cursor)
        if document is None:
            raise ValueError("fact document does not exist")
        self._require_document_source_content_match(
            {**document, "source_content_sha256": fact.get("source_content_sha256")}
        )

    def _require_document_source_content_match(self, document: Mapping[str, Any]) -> None:
        registered_digest = str(document.get("sha256") or "")
        source_digest = str(document.get("source_content_sha256") or "")
        if registered_digest != source_digest:
            raise SourceContentMismatchError(
                "source_content_mismatch: document SHA-256 does not match the CURRENT financial source"
            )
        path = self._resolve_document_file(str(document.get("storage_key") or ""))
        try:
            current_digest = sha256_file(path)
        except SourceIdentityError as exc:
            raise SourceContentMismatchError(f"source_content_mismatch: {exc}") from exc
        if current_digest != registered_digest or path.stat().st_size != int(document["size_bytes"]):
            raise SourceContentMismatchError(
                "source_content_mismatch: stored PDF bytes do not match the registered document"
            )

    def _resolve_document_file(self, storage_key: str) -> Path:
        if "\\" in storage_key:
            raise SourceContentMismatchError("source_content_mismatch: document storage key is invalid")
        key = PurePosixPath(storage_key)
        if key.is_absolute() or ".." in key.parts or not key.parts:
            raise SourceContentMismatchError("source_content_mismatch: document storage key is invalid")
        try:
            resolved = (self.output_root / Path(*key.parts)).resolve(strict=True)
        except OSError as exc:
            raise SourceContentMismatchError("source_content_mismatch: stored PDF does not exist") from exc
        if not resolved.is_relative_to(self.output_root) or not resolved.is_file():
            raise SourceContentMismatchError("source_content_mismatch: stored PDF path is invalid")
        return resolved

    def list_facts(
        self,
        *,
        validation_status: str | None = None,
        company_id: str | None = None,
        period: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        self._validate_limit(limit)
        self._validate_offset(offset)
        where: list[str] = []
        params: list[Any] = []
        with self.database_factory.unit_of_work() as database:
            where.append(f"fs.authority_status = {database.parameter_marker}")
            params.append(SourceAuthorityStatus.CURRENT.value)
            if validation_status is not None:
                ValidationStatus(validation_status)
                where.append(f"ff.validation_status = {database.parameter_marker}")
                params.append(validation_status)
            if company_id is not None:
                where.append(f"ff.company_id = {database.parameter_marker}")
                params.append(company_id)
            if period is not None:
                where.append(f"ff.period = {database.parameter_marker}")
                params.append(period)
            where_sql = f" WHERE {' AND '.join(where)}"
            filtered_sql = (
                " FROM financial_fact ff "
                "INNER JOIN financial_source fs ON fs.source_key = ff.source_key "
                "AND fs.stock_code = ff.stock_code AND fs.period = ff.period"
                f"{where_sql}"
            )
            total = int(
                database.query(
                    f"SELECT COUNT(*) AS total{filtered_sql}",
                    tuple(params),
                    use_cache=False,
                )[0]["total"]
            )
            rows = database.query(
                f"SELECT ff.*{filtered_sql} ORDER BY ff.fact_key LIMIT {database.parameter_marker} "
                f"OFFSET {database.parameter_marker}",
                (*params, limit, offset),
                use_cache=False,
            )
        return [self._public_fact_snapshot(row) for row in rows], total

    def get_fact(self, fact_key: str) -> dict[str, Any]:
        with self.database_factory.unit_of_work() as database:
            rows = database.query(
                f"SELECT * FROM financial_fact WHERE fact_key = {database.parameter_marker}",
                (fact_key,),
                use_cache=False,
            )
        if not rows:
            raise KeyError(f"Unknown financial fact: {fact_key}")
        return self._public_fact_snapshot(rows[0])

    @staticmethod
    def _public_fact_snapshot(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: row.get(key)
            for key in (
                "fact_key",
                "document_id",
                "company_id",
                "stock_code",
                "period",
                "statement_scope",
                "period_type",
                "metric",
                "raw_value",
                "normalized_value",
                "source_unit",
                "target_unit",
                "currency",
                "page_no",
                "table_name",
                "row_label",
                "column_label",
                "confidence",
                "validation_status",
                "validation_issues",
                "extractor_version",
                "review_version",
            )
        }

    @staticmethod
    def _fact_from_row(
        row: Mapping[str, Any],
        *,
        validation_status: ValidationStatus,
        issues: tuple[ValidationIssue, ...] = (),
    ) -> FinancialFact:
        return FinancialFact(
            company_id=str(row["company_id"]),
            stock_code=str(row["stock_code"]),
            period=str(row["period"]),
            statement_scope=StatementScope(str(row["statement_scope"])),
            period_type=PeriodType(str(row["period_type"])),
            metric=str(row["metric"]),
            raw_value=row["raw_value"],
            normalized_value=float(row["normalized_value"]),
            source_unit=str(row["source_unit"]),
            target_unit=str(row["target_unit"]),
            currency=str(row["currency"]),
            source_file=str(row["source_file"]),
            source_content_sha256=str(row["source_content_sha256"]),
            page_no=row.get("page_no"),
            table_name=row.get("table_name"),
            row_label=row.get("row_label"),
            column_label=row.get("column_label"),
            confidence=float(row["confidence"]),
            validation_status=validation_status,
            validation_issues=issues,
            extractor_version=str(row["extractor_version"]),
        )

    def _corrected_fact(self, row: Mapping[str, Any], corrections: Mapping[str, Any]) -> FinancialFact:
        merged = dict(row)
        merged.update(corrections)
        candidate = ExtractedFactCandidate(
            metric=str(merged["metric"]),
            raw_value=merged["raw_value"],
            source_unit=str(merged["source_unit"]),
            page_no=merged.get("page_no"),
            table_name=merged.get("table_name"),
            row_label=merged.get("row_label"),
            column_label=merged.get("column_label"),
            confidence=float(merged["confidence"]),
        )
        candidate_fact = FinancialFact.from_candidate(
            candidate,
            company_id=str(merged["company_id"]),
            stock_code=str(merged["stock_code"]),
            period=str(merged["period"]),
            statement_scope=StatementScope(str(merged["statement_scope"])),
            period_type=PeriodType(str(merged["period_type"])),
            target_unit=str(merged["target_unit"]),
            currency=str(merged["currency"]),
            source_file=str(merged["source_file"]),
            source_content_sha256=str(merged["source_content_sha256"]),
            extractor_version=f"human-correction:{merged['extractor_version']}",
        )
        if (
            "normalized_value" in corrections
            and float(corrections["normalized_value"]) != candidate_fact.normalized_value
        ):
            raise ValueError("normalized_value does not match raw_value and unit conversion")
        return replace(candidate_fact, validation_status=ValidationStatus.VALIDATED, validation_issues=())

    @staticmethod
    def _update_review_state(
        database: FinanceDatabase,
        cursor: Any,
        fact_key: str,
        *,
        status: ValidationStatus,
        issues: tuple[ValidationIssue, ...],
        version: int,
    ) -> None:
        cursor.execute(
            f"UPDATE financial_fact SET validation_status = {database.parameter_marker}, "
            f"validation_issues = {database.parameter_marker}, review_version = {database.parameter_marker} "
            f"WHERE fact_key = {database.parameter_marker}",
            (status.value, _json_dump([asdict(issue) for issue in issues]), version, fact_key),
        )

    def _require_projection_and_no_conflict(
        self,
        database: FinanceDatabase,
        cursor: Any,
        fact: Mapping[str, Any],
        excluded_fact_key: str,
    ) -> None:
        target_tables = [
            table
            for table, fields in database.schema.items()
            if str(fact["metric"]) in {field.field_name for field in fields}
        ]
        if not target_tables:
            raise ValueError(f"fact metric has no compatibility projection: {fact['metric']!r}")
        expected_period_types = {expected_period_type_for_table(table).value for table in target_tables}
        period_type = getattr(fact["period_type"], "value", fact["period_type"])
        if period_type not in expected_period_types:
            raise ValueError(f"fact period_type {period_type!r} does not match metric table")
        scope = getattr(fact["statement_scope"], "value", fact["statement_scope"])
        cursor.execute(
            "SELECT ff.fact_key FROM financial_fact ff INNER JOIN financial_source fs "
            "ON fs.source_key = ff.source_key AND fs.stock_code = ff.stock_code AND fs.period = ff.period "
            "WHERE ff.stock_code = {m} AND ff.period = {m} AND ff.statement_scope = {m} "
            "AND ff.period_type = {m} AND ff.metric = {m} AND ff.validation_status = 'VALIDATED' "
            "AND fs.authority_status = {m} AND ff.fact_key <> {m}".format(m=database.parameter_marker),
            (
                fact["stock_code"],
                fact["period"],
                scope,
                period_type,
                fact["metric"],
                SourceAuthorityStatus.CURRENT.value,
                excluded_fact_key,
            ),
        )
        if cursor.fetchone() is not None:
            raise ValueError("a conflicting VALIDATED fact already exists for this business key")
        if scope != StatementScope.CONSOLIDATED.value:
            return
        for table in target_tables:
            cursor.execute(
                f"SELECT stock_code FROM {database._quote_identifier(table)} WHERE stock_code = {database.parameter_marker} "
                f"AND report_period = {database.parameter_marker}",
                (fact["stock_code"], fact["period"]),
            )
            if cursor.fetchone() is not None:
                return
        raise ValueError("fact has no projection row; ingest the report before applying review")

    @staticmethod
    def _update_projection(database: FinanceDatabase, cursor: Any, fact: Mapping[str, Any], value: Any) -> None:
        scope = getattr(fact["statement_scope"], "value", fact["statement_scope"])
        if scope != StatementScope.CONSOLIDATED.value:
            return
        for table, fields in database.schema.items():
            if str(fact["metric"]) not in {field.field_name for field in fields}:
                continue
            cursor.execute(
                f"UPDATE {database._quote_identifier(table)} SET {database._quote_identifier(str(fact['metric']))} = "
                f"{database.parameter_marker} WHERE stock_code = {database.parameter_marker} "
                f"AND report_period = {database.parameter_marker}",
                (value, fact["stock_code"], fact["period"]),
            )

    @staticmethod
    def _insert_job_event(
        database: FinanceDatabase,
        cursor: Any,
        job_id: str,
        status: str,
        *,
        stage: str | None,
        message: str | None,
        created_at: str,
    ) -> None:
        cursor.execute(
            f"INSERT INTO job_event (event_id, job_id, status, stage, message, created_at) "
            f"VALUES ({database.parameter_markers(6)})",
            (str(uuid4()), job_id, status, stage, message, created_at),
        )

    @staticmethod
    def _job_record(row: Mapping[str, Any]) -> JobRecord:
        return JobRecord(
            job_id=str(row["job_id"]),
            job_type=str(row["job_type"]),
            input_payload=_json_load(str(row["input_json"]), default={}),
            status=str(row["status"]),
            idempotency_key=str(row["idempotency_key"]),
            parent_job_id=row.get("parent_job_id"),
            attempt=int(row.get("attempt") or 1),
            run_key=str(row["run_key"]),
            lease_owner=row.get("lease_owner"),
            lease_token=row.get("lease_token"),
            lease_expires_at=row.get("lease_expires_at"),
            heartbeat_at=row.get("heartbeat_at"),
            stage=row.get("stage"),
            progress=float(row["progress"]),
            error_code=row.get("error_code"),
            error_message=row.get("error_message"),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            started_at=row.get("started_at"),
            finished_at=row.get("finished_at"),
        )

    @staticmethod
    def _turn_record(row: Mapping[str, Any]) -> TurnRecord:
        return TurnRecord(
            turn_id=str(row["turn_id"]),
            conversation_id=str(row["conversation_id"]),
            sequence_no=int(row["sequence_no"]),
            idempotency_key=str(row["idempotency_key"]),
            question=str(row["question"]),
            answer_payload=_json_load(str(row["answer_payload_json"]), default={}),
            query_run_id=row.get("query_run_id"),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer from 1 to 1000")

    @staticmethod
    def _validate_offset(offset: int) -> None:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")

    @staticmethod
    def _validate_lease(worker_id: str, lease_seconds: int) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id must be non-empty")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds < 1:
            raise ValueError("lease_seconds must be a positive integer")

    @staticmethod
    def _validate_page_count(page_count: int | None) -> None:
        if page_count is None:
            return
        if isinstance(page_count, bool) or not isinstance(page_count, int) or page_count < 1:
            raise ValueError("page_count must be a positive integer")

    @staticmethod
    def _validate_document_status(
        status: str,
        *,
        error_code: str | None,
        error_message: str | None,
    ) -> None:
        if status not in DOCUMENT_STATUSES:
            raise ValueError(f"unsupported document status: {status!r}")
        if status == "FAILED":
            if not error_code or not error_code.strip() or not error_message or not error_message.strip():
                raise ValueError("FAILED documents require an error code and message")
            return
        if error_code is not None or error_message is not None:
            raise ValueError("document errors are only allowed for FAILED status")


def _control_plane_ddl(backend: str) -> tuple[str, ...]:
    if backend == "mysql":
        return _mysql_control_plane_ddl()
    if backend != "sqlite":
        raise ValueError(f"unsupported migration backend: {backend!r}")
    return _sqlite_control_plane_ddl()


def _sqlite_control_plane_ddl() -> tuple[str, ...]:
    return (
        """CREATE TABLE IF NOT EXISTS document (
            document_id TEXT PRIMARY KEY, kind TEXT NOT NULL, original_name TEXT NOT NULL,
            storage_key TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
            mime_type TEXT NOT NULL, status TEXT NOT NULL, error_code TEXT, error_message TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS job (
            job_id TEXT PRIMARY KEY, job_type TEXT NOT NULL, input_json TEXT NOT NULL, status TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE, parent_job_id TEXT, run_key TEXT NOT NULL UNIQUE,
            lease_owner TEXT, lease_expires_at TEXT, heartbeat_at TEXT, stage TEXT, progress REAL NOT NULL DEFAULT 0,
            attempt INTEGER NOT NULL DEFAULT 1,
            error_code TEXT, error_message TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            started_at TEXT, finished_at TEXT, FOREIGN KEY(parent_job_id) REFERENCES job(job_id)
        )""",
        """CREATE TABLE IF NOT EXISTS job_event (
            event_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, status TEXT NOT NULL, stage TEXT, message TEXT,
            created_at TEXT NOT NULL, FOREIGN KEY(job_id) REFERENCES job(job_id)
        )""",
        """CREATE TABLE IF NOT EXISTS artifact (
            artifact_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, kind TEXT NOT NULL, storage_key TEXT NOT NULL UNIQUE,
            sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, mime_type TEXT NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES job(job_id)
        )""",
        """CREATE TABLE IF NOT EXISTS conversation (
            conversation_id TEXT PRIMARY KEY, title TEXT NOT NULL, session_state_json TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS conversation_turn (
            turn_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, sequence_no INTEGER NOT NULL,
            idempotency_key TEXT NOT NULL, question TEXT NOT NULL, answer_payload_json TEXT NOT NULL,
            query_run_id TEXT, created_at TEXT NOT NULL, FOREIGN KEY(conversation_id) REFERENCES conversation(conversation_id),
            UNIQUE(conversation_id, sequence_no), UNIQUE(conversation_id, idempotency_key)
        )""",
        """CREATE TABLE IF NOT EXISTS query_run (
            query_run_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, turn_id TEXT NOT NULL,
            normalized_question TEXT NOT NULL, query_spec_json TEXT NOT NULL, sql_text TEXT NOT NULL,
            params_json TEXT NOT NULL, fact_snapshot_json TEXT NOT NULL, calculation_json TEXT NOT NULL,
            answer_payload_json TEXT NOT NULL, request_id TEXT NOT NULL, schema_version TEXT NOT NULL,
            code_version TEXT NOT NULL, status TEXT NOT NULL, error_message TEXT, created_at TEXT NOT NULL,
            FOREIGN KEY(conversation_id) REFERENCES conversation(conversation_id)
        )""",
        """CREATE TABLE IF NOT EXISTS fact_review_event (
            event_id TEXT PRIMARY KEY, fact_key TEXT NOT NULL, replacement_fact_key TEXT, action TEXT NOT NULL,
            before_json TEXT NOT NULL, after_json TEXT NOT NULL, reason TEXT NOT NULL, note TEXT NOT NULL,
            actor_label TEXT NOT NULL, expected_version INTEGER NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(fact_key) REFERENCES financial_fact(fact_key)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_job_claim ON job(status, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_fact_review_created ON fact_review_event(fact_key, created_at)",
    )


def _mysql_control_plane_ddl() -> tuple[str, ...]:
    return (
        """CREATE TABLE IF NOT EXISTS document (
            document_id VARCHAR(36) PRIMARY KEY, kind VARCHAR(32) NOT NULL, original_name VARCHAR(512) NOT NULL,
            storage_key VARCHAR(512) NOT NULL UNIQUE, sha256 CHAR(64) NOT NULL, size_bytes BIGINT NOT NULL,
            mime_type VARCHAR(128) NOT NULL, status VARCHAR(32) NOT NULL, error_code VARCHAR(64) NULL,
            error_message TEXT NULL, created_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS job (
            job_id VARCHAR(36) PRIMARY KEY, job_type VARCHAR(64) NOT NULL, input_json LONGTEXT NOT NULL,
            status VARCHAR(32) NOT NULL, idempotency_key VARCHAR(255) NOT NULL UNIQUE, parent_job_id VARCHAR(36) NULL,
            run_key VARCHAR(512) NOT NULL UNIQUE, lease_owner VARCHAR(128) NULL, lease_expires_at VARCHAR(40) NULL,
            heartbeat_at VARCHAR(40) NULL, stage VARCHAR(64) NULL, progress DOUBLE NOT NULL DEFAULT 0,
            attempt BIGINT NOT NULL DEFAULT 1,
            error_code VARCHAR(64) NULL, error_message TEXT NULL, created_at VARCHAR(40) NOT NULL,
            updated_at VARCHAR(40) NOT NULL, started_at VARCHAR(40) NULL, finished_at VARCHAR(40) NULL,
            FOREIGN KEY(parent_job_id) REFERENCES job(job_id), INDEX idx_job_claim(status, created_at)
        )""",
        """CREATE TABLE IF NOT EXISTS job_event (
            event_id VARCHAR(36) PRIMARY KEY, job_id VARCHAR(36) NOT NULL, status VARCHAR(32) NOT NULL,
            stage VARCHAR(64) NULL, message TEXT NULL, created_at VARCHAR(40) NOT NULL,
            FOREIGN KEY(job_id) REFERENCES job(job_id)
        )""",
        """CREATE TABLE IF NOT EXISTS artifact (
            artifact_id VARCHAR(36) PRIMARY KEY, job_id VARCHAR(36) NOT NULL, kind VARCHAR(64) NOT NULL,
            storage_key VARCHAR(512) NOT NULL UNIQUE, sha256 CHAR(64) NOT NULL, size_bytes BIGINT NOT NULL,
            mime_type VARCHAR(128) NOT NULL, created_at VARCHAR(40) NOT NULL,
            FOREIGN KEY(job_id) REFERENCES job(job_id)
        )""",
        """CREATE TABLE IF NOT EXISTS conversation (
            conversation_id VARCHAR(36) PRIMARY KEY, title VARCHAR(255) NOT NULL, session_state_json LONGTEXT NOT NULL,
            version BIGINT NOT NULL DEFAULT 1, created_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS conversation_turn (
            turn_id VARCHAR(36) PRIMARY KEY, conversation_id VARCHAR(36) NOT NULL, sequence_no BIGINT NOT NULL,
            idempotency_key VARCHAR(255) NOT NULL, question TEXT NOT NULL, answer_payload_json LONGTEXT NOT NULL,
            query_run_id VARCHAR(36) NULL, created_at VARCHAR(40) NOT NULL,
            FOREIGN KEY(conversation_id) REFERENCES conversation(conversation_id),
            UNIQUE KEY uniq_conversation_sequence(conversation_id, sequence_no),
            UNIQUE KEY uniq_conversation_idempotency(conversation_id, idempotency_key)
        )""",
        """CREATE TABLE IF NOT EXISTS query_run (
            query_run_id VARCHAR(36) PRIMARY KEY, conversation_id VARCHAR(36) NOT NULL, turn_id VARCHAR(36) NOT NULL,
            normalized_question TEXT NOT NULL, query_spec_json LONGTEXT NOT NULL, sql_text LONGTEXT NOT NULL,
            params_json LONGTEXT NOT NULL, fact_snapshot_json LONGTEXT NOT NULL, calculation_json LONGTEXT NOT NULL,
            answer_payload_json LONGTEXT NOT NULL, request_id VARCHAR(128) NOT NULL, schema_version VARCHAR(64) NOT NULL,
            code_version VARCHAR(64) NOT NULL, status VARCHAR(32) NOT NULL, error_message TEXT NULL,
            created_at VARCHAR(40) NOT NULL, FOREIGN KEY(conversation_id) REFERENCES conversation(conversation_id)
        )""",
        """CREATE TABLE IF NOT EXISTS fact_review_event (
            event_id VARCHAR(36) PRIMARY KEY, fact_key CHAR(64) NOT NULL, replacement_fact_key CHAR(64) NULL,
            action VARCHAR(32) NOT NULL, before_json LONGTEXT NOT NULL, after_json LONGTEXT NOT NULL,
            reason TEXT NOT NULL, note TEXT NOT NULL, actor_label VARCHAR(255) NOT NULL, expected_version BIGINT NOT NULL,
            created_at VARCHAR(40) NOT NULL, FOREIGN KEY(fact_key) REFERENCES financial_fact(fact_key),
            INDEX idx_fact_review_created(fact_key, created_at)
        )""",
    )
