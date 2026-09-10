"""Register an existing authoritative batch baseline with the Web control plane."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid5

from pypdf import PdfReader

from .config import AppConfig
from .database import FinanceDatabase, FinanceDatabaseFactory
from .facts import SourceAuthorityStatus, SourceOwner, ValidationStatus, build_source_key
from .ingestion_state import SourceIdentityError, dataset_source_uri, sha256_file
from .schema import load_schema_from_xlsx
from .web_store import WebStore

PDF_MIME_TYPE = "application/pdf"
BASELINE_DOCUMENT_NAMESPACE = UUID("b8381a3b-0a89-54e7-8e7d-242db0a0f92f")
BASELINE_STORAGE_DIRECTORY = PurePosixPath("documents", "baseline")
SUPPORTED_FILE_MODES = frozenset({"hardlink", "copy"})
DEFAULT_FILE_MODE = "copy"
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


class WebBootstrapError(RuntimeError):
    """Base error for baseline-to-Web registration failures."""


class BaselineSourceError(WebBootstrapError):
    """The authoritative baseline does not identify a valid source PDF."""


class BaselineConflictError(WebBootstrapError):
    """Persisted source, fact, or document identity conflicts with the baseline."""


class FileMaterializationError(WebBootstrapError):
    """A source PDF could not be materialized in Web storage."""


@dataclass(frozen=True, slots=True)
class BootstrapDocument:
    document_id: str
    source_uri: str
    storage_key: str
    sha256: str
    page_count: int
    fact_count: int
    created: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "source_uri": self.source_uri,
            "storage_key": self.storage_key,
            "sha256": self.sha256,
            "page_count": self.page_count,
            "fact_count": self.fact_count,
            "created": self.created,
        }


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    documents: tuple[BootstrapDocument, ...]

    @property
    def document_count(self) -> int:
        return len(self.documents)

    @property
    def created_count(self) -> int:
        return sum(document.created for document in self.documents)

    @property
    def existing_count(self) -> int:
        return self.document_count - self.created_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_count": self.document_count,
            "created_count": self.created_count,
            "existing_count": self.existing_count,
            "documents": [document.to_dict() for document in self.documents],
        }


@dataclass(frozen=True, slots=True)
class _CurrentSource:
    source_key: str
    source_file: str
    content_sha256: str
    stock_code: str
    period: str
    document_id: str | None


@dataclass(frozen=True, slots=True)
class _PlannedDocument:
    source: _CurrentSource
    source_path: Path
    source_uri: str
    document_id: str
    storage_key: str
    sha256: str
    size_bytes: int
    page_count: int
    fact_count: int


def bootstrap_web_documents(
    database_factory: FinanceDatabaseFactory,
    *,
    dataset_root: str | Path,
    storage_root: str | Path,
    file_mode: str = DEFAULT_FILE_MODE,
) -> BootstrapResult:
    """Register every CURRENT batch source and materialize it in Web review storage."""
    if file_mode not in SUPPORTED_FILE_MODES:
        raise ValueError(f"file_mode must be one of {sorted(SUPPORTED_FILE_MODES)}")
    dataset = _resolve_dataset_root(dataset_root)
    storage = _resolve_storage_root(storage_root)
    plans = _build_plans(database_factory, dataset)

    created_files: list[Path] = []
    try:
        for plan in plans:
            destination = _storage_destination(storage, plan.storage_key)
            if _materialize_file(plan, destination, file_mode=file_mode):
                created_files.append(destination)
        created_document_ids, registered_fact_counts = _register_plans(database_factory, plans)
    except Exception as exc:
        _cleanup_created_files(created_files, cause=exc)
        raise

    documents = tuple(
        BootstrapDocument(
            document_id=plan.document_id,
            source_uri=plan.source_uri,
            storage_key=plan.storage_key,
            sha256=plan.sha256,
            page_count=plan.page_count,
            fact_count=registered_fact_counts[plan.document_id],
            created=plan.document_id in created_document_ids,
        )
        for plan in plans
    )
    return BootstrapResult(documents=documents)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Register an existing Smart FinQA batch baseline for Web review")
    parser.add_argument("--dataset-root", type=Path, required=True, help="Root directory containing source PDFs")
    parser.add_argument("--storage-root", type=Path, required=True, help="Web storage root used by API and worker")
    parser.add_argument("--schema-xlsx", type=Path, required=True, help="Database schema workbook used by the baseline")
    parser.add_argument("--config", type=Path, help="Optional application YAML containing database settings")
    parser.add_argument("--db-path", type=Path, help="Existing SQLite database; overrides SQLITE_DB_PATH")
    parser.add_argument(
        "--file-mode",
        choices=sorted(SUPPORTED_FILE_MODES),
        default=DEFAULT_FILE_MODE,
        help=(
            "PDF materialization mode: copy is the safe default; hardlink is an expert option "
            "and requires source files to remain immutable"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        schema_path = args.schema_xlsx.expanduser().resolve(strict=True)
        if not schema_path.is_file():
            raise ValueError(f"schema workbook is not a file: {schema_path}")
        schema = load_schema_from_xlsx(schema_path)
        app_config = AppConfig.from_file_or_env(args.config.resolve() if args.config else None)
        db_config = app_config.db
        if args.db_path is not None:
            if db_config.backend != "sqlite":
                raise ValueError("--db-path cannot be combined with a MySQL database configuration")
            sqlite_path = args.db_path.expanduser().resolve(strict=True)
            if not sqlite_path.is_file():
                raise ValueError(f"SQLite database is not a file: {sqlite_path}")
            db_config = replace(db_config, sqlite_path=str(sqlite_path))
        elif db_config.backend == "sqlite":
            if not db_config.sqlite_path:
                raise ValueError("--db-path or SQLITE_DB_PATH is required for SQLite")
            sqlite_path = Path(db_config.sqlite_path).expanduser().resolve(strict=True)
            if not sqlite_path.is_file():
                raise ValueError(f"SQLite database is not a file: {sqlite_path}")
            db_config = replace(db_config, sqlite_path=str(sqlite_path))
        else:
            sqlite_path = Path.cwd() / ".mysql-control-plane-placeholder"

        factory = FinanceDatabaseFactory(sqlite_path, schema, db_config=db_config, enable_cache=False)
        storage_root = args.storage_root.expanduser().resolve()
        WebStore(factory, storage_root).initialize()
        result = bootstrap_web_documents(
            factory,
            dataset_root=args.dataset_root,
            storage_root=storage_root,
            file_mode=args.file_mode,
        )
    except (OSError, ValueError, WebBootstrapError) as exc:
        print(f"Web baseline bootstrap failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0


def _resolve_dataset_root(dataset_root: str | Path) -> Path:
    candidate = Path(dataset_root).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise BaselineSourceError(f"dataset root does not exist: {candidate}") from exc
    if not resolved.is_dir():
        raise BaselineSourceError(f"dataset root is not a directory: {resolved}")
    return resolved


def _resolve_storage_root(storage_root: str | Path) -> Path:
    candidate = Path(storage_root).expanduser()
    if candidate.exists() and not candidate.is_dir():
        raise FileMaterializationError(f"storage root is not a directory: {candidate}")
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate.resolve(strict=True)
    except OSError as exc:
        raise FileMaterializationError(f"unable to prepare storage root: {candidate}") from exc


def _build_plans(database_factory: FinanceDatabaseFactory, dataset_root: Path) -> tuple[_PlannedDocument, ...]:
    with database_factory.unit_of_work() as database:
        rows = database.query(
            "SELECT source_key, source_file, content_sha256, stock_code, period, current_slot, document_id "
            "FROM financial_source WHERE authority_status = {m} AND source_owner = {m} "
            "ORDER BY stock_code, period, source_file".format(m=database.parameter_marker),
            (SourceAuthorityStatus.CURRENT.value, SourceOwner.BATCH.value),
            use_cache=False,
        )
        if not rows:
            raise BaselineSourceError("baseline has no CURRENT financial sources")
        sources = _validate_current_source_rows(rows)
        plans = tuple(_build_plan(database, source, dataset_root) for source in sources)
        _validate_unique_plan_identities(plans)
    return plans


def _validate_unique_plan_identities(plans: tuple[_PlannedDocument, ...]) -> None:
    source_uris: set[str] = set()
    document_ids: set[str] = set()
    for plan in plans:
        if plan.source_uri in source_uris or plan.document_id in document_ids:
            raise BaselineConflictError(f"one CURRENT source PDF is reused by multiple periods: {plan.source_uri}")
        source_uris.add(plan.source_uri)
        document_ids.add(plan.document_id)


def _validate_current_source_rows(rows: list[dict[str, Any]]) -> tuple[_CurrentSource, ...]:
    sources: list[_CurrentSource] = []
    business_keys: set[tuple[str, str]] = set()
    source_files: set[str] = set()
    for row in rows:
        source_file = str(row.get("source_file") or "")
        content_sha256 = str(row.get("content_sha256") or "")
        stock_code = str(row.get("stock_code") or "")
        period = str(row.get("period") or "")
        source_key = str(row.get("source_key") or "")
        if not all(
            (source_file.strip(), content_sha256.strip(), stock_code.strip(), period.strip(), source_key.strip())
        ):
            raise BaselineSourceError("CURRENT source contains empty identity fields")
        if row.get("current_slot") != SourceAuthorityStatus.CURRENT.value:
            raise BaselineConflictError(f"CURRENT source has invalid current_slot: {stock_code}/{period}")
        if source_key != build_source_key(source_file, content_sha256):
            raise BaselineConflictError(f"CURRENT source_key does not match source identity: {stock_code}/{period}")
        business_key = (stock_code, period)
        if business_key in business_keys:
            raise BaselineConflictError(f"multiple CURRENT sources exist for {stock_code}/{period}")
        if source_file in source_files:
            raise BaselineConflictError(f"one CURRENT source_file is reused by multiple periods: {source_file}")
        business_keys.add(business_key)
        source_files.add(source_file)
        sources.append(
            _CurrentSource(
                source_key=source_key,
                source_file=source_file,
                content_sha256=content_sha256,
                stock_code=stock_code,
                period=period,
                document_id=str(row["document_id"]) if row.get("document_id") is not None else None,
            )
        )
    return tuple(sources)


def _build_plan(database: FinanceDatabase, source: _CurrentSource, dataset_root: Path) -> _PlannedDocument:
    source_path, source_uri = _resolve_source_file(dataset_root, source.source_file)
    try:
        digest = sha256_file(source_path)
    except SourceIdentityError as exc:
        raise BaselineSourceError(str(exc)) from exc
    if digest != source.content_sha256:
        raise BaselineConflictError(
            f"CURRENT source content SHA-256 does not match the PDF: {source.stock_code}/{source.period}"
        )
    page_count = _pdf_page_count(source_path)
    rows = database.query(
        "SELECT fact_key, source_key, stock_code, period, page_no, validation_status, document_id "
        "FROM financial_fact WHERE source_key = {m} ORDER BY fact_key".format(m=database.parameter_marker),
        (source.source_key,),
        use_cache=False,
    )
    _validate_fact_rows(source, rows, page_count, expected_document_id=None)
    document_id = str(uuid5(BASELINE_DOCUMENT_NAMESPACE, f"{source_uri}\nsha256:{digest}"))
    storage_key = (BASELINE_STORAGE_DIRECTORY / f"{document_id}.pdf").as_posix()
    return _PlannedDocument(
        source=source,
        source_path=source_path,
        source_uri=source_uri,
        document_id=document_id,
        storage_key=storage_key,
        sha256=digest,
        size_bytes=source_path.stat().st_size,
        page_count=page_count,
        fact_count=len(rows),
    )


def _resolve_source_file(dataset_root: Path, source_file: str) -> tuple[Path, str]:
    if source_file.startswith("dataset:"):
        source_path = _path_from_dataset_uri(dataset_root, source_file)
        canonical_uri = dataset_source_uri(dataset_root, source_path)
        if canonical_uri != source_file:
            raise BaselineSourceError(f"CURRENT source has a non-canonical dataset URI: {source_file}")
        return source_path, canonical_uri

    portable_value = source_file.replace("\\", "/")
    raw_path = Path(source_file).expanduser()
    if raw_path.is_absolute():
        candidate = raw_path
    else:
        relative = PurePosixPath(portable_value)
        if relative.is_absolute() or ".." in relative.parts:
            raise BaselineSourceError(f"CURRENT source is outside dataset root: {source_file}")
        candidate = dataset_root.joinpath(*relative.parts)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise BaselineSourceError(f"CURRENT source PDF does not exist: {candidate}") from exc
    if not resolved.is_file() or not resolved.is_relative_to(dataset_root):
        raise BaselineSourceError(f"CURRENT source is outside dataset root: {source_file}")
    try:
        return resolved, dataset_source_uri(dataset_root, resolved)
    except SourceIdentityError as exc:
        raise BaselineSourceError(str(exc)) from exc


def _path_from_dataset_uri(dataset_root: Path, source_uri: str) -> Path:
    if _INVALID_PERCENT_ESCAPE.search(source_uri):
        raise BaselineSourceError(f"CURRENT source has invalid percent encoding: {source_uri}")
    parts = urlsplit(source_uri)
    if parts.scheme != "dataset" or parts.netloc or parts.query or parts.fragment or not parts.path.startswith("/"):
        raise BaselineSourceError(f"CURRENT source is not a valid dataset URI: {source_uri}")
    relative = PurePosixPath(unquote(parts.path.lstrip("/")))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts or "\\" in str(relative):
        raise BaselineSourceError(f"CURRENT source URI is outside dataset root: {source_uri}")
    candidate = dataset_root.joinpath(*relative.parts)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise BaselineSourceError(f"CURRENT source PDF does not exist: {candidate}") from exc
    if not resolved.is_file() or not resolved.is_relative_to(dataset_root):
        raise BaselineSourceError(f"CURRENT source URI is outside dataset root: {source_uri}")
    return resolved


def _pdf_page_count(source_path: Path) -> int:
    try:
        with source_path.open("rb") as stream:
            page_count = len(PdfReader(stream).pages)
    except Exception as exc:
        raise BaselineSourceError(f"unable to read CURRENT source PDF {source_path.name}: {exc}") from exc
    if page_count < 1:
        raise BaselineSourceError(f"CURRENT source PDF has no pages: {source_path.name}")
    return page_count


def _validate_fact_rows(
    source: _CurrentSource,
    rows: list[dict[str, Any]],
    page_count: int,
    *,
    expected_document_id: str | None,
) -> str:
    if not rows:
        raise BaselineSourceError(
            f"CURRENT source has no matching facts: {source.stock_code}/{source.period}/{source.source_file}"
        )
    pending = False
    for row in rows:
        if str(row.get("stock_code")) != source.stock_code or str(row.get("period")) != source.period:
            raise BaselineConflictError(f"fact identity conflicts with CURRENT source: {row.get('fact_key')}")
        if str(row.get("source_key")) != source.source_key:
            raise BaselineConflictError(f"fact source_key conflicts with CURRENT source: {row.get('fact_key')}")
        try:
            status = ValidationStatus(str(row.get("validation_status")))
        except ValueError as exc:
            raise BaselineSourceError(f"fact has invalid validation_status: {row.get('fact_key')}") from exc
        pending = pending or status is ValidationStatus.NEEDS_REVIEW
        page_no = row.get("page_no")
        if page_no is not None:
            if isinstance(page_no, bool) or not isinstance(page_no, int) or page_no < 1:
                raise BaselineSourceError(f"fact has invalid page_no: {row.get('fact_key')}")
            if page_no > page_count:
                raise BaselineSourceError(
                    f"fact {row.get('fact_key')} page_no {page_no} exceeds PDF page_count {page_count}"
                )
        linked_document_id = row.get("document_id")
        if (
            expected_document_id is not None
            and linked_document_id is not None
            and str(linked_document_id) != expected_document_id
        ):
            raise BaselineConflictError(f"fact {row.get('fact_key')} is linked to a different document")
    return "NEEDS_REVIEW" if pending else "COMPLETED"


def _storage_destination(storage_root: Path, storage_key: str) -> Path:
    key = PurePosixPath(storage_key)
    if key.is_absolute() or ".." in key.parts:
        raise FileMaterializationError(f"invalid storage key: {storage_key}")
    destination = storage_root.joinpath(*key.parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    parent = destination.parent.resolve(strict=True)
    if not parent.is_relative_to(storage_root):
        raise FileMaterializationError(f"storage key escapes storage root: {storage_key}")
    return parent / destination.name


def _materialize_file(plan: _PlannedDocument, destination: Path, *, file_mode: str) -> bool:
    if destination.exists() or destination.is_symlink():
        _validate_stored_file(plan, destination)
        return False
    if file_mode == "hardlink":
        try:
            os.link(plan.source_path, destination)
        except FileExistsError:
            _validate_stored_file(plan, destination)
            return False
        except OSError as exc:
            raise FileMaterializationError(
                f"unable to hardlink {plan.source_uri} into Web storage; use --file-mode copy explicitly: {exc}"
            ) from exc
    else:
        _copy_file_atomically(plan.source_path, destination)
    try:
        _validate_stored_file(plan, destination)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return True


def _copy_file_atomically(source: Path, destination: Path) -> None:
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        temporary_path = Path(temporary_name)
        with source.open("rb") as input_stream, os.fdopen(descriptor, "wb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        os.replace(temporary_path, destination)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise FileMaterializationError(f"unable to copy source PDF into Web storage: {destination}") from exc


def _validate_stored_file(plan: _PlannedDocument, destination: Path) -> None:
    try:
        resolved = destination.resolve(strict=True)
    except OSError as exc:
        raise FileMaterializationError(f"stored PDF does not exist: {destination}") from exc
    if destination.is_symlink() or not resolved.is_file() or resolved != destination:
        raise FileMaterializationError(f"stored PDF must be a regular non-symlink file: {destination}")
    try:
        digest = sha256_file(resolved)
    except SourceIdentityError as exc:
        raise FileMaterializationError(str(exc)) from exc
    if digest != plan.sha256 or resolved.stat().st_size != plan.size_bytes:
        raise FileMaterializationError(f"stored PDF content conflicts with source SHA-256: {destination}")


def _register_plans(
    database_factory: FinanceDatabaseFactory,
    plans: tuple[_PlannedDocument, ...],
) -> tuple[frozenset[str], dict[str, int]]:
    created_document_ids: set[str] = set()
    registered_fact_counts: dict[str, int] = {}
    with database_factory.unit_of_work() as database:
        database._begin_transaction()
        cursor = database._conn.cursor(dictionary=True) if database.backend == "mysql" else database._conn.cursor()
        try:
            _lock_and_validate_current_set(database, cursor, plans)
            for plan in plans:
                rows = _locked_fact_rows(database, cursor, plan.source)
                document_status = _validate_fact_rows(
                    plan.source,
                    rows,
                    plan.page_count,
                    expected_document_id=plan.document_id,
                )
                if _register_document(database, cursor, plan, status=document_status):
                    created_document_ids.add(plan.document_id)
                _link_source_and_facts(database, cursor, plan, len(rows))
                registered_fact_counts[plan.document_id] = len(rows)
            database._conn.commit()
        except Exception:
            database._conn.rollback()
            raise
        finally:
            cursor.close()
        database.clear_cache()
    return frozenset(created_document_ids), registered_fact_counts


def _lock_and_validate_current_set(database: FinanceDatabase, cursor: Any, plans: tuple[_PlannedDocument, ...]) -> None:
    lock = " FOR UPDATE" if database.backend == "mysql" else ""
    cursor.execute(
        "SELECT source_key, source_file, content_sha256, stock_code, period, current_slot, document_id "
        "FROM financial_source WHERE authority_status = {m} AND source_owner = {m} "
        "ORDER BY stock_code, period, source_file{lock}".format(
            m=database.parameter_marker,
            lock=lock,
        ),
        (SourceAuthorityStatus.CURRENT.value, SourceOwner.BATCH.value),
    )
    current = _validate_current_source_rows([dict(row) for row in cursor.fetchall()])
    expected = {
        (
            plan.source.source_key,
            plan.source.source_file,
            plan.source.content_sha256,
            plan.source.stock_code,
            plan.source.period,
        )
        for plan in plans
    }
    actual = {
        (source.source_key, source.source_file, source.content_sha256, source.stock_code, source.period)
        for source in current
    }
    if actual != expected:
        raise BaselineConflictError("CURRENT source set changed during Web bootstrap")
    planned_by_key = {plan.source.source_key: plan for plan in plans}
    for source in current:
        plan = planned_by_key[source.source_key]
        if source.document_id not in {None, plan.document_id}:
            raise BaselineConflictError(
                f"CURRENT source {source.stock_code}/{source.period} is linked to a different document"
            )


def _locked_fact_rows(database: FinanceDatabase, cursor: Any, source: _CurrentSource) -> list[dict[str, Any]]:
    lock = " FOR UPDATE" if database.backend == "mysql" else ""
    cursor.execute(
        "SELECT fact_key, source_key, stock_code, period, page_no, validation_status, document_id "
        "FROM financial_fact WHERE source_key = {m} ORDER BY fact_key{lock}".format(
            m=database.parameter_marker,
            lock=lock,
        ),
        (source.source_key,),
    )
    return [dict(row) for row in cursor.fetchall()]


def _register_document(
    database: FinanceDatabase,
    cursor: Any,
    plan: _PlannedDocument,
    *,
    status: str,
) -> bool:
    lock = " FOR UPDATE" if database.backend == "mysql" else ""
    cursor.execute(
        "SELECT * FROM document WHERE document_id = {m} OR storage_key = {m}{lock}".format(
            m=database.parameter_marker,
            lock=lock,
        ),
        (plan.document_id, plan.storage_key),
    )
    existing = [dict(row) for row in cursor.fetchall()]
    if existing:
        if len(existing) != 1:
            raise BaselineConflictError(
                f"document identity and storage key resolve to different rows: {plan.source_uri}"
            )
        _validate_existing_document(existing[0], plan, status=status)
        return False

    now = datetime.now(timezone.utc).isoformat()
    cursor.execute(
        "INSERT INTO document (document_id, kind, original_name, storage_key, sha256, size_bytes, mime_type, "
        "page_count, active_ingestion_job_id, status, created_at, updated_at) "
        f"VALUES ({database.parameter_markers(12)})",
        (
            plan.document_id,
            "financial_report",
            plan.source_path.name,
            plan.storage_key,
            plan.sha256,
            plan.size_bytes,
            PDF_MIME_TYPE,
            plan.page_count,
            None,
            status,
            now,
            now,
        ),
    )
    return True


def _validate_existing_document(row: dict[str, Any], plan: _PlannedDocument, *, status: str) -> None:
    expected = {
        "document_id": plan.document_id,
        "kind": "financial_report",
        "original_name": plan.source_path.name,
        "storage_key": plan.storage_key,
        "sha256": plan.sha256,
        "size_bytes": plan.size_bytes,
        "mime_type": PDF_MIME_TYPE,
        "page_count": plan.page_count,
        "status": status,
    }
    mismatches = [name for name, value in expected.items() if row.get(name) != value]
    if mismatches:
        raise BaselineConflictError(
            f"existing document metadata conflicts for {plan.source_uri}: {', '.join(sorted(mismatches))}"
        )
    if row.get("active_ingestion_job_id") is not None:
        raise BaselineConflictError(f"existing baseline document has an incompatible active state: {plan.document_id}")


def _link_source_and_facts(
    database: FinanceDatabase,
    cursor: Any,
    plan: _PlannedDocument,
    fact_count: int,
) -> None:
    cursor.execute(
        "UPDATE financial_source SET document_id = {m} WHERE source_key = {m} AND authority_status = {m} "
        "AND source_owner = {m} AND document_id IS NULL".format(m=database.parameter_marker),
        (
            plan.document_id,
            plan.source.source_key,
            SourceAuthorityStatus.CURRENT.value,
            SourceOwner.BATCH.value,
        ),
    )
    cursor.execute(
        "UPDATE financial_fact SET document_id = {m} WHERE source_key = {m} AND document_id IS NULL".format(
            m=database.parameter_marker
        ),
        (plan.document_id, plan.source.source_key),
    )

    cursor.execute(
        "SELECT document_id FROM financial_source WHERE source_key = {m} AND authority_status = {m} "
        "AND source_owner = {m}".format(m=database.parameter_marker),
        (plan.source.source_key, SourceAuthorityStatus.CURRENT.value, SourceOwner.BATCH.value),
    )
    source_row = cursor.fetchone()
    if source_row is None or dict(source_row).get("document_id") != plan.document_id:
        raise BaselineConflictError(f"CURRENT source link failed for {plan.source.stock_code}/{plan.source.period}")
    cursor.execute(
        "SELECT COUNT(*) AS fact_count FROM financial_fact WHERE source_key = {m} AND document_id = {m}".format(
            m=database.parameter_marker
        ),
        (plan.source.source_key, plan.document_id),
    )
    linked_count = int(dict(cursor.fetchone()).get("fact_count") or 0)
    if linked_count != fact_count:
        raise BaselineConflictError(
            f"expected to link {fact_count} facts for {plan.source.stock_code}/{plan.source.period}, got {linked_count}"
        )


def _cleanup_created_files(paths: list[Path], *, cause: Exception) -> None:
    failures: list[str] = []
    for path in reversed(paths):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            failures.append(f"{path}: {exc}")
    if failures:
        raise FileMaterializationError(
            f"bootstrap failed and newly materialized files could not be cleaned up: {'; '.join(failures)}"
        ) from cause


if __name__ == "__main__":
    raise SystemExit(main())
