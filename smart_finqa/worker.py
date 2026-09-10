"""Durable worker loop with injected job handlers."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .promotion import ArtifactManifest, PromotionPlan
from .web_store import ArtifactRecord, ConflictError, JobRecord, WebStore


class WorkerConfigurationError(RuntimeError):
    """Raised when a claimed job has no explicitly configured handler."""


class LeaseLostError(RuntimeError):
    """Raised when a worker can no longer prove ownership of a running job."""


@dataclass(frozen=True, slots=True)
class ProducedArtifact:
    kind: str
    path: Path
    sha256: str
    mime_type: str


@dataclass(frozen=True, slots=True)
class JobExecutionResult:
    artifacts: tuple[ProducedArtifact, ...] = ()
    promotion_plan: PromotionPlan | None = None


JobHeartbeat = Callable[[str | None, float | None], JobRecord]
JobHandler = Callable[[JobRecord, Path, JobHeartbeat], Sequence[ProducedArtifact] | JobExecutionResult]


class DurableWorker:
    """Claim one durable job and make every terminal result explicit."""

    def __init__(
        self,
        store: WebStore,
        worker_id: str,
        *,
        handlers: Mapping[str, JobHandler] | None = None,
        lease_seconds: int = 60,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id must be non-empty")
        self.store = store
        self.worker_id = worker_id
        self.handlers = dict(handlers or {})
        self.lease_seconds = lease_seconds

    def run_once(self) -> JobRecord | None:
        claimed = self.store.claim_job(self.worker_id, lease_seconds=self.lease_seconds)
        if claimed is None:
            return None
        if not claimed.lease_token:
            raise LeaseLostError("claimed job has no lease token")
        lease_token = claimed.lease_token
        handler = self.handlers.get(claimed.job_type)
        if handler is None:
            return self.store.fail_job(
                claimed.job_id,
                self.worker_id,
                lease_token,
                error_code="NOT_CONFIGURED",
                error_message=f"No handler configured for job type {claimed.job_type!r}",
            )
        run_dir = self.store.run_directory(claimed.job_id)
        stop_renewal = threading.Event()
        renewal_lock = threading.Lock()
        renewal_failure: list[BaseException] = []

        def heartbeat(stage: str | None = None, progress: float | None = None) -> JobRecord:
            with renewal_lock:
                self._raise_if_lease_lost(renewal_failure)
                try:
                    return self.store.heartbeat_job(
                        claimed.job_id,
                        self.worker_id,
                        lease_token,
                        lease_seconds=self.lease_seconds,
                        stage=stage,
                        progress=progress,
                    )
                except Exception as exc:
                    renewal_failure.append(exc)
                    raise LeaseLostError(f"job lease was lost: {exc}") from exc

        def renew_lease() -> None:
            interval = max(0.1, self.lease_seconds / 3)
            while not stop_renewal.wait(interval):
                try:
                    heartbeat()
                except LeaseLostError:
                    return

        renewal_thread = threading.Thread(
            target=renew_lease,
            name=f"smart-finqa-lease-{claimed.job_id}",
            daemon=True,
        )
        renewal_thread.start()

        try:
            result = handler(claimed, run_dir, heartbeat)
            if isinstance(result, JobExecutionResult):
                artifacts = result.artifacts
                promotion_plan = result.promotion_plan
            else:
                artifacts = tuple(result)
                promotion_plan = None
            self._raise_if_lease_lost(renewal_failure)
            if promotion_plan is not None:
                manifests = tuple(self._artifact_manifest(run_dir, artifact) for artifact in artifacts)
                self._raise_if_lease_lost(renewal_failure)
                heartbeat("promoting", 0.98)
                return self.store.promote_document_ingestion(
                    claimed.job_id,
                    self.worker_id,
                    lease_token,
                    plan=promotion_plan,
                    artifacts=manifests,
                )
            for artifact in artifacts:
                self._record_artifact(claimed, run_dir, lease_token, artifact)
            self._raise_if_lease_lost(renewal_failure)
            return self.store.complete_job(claimed.job_id, self.worker_id, lease_token)
        except LeaseLostError:
            raise
        except Exception as exc:
            self._raise_if_lease_lost(renewal_failure)
            try:
                return self.store.fail_job(
                    claimed.job_id,
                    self.worker_id,
                    lease_token,
                    error_code="HANDLER_FAILED",
                    error_message=f"{type(exc).__name__}: {exc}",
                )
            except ConflictError as lease_error:
                raise LeaseLostError(f"job lease was lost while recording failure: {lease_error}") from lease_error
        finally:
            stop_renewal.set()
            renewal_thread.join(timeout=max(1.0, self.lease_seconds))

    def _record_artifact(
        self,
        job: JobRecord,
        run_dir: Path,
        lease_token: str,
        artifact: ProducedArtifact,
    ) -> ArtifactRecord:
        manifest = self._artifact_manifest(run_dir, artifact)
        return self.store.create_artifact(
            job.job_id,
            self.worker_id,
            lease_token,
            kind=manifest.kind,
            storage_key=manifest.storage_key,
            sha256=manifest.sha256,
            size_bytes=manifest.size_bytes,
            mime_type=manifest.mime_type,
        )

    def _artifact_manifest(self, run_dir: Path, artifact: ProducedArtifact) -> ArtifactManifest:
        resolved_path = artifact.path.resolve()
        if resolved_path.parent != run_dir and run_dir not in resolved_path.parents:
            raise ValueError("worker artifact must stay inside its job run directory")
        if not resolved_path.is_file():
            raise FileNotFoundError(f"worker artifact does not exist: {resolved_path}")
        storage_key = resolved_path.relative_to(self.store.output_root).as_posix()
        return ArtifactManifest(
            kind=artifact.kind,
            storage_key=storage_key,
            sha256=artifact.sha256,
            size_bytes=resolved_path.stat().st_size,
            mime_type=artifact.mime_type,
        )

    @staticmethod
    def _raise_if_lease_lost(failures: Sequence[BaseException]) -> None:
        if failures:
            cause = failures[0]
            raise LeaseLostError(f"job lease was lost: {cause}") from cause
