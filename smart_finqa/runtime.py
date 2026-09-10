"""Composition root for the local Web API and durable ingestion worker."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

from pypdf import PdfReader

from .application import ApplicationService, WebApplicationFacade
from .config import AppConfig, DatabaseConfig
from .database import FinanceDatabaseFactory
from .pipeline import PipelinePaths, SmartFinancePipeline
from .promotion import build_promotion_plan
from .schema import load_schema_from_xlsx
from .task2 import CompanyResolver, QuestionAnalyzer
from .web_store import JobRecord, WebStore
from .worker import DurableWorker, JobExecutionResult, JobHeartbeat, ProducedArtifact

DEFAULT_STORAGE_ROOT = Path("outputs/web_storage")


def create_web_service() -> WebApplicationFacade:
    """Build the real HTTP service from explicit local configuration."""

    _, config, paths, factory, store = _runtime_components()
    analyzer = QuestionAnalyzer(CompanyResolver.from_xlsx(paths.company_xlsx))
    question_service = ApplicationService(
        store,
        factory,
        analyzer,
        timeout_seconds=config.query_timeout_seconds,
    )
    return WebApplicationFacade(store, question_service)


def create_worker(worker_id: str) -> DurableWorker:
    base_dir, config, paths, factory, store = _runtime_components()
    handler = _document_ingestion_handler(
        store=store,
        database_factory=factory,
        base_paths=paths,
        app_config=config,
        base_dir=base_dir,
    )
    return DurableWorker(store, worker_id, handlers={"document_ingestion": handler})


def worker_main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the Smart FinQA durable local worker")
    parser.add_argument("--base-dir", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path)
    parser.add_argument("--storage-root", type=Path, default=DEFAULT_STORAGE_ROOT)
    parser.add_argument("--worker-id", default=f"local-{os.getpid()}")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.poll_seconds <= 0:
        raise ValueError("--poll-seconds must be positive")
    _set_runtime_environment(args.base_dir, args.config, args.storage_root)
    worker = create_worker(args.worker_id)
    worker.store.recover_interrupted()
    if args.once:
        worker.run_once()
        return
    try:
        while True:
            if worker.run_once() is None:
                time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        return


def _runtime_components() -> tuple[Path, AppConfig, PipelinePaths, FinanceDatabaseFactory, WebStore]:
    base_dir = Path(os.getenv("SMART_FINQA_BASE_DIR", Path.cwd())).resolve()
    config_text = os.getenv("SMART_FINQA_CONFIG", "").strip()
    config_path = Path(config_text).resolve() if config_text else None
    config = AppConfig.from_file_or_env(config_path)
    paths = PipelinePaths.from_base_dir(base_dir, full_data=config.full_data)
    _require_runtime_file(paths.schema_xlsx, "database schema workbook")
    _require_runtime_file(paths.company_xlsx, "company workbook")
    schema = load_schema_from_xlsx(paths.schema_xlsx)
    db_config = _resolved_database_config(base_dir, paths, config.db)
    config = replace(config, db=db_config, enable_cache=False)
    factory = FinanceDatabaseFactory(paths.db_path, schema, db_config=db_config, enable_cache=False)
    storage_root = Path(os.getenv("SMART_FINQA_STORAGE_ROOT", DEFAULT_STORAGE_ROOT))
    if not storage_root.is_absolute():
        storage_root = base_dir / storage_root
    store = WebStore(factory, storage_root)
    store.initialize()
    return base_dir, config, paths, factory, store


def _resolved_database_config(base_dir: Path, paths: PipelinePaths, config: DatabaseConfig) -> DatabaseConfig:
    if config.backend != "sqlite":
        return config
    configured = Path(config.sqlite_path) if config.sqlite_path else paths.db_path
    sqlite_path = configured if configured.is_absolute() else base_dir / configured
    return replace(config, sqlite_path=str(sqlite_path.resolve()))


def _require_runtime_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"Required {label} not found: {path}")


def _set_runtime_environment(base_dir: Path, config: Path | None, storage_root: Path) -> None:
    resolved_base = base_dir.resolve()
    resolved_storage = storage_root if storage_root.is_absolute() else resolved_base / storage_root
    os.environ["SMART_FINQA_BASE_DIR"] = str(resolved_base)
    os.environ["SMART_FINQA_STORAGE_ROOT"] = str(resolved_storage.resolve())
    if config is not None:
        os.environ["SMART_FINQA_CONFIG"] = str(config.resolve())


def _document_ingestion_handler(
    *,
    store: WebStore,
    database_factory: FinanceDatabaseFactory,
    base_paths: PipelinePaths,
    app_config: AppConfig,
    base_dir: Path,
) -> Callable[[JobRecord, Path, JobHeartbeat], JobExecutionResult]:
    def ingest(job: JobRecord, run_dir: Path, heartbeat: JobHeartbeat) -> JobExecutionResult:
        document_id = str(job.input_payload.get("document_id") or "")
        if not document_id:
            raise ValueError("document ingestion job has no document_id")
        if not job.lease_token:
            raise ValueError("document ingestion job has no lease token")
        lease_token = job.lease_token
        document = store.get_document(document_id)
        if document.kind != "financial_report":
            store.update_document_status_for_job(
                document_id,
                "FAILED",
                job_id=job.job_id,
                worker_id=job.lease_owner or "",
                lease_token=lease_token,
                error_code="UNSUPPORTED_DOCUMENT_KIND",
                error_message=f"Ingestion is not configured for document kind {document.kind!r}",
            )
            raise ValueError(f"unsupported document kind: {document.kind!r}")
        pipeline: SmartFinancePipeline | None = None
        try:
            source_path = _resolve_storage_file(store.output_root, document.storage_key)
            if Path(document.original_name).name != document.original_name:
                raise ValueError("document original_name must not contain a path")
            page_count = len(PdfReader(source_path).pages)
            if page_count < 1:
                raise ValueError("financial report PDF must contain at least one page")
            store.update_document_status_for_job(
                document_id,
                "PROCESSING",
                job_id=job.job_id,
                worker_id=job.lease_owner or "",
                lease_token=lease_token,
                page_count=page_count,
            )
            heartbeat("preparing", 0.05)
            input_dir = _prepare_job_subdirectory(run_dir, "input")
            staging_dir = _prepare_job_subdirectory(run_dir, "staging")
            output_dir = _prepare_job_subdirectory(run_dir, "outputs")
            result_dir = _prepare_job_subdirectory(run_dir, "result")
            isolated_pdf = input_dir / document.original_name
            if isolated_pdf.exists() or isolated_pdf.is_symlink():
                raise FileExistsError("job-local input PDF already exists")
            shutil.copy2(source_path, isolated_pdf)
            staging_db_path = staging_dir / "finance.db"
            if staging_db_path.exists() or staging_db_path.is_symlink():
                raise FileExistsError("job-local staging database already exists")
            run_paths = replace(
                base_paths,
                base_dir=base_dir,
                reports_dir=input_dir,
                output_dir=output_dir,
                result_dir=result_dir,
                db_path=staging_db_path,
            )
            staging_config = replace(
                app_config,
                db=DatabaseConfig(
                    backend="sqlite",
                    sqlite_path=str(staging_db_path),
                    sqlite_busy_timeout_ms=app_config.db.sqlite_busy_timeout_ms,
                ),
                incremental_ingest=False,
                enable_cache=False,
            )
            heartbeat("extracting", 0.15)
            pipeline = SmartFinancePipeline(run_paths, app_config=staging_config)
            outputs = pipeline.run(mode="ingest")
            heartbeat("validating_staging", 0.85)
            promotion_plan = build_promotion_plan(
                staging_db_path,
                pipeline.schema,
                document_id=document_id,
                source_file=document.storage_key,
                source_content_sha256=document.sha256,
                page_count=page_count,
            )
            heartbeat("finalizing", 0.95)
            return JobExecutionResult(
                artifacts=_output_artifacts(run_dir, outputs),
                promotion_plan=promotion_plan,
            )
        except Exception as exc:
            store.update_document_status_for_job(
                document_id,
                "FAILED",
                job_id=job.job_id,
                worker_id=job.lease_owner or "",
                lease_token=lease_token,
                error_code="INGESTION_FAILED",
                error_message=f"{type(exc).__name__}: {exc}",
            )
            raise
        finally:
            if pipeline is not None:
                pipeline.db.close()

    return ingest


def _prepare_job_subdirectory(run_dir: Path, name: str) -> Path:
    resolved_run_dir = run_dir.resolve(strict=True)
    directory = resolved_run_dir / name
    if directory.is_symlink():
        raise ValueError(f"job subdirectory {name!r} must not be a symbolic link")
    directory.mkdir(exist_ok=True)
    resolved = directory.resolve(strict=True)
    if resolved.parent != resolved_run_dir:
        raise ValueError(f"job subdirectory {name!r} escapes the job run directory")
    return resolved


def _resolve_storage_file(storage_root: Path, storage_key: str) -> Path:
    root = storage_root.resolve()
    relative = Path(storage_key)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("document storage key escapes the storage root")
    resolved = (root / relative).resolve(strict=True)
    if not resolved.is_file() or not resolved.is_relative_to(root):
        raise ValueError("document storage key does not resolve to a stored file")
    return resolved


def _output_artifacts(run_dir: Path, outputs: dict[str, str]) -> tuple[ProducedArtifact, ...]:
    artifacts: list[ProducedArtifact] = []
    mime_types = {
        "run_log": "application/json",
        "validation_report": "application/json",
        "result_2": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "result_3": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    for kind, raw_path in outputs.items():
        if kind not in mime_types or not raw_path:
            continue
        path = Path(raw_path).resolve()
        if not path.is_file() or not path.is_relative_to(run_dir.resolve()):
            raise ValueError(f"pipeline output {kind!r} is outside the job run directory")
        artifacts.append(
            ProducedArtifact(
                kind=kind,
                path=path,
                sha256=_sha256(path),
                mime_type=mime_types[kind],
            )
        )
    return tuple(artifacts)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    worker_main()
