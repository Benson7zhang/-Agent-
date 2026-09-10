from __future__ import annotations

import argparse
import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from zipfile import BadZipFile

from openpyxl.utils.exceptions import InvalidFileException
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from .config import AppConfig, LLMConfig
from .document_conversion import DocxInspection, inspect_docx_package
from .ingestion import CompanyIndex, PageText, PdfPageExtractor
from .llm import OpenAICompatibleClient, StructuredModelClient
from .llm_extraction import ExtractionStageExecutor
from .schema import FieldSpec, load_schema_from_xlsx


class LLMExtractionCliError(RuntimeError):
    """CLI input or composition failed before the document pipeline could run."""


class DocumentPromptChainResult(Protocol):
    audit_path: Path


class DocumentPromptChain(Protocol):
    def run_pdf(self, input_path: Path, output_dir: Path) -> Path | DocumentPromptChainResult: ...

    def run_docx(self, input_path: Path, output_dir: Path) -> Path | DocumentPromptChainResult: ...


@dataclass(frozen=True, slots=True)
class LoadedPdfInput:
    path: Path
    source_content_sha256: str
    pages: tuple[PageText, ...]

    @property
    def total_pages(self) -> int:
        return len(self.pages)


@dataclass(frozen=True, slots=True)
class LoadedDocxInput:
    path: Path
    inspection: DocxInspection

    @property
    def source_content_sha256(self) -> str:
        return self.inspection.source_sha256


@dataclass(frozen=True, slots=True)
class CliRuntime:
    document: LoadedPdfInput | LoadedDocxInput
    schema_path: Path
    schema: dict[str, list[FieldSpec]]
    company_path: Path
    company_index: CompanyIndex
    output_dir: Path
    config: AppConfig
    executor: ExtractionStageExecutor


ClientFactory = Callable[[LLMConfig], StructuredModelClient]
ExecutorFactory = Callable[[StructuredModelClient, dict[str, list[FieldSpec]]], ExtractionStageExecutor]
PipelineFactory = Callable[[ExtractionStageExecutor, CompanyIndex, AppConfig], DocumentPromptChain]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="执行受控 LLM 财务文档抽取提示词链路")
    parser.add_argument("--input", type=Path, required=True, help="待处理的 PDF 或 DOCX 文件")
    parser.add_argument("--schema", type=Path, required=True, help="数据库字段定义 XLSX")
    parser.add_argument("--company", type=Path, required=True, help="上市公司主数据 XLSX")
    parser.add_argument("--output-dir", type=Path, required=True, help="审计产物输出目录")
    parser.add_argument("--config", type=Path, help="可选 YAML 配置；未指定时读取环境变量")
    return parser


def _resolve_input_file(path: Path, *, label: str, suffix: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise LLMExtractionCliError(f"{label} does not exist: {path}") from exc
    if not resolved.is_file():
        raise LLMExtractionCliError(f"{label} is not a file: {resolved}")
    if resolved.suffix.casefold() != suffix:
        raise LLMExtractionCliError(f"{label} must be a {suffix} file: {resolved}")
    return resolved


def _resolve_output_dir(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.exists() and not resolved.is_dir():
        raise LLMExtractionCliError(f"output directory path is not a directory: {resolved}")
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LLMExtractionCliError(f"cannot create output directory: {resolved}") from exc
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_pdf_input(path: Path) -> LoadedPdfInput:
    resolved = _resolve_input_file(path, label="input document", suffix=".pdf")
    try:
        with resolved.open("rb") as source:
            if source.read(5) != b"%PDF-":
                raise LLMExtractionCliError(f"input document does not have a PDF signature: {resolved}")
        reader = PdfReader(str(resolved))
        if reader.is_encrypted:
            try:
                password_type = reader.decrypt("")
            except (OSError, PyPdfError, TypeError, ValueError) as exc:
                raise LLMExtractionCliError(f"encrypted PDF cannot be opened without a password: {resolved}") from exc
            if password_type == 0:
                raise LLMExtractionCliError(f"encrypted PDF requires a password: {resolved}")
        if not reader.pages:
            raise LLMExtractionCliError(f"input PDF contains no pages: {resolved}")
        pages = tuple(
            PageText(page_no=page_no, text=page.extract_text() or "") for page_no, page in enumerate(reader.pages, 1)
        )
    except LLMExtractionCliError:
        raise
    except (OSError, PyPdfError, KeyError, TypeError, ValueError) as exc:
        raise LLMExtractionCliError(f"failed to read input PDF: {resolved}") from exc
    return LoadedPdfInput(path=resolved, source_content_sha256=_sha256_file(resolved), pages=pages)


def load_docx_input(path: Path) -> LoadedDocxInput:
    resolved = _resolve_input_file(path, label="input document", suffix=".docx")
    try:
        inspection = inspect_docx_package(resolved)
    except (OSError, BadZipFile, KeyError, TypeError, ValueError) as exc:
        raise LLMExtractionCliError(f"failed DOCX security inspection for {resolved}: {exc}") from exc
    return LoadedDocxInput(path=resolved, inspection=inspection)


def load_document_input(path: Path) -> LoadedPdfInput | LoadedDocxInput:
    suffix = path.suffix.casefold()
    if suffix == ".pdf":
        return load_pdf_input(path)
    if suffix == ".docx":
        return load_docx_input(path)
    raise LLMExtractionCliError(f"input document must be a .pdf or .docx file: {path}")


def prepare_runtime(
    args: argparse.Namespace,
    *,
    client_factory: ClientFactory = OpenAICompatibleClient,
    executor_factory: ExecutorFactory = ExtractionStageExecutor,
) -> CliRuntime:
    config_path = None
    if args.config is not None:
        config_path = _resolve_input_file(args.config, label="config", suffix=".yaml")
    config = AppConfig.from_file_or_env(config_path)
    document = load_document_input(args.input)
    if not config.llm.enabled:
        raise LLMExtractionCliError(
            "LLM is not configured; set LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL or provide --config"
        )
    schema_path = _resolve_input_file(args.schema, label="schema workbook", suffix=".xlsx")
    company_path = _resolve_input_file(args.company, label="company workbook", suffix=".xlsx")
    try:
        schema = load_schema_from_xlsx(schema_path)
    except (OSError, BadZipFile, InvalidFileException, KeyError, TypeError, ValueError) as exc:
        raise LLMExtractionCliError(f"failed to load schema workbook: {schema_path}") from exc
    try:
        company_index = CompanyIndex.from_xlsx(company_path)
    except (OSError, BadZipFile, InvalidFileException, KeyError, TypeError, ValueError) as exc:
        raise LLMExtractionCliError(f"failed to load company workbook: {company_path}") from exc
    if not company_index.code_to_abbr:
        raise LLMExtractionCliError(f"company workbook contains no usable records: {company_path}")

    client = client_factory(config.llm)
    if not client.enabled:
        raise LLMExtractionCliError("configured structured model client is disabled")
    try:
        executor = executor_factory(client, schema)
    except (TypeError, ValueError) as exc:
        raise LLMExtractionCliError("failed to construct the extraction stage executor") from exc
    output_dir = _resolve_output_dir(args.output_dir)
    return CliRuntime(
        document=document,
        schema_path=schema_path,
        schema=schema,
        company_path=company_path,
        company_index=company_index,
        output_dir=output_dir,
        config=config,
        executor=executor,
    )


def _default_pipeline_factory(
    executor: ExtractionStageExecutor,
    company_index: CompanyIndex,
    config: AppConfig,
) -> DocumentPromptChain:
    from .llm_document_pipeline import LLMDocumentPipeline

    return LLMDocumentPipeline(
        executor,
        company_index=company_index,
        page_extractor=PdfPageExtractor(config.ocr),
        extractor_version="llm-document-pipeline-v2",
    )


def run(
    argv: Sequence[str] | None = None,
    *,
    client_factory: ClientFactory = OpenAICompatibleClient,
    executor_factory: ExecutorFactory = ExtractionStageExecutor,
    pipeline_factory: PipelineFactory = _default_pipeline_factory,
) -> Path:
    args = build_parser().parse_args(argv)
    runtime = prepare_runtime(args, client_factory=client_factory, executor_factory=executor_factory)
    pipeline = pipeline_factory(runtime.executor, runtime.company_index, runtime.config)
    if isinstance(runtime.document, LoadedDocxInput):
        pipeline_result = pipeline.run_docx(runtime.document.path, runtime.output_dir)
    else:
        pipeline_result = pipeline.run_pdf(runtime.document.path, runtime.output_dir)
    audit_path = pipeline_result if isinstance(pipeline_result, Path) else getattr(pipeline_result, "audit_path", None)
    if not isinstance(audit_path, Path):
        raise LLMExtractionCliError("document pipeline result does not provide an audit_path")
    try:
        audit_bundle = audit_path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise LLMExtractionCliError(f"document pipeline audit bundle does not exist: {audit_path}") from exc
    if not audit_bundle.is_file():
        raise LLMExtractionCliError(f"document pipeline did not return an audit bundle file: {audit_bundle}")
    try:
        audit_bundle.relative_to(runtime.output_dir)
    except ValueError as exc:
        raise LLMExtractionCliError("document pipeline returned an audit bundle outside --output-dir") from exc
    return audit_bundle


def main() -> None:
    audit_bundle = run()
    print(audit_bundle)


if __name__ == "__main__":
    main()
