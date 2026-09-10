from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import write_company_workbook, write_schema_workbook
from pypdf import PdfWriter

from smart_finqa.document_conversion import DocxRendererUnavailableError
from smart_finqa.llm_extraction_cli import (
    LLMExtractionCliError,
    build_parser,
    load_docx_input,
    load_pdf_input,
    prepare_runtime,
    run,
)


class _FakeClient:
    enabled = True

    def __init__(self, config) -> None:
        self.config = config


class _FakeExecutor:
    def __init__(self, client, schema) -> None:
        self.client = client
        self.schema = schema


class _FakePipeline:
    def __init__(self, executor) -> None:
        self.executor = executor
        self.calls = []

    def run_pdf(self, input_path: Path, output_dir: Path) -> object:
        self.calls.append(("pdf", input_path, output_dir))
        output_path = output_dir / "audit-bundle.json"
        output_path.write_text(json.dumps({"status": "NEEDS_REVIEW"}), encoding="utf-8")
        return SimpleNamespace(audit_path=output_path)

    def run_docx(self, input_path: Path, output_dir: Path) -> object:
        self.calls.append(("docx", input_path, output_dir))
        output_path = output_dir / "audit-bundle.json"
        output_path.write_text(json.dumps({"status": "NEEDS_REVIEW"}), encoding="utf-8")
        return SimpleNamespace(audit_path=output_path)


def _write_pdf(path: Path) -> Path:
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with path.open("wb") as output:
        writer.write(output)
    return path


def _write_docx(path: Path) -> Path:
    parts = {
        "[Content_Types].xml": (
            b'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
        ),
        "_rels/.rels": (
            b'<?xml version="1.0"?><Relationships '
            b'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
        ),
        "word/document.xml": (
            b'<?xml version="1.0"?><w:document '
            b'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p/></w:body>'
            b"</w:document>"
        ),
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
        for name, content in parts.items():
            package.writestr(name, content)
    return path


def _arguments(tmp_path: Path, *, input_path: Path | None = None) -> list[str]:
    return [
        "--input",
        str(input_path or _write_pdf(tmp_path / "report.pdf")),
        "--schema",
        str(write_schema_workbook(tmp_path / "schema.xlsx")),
        "--company",
        str(write_company_workbook(tmp_path / "company.xlsx")),
        "--output-dir",
        str(tmp_path / "outputs"),
    ]


def _set_llm_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-secret")
    monkeypatch.setenv("LLM_MODEL", "test-model")


def test_parser_requires_all_document_inputs() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_load_pdf_input_returns_hash_and_one_based_pages(tmp_path: Path) -> None:
    pdf_path = _write_pdf(tmp_path / "report.pdf")

    document = load_pdf_input(pdf_path)

    assert document.path == pdf_path.resolve()
    assert document.total_pages == 1
    assert document.pages[0].page_no == 1
    assert len(document.source_content_sha256) == 64


def test_load_pdf_input_rejects_fake_pdf(tmp_path: Path) -> None:
    fake_pdf = tmp_path / "report.pdf"
    fake_pdf.write_bytes(b"not-a-pdf")

    with pytest.raises(LLMExtractionCliError, match="PDF signature"):
        load_pdf_input(fake_pdf)


def test_load_docx_input_runs_safe_package_inspection(tmp_path: Path) -> None:
    document = load_docx_input(_write_docx(tmp_path / "report.docx"))

    assert document.path == (tmp_path / "report.docx").resolve()
    assert len(document.source_content_sha256) == 64
    assert document.inspection.entry_count == 3


def test_load_docx_input_rejects_invalid_package(tmp_path: Path) -> None:
    invalid = tmp_path / "report.docx"
    invalid.write_bytes(b"not-a-docx")

    with pytest.raises(LLMExtractionCliError, match="DOCX security inspection"):
        load_docx_input(invalid)


def test_prepare_runtime_requires_explicit_llm_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    for variable in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(variable, raising=False)
    args = build_parser().parse_args(_arguments(tmp_path))

    with pytest.raises(LLMExtractionCliError, match="LLM is not configured"):
        prepare_runtime(args, client_factory=_FakeClient, executor_factory=_FakeExecutor)


def test_prepare_runtime_loads_schema_company_and_executor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_llm_environment(monkeypatch)
    args = build_parser().parse_args(_arguments(tmp_path))

    runtime = prepare_runtime(args, client_factory=_FakeClient, executor_factory=_FakeExecutor)

    assert runtime.document.total_pages == 1
    assert runtime.schema["income_sheet"]
    assert runtime.company_index.code_to_abbr["600080"] == "金花股份"
    assert runtime.output_dir.is_dir()
    assert runtime.executor.client.config.api_key == "test-secret"


def test_run_delegates_to_injected_document_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_llm_environment(monkeypatch)
    pipelines: list[_FakePipeline] = []

    def pipeline_factory(executor, company_index, _config) -> _FakePipeline:
        assert company_index.code_to_abbr["600080"] == "金花股份"
        pipeline = _FakePipeline(executor)
        pipelines.append(pipeline)
        return pipeline

    audit_bundle = run(
        _arguments(tmp_path),
        client_factory=_FakeClient,
        executor_factory=_FakeExecutor,
        pipeline_factory=pipeline_factory,
    )

    assert audit_bundle == (tmp_path / "outputs" / "audit-bundle.json").resolve()
    assert pipelines[0].calls == [("pdf", (tmp_path / "report.pdf").resolve(), (tmp_path / "outputs").resolve())]


def test_run_delegates_docx_to_document_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_llm_environment(monkeypatch)
    pipelines: list[_FakePipeline] = []

    def pipeline_factory(executor, company_index, _config) -> _FakePipeline:
        pipeline = _FakePipeline(executor)
        pipelines.append(pipeline)
        return pipeline

    docx = _write_docx(tmp_path / "report.docx")
    run(
        _arguments(tmp_path, input_path=docx),
        client_factory=_FakeClient,
        executor_factory=_FakeExecutor,
        pipeline_factory=pipeline_factory,
    )

    assert pipelines[0].calls == [("docx", docx.resolve(), (tmp_path / "outputs").resolve())]


def test_run_preserves_explicit_renderer_unavailable_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_llm_environment(monkeypatch)

    class _UnavailablePipeline:
        def run_docx(self, input_path: Path, output_dir: Path) -> object:
            raise DocxRendererUnavailableError("DOCX_RENDERER_UNAVAILABLE: install LibreOffice")

    with pytest.raises(DocxRendererUnavailableError, match="DOCX_RENDERER_UNAVAILABLE"):
        run(
            _arguments(tmp_path, input_path=_write_docx(tmp_path / "report.docx")),
            client_factory=_FakeClient,
            executor_factory=_FakeExecutor,
            pipeline_factory=lambda executor, company_index, config: _UnavailablePipeline(),
        )


def test_run_rejects_audit_bundle_outside_output_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_llm_environment(monkeypatch)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")

    class _OutsidePipeline:
        def run_pdf(self, input_path: Path, output_dir: Path) -> Path:
            return outside

    with pytest.raises(LLMExtractionCliError, match="outside --output-dir"):
        run(
            _arguments(tmp_path),
            client_factory=_FakeClient,
            executor_factory=_FakeExecutor,
            pipeline_factory=lambda executor, company_index, config: _OutsidePipeline(),
        )


def test_run_rejects_missing_audit_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_llm_environment(monkeypatch)

    class _MissingPipeline:
        def run_pdf(self, input_path: Path, output_dir: Path) -> object:
            return SimpleNamespace(audit_path=output_dir / "missing.json")

    with pytest.raises(LLMExtractionCliError, match="does not exist"):
        run(
            _arguments(tmp_path),
            client_factory=_FakeClient,
            executor_factory=_FakeExecutor,
            pipeline_factory=lambda executor, company_index, config: _MissingPipeline(),
        )
