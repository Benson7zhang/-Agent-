from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from smart_finqa.ingestion_state import (
    IngestionStateReadError,
    IngestionStateStore,
    IngestionStateWriteError,
    InvalidIngestionStateError,
    SourceIdentityError,
    dataset_source_uri,
    ingestion_context_fingerprint,
    sha256_file,
)


def test_state_store_replaces_json_atomically_in_same_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "state" / "ingestion_state.json"
    store = IngestionStateStore(state_path)
    store.save({"version": 1, "files": {}})
    original_replace = os.replace
    observed: dict[str, object] = {}

    def inspect_replace(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        observed["same_directory"] = source_path.parent == destination_path.parent
        observed["temporary_exists"] = source_path.is_file()
        observed["old_state"] = json.loads(destination_path.read_text(encoding="utf-8"))
        original_replace(source_path, destination_path)

    monkeypatch.setattr("smart_finqa.ingestion_state.os.replace", inspect_replace)
    replacement = {"version": 2, "files": {"dataset:///report.pdf": {"status": "parsed"}}}
    store.save(replacement)

    assert observed == {
        "same_directory": True,
        "temporary_exists": True,
        "old_state": {"version": 1, "files": {}},
    }
    assert store.load() == replacement
    assert list(state_path.parent.glob(f".{state_path.name}.*.tmp")) == []


def test_state_store_preserves_previous_file_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "ingestion_state.json"
    store = IngestionStateStore(state_path)
    original = {"version": 1, "files": {}}
    store.save(original)

    def fail_replace(_source: str | Path, _destination: str | Path) -> None:
        raise PermissionError("synthetic replace failure")

    monkeypatch.setattr("smart_finqa.ingestion_state.os.replace", fail_replace)
    with pytest.raises(IngestionStateWriteError, match="Unable to replace ingestion state"):
        store.save({"version": 2, "files": {}})

    assert store.load() == original
    assert list(tmp_path.glob(f".{state_path.name}.*.tmp")) == []


def test_state_store_rejects_invalid_data_and_malformed_json(tmp_path: Path) -> None:
    state_path = tmp_path / "ingestion_state.json"
    store = IngestionStateStore(state_path)

    with pytest.raises(IngestionStateReadError, match="Unable to read ingestion state"):
        store.load()

    with pytest.raises(InvalidIngestionStateError, match="not JSON serializable"):
        store.save({"invalid": object()})
    assert not state_path.exists()

    state_path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(InvalidIngestionStateError, match="Invalid ingestion state JSON"):
        store.load()


def test_sha256_file_detects_content_change_when_size_and_mtime_are_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "report.pdf"
    source.write_bytes(b"AAAA")
    before = source.stat()
    first_digest = sha256_file(source)
    assert first_digest == hashlib.sha256(b"AAAA").hexdigest()

    source.write_bytes(b"BBBB")
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = source.stat()

    assert after.st_size == before.st_size
    assert after.st_mtime_ns == before.st_mtime_ns
    assert sha256_file(source) != first_digest


def test_context_fingerprint_is_deterministic_and_changes_with_every_component() -> None:
    components = {
        "dataset_sha256": "a" * 64,
        "schema_sha256": "b" * 64,
        "company_master_sha256": "c" * 64,
    }
    baseline = ingestion_context_fingerprint(**components)

    assert baseline == ingestion_context_fingerprint(**components)
    assert baseline == ingestion_context_fingerprint(**{key: value.upper() for key, value in components.items()})
    for name, value in components.items():
        changed = dict(components)
        changed[name] = "d" * 64 if value != "d" * 64 else "e" * 64
        assert ingestion_context_fingerprint(**changed) != baseline

    with pytest.raises(SourceIdentityError, match="dataset_sha256"):
        ingestion_context_fingerprint(**{**components, "dataset_sha256": "not-a-hash"})


def test_dataset_source_uri_is_relative_stable_and_normalizes_windows_separators(tmp_path: Path) -> None:
    first_root = tmp_path / "first" / "正式数据"
    second_root = tmp_path / "second" / "正式数据"
    relative = Path("附件2：财务报告") / "reports" / "测试 公司.pdf"
    first_source = first_root / relative
    second_source = second_root / relative
    first_source.parent.mkdir(parents=True)
    second_source.parent.mkdir(parents=True)
    first_source.write_bytes(b"first")
    second_source.write_bytes(b"second")
    windows_relative = str(relative).replace("/", "\\")

    first_uri = dataset_source_uri(first_root, windows_relative)
    assert first_uri == dataset_source_uri(first_root, first_source)
    assert first_uri == dataset_source_uri(second_root, second_source)
    assert first_uri.startswith("dataset:///")
    assert "\\" not in first_uri
    assert "%20" in first_uri


def test_dataset_source_uri_rejects_paths_outside_dataset(tmp_path: Path) -> None:
    dataset_root = tmp_path / "正式数据"
    dataset_root.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"outside")

    with pytest.raises(SourceIdentityError, match="escapes dataset root"):
        dataset_source_uri(dataset_root, outside)
    with pytest.raises(SourceIdentityError, match="escapes dataset root"):
        dataset_source_uri(dataset_root, "..\\outside.pdf")
