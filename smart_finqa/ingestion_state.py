"""Atomic ingestion-state storage and source identity helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

INGESTION_CONTEXT_VERSION = 1
FILE_HASH_CHUNK_SIZE = 1024 * 1024
_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}")


class IngestionStateError(RuntimeError):
    """Base error for ingestion-state I/O failures."""


class IngestionStateReadError(IngestionStateError):
    """Raised when a state file cannot be read."""


class IngestionStateWriteError(IngestionStateError):
    """Raised when a state file cannot be atomically replaced."""


class InvalidIngestionStateError(ValueError):
    """Raised when state data is not a valid JSON object."""


class SourceIdentityError(ValueError):
    """Raised when source identity cannot be established safely."""


@dataclass(frozen=True, slots=True)
class IngestionStateStore:
    """Read and atomically replace one JSON ingestion-state object."""

    path: Path

    def __init__(self, path: str | Path) -> None:
        object.__setattr__(self, "path", Path(path).expanduser())

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise IngestionStateReadError(f"Unable to read ingestion state: {self.path}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise InvalidIngestionStateError(f"Invalid ingestion state JSON: {self.path}") from exc
        if not isinstance(payload, dict):
            raise InvalidIngestionStateError(f"Ingestion state must be a JSON object: {self.path}")
        return payload

    def save(self, state: Mapping[str, Any]) -> None:
        serialized = _serialize_state(state, self.path)
        destination = _resolve_destination(self.path)
        temporary_path = _write_temporary_file(destination, serialized)
        try:
            os.replace(temporary_path, destination)
        except OSError as exc:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError as cleanup_exc:
                raise IngestionStateWriteError(
                    f"Unable to replace ingestion state {destination}; temporary cleanup also failed: {cleanup_exc}"
                ) from exc
            raise IngestionStateWriteError(f"Unable to replace ingestion state: {destination}") from exc


def sha256_file(path: str | Path) -> str:
    """Hash current file bytes without trusting timestamps or file size."""
    source = Path(path).expanduser()
    try:
        resolved = source.resolve(strict=True)
    except OSError as exc:
        raise SourceIdentityError(f"Source file does not exist: {source}") from exc
    if not resolved.is_file():
        raise SourceIdentityError(f"Source path is not a file: {resolved}")

    digest = hashlib.sha256()
    try:
        with resolved.open("rb") as stream:
            for chunk in iter(lambda: stream.read(FILE_HASH_CHUNK_SIZE), b""):
                digest.update(chunk)
    except OSError as exc:
        raise SourceIdentityError(f"Unable to hash source file: {resolved}") from exc
    return digest.hexdigest()


def ingestion_context_fingerprint(
    *,
    dataset_sha256: str,
    schema_sha256: str,
    company_master_sha256: str,
) -> str:
    """Combine versioned context hashes into one deterministic fingerprint."""
    components = {
        "company_master_sha256": _normalize_sha256("company_master_sha256", company_master_sha256),
        "dataset_sha256": _normalize_sha256("dataset_sha256", dataset_sha256),
        "schema_sha256": _normalize_sha256("schema_sha256", schema_sha256),
        "version": INGESTION_CONTEXT_VERSION,
    }
    canonical = json.dumps(components, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"smart-finqa:ingestion-context\n{canonical}".encode()).hexdigest()


def dataset_source_uri(dataset_root: str | Path, source_path: str | Path) -> str:
    """Return a relocation-stable URI for one file contained by a dataset."""
    root_input = Path(dataset_root).expanduser()
    try:
        root = root_input.resolve(strict=True)
    except OSError as exc:
        raise SourceIdentityError(f"Dataset root does not exist: {root_input}") from exc
    if not root.is_dir():
        raise SourceIdentityError(f"Dataset root is not a directory: {root}")

    raw_source = Path(source_path).expanduser()
    if raw_source.is_absolute():
        candidate = raw_source
    else:
        portable = PurePosixPath(os.fspath(source_path).replace("\\", "/"))
        if portable.is_absolute() or ".." in portable.parts:
            raise SourceIdentityError(f"Source path escapes dataset root: {source_path}")
        candidate = root.joinpath(*portable.parts)

    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise SourceIdentityError(f"Source file does not exist: {candidate}") from exc
    if not resolved.is_relative_to(root):
        raise SourceIdentityError(f"Source path escapes dataset root: {source_path}")
    if not resolved.is_file():
        raise SourceIdentityError(f"Dataset source is not a file: {resolved}")

    relative = resolved.relative_to(root).as_posix()
    return f"dataset:///{quote(relative, safe='/-._~')}"


def _serialize_state(state: Mapping[str, Any], path: Path) -> bytes:
    if not isinstance(state, Mapping):
        raise InvalidIngestionStateError("Ingestion state must be a mapping")
    try:
        text = json.dumps(dict(state), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    except (TypeError, ValueError) as exc:
        raise InvalidIngestionStateError(f"Ingestion state is not JSON serializable: {path}") from exc
    return text.encode("utf-8")


def _resolve_destination(path: Path) -> Path:
    if path.exists() and path.is_dir():
        raise IngestionStateWriteError(f"Ingestion state destination is a directory: {path}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise IngestionStateWriteError(f"Unable to prepare ingestion state directory: {path.parent}") from exc
    return parent / path.name


def _write_temporary_file(destination: Path, payload: bytes) -> Path:
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
    except OSError as exc:
        raise IngestionStateWriteError(
            f"Unable to create ingestion state temporary file: {destination.parent}"
        ) from exc

    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError as cleanup_exc:
            raise IngestionStateWriteError(
                f"Unable to write ingestion state temporary file {temporary_path}; cleanup also failed: {cleanup_exc}"
            ) from exc
        raise IngestionStateWriteError(f"Unable to write ingestion state temporary file: {temporary_path}") from exc
    return temporary_path


def _normalize_sha256(name: str, value: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise SourceIdentityError(f"{name} must be a 64-character hexadecimal SHA-256 digest")
    return value.lower()
