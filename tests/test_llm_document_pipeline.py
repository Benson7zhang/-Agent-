from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

import pytest
from pypdf import PdfWriter

from smart_finqa.document_conversion import DocxRendererUnavailableError, RenderedPdf
from smart_finqa.ingestion import PageText
from smart_finqa.ingestion_state import sha256_file
from smart_finqa.llm import ModelCapabilities, ModelResponse
from smart_finqa.llm_document_pipeline import DocumentExtractionError, LLMDocumentPipeline
from smart_finqa.llm_extraction import ExtractionStageExecutor, StageResponseValidationError
from smart_finqa.ocr import OCRToken
from smart_finqa.schema import FieldSpec
from smart_finqa.task2 import MetricSpec


class _PromptChainClient:
    enabled = True
    capabilities = ModelCapabilities()
    layout_region_id = "p1-line5"
    layout_bbox = None

    def __init__(self) -> None:
        self.stages: list[str] = []

    def invoke(self, model_request, *, max_attempts=3) -> ModelResponse:
        manifest = model_request.input_json["request_manifest"]
        stage = model_request.stage
        self.stages.append(stage)
        candidate_ids = re.findall(r"fact_[0-9a-f]{64}", model_request.input_json["prompt_sections"])
        candidate_id = candidate_ids[0] if candidate_ids else None
        payload = self._payload(stage, manifest, candidate_id)
        envelope = {
            "schema_version": "1.0.0",
            "request_id": manifest["request_id"],
            "document_id": manifest["document_id"],
            "source_content_sha256": manifest["source_content_sha256"],
            "stage": stage,
            "prompt_version": manifest["prompt_version"],
            "input_scope_id": manifest["input_scope_id"],
            "status": "OK",
            "payload": payload,
            "issues": [],
        }
        return ModelResponse("response-1", "fake-model", envelope, {}, 1, "stop")

    @classmethod
    def _payload(cls, stage: str, manifest: dict, candidate_id: str | None) -> dict:
        document_id = manifest["document_id"]
        source_hash = manifest["source_content_sha256"]
        if stage == "document-routing":
            return {
                "document_id": document_id,
                "document_type": "financial_report",
                "report_kind": "annual",
                "document_type_evidence": [{"locator": "pdf:p1", "text": "2025年度报告"}],
                "company_candidates": [
                    {
                        "company_name": "金花股份",
                        "stock_code": "600080",
                        "locator": "pdf:p1",
                        "evidence_text": "金花股份 股票代码 600080",
                    }
                ],
                "selected_company": {
                    "company_name": "金花股份",
                    "stock_code": "600080",
                    "selection_reason": "封面明确披露",
                },
                "report_period_candidates": [
                    {"report_period": "2025FY", "locator": "pdf:p1", "evidence_text": "2025年度报告"}
                ],
                "selected_report_period": "2025FY",
                "publication_date": None,
                "sections": [{"kind": "financial_statements", "start_locator": "pdf:p1", "end_locator": "pdf:p1"}],
                "route": "EXTRACT_FINANCIAL_FACTS",
                "confidence": 0.95,
                "issues": [],
            }
        if stage == "layout-recovery":
            return {
                "document_id": document_id,
                "fragments": [
                    {
                        "fragment_id": "fragment-1",
                        "locator_type": "pdf_page",
                        "start_locator": "pdf:p1",
                        "end_locator": "pdf:p1",
                        "physical_page_numbers": [1],
                        "table_index": 1,
                        "table_name": "合并利润表",
                        "statement_table": "income_sheet",
                        "statement_scope": "consolidated",
                        "source_unit": "元",
                        "currency": "CNY",
                        "header_rows": [{"row_index": 1, "cells": ["项目", "2025年度"]}],
                        "data_columns": [
                            {
                                "column_index": 2,
                                "column_label": "2025年度",
                                "column_role": "current_period",
                                "period_hint": "2025FY",
                            }
                        ],
                        "row_range": {"start": 2, "end": 2},
                        "data_rows": [
                            {
                                "row_index": 2,
                                "row_path": ["净利润"],
                                "cells": [
                                    {
                                        "column_index": 2,
                                        "raw_text": "净利润 1,234.50",
                                        "value_kind": "data_value",
                                        "region_id": cls.layout_region_id,
                                        "bbox": cls.layout_bbox,
                                    }
                                ],
                            }
                        ],
                        "repeated_header_rows": [],
                        "excluded_regions": [],
                        "continuation": {
                            "continues_from_fragment_id": None,
                            "may_continue_to_next": False,
                            "evidence": [],
                        },
                        "confidence": 0.95,
                        "issues": [],
                    }
                ],
                "unreadable_regions": [],
                "issues": [],
            }
        if stage == "financial-fact-extraction":
            return {
                "document_id": document_id,
                "fragment_id": "fragment-1",
                "facts": [
                    {
                        "document_id": document_id,
                        "document_type": "financial_report",
                        "company_id": "600080",
                        "company_name": "金花股份",
                        "stock_code": "600080",
                        "report_period": "2025FY",
                        "statement_table": "income_sheet",
                        "statement_scope": "consolidated",
                        "period_type": "duration",
                        "period_semantics": {
                            "as_of_date": None,
                            "period_start": "2025-01-01",
                            "period_end": "2025-12-31",
                            "basis": "annual",
                            "column_role": "current_period",
                        },
                        "metric": "net_profit",
                        "raw_value": "1,234.50",
                        "normalized_value": None,
                        "source_unit": "元",
                        "target_unit": "万元",
                        "currency": "CNY",
                        "conversion": None,
                        "source": {
                            "source_file_name": manifest["source_file_name"],
                            "source_content_sha256": source_hash,
                            "locator_type": "pdf_page",
                            "region_id": cls.layout_region_id,
                            "page_no": 1,
                            "paragraph_no": None,
                            "table_name": "合并利润表",
                            "table_index": 1,
                            "row_index": 2,
                            "column_index": 2,
                            "bbox": cls.layout_bbox,
                            "row_label": "净利润",
                            "column_label": "2025年度",
                            "evidence_text": "净利润 1,234.50",
                        },
                        "row_path": ["净利润"],
                        "negative_interpretation": False,
                        "confidence": 0.95,
                        "candidate_decision": "NEEDS_REVIEW",
                        "issues": [],
                    }
                ],
                "comparison_values": [],
                "unresolved_rows": [],
                "issues": [],
            }
        assert candidate_id is not None
        if stage == "cross-page-reconciliation":
            return {
                "document_id": document_id,
                "fragment_groups": [
                    {
                        "group_id": "group-1",
                        "fragment_ids": ["fragment-1"],
                        "action": "KEEP_SEPARATE",
                        "evidence": ["单页表格"],
                        "confidence": 0.99,
                        "issues": [],
                    }
                ],
                "candidate_assessments": [
                    {
                        "candidate_ids": [candidate_id],
                        "assessment": "DISTINCT",
                        "reason": "唯一候选",
                        "issues": [],
                    }
                ],
                "issues": [],
            }
        if stage == "fact-normalization-plan":
            return {
                "document_id": document_id,
                "facts": [
                    {
                        "candidate_id": candidate_id,
                        "raw_value": "1,234.50",
                        "normalized_value": None,
                        "source_unit": "元",
                        "target_unit": "万元",
                        "currency": "CNY",
                        "report_period": "2025FY",
                        "statement_scope": "consolidated",
                        "period_type": "duration",
                        "conversion": {
                            "parsed_decimal_proposal": "1234.50",
                            "operation": "multiply",
                            "factor": "0.0001",
                            "rule_id": "CNY_YUAN_TO_WANYUAN_V1",
                        },
                        "candidate_decision": "NEEDS_REVIEW",
                        "issues": [],
                    }
                ],
                "issues": [],
            }
        if stage == "validation-explanation":
            return {
                "document_id": document_id,
                "checks": [
                    {
                        "check_id": f"normalization-{candidate_id}",
                        "status": "passed",
                        "issue_code": None,
                        "summary": "单位换算由程序复算通过",
                        "related_candidate_ids": [candidate_id],
                        "review_steps": ["核对原表单位和目标单位"],
                        "candidate_decision": "NEEDS_REVIEW",
                    },
                    {
                        "check_id": "net-profit-margin-600080-2025FY-consolidated",
                        "status": "unavailable",
                        "issue_code": "DETERMINISTIC_CHECK_UNAVAILABLE",
                        "summary": "缺少营业收入，无法复算销售净利率",
                        "related_candidate_ids": [candidate_id],
                        "review_steps": ["补充营业收入后重新校验"],
                        "candidate_decision": "NEEDS_REVIEW",
                    },
                ],
                "issues": [],
            }
        if stage == "candidate-arbitration":
            return {
                "document_id": document_id,
                "groups": [
                    {
                        "business_key": {
                            "stock_code": "600080",
                            "report_period": "2025FY",
                            "statement_scope": "consolidated",
                            "period_type": "duration",
                            "metric": "net_profit",
                        },
                        "candidate_ids": [candidate_id],
                        "selected_candidate_id": candidate_id,
                        "action": "SELECT_FOR_REVIEW",
                        "reason_codes": ["DIRECT_CELL_EVIDENCE"],
                        "reason": "候选与原始行证据一致",
                        "candidate_decision": "NEEDS_REVIEW",
                        "issues": [],
                    }
                ],
                "issues": [],
            }
        if stage == "review-task-generation":
            return {
                "document_id": document_id,
                "document_recommendation": "NEEDS_REVIEW",
                "facts": [
                    {
                        "candidate_id": candidate_id,
                        "persistence_status": "NEEDS_REVIEW",
                        "review_priority": "P1",
                        "review_reasons": [],
                        "review_actions": [{"locator": "pdf:p1:table1:r2:c2", "action": "核对原表数值"}],
                    }
                ],
                "summary": {
                    "candidate_count": 1,
                    "persist_needs_review_count": 1,
                    "do_not_persist_count": 0,
                    "error_count": 0,
                },
                "issues": [],
            }
        raise AssertionError(stage)


def _schema() -> dict[str, list[FieldSpec]]:
    return {
        "income_sheet": [FieldSpec("net_profit", "净利润（万元）", "decimal", "净利润，万元")],
        "balance_sheet": [],
        "cash_flow_sheet": [],
        "core_performance_indicators_sheet": [],
    }


def _metrics() -> dict[str, MetricSpec]:
    return {"net_profit": MetricSpec("net_profit", "净利润", "income_sheet", "net_profit", "万元", ("净利润",))}


def _write_docx(path: Path, *, with_revision: bool = False) -> Path:
    paragraph = b"<w:ins><w:p/></w:ins>" if with_revision else b"<w:p/>"
    document = (
        b'<?xml version="1.0"?><w:document '
        b'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        + paragraph
        + b"</w:body></w:document>"
    )
    parts = {
        "[Content_Types].xml": (
            b'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
        ),
        "_rels/.rels": (
            b'<?xml version="1.0"?><Relationships '
            b'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
        ),
        "word/document.xml": document,
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
        for name, content in parts.items():
            package.writestr(name, content)
    return path


def _readable_report_pages(_path: Path) -> tuple[PageText, ...]:
    return (
        PageText(
            1,
            "金花股份 股票代码 600080\n2025年度报告\n合并利润表 单位 元\n项目 2025年度\n净利润 1,234.50",
        ),
    )


def test_run_pdf_executes_financial_prompt_chain_and_writes_audit(monkeypatch, tmp_path: Path) -> None:
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-fake")
    monkeypatch.setattr(
        "smart_finqa.llm_document_pipeline.extract_pdf_pages",
        _readable_report_pages,
    )
    client = _PromptChainClient()
    executor = ExtractionStageExecutor(client, _schema(), task2_metrics=_metrics())

    result = LLMDocumentPipeline(executor).run_pdf(pdf, tmp_path / "output")

    assert result.status == "NEEDS_REVIEW"
    assert result.candidate_count == 1
    assert result.normalized_count == 1
    assert client.stages == [
        "document-routing",
        "layout-recovery",
        "financial-fact-extraction",
        "cross-page-reconciliation",
        "fact-normalization-plan",
        "validation-explanation",
        "candidate-arbitration",
        "review-task-generation",
    ]
    audit = json.loads(result.audit_path.read_text(encoding="utf-8"))
    assert audit["status"] == "NEEDS_REVIEW"
    assert audit["normalization_results"][0]["normalized_value"] == "0.123450"
    assert [stage["stage"] for stage in audit["stages"]] == client.stages


def test_run_pdf_passes_immutable_ocr_tokens_to_layout_and_records_provenance(monkeypatch, tmp_path: Path) -> None:
    pdf = tmp_path / "scanned-report.pdf"
    pdf.write_bytes(b"%PDF-fake")
    image_hash = "e" * 64
    bbox = (0.1, 0.2, 0.8, 0.3)
    token_texts = ("金花股份 股票代码 600080", "2025年度报告", "合并利润表 单位 元", "项目 2025年度", "净利润 1,234.50")
    tokens = tuple(
        OCRToken(f"ocr-p1-{index:04d}", 1, text, 0.98, bbox, image_hash) for index, text in enumerate(token_texts, 1)
    )
    pages = (
        PageText(
            1,
            "\n".join(token_texts),
            source_type="ocr",
            ocr_tokens=tokens,
            ocr_engine="rapidocr",
            ocr_engine_version="rapidocr-test/PP-OCRv6",
            page_image_sha256=image_hash,
        ),
    )
    monkeypatch.setattr("smart_finqa.llm_document_pipeline.extract_pdf_pages", lambda _path: pages)

    class OCRPromptChainClient(_PromptChainClient):
        layout_region_id = "ocr-p1-0005"
        layout_bbox = {
            "x": 0.1,
            "y": 0.2,
            "width": 0.7,
            "height": 0.1,
            "coordinate_space": "normalized_top_left",
        }

        def __init__(self) -> None:
            super().__init__()
            self.layout_prompt = ""

        def invoke(self, model_request, *, max_attempts=3) -> ModelResponse:
            if model_request.stage == "layout-recovery":
                self.layout_prompt = model_request.input_json["prompt_sections"]
            return super().invoke(model_request, max_attempts=max_attempts)

    client = OCRPromptChainClient()
    result = LLMDocumentPipeline(ExtractionStageExecutor(client, _schema(), task2_metrics=_metrics())).run_pdf(
        pdf,
        tmp_path / "output",
    )

    audit = json.loads(result.audit_path.read_text(encoding="utf-8"))
    assert '"region_id":"ocr-p1-0005"' in client.layout_prompt
    assert '"confidence":0.98' in client.layout_prompt
    assert audit["evidence_mode"] == "mixed_text_ocr_regions"
    assert audit["ocr"]["physical_pages"] == [1]
    assert audit["ocr"]["token_count"] == 5


def test_run_pdf_rejects_layout_bbox_that_differs_from_ocr_evidence(monkeypatch, tmp_path: Path) -> None:
    pdf = tmp_path / "scanned-report.pdf"
    pdf.write_bytes(b"%PDF-fake")
    image_hash = "e" * 64
    source_bbox = (0.1, 0.2, 0.8, 0.3)
    token_texts = ("金花股份 股票代码 600080", "2025年度报告", "合并利润表 单位 元", "项目 2025年度", "净利润 1,234.50")
    tokens = tuple(
        OCRToken(f"ocr-p1-{index:04d}", 1, text, 0.98, source_bbox, image_hash)
        for index, text in enumerate(token_texts, 1)
    )
    pages = (
        PageText(
            1,
            "\n".join(token_texts),
            source_type="ocr",
            ocr_tokens=tokens,
            ocr_engine="rapidocr",
            ocr_engine_version="rapidocr-test/PP-OCRv6",
            page_image_sha256=image_hash,
        ),
    )
    monkeypatch.setattr("smart_finqa.llm_document_pipeline.extract_pdf_pages", lambda _path: pages)

    class TamperedBBoxClient(_PromptChainClient):
        layout_region_id = "ocr-p1-0005"
        layout_bbox = {
            "x": 0.11,
            "y": 0.2,
            "width": 0.7,
            "height": 0.1,
            "coordinate_space": "normalized_top_left",
        }

    pipeline = LLMDocumentPipeline(ExtractionStageExecutor(TamperedBBoxClient(), _schema(), task2_metrics=_metrics()))

    with pytest.raises(DocumentExtractionError, match="bbox differs from immutable OCR evidence"):
        pipeline.run_pdf(pdf, tmp_path / "output")


@pytest.mark.parametrize(
    ("fact_bbox", "expected_error"),
    [
        (None, "OCR-backed fact source omitted its immutable bbox"),
        (
            {
                "x": 0.11,
                "y": 0.2,
                "width": 0.7,
                "height": 0.1,
                "coordinate_space": "normalized_top_left",
            },
            "fact source bbox differs from immutable OCR evidence",
        ),
    ],
)
def test_run_pdf_rejects_missing_or_tampered_fact_bbox(
    monkeypatch,
    tmp_path: Path,
    fact_bbox: dict[str, object] | None,
    expected_error: str,
) -> None:
    pdf = tmp_path / "scanned-report.pdf"
    pdf.write_bytes(b"%PDF-fake")
    image_hash = "e" * 64
    source_bbox = (0.1, 0.2, 0.8, 0.3)
    token_texts = ("金花股份 股票代码 600080", "2025年度报告", "合并利润表 单位 元", "项目 2025年度", "净利润 1,234.50")
    tokens = tuple(
        OCRToken(f"ocr-p1-{index:04d}", 1, text, 0.98, source_bbox, image_hash)
        for index, text in enumerate(token_texts, 1)
    )
    pages = (
        PageText(
            1,
            "\n".join(token_texts),
            source_type="ocr",
            ocr_tokens=tokens,
            ocr_engine="rapidocr",
            ocr_engine_version="rapidocr-test/PP-OCRv6",
            page_image_sha256=image_hash,
        ),
    )
    monkeypatch.setattr("smart_finqa.llm_document_pipeline.extract_pdf_pages", lambda _path: pages)

    class FactBBoxClient(_PromptChainClient):
        layout_region_id = "ocr-p1-0005"
        layout_bbox = {
            "x": 0.1,
            "y": 0.2,
            "width": 0.7,
            "height": 0.1,
            "coordinate_space": "normalized_top_left",
        }

        @classmethod
        def _payload(cls, stage: str, manifest: dict, candidate_id: str | None) -> dict:
            payload = super()._payload(stage, manifest, candidate_id)
            if stage == "financial-fact-extraction":
                payload["facts"][0]["source"]["bbox"] = fact_bbox
            return payload

    pipeline = LLMDocumentPipeline(ExtractionStageExecutor(FactBBoxClient(), _schema(), task2_metrics=_metrics()))

    with pytest.raises(DocumentExtractionError, match=expected_error):
        pipeline.run_pdf(pdf, tmp_path / "output")


def test_run_docx_renders_then_records_complete_lineage(monkeypatch, tmp_path: Path) -> None:
    docx = _write_docx(tmp_path / "report.docx", with_revision=True)

    def render(source_path: Path, output_dir: Path) -> RenderedPdf:
        output_dir.mkdir(parents=True)
        pdf_path = output_dir / "report.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        with pdf_path.open("wb") as output:
            writer.write(output)
        return RenderedPdf(
            source_path=source_path.resolve(),
            source_sha256=sha256_file(source_path),
            pdf_path=pdf_path.resolve(),
            pdf_sha256=sha256_file(pdf_path),
            page_count=1,
            renderer_name="soffice.exe",
            renderer_version="LibreOffice 25.2.1",
            renderer_binary_sha256="b" * 64,
            review_codes=("REVISION_CONFLICT",),
        )

    monkeypatch.setattr("smart_finqa.llm_document_pipeline.extract_pdf_pages", _readable_report_pages)
    client = _PromptChainClient()
    executor = ExtractionStageExecutor(client, _schema(), task2_metrics=_metrics())

    result = LLMDocumentPipeline(executor, docx_renderer=render).run_docx(docx, tmp_path / "output")

    assert result.source_content_sha256 == sha256_file(docx)
    audit = json.loads(result.audit_path.read_text(encoding="utf-8"))
    lineage = audit["document_lineage"]
    assert lineage["source_document"] == {
        "file_name": "report.docx",
        "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "sha256": sha256_file(docx),
    }
    assert lineage["rendered_pdf"]["sha256"] == sha256_file(
        tmp_path / "output" / "document-derivatives" / result.document_id / "report.pdf"
    )
    assert lineage["rendered_pdf"]["page_count"] == 1
    assert lineage["renderer"] == {
        "name": "soffice.exe",
        "version": "LibreOffice 25.2.1",
        "binary_sha256": "b" * 64,
    }
    assert lineage["review_codes"] == ["REVISION_CONFLICT"]
    assert audit["source_content_sha256"] == lineage["rendered_pdf"]["sha256"]


def test_run_docx_fails_explicitly_when_renderer_is_unavailable(tmp_path: Path) -> None:
    docx = _write_docx(tmp_path / "report.docx")

    def unavailable_renderer(source_path: Path, output_dir: Path) -> RenderedPdf:
        raise DocxRendererUnavailableError("DOCX_RENDERER_UNAVAILABLE: install LibreOffice")

    client = _PromptChainClient()
    executor = ExtractionStageExecutor(client, _schema(), task2_metrics=_metrics())

    with pytest.raises(DocxRendererUnavailableError, match="DOCX_RENDERER_UNAVAILABLE"):
        LLMDocumentPipeline(executor, docx_renderer=unavailable_renderer).run_docx(docx, tmp_path / "output")

    assert client.stages == []


def test_routing_refusal_writes_failed_audit(monkeypatch, tmp_path: Path) -> None:
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-fake")
    monkeypatch.setattr("smart_finqa.llm_document_pipeline.extract_pdf_pages", _readable_report_pages)

    class RefusingClient(_PromptChainClient):
        def invoke(self, model_request, *, max_attempts=3) -> ModelResponse:
            response = super().invoke(model_request, max_attempts=max_attempts)
            response.raw_json["status"] = "REFUSED"
            return response

    executor = ExtractionStageExecutor(RefusingClient(), _schema(), task2_metrics=_metrics())
    output_dir = tmp_path / "output"

    with pytest.raises(StageResponseValidationError, match="REFUSED"):
        LLMDocumentPipeline(executor).run_pdf(pdf, output_dir)

    audit_files = list(output_dir.glob("*.llm-extraction.json"))
    assert len(audit_files) == 1
    audit = json.loads(audit_files[0].read_text(encoding="utf-8"))
    assert audit["status"] == "FAILED"
    assert audit["error"]["type"] == "StageResponseValidationError"
    assert audit["source_integrity"] == "VERIFIED"


def test_source_mutation_fails_final_integrity_check(monkeypatch, tmp_path: Path) -> None:
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-fake")
    monkeypatch.setattr("smart_finqa.llm_document_pipeline.extract_pdf_pages", _readable_report_pages)

    class MutatingClient(_PromptChainClient):
        def invoke(self, model_request, *, max_attempts=3) -> ModelResponse:
            response = super().invoke(model_request, max_attempts=max_attempts)
            if model_request.stage == "review-task-generation":
                pdf.write_bytes(b"%PDF-mutated")
            return response

    executor = ExtractionStageExecutor(MutatingClient(), _schema(), task2_metrics=_metrics())
    output_dir = tmp_path / "output"

    with pytest.raises(DocumentExtractionError, match="PDF source changed"):
        LLMDocumentPipeline(executor).run_pdf(pdf, output_dir)

    audit = json.loads(next(output_dir.glob("*.llm-extraction.json")).read_text(encoding="utf-8"))
    assert audit["status"] == "FAILED"
    assert audit["source_integrity"] == "FAILED"


def test_fact_column_must_match_recovered_layout(monkeypatch, tmp_path: Path) -> None:
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-fake")
    monkeypatch.setattr("smart_finqa.llm_document_pipeline.extract_pdf_pages", _readable_report_pages)

    class WrongColumnClient(_PromptChainClient):
        @staticmethod
        def _payload(stage: str, manifest: dict, candidate_id: str | None) -> dict:
            payload = _PromptChainClient._payload(stage, manifest, candidate_id)
            if stage == "financial-fact-extraction":
                payload["facts"][0]["source"]["column_label"] = "2024年度"
            return payload

    executor = ExtractionStageExecutor(WrongColumnClient(), _schema(), task2_metrics=_metrics())

    with pytest.raises(DocumentExtractionError, match="column label"):
        LLMDocumentPipeline(executor).run_pdf(pdf, tmp_path / "output")


def test_review_stage_must_cover_every_server_candidate(monkeypatch, tmp_path: Path) -> None:
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-fake")
    monkeypatch.setattr("smart_finqa.llm_document_pipeline.extract_pdf_pages", _readable_report_pages)

    class IncompleteReviewClient(_PromptChainClient):
        @staticmethod
        def _payload(stage: str, manifest: dict, candidate_id: str | None) -> dict:
            payload = _PromptChainClient._payload(stage, manifest, candidate_id)
            if stage == "review-task-generation":
                payload["facts"] = []
                payload["summary"] = {
                    "candidate_count": 0,
                    "persist_needs_review_count": 0,
                    "do_not_persist_count": 0,
                    "error_count": 0,
                }
            return payload

    executor = ExtractionStageExecutor(IncompleteReviewClient(), _schema(), task2_metrics=_metrics())

    with pytest.raises(DocumentExtractionError, match="review recommendations"):
        LLMDocumentPipeline(executor).run_pdf(pdf, tmp_path / "output")
