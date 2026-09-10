from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from pypdf import PdfWriter

import smart_finqa.web_bootstrap as web_bootstrap
from smart_finqa.database import FinanceDatabaseFactory
from smart_finqa.facts import ExtractedFactCandidate, FinancialFact, PeriodType, SourceOwner, StatementScope
from smart_finqa.ingestion_state import dataset_source_uri
from smart_finqa.schema import load_schema_from_xlsx
from smart_finqa.web_bootstrap import (
    BaselineConflictError,
    BaselineSourceError,
    FileMaterializationError,
    bootstrap_web_documents,
    main,
)
from smart_finqa.web_store import WebStore
from tests.helpers import write_schema_workbook


def _write_pdf(path: Path, *, page_count: int = 2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=100, height=100)
    with path.open("wb") as stream:
        writer.write(stream)
    return path


def _factory(tmp_path: Path) -> tuple[FinanceDatabaseFactory, Path]:
    schema_path = write_schema_workbook(tmp_path / "schema.xlsx")
    schema = load_schema_from_xlsx(schema_path)
    factory = FinanceDatabaseFactory(tmp_path / "finance.db", schema, enable_cache=False)
    with factory.unit_of_work() as database:
        database.create_tables()
    WebStore(factory, tmp_path / "storage").initialize()
    return factory, schema_path


def _insert_current_fact(
    factory: FinanceDatabaseFactory,
    source_file: str,
    *,
    stock_code: str = "600080",
    period: str = "2024FY",
    page_no: int = 2,
    metric: str = "total_profit",
    content_path: Path | None = None,
    source_content_sha256: str | None = None,
    source_owner: SourceOwner = SourceOwner.BATCH,
    document_id: str | None = None,
) -> str:
    source_path = content_path or Path(source_file)
    content_sha256 = source_content_sha256 or hashlib.sha256(source_path.read_bytes()).hexdigest()
    fact = FinancialFact.from_candidate(
        ExtractedFactCandidate(
            metric=metric,
            raw_value=100.0,
            source_unit="万元",
            page_no=page_no,
            table_name="income_sheet",
            row_label="利润总额",
            column_label="本期金额",
            confidence=0.9,
        ),
        company_id=stock_code,
        stock_code=stock_code,
        period=period,
        statement_scope=StatementScope.CONSOLIDATED,
        period_type=PeriodType.DURATION,
        target_unit="万元",
        currency="CNY",
        source_file=source_file,
        source_content_sha256=content_sha256,
        extractor_version="test-v1",
    )
    with factory.unit_of_work() as database:
        database.upsert_financial_facts([fact])
        database.execute(
            "UPDATE financial_source SET source_owner = ?, document_id = ? WHERE source_key = ?",
            (source_owner.value, document_id, fact.source_key),
        )
        if document_id is not None:
            database.execute(
                "UPDATE financial_fact SET document_id = ? WHERE source_key = ?",
                (document_id, fact.source_key),
            )
        rows = database.query(
            f"SELECT fact_key FROM financial_fact WHERE source_key = {database.parameter_marker}",
            (fact.source_key,),
            use_cache=False,
        )
    return str(rows[0]["fact_key"])


def _rows(factory: FinanceDatabaseFactory, sql: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
    with factory.unit_of_work() as database:
        return database.query(sql, params, use_cache=False)


def test_bootstrap_copies_current_baseline_by_default_and_is_idempotent(tmp_path: Path) -> None:
    factory, _schema_path = _factory(tmp_path)
    dataset_root = tmp_path / "正式数据"
    source_path = _write_pdf(dataset_root / "附件2：财务报告" / "金花股份.pdf", page_count=2)
    fact_key = _insert_current_fact(factory, str(source_path))
    storage_root = tmp_path / "storage"

    first = bootstrap_web_documents(factory, dataset_root=dataset_root, storage_root=storage_root)
    second = bootstrap_web_documents(factory, dataset_root=dataset_root, storage_root=storage_root)

    assert first.created_count == 1
    assert first.existing_count == 0
    assert second.created_count == 0
    assert second.existing_count == 1
    assert first.documents[0].document_id == second.documents[0].document_id
    document = first.documents[0]
    stored_path = storage_root / Path(document.storage_key)
    stored_bytes = stored_path.read_bytes()
    assert not os.path.samefile(source_path, stored_path)
    assert document.source_uri == dataset_source_uri(dataset_root, source_path)
    assert document.sha256 == hashlib.sha256(source_path.read_bytes()).hexdigest()
    assert document.page_count == 2
    assert document.fact_count == 1

    source_path.write_bytes(b"source changed after bootstrap")
    assert stored_path.read_bytes() == stored_bytes

    documents = _rows(factory, "SELECT * FROM document")
    assert len(documents) == 1
    assert documents[0]["document_id"] == document.document_id
    assert documents[0]["page_count"] == 2
    assert documents[0]["status"] == "NEEDS_REVIEW"
    assert _rows(factory, "SELECT COUNT(*) AS count FROM job")[0]["count"] == 0
    assert _rows(factory, "SELECT document_id FROM financial_fact WHERE fact_key = ?", (fact_key,)) == [
        {"document_id": document.document_id}
    ]
    assert _rows(factory, "SELECT document_id FROM financial_source WHERE authority_status = 'CURRENT'") == [
        {"document_id": document.document_id}
    ]


def test_bootstrap_ignores_current_web_source_on_first_and_idempotent_runs(tmp_path: Path) -> None:
    factory, _schema_path = _factory(tmp_path)
    dataset_root = tmp_path / "正式数据"
    batch_path = _write_pdf(dataset_root / "附件2：财务报告" / "batch.pdf")
    batch_fact_key = _insert_current_fact(factory, str(batch_path), period="2024FY")
    storage_root = tmp_path / "storage"

    web_path = _write_pdf(storage_root / "documents" / "web-upload.pdf")
    web_sha256 = hashlib.sha256(web_path.read_bytes()).hexdigest()
    web_document = WebStore(factory, storage_root).create_document(
        kind="financial_report",
        original_name=web_path.name,
        storage_key="documents/web-upload.pdf",
        sha256=web_sha256,
        size_bytes=web_path.stat().st_size,
        mime_type="application/pdf",
        status="NEEDS_REVIEW",
        page_count=2,
    )
    web_fact_key = _insert_current_fact(
        factory,
        "documents/web-upload.pdf",
        period="2023FY",
        content_path=web_path,
        source_owner=SourceOwner.WEB,
        document_id=web_document.document_id,
    )

    first = bootstrap_web_documents(factory, dataset_root=dataset_root, storage_root=storage_root)
    second = bootstrap_web_documents(factory, dataset_root=dataset_root, storage_root=storage_root)

    assert first.created_count == 1
    assert first.existing_count == 0
    assert second.created_count == 0
    assert second.existing_count == 1
    assert first.documents[0].document_id == second.documents[0].document_id
    assert first.documents[0].source_uri == dataset_source_uri(dataset_root, batch_path)
    assert _rows(factory, "SELECT document_id FROM financial_fact WHERE fact_key = ?", (batch_fact_key,)) == [
        {"document_id": first.documents[0].document_id}
    ]
    assert _rows(
        factory,
        "SELECT source_owner, document_id FROM financial_source WHERE source_file = ?",
        ("documents/web-upload.pdf",),
    ) == [{"source_owner": SourceOwner.WEB.value, "document_id": web_document.document_id}]
    assert _rows(factory, "SELECT document_id FROM financial_fact WHERE fact_key = ?", (web_fact_key,)) == [
        {"document_id": web_document.document_id}
    ]


def test_bootstrap_hardlink_mode_is_explicit_and_shares_the_source_inode(tmp_path: Path) -> None:
    factory, _schema_path = _factory(tmp_path)
    dataset_root = tmp_path / "正式数据"
    source_path = _write_pdf(dataset_root / "附件2：财务报告" / "金花股份.pdf")
    _insert_current_fact(factory, str(source_path))
    storage_root = tmp_path / "storage"

    result = bootstrap_web_documents(
        factory,
        dataset_root=dataset_root,
        storage_root=storage_root,
        file_mode="hardlink",
    )

    stored_path = storage_root / Path(result.documents[0].storage_key)
    assert stored_path.read_bytes() == source_path.read_bytes()
    assert os.path.samefile(source_path, stored_path)


def test_bootstrap_hardlink_failure_is_explicit_and_does_not_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, _schema_path = _factory(tmp_path)
    dataset_root = tmp_path / "正式数据"
    source_path = _write_pdf(dataset_root / "附件2：财务报告" / "金花股份.pdf")
    _insert_current_fact(factory, str(source_path))
    storage_root = tmp_path / "storage"

    def fail_link(_source: Path, _destination: Path) -> None:
        raise OSError("synthetic cross-device link failure")

    monkeypatch.setattr("smart_finqa.web_bootstrap.os.link", fail_link)
    with pytest.raises(FileMaterializationError, match="hardlink"):
        bootstrap_web_documents(
            factory,
            dataset_root=dataset_root,
            storage_root=storage_root,
            file_mode="hardlink",
        )

    assert _rows(factory, "SELECT COUNT(*) AS count FROM document")[0]["count"] == 0
    assert list((storage_root / "documents").rglob("*.pdf")) == []


def test_bootstrap_rejects_conflicting_existing_storage_content(tmp_path: Path) -> None:
    factory, _schema_path = _factory(tmp_path)
    dataset_root = tmp_path / "正式数据"
    source_path = _write_pdf(dataset_root / "附件2：财务报告" / "金花股份.pdf")
    _insert_current_fact(factory, str(source_path))
    storage_root = tmp_path / "storage"

    first = bootstrap_web_documents(
        factory,
        dataset_root=dataset_root,
        storage_root=storage_root,
        file_mode="copy",
    )
    stored_path = storage_root / Path(first.documents[0].storage_key)
    stored_path.write_bytes(b"not-the-source-pdf")

    with pytest.raises(FileMaterializationError, match="content conflicts with source SHA-256"):
        bootstrap_web_documents(
            factory,
            dataset_root=dataset_root,
            storage_root=storage_root,
            file_mode="copy",
        )

    assert stored_path.read_bytes() == b"not-the-source-pdf"
    assert _rows(factory, "SELECT COUNT(*) AS count FROM document")[0]["count"] == 1


def test_bootstrap_rejects_current_source_outside_dataset(tmp_path: Path) -> None:
    factory, _schema_path = _factory(tmp_path)
    dataset_root = tmp_path / "正式数据"
    dataset_root.mkdir()
    outside = _write_pdf(tmp_path / "outside.pdf")
    _insert_current_fact(factory, str(outside))

    with pytest.raises(BaselineSourceError, match="outside dataset root"):
        bootstrap_web_documents(factory, dataset_root=dataset_root, storage_root=tmp_path / "storage")


def test_bootstrap_accepts_canonical_dataset_uri_and_windows_relative_source(tmp_path: Path) -> None:
    factory, _schema_path = _factory(tmp_path)
    dataset_root = tmp_path / "正式数据"
    first_path = _write_pdf(dataset_root / "附件2：财务报告" / "uri source.pdf")
    second_path = _write_pdf(dataset_root / "附件2：财务报告" / "windows source.pdf")
    source_uri = dataset_source_uri(dataset_root, first_path)
    windows_relative = str(second_path.relative_to(dataset_root)).replace("/", "\\")
    _insert_current_fact(factory, source_uri, period="2023FY", content_path=first_path)
    _insert_current_fact(factory, windows_relative, period="2024FY", content_path=second_path)

    result = bootstrap_web_documents(factory, dataset_root=dataset_root, storage_root=tmp_path / "storage")

    assert {document.source_uri for document in result.documents} == {
        source_uri,
        dataset_source_uri(dataset_root, second_path),
    }


def test_bootstrap_rejects_same_pdf_registered_under_multiple_path_forms(tmp_path: Path) -> None:
    factory, _schema_path = _factory(tmp_path)
    dataset_root = tmp_path / "正式数据"
    source_path = _write_pdf(dataset_root / "附件2：财务报告" / "shared.pdf")
    relative_source = str(source_path.relative_to(dataset_root)).replace("/", "\\")
    _insert_current_fact(factory, str(source_path), period="2023FY")
    _insert_current_fact(factory, relative_source, period="2024FY", content_path=source_path)

    with pytest.raises(BaselineConflictError, match="reused by multiple periods"):
        bootstrap_web_documents(factory, dataset_root=dataset_root, storage_root=tmp_path / "storage")

    assert _rows(factory, "SELECT COUNT(*) AS count FROM document")[0]["count"] == 0
    assert list((tmp_path / "storage" / "documents").rglob("*.pdf")) == []


def test_bootstrap_reports_fact_count_from_locked_registration_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, _schema_path = _factory(tmp_path)
    dataset_root = tmp_path / "正式数据"
    source_path = _write_pdf(dataset_root / "附件2：财务报告" / "金花股份.pdf")
    _insert_current_fact(factory, str(source_path))
    original_materialize = web_bootstrap._materialize_file
    inserted = False

    def materialize_with_concurrent_fact(plan: object, destination: Path, *, file_mode: str) -> bool:
        nonlocal inserted
        created = original_materialize(plan, destination, file_mode=file_mode)
        if not inserted:
            _insert_current_fact(factory, str(source_path), metric="net_profit")
            inserted = True
        return created

    monkeypatch.setattr(web_bootstrap, "_materialize_file", materialize_with_concurrent_fact)

    result = bootstrap_web_documents(factory, dataset_root=dataset_root, storage_root=tmp_path / "storage")

    assert result.documents[0].fact_count == 2
    assert _rows(factory, "SELECT COUNT(*) AS count FROM financial_fact WHERE document_id IS NOT NULL")[0]["count"] == 2


def test_bootstrap_links_only_current_sources(tmp_path: Path) -> None:
    factory, _schema_path = _factory(tmp_path)
    dataset_root = tmp_path / "正式数据"
    current_path = _write_pdf(dataset_root / "附件2：财务报告" / "current.pdf")
    superseded_path = _write_pdf(dataset_root / "附件2：财务报告" / "superseded.pdf")
    _insert_current_fact(factory, str(current_path), period="2024FY")
    superseded_fact = _insert_current_fact(factory, str(superseded_path), period="2023FY")
    with factory.unit_of_work() as database:
        database.execute(
            "UPDATE financial_source SET authority_status = ?, current_slot = NULL WHERE source_file = ?",
            ("SUPERSEDED", str(superseded_path)),
        )

    result = bootstrap_web_documents(factory, dataset_root=dataset_root, storage_root=tmp_path / "storage")

    assert result.document_count == 1
    assert result.documents[0].source_uri == dataset_source_uri(dataset_root, current_path)
    assert _rows(factory, "SELECT document_id FROM financial_fact WHERE fact_key = ?", (superseded_fact,)) == [
        {"document_id": None}
    ]


def test_bootstrap_links_facts_by_source_revision_instead_of_reused_path(tmp_path: Path) -> None:
    factory, _schema_path = _factory(tmp_path)
    dataset_root = tmp_path / "正式数据"
    source_path = _write_pdf(dataset_root / "附件2：财务报告" / "same-path.pdf")
    current_fact = _insert_current_fact(factory, str(source_path))
    historical_fact = _insert_current_fact(
        factory,
        str(source_path),
        metric="net_profit",
        source_content_sha256="0" * 64,
    )

    bootstrap_web_documents(factory, dataset_root=dataset_root, storage_root=tmp_path / "storage")

    assert _rows(
        factory,
        "SELECT fact_key, document_id FROM financial_fact ORDER BY fact_key",
    ) == sorted(
        [
            {
                "fact_key": current_fact,
                "document_id": _rows(factory, "SELECT document_id FROM document")[0]["document_id"],
            },
            {"fact_key": historical_fact, "document_id": None},
        ],
        key=lambda row: str(row["fact_key"]),
    )


def test_bootstrap_rejects_current_source_when_registered_content_hash_differs_from_pdf(tmp_path: Path) -> None:
    factory, _schema_path = _factory(tmp_path)
    dataset_root = tmp_path / "正式数据"
    source_path = _write_pdf(dataset_root / "附件2：财务报告" / "changed.pdf")
    _insert_current_fact(factory, str(source_path), source_content_sha256="0" * 64)

    with pytest.raises(BaselineConflictError, match="content SHA-256"):
        bootstrap_web_documents(factory, dataset_root=dataset_root, storage_root=tmp_path / "storage")


def test_bootstrap_rejects_fact_page_outside_pdf_and_cleans_materialized_file(tmp_path: Path) -> None:
    factory, _schema_path = _factory(tmp_path)
    dataset_root = tmp_path / "正式数据"
    source_path = _write_pdf(dataset_root / "附件2：财务报告" / "金花股份.pdf", page_count=1)
    _insert_current_fact(factory, str(source_path), page_no=2)
    storage_root = tmp_path / "storage"

    with pytest.raises(BaselineSourceError, match="exceeds PDF page_count"):
        bootstrap_web_documents(
            factory,
            dataset_root=dataset_root,
            storage_root=storage_root,
            file_mode="copy",
        )

    assert _rows(factory, "SELECT COUNT(*) AS count FROM document")[0]["count"] == 0
    assert list((storage_root / "documents").rglob("*.pdf")) == []


def test_bootstrap_rolls_back_all_documents_when_one_current_source_conflicts(tmp_path: Path) -> None:
    factory, _schema_path = _factory(tmp_path)
    dataset_root = tmp_path / "正式数据"
    first_path = _write_pdf(dataset_root / "附件2：财务报告" / "2023.pdf")
    second_path = _write_pdf(dataset_root / "附件2：财务报告" / "2024.pdf")
    _insert_current_fact(factory, str(first_path), period="2023FY")
    conflicting_fact = _insert_current_fact(factory, str(second_path), period="2024FY")
    with factory.unit_of_work() as database:
        database.execute(
            "UPDATE financial_fact SET document_id = ? WHERE fact_key = ?",
            ("00000000-0000-0000-0000-000000000001", conflicting_fact),
        )
    storage_root = tmp_path / "storage"

    with pytest.raises(BaselineConflictError, match="different document"):
        bootstrap_web_documents(
            factory,
            dataset_root=dataset_root,
            storage_root=storage_root,
            file_mode="copy",
        )

    assert _rows(factory, "SELECT COUNT(*) AS count FROM document")[0]["count"] == 0
    assert _rows(factory, "SELECT document_id FROM financial_fact WHERE source_file = ?", (str(first_path),)) == [
        {"document_id": None}
    ]
    assert list((storage_root / "documents").rglob("*.pdf")) == []


def test_cli_imports_existing_sqlite_baseline_without_creating_job(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    factory, schema_path = _factory(tmp_path)
    dataset_root = tmp_path / "正式数据"
    source_path = _write_pdf(dataset_root / "附件2：财务报告" / "金花股份.pdf")
    _insert_current_fact(factory, str(source_path))
    storage_root = tmp_path / "cli-storage"

    exit_code = main(
        [
            "--dataset-root",
            str(dataset_root),
            "--storage-root",
            str(storage_root),
            "--schema-xlsx",
            str(schema_path),
            "--db-path",
            str(tmp_path / "finance.db"),
        ]
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["document_count"] == 1
    stored_path = storage_root / Path(output["documents"][0]["storage_key"])
    stored_bytes = stored_path.read_bytes()
    source_path.write_bytes(b"source changed after CLI bootstrap")
    assert stored_path.read_bytes() == stored_bytes
    assert not os.path.samefile(source_path, stored_path)
    assert _rows(factory, "SELECT COUNT(*) AS count FROM job")[0]["count"] == 0
