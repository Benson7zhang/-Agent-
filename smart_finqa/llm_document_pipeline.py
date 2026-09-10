"""Document-level orchestration for the versioned LLM extraction prompts."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from .document_conversion import DocxInspection, RenderedPdf, inspect_docx_package, render_docx_to_pdf
from .ingestion import (
    FINANCIAL_STATEMENT_PAGE_PATTERN,
    CompanyIndex,
    PageText,
    PdfPageExtractor,
    extract_pdf_pages,
)
from .ingestion_state import sha256_file
from .llm_extraction import (
    DEFAULT_CONVERSION_RULES,
    AssignedFactCandidate,
    ExtractionStageExecutor,
    NormalizationError,
    NormalizedFactCandidate,
    StageExecutionResult,
    assign_candidate_ids,
    normalize_candidates,
)
from .llm_extraction_contracts import (
    CandidateArbitrationPayload,
    CrossPageReconciliationPayload,
    DocumentRoutingPayload,
    FactNormalizationPayload,
    FinancialFactExtractionPayload,
    LayoutRecoveryPayload,
    RequestManifest,
    ResearchEvidenceExtractionPayload,
    ReviewTaskGenerationPayload,
    TableFragment,
    ValidationExplanationPayload,
)
from .llm_prompts import get_prompt

DEFAULT_PAGE_WINDOW_SIZE = 3
MAX_REGION_TEXT_LENGTH = 4000
PDF_LOCATOR_PATTERN = re.compile(r"^pdf:p(?P<page>[1-9][0-9]*)$")


class DocumentExtractionError(RuntimeError):
    """A document cannot complete the controlled extraction chain."""


@dataclass(frozen=True, slots=True)
class SourceRegion:
    region_id: str
    page_no: int
    line_no: int
    text: str
    source_type: str = "native_text"
    bbox: tuple[float, float, float, float] | None = None
    confidence: float | None = None
    page_image_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "region_id": self.region_id,
            "locator": f"pdf:p{self.page_no}:line{self.line_no}",
            "page_no": self.page_no,
            "line_no": self.line_no,
            "text": self.text,
            "source_type": self.source_type,
        }
        if self.bbox is not None:
            left, top, right, bottom = self.bbox
            payload["bbox"] = {
                "x": left,
                "y": top,
                "width": round(right - left, 6),
                "height": round(bottom - top, 6),
                "coordinate_space": "normalized_top_left",
            }
        if self.confidence is not None:
            payload["confidence"] = self.confidence
        if self.page_image_sha256 is not None:
            payload["page_image_sha256"] = self.page_image_sha256
        return payload


@dataclass(frozen=True, slots=True)
class DocumentExtractionResult:
    document_id: str
    source_content_sha256: str
    status: str
    audit_path: Path
    candidate_count: int
    normalized_count: int
    review_recommendation: str


@dataclass(frozen=True, slots=True)
class DocumentLineage:
    source_file_name: str
    source_content_sha256: str
    rendered_pdf: RenderedPdf


DocxRenderer = Callable[[Path, Path], RenderedPdf]


class LLMDocumentPipeline:
    """Execute P0-P8 while keeping facts untrusted and fully auditable."""

    def __init__(
        self,
        executor: ExtractionStageExecutor,
        *,
        company_index: CompanyIndex | None = None,
        extractor_version: str = "llm-document-pipeline-v1",
        page_window_size: int = DEFAULT_PAGE_WINDOW_SIZE,
        docx_renderer: DocxRenderer | None = None,
        page_extractor: PdfPageExtractor | None = None,
    ) -> None:
        if isinstance(page_window_size, bool) or not isinstance(page_window_size, int) or page_window_size < 1:
            raise ValueError("page_window_size must be a positive integer")
        self.executor = executor
        self.company_index = company_index
        self.extractor_version = extractor_version
        self.page_window_size = page_window_size
        self.docx_renderer = docx_renderer or render_docx_to_pdf
        self.page_extractor = page_extractor
        self._request_sequence = 0

    def run_pdf(self, input_path: Path, output_dir: Path) -> DocumentExtractionResult:
        return self._run_pdf(input_path, output_dir)

    def run_docx(self, input_path: Path, output_dir: Path) -> DocumentExtractionResult:
        source_path = input_path.resolve(strict=True)
        if not source_path.is_file() or source_path.suffix.casefold() != ".docx":
            raise ValueError("LLM DOCX extraction requires a DOCX input")
        inspection = inspect_docx_package(source_path)
        destination = output_dir.resolve()
        destination.mkdir(parents=True, exist_ok=True)
        derivative_dir = destination / "document-derivatives" / f"doc_{inspection.source_sha256[:24]}"
        rendered_pdf = self.docx_renderer(source_path, derivative_dir)
        self._validate_rendered_pdf(source_path, inspection, rendered_pdf)
        return self._run_pdf(
            rendered_pdf.pdf_path,
            destination,
            lineage=DocumentLineage(source_path.name, inspection.source_sha256, rendered_pdf),
        )

    def _run_pdf(
        self,
        input_path: Path,
        output_dir: Path,
        *,
        lineage: DocumentLineage | None = None,
    ) -> DocumentExtractionResult:
        source_path = input_path.resolve(strict=True)
        if not source_path.is_file() or source_path.suffix.casefold() != ".pdf":
            raise ValueError("LLM document extraction currently requires a PDF input")
        source_hash = sha256_file(source_path)
        result_source_hash = lineage.source_content_sha256 if lineage is not None else source_hash
        document_id = "doc_" + result_source_hash[:24]
        pages = (
            self.page_extractor.extract(source_path)
            if self.page_extractor is not None
            else extract_pdf_pages(source_path)
        )
        self._verify_source_integrity(source_path, source_hash, lineage)
        if lineage is not None and len(pages) != lineage.rendered_pdf.page_count:
            raise DocumentExtractionError("rendered PDF page count differs from renderer lineage")
        if not pages or not any(page.text.strip() for page in pages):
            raise DocumentExtractionError("PDF has no readable text; configure OCR or a vision-capable model")
        regions = _build_source_regions(pages)
        if not regions:
            raise DocumentExtractionError("PDF contains no non-empty source regions")
        ocr_pages = [page for page in pages if page.ocr_engine is not None]
        audit: dict[str, Any] = {
            "schema_version": "1.0.0",
            "document_id": document_id,
            "source_file_name": source_path.name,
            "source_content_sha256": source_hash,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "extractor_version": self.extractor_version,
            "metric_catalog_version": self.executor.metric_catalog.catalog_version,
            "evidence_mode": "mixed_text_ocr_regions" if ocr_pages else "pdf_text_regions",
            "automatic_persistence": False,
            "limitations": ["NO_DETERMINISTIC_TABLE_GRID" if ocr_pages else "NO_VISUAL_TABLE_GEOMETRY"],
            "ocr": _ocr_audit_summary(ocr_pages),
            "stages": [],
            "normalization_results": [],
            "deterministic_checks": [],
        }
        if lineage is not None:
            audit["document_lineage"] = _document_lineage_dict(lineage)
        destination = output_dir.resolve()
        destination.mkdir(parents=True, exist_ok=True)
        audit_path = destination / f"{document_id}.llm-extraction.json"

        try:
            routing_regions = _routing_sample_regions(regions)
            routing = self._execute(
                stage="document-routing",
                document_id=document_id,
                source_path=source_path,
                source_hash=source_hash,
                scope_id="routing-sample",
                regions=routing_regions,
                manifest_payload={"total_pages": len(pages)},
                inputs={"document_content": _page_content(routing_regions)},
            )
            _append_stage_audit(audit, routing)
            route_payload = _require_payload(routing, DocumentRoutingPayload)
            if route_payload.route == "STOP_NEEDS_REVIEW":
                audit["status"] = "NEEDS_REVIEW"
                audit["review_recommendation"] = "NEEDS_REVIEW"
                self._verify_source_integrity(source_path, source_hash, lineage)
                audit["source_integrity"] = "VERIFIED"
                _write_audit(audit_path, audit)
                return DocumentExtractionResult(
                    document_id, result_source_hash, "NEEDS_REVIEW", audit_path, 0, 0, "NEEDS_REVIEW"
                )
            if route_payload.route == "EXTRACT_RESEARCH_EVIDENCE":
                claims = self._run_research_chain(
                    document_id,
                    source_path,
                    source_hash,
                    regions,
                    route_payload,
                    audit,
                )
                audit["status"] = "NEEDS_REVIEW"
                audit["review_recommendation"] = "NEEDS_REVIEW"
                audit["research_claim_count"] = claims
                self._verify_source_integrity(source_path, source_hash, lineage)
                audit["source_integrity"] = "VERIFIED"
                _write_audit(audit_path, audit)
                return DocumentExtractionResult(
                    document_id, result_source_hash, "NEEDS_REVIEW", audit_path, 0, 0, "NEEDS_REVIEW"
                )

            company_id, stock_code, report_period = self._trusted_financial_metadata(route_payload, routing_regions)
            selected_regions = _regions_for_sections(regions, route_payload.sections)
            layouts: list[LayoutRecoveryPayload] = []
            for window_no, window in enumerate(_chunked(selected_regions, self.page_window_size), 1):
                layout_result = self._execute(
                    stage="layout-recovery",
                    document_id=document_id,
                    source_path=source_path,
                    source_hash=source_hash,
                    scope_id=f"layout-{window_no:04d}",
                    regions=window,
                    manifest_payload={"total_pages": len(pages)},
                    inputs={
                        "confirmed_metadata": route_payload.model_dump(mode="json"),
                        "document_content": [region.to_dict() for region in window],
                        "ocr_tokens": [region.to_dict() for region in window if region.source_type == "ocr"],
                    },
                )
                _append_stage_audit(audit, layout_result)
                layout_payload = _require_payload(layout_result, LayoutRecoveryPayload)
                _validate_layout_evidence(layout_payload, window)
                layouts.append(layout_payload)

            all_fragments = [fragment for layout in layouts for fragment in layout.fragments]
            fragment_ids = [fragment.fragment_id for fragment in all_fragments]
            if len(fragment_ids) != len(set(fragment_ids)):
                raise DocumentExtractionError("layout stages produced duplicate fragment identities")

            assigned_candidates: list[AssignedFactCandidate] = []
            for fragment in all_fragments:
                fragment_region_ids = {cell.region_id for row in fragment.data_rows for cell in row.cells}
                fragment_regions = [region for region in regions if region.region_id in fragment_region_ids]
                if not fragment_regions:
                    continue
                p2_result = self._execute(
                    stage="financial-fact-extraction",
                    document_id=document_id,
                    source_path=source_path,
                    source_hash=source_hash,
                    scope_id=f"facts-{fragment.fragment_id}",
                    regions=fragment_regions,
                    manifest_payload={
                        "total_pages": len(pages),
                        "confirmed_company_id": company_id,
                        "confirmed_stock_code": stock_code,
                        "confirmed_report_period": report_period,
                        "confirmed_fragment": fragment.model_dump(mode="json"),
                    },
                    inputs={
                        "confirmed_metadata": route_payload.model_dump(mode="json"),
                        "table_fragment": fragment.model_dump(mode="json"),
                    },
                )
                _append_stage_audit(audit, p2_result)
                payload = _require_payload(p2_result, FinancialFactExtractionPayload)
                _validate_fact_fragment_binding(payload, fragment, fragment_regions, report_period)
                assigned_candidates.extend(assign_candidate_ids(payload, p2_result.manifest))

            candidate_ids = tuple(candidate.candidate_id for candidate in assigned_candidates)
            if len(candidate_ids) != len(set(candidate_ids)):
                raise DocumentExtractionError("duplicate server candidate identities were produced across fragments")
            candidate_data = [_assigned_candidate_dict(candidate) for candidate in assigned_candidates]
            cross_page = self._execute(
                stage="cross-page-reconciliation",
                document_id=document_id,
                source_path=source_path,
                source_hash=source_hash,
                scope_id="cross-page",
                regions=regions,
                allowed_candidate_ids=candidate_ids,
                manifest_payload={"total_pages": len(pages)},
                inputs={
                    "fragments": [fragment.model_dump(mode="json") for fragment in all_fragments],
                    "candidate_data": candidate_data,
                },
            )
            _append_stage_audit(audit, cross_page)
            cross_page_payload = _require_payload(cross_page, CrossPageReconciliationPayload)
            _validate_cross_page_closure(
                cross_page_payload,
                fragment_ids,
                candidate_ids,
            )

            normalization = self._execute(
                stage="fact-normalization-plan",
                document_id=document_id,
                source_path=source_path,
                source_hash=source_hash,
                scope_id="normalization",
                regions=regions,
                allowed_candidate_ids=candidate_ids,
                manifest_payload={"total_pages": len(pages)},
                inputs={
                    "conversion_rules": _conversion_rules_json(),
                    "confirmed_metadata": route_payload.model_dump(mode="json"),
                    "candidate_data": candidate_data,
                },
            )
            _append_stage_audit(audit, normalization)
            normalization_payload = _require_payload(normalization, FactNormalizationPayload)
            normalized, checks = _normalize_with_checks(assigned_candidates, normalization_payload)
            checks.extend(_financial_checks(normalized))
            audit["normalization_results"] = [_normalized_candidate_dict(item) for item in normalized]
            audit["deterministic_checks"] = checks

            validation = self._execute(
                stage="validation-explanation",
                document_id=document_id,
                source_path=source_path,
                source_hash=source_hash,
                scope_id="validation",
                regions=regions,
                allowed_candidate_ids=candidate_ids,
                manifest_payload={"total_pages": len(pages)},
                inputs={"candidate_data": audit["normalization_results"], "check_results": checks},
            )
            _append_stage_audit(audit, validation)
            validation_payload = _require_payload(validation, ValidationExplanationPayload)
            _validate_validation_closure(validation_payload, checks)

            arbitration = self._execute(
                stage="candidate-arbitration",
                document_id=document_id,
                source_path=source_path,
                source_hash=source_hash,
                scope_id="arbitration",
                regions=regions,
                allowed_candidate_ids=candidate_ids,
                manifest_payload={"total_pages": len(pages)},
                inputs={
                    "candidate_groups": _candidate_groups(assigned_candidates),
                    "check_results": checks,
                    "source_evidence": [region.to_dict() for region in regions],
                },
            )
            _append_stage_audit(audit, arbitration)
            arbitration_payload = _require_payload(arbitration, CandidateArbitrationPayload)
            _validate_arbitration_closure(arbitration_payload, _candidate_groups(assigned_candidates))

            review = self._execute(
                stage="review-task-generation",
                document_id=document_id,
                source_path=source_path,
                source_hash=source_hash,
                scope_id="review",
                regions=regions,
                allowed_candidate_ids=candidate_ids,
                manifest_payload={"total_pages": len(pages)},
                inputs={
                    "candidate_data": {
                        "candidates": candidate_data,
                        "normalization_results": audit["normalization_results"],
                        "arbitration": arbitration_payload.model_dump(mode="json"),
                    },
                    "schema_results": {"status": "passed"},
                    "check_results": checks,
                },
            )
            _append_stage_audit(audit, review)
            review_payload = _require_payload(review, ReviewTaskGenerationPayload)
            _validate_review_closure(review_payload, candidate_ids, normalized)
            audit["status"] = review_payload.document_recommendation
            audit["review_recommendation"] = review_payload.document_recommendation
            audit["candidate_count"] = len(assigned_candidates)
            audit["normalized_count"] = len(normalized)
            self._verify_source_integrity(source_path, source_hash, lineage)
            audit["source_integrity"] = "VERIFIED"
            _write_audit(audit_path, audit)
            return DocumentExtractionResult(
                document_id=document_id,
                source_content_sha256=result_source_hash,
                status=review_payload.document_recommendation,
                audit_path=audit_path,
                candidate_count=len(assigned_candidates),
                normalized_count=len(normalized),
                review_recommendation=review_payload.document_recommendation,
            )
        except Exception as exc:
            audit["status"] = "FAILED"
            audit["error"] = {"type": type(exc).__name__, "message": str(exc)}
            try:
                self._verify_source_integrity(source_path, source_hash, lineage)
                audit["source_integrity"] = "VERIFIED"
            except (DocumentExtractionError, FileNotFoundError, OSError) as integrity_exc:
                audit["source_integrity"] = "FAILED"
                audit["source_integrity_error"] = {
                    "type": type(integrity_exc).__name__,
                    "message": str(integrity_exc),
                }
            _write_audit(audit_path, audit)
            raise

    @staticmethod
    def _verify_source_integrity(
        source_path: Path,
        source_hash: str,
        lineage: DocumentLineage | None,
    ) -> None:
        if sha256_file(source_path) != source_hash:
            raise DocumentExtractionError("PDF source changed during LLM extraction")
        if lineage is None:
            return
        original_path = lineage.rendered_pdf.source_path.resolve(strict=True)
        if sha256_file(original_path) != lineage.source_content_sha256:
            raise DocumentExtractionError("DOCX source changed during LLM extraction")
        if lineage.rendered_pdf.pdf_path.resolve(strict=True) != source_path:
            raise DocumentExtractionError("rendered PDF path changed during LLM extraction")

    @staticmethod
    def _validate_rendered_pdf(
        source_path: Path,
        inspection: DocxInspection,
        rendered_pdf: RenderedPdf,
    ) -> None:
        if rendered_pdf.source_path.resolve(strict=True) != source_path:
            raise DocumentExtractionError("DOCX renderer returned lineage for a different source file")
        if rendered_pdf.source_sha256 != inspection.source_sha256:
            raise DocumentExtractionError("DOCX renderer source hash differs from the inspected document")
        if sha256_file(source_path) != inspection.source_sha256:
            raise DocumentExtractionError("DOCX source changed after security inspection")
        pdf_path = rendered_pdf.pdf_path.resolve(strict=True)
        if not pdf_path.is_file() or pdf_path.suffix.casefold() != ".pdf":
            raise DocumentExtractionError("DOCX renderer did not return a PDF file")
        if sha256_file(pdf_path) != rendered_pdf.pdf_sha256:
            raise DocumentExtractionError("rendered PDF hash does not match renderer lineage")
        if rendered_pdf.page_count < 1:
            raise DocumentExtractionError("rendered PDF lineage has no pages")
        try:
            actual_page_count = len(PdfReader(str(pdf_path)).pages)
        except (OSError, PyPdfError, TypeError, ValueError) as exc:
            raise DocumentExtractionError("rendered PDF cannot be independently parsed") from exc
        if actual_page_count != rendered_pdf.page_count:
            raise DocumentExtractionError("rendered PDF page count does not match renderer lineage")
        if not rendered_pdf.renderer_name.strip() or not rendered_pdf.renderer_version.strip():
            raise DocumentExtractionError("DOCX renderer identity is incomplete")
        if re.fullmatch(r"[0-9a-f]{64}", rendered_pdf.renderer_binary_sha256) is None:
            raise DocumentExtractionError("DOCX renderer binary hash is invalid")
        if tuple(rendered_pdf.review_codes) != inspection.review_codes:
            raise DocumentExtractionError("DOCX renderer review codes differ from security inspection")

    def _execute(
        self,
        *,
        stage: str,
        document_id: str,
        source_path: Path,
        source_hash: str,
        scope_id: str,
        regions: Sequence[SourceRegion],
        manifest_payload: Mapping[str, Any],
        inputs: Mapping[str, Any],
        allowed_candidate_ids: Sequence[str] = (),
    ) -> StageExecutionResult:
        definition = get_prompt(stage)
        self._request_sequence += 1
        evidence_regions = {region.region_id: region.text for region in regions}
        manifest = RequestManifest(
            request_id=f"{document_id}-{self._request_sequence:06d}",
            document_id=document_id,
            source_file_name=source_path.name,
            source_content_sha256=source_hash,
            stage=definition.prompt_id,
            prompt_version=definition.version,
            input_scope_id=scope_id,
            allowed_region_ids=list(evidence_regions),
            allowed_candidate_ids=list(allowed_candidate_ids),
            extractor_version=self.extractor_version,
            input_locator_type="pdf_page",
            metric_catalog_version=self.executor.metric_catalog.catalog_version,
            payload={**manifest_payload, "evidence_regions": evidence_regions},
        )
        return self.executor.execute(manifest, inputs)

    def _trusted_financial_metadata(
        self,
        payload: DocumentRoutingPayload,
        regions: Sequence[SourceRegion],
    ) -> tuple[str, str, str]:
        company = payload.selected_company
        report_period = payload.selected_report_period
        if company is None or report_period is None:
            raise DocumentExtractionError("financial extraction requires selected company and report period")
        company_evidence = [
            candidate
            for candidate in payload.company_candidates
            if candidate.stock_code == company.stock_code and candidate.company_name == company.company_name
        ]
        if not company_evidence:
            raise DocumentExtractionError("selected company is not backed by a matching routing candidate")
        if not any(
            _located_evidence_matches(
                candidate.locator,
                candidate.evidence_text,
                regions,
                required_text=(company.stock_code, company.company_name),
            )
            for candidate in company_evidence
        ):
            raise DocumentExtractionError("selected company evidence is not present at its claimed PDF page")

        period_evidence = [
            candidate for candidate in payload.report_period_candidates if candidate.report_period == report_period
        ]
        if not period_evidence:
            raise DocumentExtractionError("selected report period is not backed by a matching routing candidate")
        if not any(
            _located_evidence_matches(
                candidate.locator,
                candidate.evidence_text,
                regions,
                required_text=(report_period[:4],),
            )
            for candidate in period_evidence
        ):
            raise DocumentExtractionError("selected report period evidence is not present at its claimed PDF page")
        if self.company_index is not None:
            known_name = self.company_index.code_to_abbr.get(company.stock_code)
            if known_name is None:
                raise DocumentExtractionError("selected stock code is absent from the company master")
            same_evidence_links_names = any(
                known_name in candidate.evidence_text and company.company_name in candidate.evidence_text
                for candidate in company_evidence
            )
            if not _company_names_compatible(known_name, company.company_name) and not same_evidence_links_names:
                raise DocumentExtractionError("selected company name conflicts with the company master")
        return company.stock_code, company.stock_code, report_period

    def _run_research_chain(
        self,
        document_id: str,
        source_path: Path,
        source_hash: str,
        regions: Sequence[SourceRegion],
        route_payload: DocumentRoutingPayload,
        audit: dict[str, Any],
    ) -> int:
        claim_count = 0
        for window_no, window in enumerate(_chunked(regions, self.page_window_size), 1):
            result = self._execute(
                stage="research-evidence-extraction",
                document_id=document_id,
                source_path=source_path,
                source_hash=source_hash,
                scope_id=f"research-{window_no:04d}",
                regions=window,
                manifest_payload={"total_pages": max(region.page_no for region in regions)},
                inputs={
                    "confirmed_metadata": route_payload.model_dump(mode="json"),
                    "document_content": [region.to_dict() for region in window],
                },
            )
            _append_stage_audit(audit, result)
            claim_count += len(_require_payload(result, ResearchEvidenceExtractionPayload).claims)
        return claim_count


def _build_source_regions(pages: Sequence[PageText]) -> tuple[SourceRegion, ...]:
    regions: list[SourceRegion] = []
    for page in pages:
        text_layer = page.text_layer_text if page.source_type == "mixed" else page.text
        if page.source_type != "ocr":
            _append_text_regions(regions, page.page_no, text_layer or "")
        if page.ocr_tokens:
            for line_no, token in enumerate(page.ocr_tokens, 1):
                regions.append(
                    SourceRegion(
                        token.token_id,
                        page.page_no,
                        line_no,
                        token.text,
                        source_type="ocr",
                        bbox=token.bbox,
                        confidence=token.confidence,
                        page_image_sha256=token.page_image_sha256,
                    )
                )
    return tuple(regions)


def _append_text_regions(regions: list[SourceRegion], page_no: int, page_text: str) -> None:
    line_no = 0
    for raw_line in page_text.splitlines():
        text = raw_line.strip()
        if not text:
            continue
        line_no += 1
        for part_no, start in enumerate(range(0, len(text), MAX_REGION_TEXT_LENGTH), 1):
            part = text[start : start + MAX_REGION_TEXT_LENGTH]
            suffix = f"-part{part_no}" if len(text) > MAX_REGION_TEXT_LENGTH else ""
            regions.append(SourceRegion(f"p{page_no}-line{line_no}{suffix}", page_no, line_no, part))


def _ocr_audit_summary(pages: Sequence[PageText]) -> dict[str, Any]:
    engines = sorted({(page.ocr_engine or "unknown", page.ocr_engine_version or "unknown") for page in pages})
    return {
        "used": bool(pages),
        "engines": [{"name": name, "version": engine_version} for name, engine_version in engines],
        "physical_pages": [page.page_no for page in pages],
        "token_count": sum(len(page.ocr_tokens) for page in pages),
        "empty_result_pages": [page.page_no for page in pages if not page.ocr_tokens],
        "page_images": [
            {"page_no": page.page_no, "sha256": page.page_image_sha256}
            for page in pages
            if page.page_image_sha256 is not None
        ],
    }


def _routing_sample_regions(regions: Sequence[SourceRegion]) -> tuple[SourceRegion, ...]:
    page_numbers = sorted({region.page_no for region in regions})
    selected_pages = set(page_numbers[:3] + page_numbers[-2:])
    statement_pages = {
        region.page_no for region in regions if FINANCIAL_STATEMENT_PAGE_PATTERN.search(region.text) is not None
    }
    available_pages = set(page_numbers)
    for page_no in statement_pages:
        selected_pages.update(page for page in range(page_no - 1, page_no + 3) if page in available_pages)
    return tuple(region for region in regions if region.page_no in selected_pages)


def _page_content(regions: Sequence[SourceRegion]) -> list[dict[str, Any]]:
    by_page: dict[int, list[str]] = defaultdict(list)
    for region in regions:
        by_page[region.page_no].append(region.text)
    return [
        {"locator": f"pdf:p{page_no}", "page_no": page_no, "text": "\n".join(lines)}
        for page_no, lines in sorted(by_page.items())
    ]


def _regions_for_sections(regions: Sequence[SourceRegion], sections: Sequence[Any]) -> tuple[SourceRegion, ...]:
    available_pages = {region.page_no for region in regions}
    page_ranges: list[tuple[int, int]] = []
    for section in sections:
        if getattr(section, "kind", None) != "financial_statements":
            continue
        start = _physical_page(getattr(section, "start_locator", ""))
        end = _physical_page(getattr(section, "end_locator", ""))
        if start is None or end is None or start > end:
            raise DocumentExtractionError("financial statement section has invalid PDF page locators")
        if start not in available_pages or end not in available_pages:
            raise DocumentExtractionError("financial statement section is outside the PDF page range")
        page_ranges.append((start, end))
    if not page_ranges:
        raise DocumentExtractionError("financial extraction requires a located financial statement section")
    selected = tuple(region for region in regions if any(start <= region.page_no <= end for start, end in page_ranges))
    if not selected:
        raise DocumentExtractionError("financial statement section contains no readable source regions")
    return selected


def _physical_page(locator: str) -> int | None:
    match = PDF_LOCATOR_PATTERN.fullmatch(locator)
    return int(match.group("page")) if match else None


def _located_evidence_matches(
    locator: str,
    evidence_text: str,
    regions: Sequence[SourceRegion],
    *,
    required_text: Sequence[str],
) -> bool:
    page_no = _physical_page(locator)
    if page_no is None or any(value not in evidence_text for value in required_text):
        return False
    page_text = "\n".join(region.text for region in regions if region.page_no == page_no)
    normalized_page = re.sub(r"\s+", " ", page_text).strip()
    normalized_evidence = re.sub(r"\s+", " ", evidence_text).strip()
    return bool(normalized_page) and normalized_evidence in normalized_page


def _company_names_compatible(master_name: str, selected_name: str) -> bool:
    master = re.sub(r"\s+", "", master_name).casefold()
    selected = re.sub(r"\s+", "", selected_name).casefold()
    if master == selected:
        return True
    return min(len(master), len(selected)) >= 4 and (master in selected or selected in master)


def _validate_layout_evidence(payload: LayoutRecoveryPayload, regions: Sequence[SourceRegion]) -> None:
    region_by_id = {region.region_id: region for region in regions}
    allowed_pages = {region.page_no for region in regions}
    for fragment in payload.fragments:
        if fragment.locator_type != "pdf_page":
            raise DocumentExtractionError("PDF layout stage returned a non-PDF fragment")
        fragment_pages = set(fragment.physical_page_numbers)
        if not fragment_pages or not fragment_pages <= allowed_pages:
            raise DocumentExtractionError("layout fragment claims pages outside its request window")
        start_page = _physical_page(fragment.start_locator)
        end_page = _physical_page(fragment.end_locator)
        if start_page is None or end_page is None or start_page > end_page:
            raise DocumentExtractionError("layout fragment has invalid physical page locators")
        if start_page not in fragment_pages or end_page not in fragment_pages:
            raise DocumentExtractionError("layout fragment locators conflict with physical_page_numbers")

        header_indexes = [row.row_index for row in fragment.header_rows]
        if len(header_indexes) != len(set(header_indexes)):
            raise DocumentExtractionError("layout fragment contains duplicate header row indices")
        available_text = tuple(region.text for region in regions if region.page_no in fragment_pages)
        for header in fragment.header_rows:
            for cell_text in header.cells:
                if cell_text.strip() and not any(cell_text in text for text in available_text):
                    raise DocumentExtractionError("layout header text is absent from immutable PDF regions")

        column_indexes = [column.column_index for column in fragment.data_columns]
        if len(column_indexes) != len(set(column_indexes)):
            raise DocumentExtractionError("layout fragment contains duplicate data column indices")
        header_cells = [cell for row in fragment.header_rows for cell in row.cells]
        for column in fragment.data_columns:
            if not any(column.column_label in cell or cell in column.column_label for cell in header_cells if cell):
                raise DocumentExtractionError("layout data column is not backed by recovered header text")

        row_indexes = [row.row_index for row in fragment.data_rows]
        if len(row_indexes) != len(set(row_indexes)):
            raise DocumentExtractionError("layout fragment contains duplicate data row indices")
        if fragment.data_rows and (not fragment.header_rows or not fragment.data_columns):
            raise DocumentExtractionError("layout data rows require explicit header and data-column evidence")
        for row in fragment.data_rows:
            if (
                fragment.row_range is not None
                and not fragment.row_range.start <= row.row_index <= fragment.row_range.end
            ):
                raise DocumentExtractionError("layout row index is outside the declared row range")
            cell_columns = [cell.column_index for cell in row.cells]
            if len(cell_columns) != len(set(cell_columns)):
                raise DocumentExtractionError("layout data row contains duplicate column indices")
            for cell in row.cells:
                region = region_by_id[cell.region_id]
                if region.page_no not in fragment_pages:
                    raise DocumentExtractionError("layout cell region is outside fragment physical pages")
                if cell.value_kind == "data_value" and cell.column_index not in set(column_indexes):
                    raise DocumentExtractionError("layout value cell has no matching data-column definition")
                _validate_immutable_bbox(cell.bbox, region.bbox, label="layout cell")


def _validate_fact_fragment_binding(
    payload: FinancialFactExtractionPayload,
    fragment: TableFragment,
    regions: Sequence[SourceRegion],
    report_period: str,
) -> None:
    if payload.fragment_id != fragment.fragment_id:
        raise DocumentExtractionError("fact payload fragment_id differs from the requested layout fragment")
    region_by_id = {region.region_id: region for region in regions}
    rows = {row.row_index: row for row in fragment.data_rows}
    columns = {column.column_index: column for column in fragment.data_columns}
    for fact in payload.facts:
        if fact.statement_table != fragment.statement_table:
            raise DocumentExtractionError("fact statement table differs from its layout fragment")
        if fact.statement_scope != fragment.statement_scope:
            raise DocumentExtractionError("fact statement scope differs from its layout fragment")
        if fact.source_unit != fragment.source_unit or fact.currency != fragment.currency:
            raise DocumentExtractionError("fact unit or currency differs from its layout fragment")
        source = fact.source
        if source.locator_type != fragment.locator_type:
            raise DocumentExtractionError("fact locator type differs from its layout fragment")
        if source.table_name != fragment.table_name or source.table_index != fragment.table_index:
            raise DocumentExtractionError("fact table identity differs from its layout fragment")
        if source.row_index is None or source.column_index is None:
            raise DocumentExtractionError("PDF fact requires explicit row and column indices")
        row = rows.get(source.row_index)
        column = columns.get(source.column_index)
        if row is None or column is None:
            raise DocumentExtractionError("fact row or column is absent from its layout fragment")
        cells = [
            cell
            for cell in row.cells
            if cell.column_index == source.column_index and cell.region_id == source.region_id
        ]
        if len(cells) != 1 or cells[0].value_kind != "data_value" or fact.raw_value not in cells[0].raw_text:
            raise DocumentExtractionError("fact value is not bound to one recovered data cell")
        if fact.row_path != row.row_path:
            raise DocumentExtractionError("fact row path differs from its recovered layout row")
        if row.row_path and source.row_label not in row.row_path:
            raise DocumentExtractionError("fact row label differs from its recovered layout row")
        if source.column_label != column.column_label:
            raise DocumentExtractionError("fact column label differs from its recovered data column")
        if column.column_role not in {"current_period", "closing_balance"}:
            raise DocumentExtractionError("comparison or unknown columns cannot become current-period facts")
        if fact.period_semantics.column_role != column.column_role:
            raise DocumentExtractionError("fact period semantics differ from its recovered data column")
        if column.period_hint is not None and column.period_hint != report_period:
            raise DocumentExtractionError("fact data column period differs from the confirmed report period")
        region = region_by_id.get(source.region_id)
        if region is None or source.page_no != region.page_no:
            raise DocumentExtractionError("fact page number differs from its immutable source region")
        _validate_immutable_bbox(source.bbox, region.bbox, label="fact source")
        if source.page_no not in fragment.physical_page_numbers:
            raise DocumentExtractionError("fact page number is outside its layout fragment")
        if source.evidence_text not in region.text:
            raise DocumentExtractionError("fact evidence text is absent from its immutable source region")


def _validate_immutable_bbox(
    actual_bbox: Any, expected_bbox: tuple[float, float, float, float] | None, *, label: str
) -> None:
    if expected_bbox is None:
        if actual_bbox is not None:
            raise DocumentExtractionError(f"{label} invented a bbox for text-only evidence")
        return
    if actual_bbox is None:
        raise DocumentExtractionError(f"OCR-backed {label} omitted its immutable bbox")
    actual = (
        actual_bbox.x,
        actual_bbox.y,
        actual_bbox.x + actual_bbox.width,
        actual_bbox.y + actual_bbox.height,
    )
    if any(abs(left - right) > 0.000002 for left, right in zip(actual, expected_bbox, strict=True)):
        raise DocumentExtractionError(f"{label} bbox differs from immutable OCR evidence")


def _validate_cross_page_closure(
    payload: CrossPageReconciliationPayload,
    fragment_ids: Sequence[str],
    candidate_ids: Sequence[str],
) -> None:
    _require_exact_partition(
        [fragment_id for group in payload.fragment_groups for fragment_id in group.fragment_ids],
        fragment_ids,
        "cross-page fragment groups",
    )
    _require_exact_partition(
        [candidate_id for item in payload.candidate_assessments for candidate_id in item.candidate_ids],
        candidate_ids,
        "cross-page candidate assessments",
    )


def _validate_validation_closure(
    payload: ValidationExplanationPayload,
    checks: Sequence[Mapping[str, Any]],
) -> None:
    _require_exact_partition(
        [item.check_id for item in payload.checks],
        [str(check["check_id"]) for check in checks],
        "validation explanations",
    )


def _validate_arbitration_closure(
    payload: CandidateArbitrationPayload,
    expected_groups: Sequence[Mapping[str, Any]],
) -> None:
    expected = {
        tuple(sorted(group["business_key"].items())): {candidate["candidate_id"] for candidate in group["candidates"]}
        for group in expected_groups
    }
    actual: dict[tuple[tuple[str, Any], ...], set[str]] = {}
    for group in payload.groups:
        key = tuple(sorted(group.business_key.model_dump(mode="json").items()))
        if key in actual or len(group.candidate_ids) != len(set(group.candidate_ids)):
            raise DocumentExtractionError("candidate arbitration contains duplicate groups or candidate IDs")
        actual[key] = set(group.candidate_ids)
    if actual != expected:
        raise DocumentExtractionError("candidate arbitration does not exactly cover the server candidate groups")


def _validate_review_closure(
    payload: ReviewTaskGenerationPayload,
    candidate_ids: Sequence[str],
    normalized: Sequence[NormalizedFactCandidate],
) -> None:
    recommendation_ids = [item.candidate_id for item in payload.facts]
    _require_exact_partition(recommendation_ids, candidate_ids, "review recommendations")
    normalized_ids = {item.candidate_id for item in normalized}
    invalid_persistence = [
        item.candidate_id
        for item in payload.facts
        if item.persistence_status == "NEEDS_REVIEW" and item.candidate_id not in normalized_ids
    ]
    if invalid_persistence:
        raise DocumentExtractionError(
            "review stage marked non-normalized candidates as persistable: " + ", ".join(sorted(invalid_persistence))
        )


def _require_exact_partition(actual: Sequence[str], expected: Sequence[str], label: str) -> None:
    if len(actual) != len(set(actual)) or len(expected) != len(set(expected)):
        raise DocumentExtractionError(f"{label} contains duplicate identifiers")
    if set(actual) != set(expected):
        raise DocumentExtractionError(f"{label} does not exactly cover the server-owned identifiers")


def _chunked(regions: Sequence[SourceRegion], page_window_size: int) -> Iterable[tuple[SourceRegion, ...]]:
    by_page: dict[int, list[SourceRegion]] = defaultdict(list)
    for region in regions:
        by_page[region.page_no].append(region)
    pages = sorted(by_page)
    for offset in range(0, len(pages), page_window_size):
        page_window = pages[offset : offset + page_window_size]
        yield tuple(region for page in page_window for region in by_page[page])


def _assigned_candidate_dict(candidate: AssignedFactCandidate) -> dict[str, Any]:
    return {"candidate_id": candidate.candidate_id, **candidate.fact.model_dump(mode="json")}


def _candidate_groups(candidates: Sequence[AssignedFactCandidate]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[AssignedFactCandidate]] = defaultdict(list)
    for candidate in candidates:
        fact = candidate.fact
        if None in (fact.stock_code, fact.report_period, fact.statement_scope, fact.period_type):
            continue
        key = (fact.stock_code, fact.report_period, fact.statement_scope, fact.period_type, fact.metric)
        groups[key].append(candidate)
    return [
        {
            "business_key": {
                "stock_code": key[0],
                "report_period": key[1],
                "statement_scope": key[2],
                "period_type": key[3],
                "metric": key[4],
            },
            "candidates": [_assigned_candidate_dict(item) for item in values],
        }
        for key, values in sorted(groups.items(), key=lambda item: tuple(str(part) for part in item[0]))
    ]


def _conversion_rules_json() -> list[dict[str, str]]:
    return [
        {
            "rule_id": rule.rule_id,
            "source_unit": rule.source_unit,
            "target_unit": rule.target_unit,
            "operation": "multiply",
            "factor": str(rule.factor),
        }
        for rule in DEFAULT_CONVERSION_RULES.values()
    ]


def _normalize_with_checks(
    candidates: Sequence[AssignedFactCandidate],
    payload: FactNormalizationPayload,
) -> tuple[list[NormalizedFactCandidate], list[dict[str, Any]]]:
    plan_by_id = {plan.candidate_id: plan for plan in payload.facts}
    if len(plan_by_id) != len(payload.facts):
        raise DocumentExtractionError("normalization response contains duplicate candidate IDs")
    candidate_ids = {candidate.candidate_id for candidate in candidates}
    if set(plan_by_id) != candidate_ids:
        raise DocumentExtractionError("normalization response does not exactly cover the server candidates")
    normalized: list[NormalizedFactCandidate] = []
    checks: list[dict[str, Any]] = []
    for candidate in candidates:
        plan = plan_by_id.get(candidate.candidate_id)
        if plan is None:
            checks.append(
                {
                    "check_id": f"normalization-{candidate.candidate_id}",
                    "code": "DETERMINISTIC_CHECK_UNAVAILABLE",
                    "status": "unavailable",
                    "candidate_ids": [candidate.candidate_id],
                    "details": {"reason": "model returned no normalization plan"},
                }
            )
            continue
        try:
            item = normalize_candidates([candidate], [plan])[0]
        except NormalizationError as exc:
            checks.append(
                {
                    "check_id": f"normalization-{candidate.candidate_id}",
                    "code": "VALUE_CONFLICT",
                    "status": "failed",
                    "candidate_ids": [candidate.candidate_id],
                    "details": {"reason": str(exc)},
                }
            )
            continue
        normalized.append(item)
        checks.append(
            {
                "check_id": f"normalization-{candidate.candidate_id}",
                "code": "NORMALIZATION_RECOMPUTED",
                "status": "passed",
                "candidate_ids": [candidate.candidate_id],
                "details": {
                    "normalized_value": str(item.normalized_value),
                    "rule_id": item.conversion_rule_id,
                },
            }
        )
    return normalized, checks


def _financial_checks(candidates: Sequence[NormalizedFactCandidate]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str | None, str | None, str | None], list[NormalizedFactCandidate]] = defaultdict(list)
    for candidate in candidates:
        fact = candidate.fact
        grouped[(fact.stock_code, fact.report_period, fact.statement_scope)].append(candidate)

    checks: list[dict[str, Any]] = []
    for business_scope, scoped_candidates in grouped.items():
        metrics: dict[str, list[NormalizedFactCandidate]] = defaultdict(list)
        for candidate in scoped_candidates:
            metrics[candidate.fact.metric].append(candidate)
        conflicts = {metric: values for metric, values in metrics.items() if len(values) > 1}
        for metric, values in sorted(conflicts.items()):
            checks.append(
                _check_result(
                    check_id=f"unique-{_scope_key(business_scope)}-{metric}",
                    code="VALUE_CONFLICT",
                    status="failed",
                    candidates=values,
                    details={"reason": "multiple normalized candidates share one business key"},
                )
            )

        single = {metric: values[0] for metric, values in metrics.items() if len(values) == 1}
        _append_equation_check(
            checks,
            business_scope,
            single,
            check_name="balance-equation",
            code="BALANCE_EQUATION_FAILED",
            left_metric="asset_total_assets",
            right_metrics=("liability_total_liabilities", "equity_total_equity"),
            absolute_tolerance=Decimal("1"),
            relative_tolerance=Decimal("0.005"),
        )
        _append_ratio_check(
            checks,
            business_scope,
            single,
            check_name="asset-liability-ratio",
            result_metric="asset_liability_ratio",
            numerator_metric="liability_total_liabilities",
            denominator_metric="asset_total_assets",
        )
        _append_ratio_check(
            checks,
            business_scope,
            single,
            check_name="net-profit-margin",
            result_metric="net_profit_margin",
            numerator_metric="net_profit",
            denominator_metric="total_operating_revenue",
        )
        _append_gross_margin_check(checks, business_scope, single)
    return checks


def _append_equation_check(
    checks: list[dict[str, Any]],
    business_scope: tuple[str | None, str | None, str | None],
    metrics: Mapping[str, NormalizedFactCandidate],
    *,
    check_name: str,
    code: str,
    left_metric: str,
    right_metrics: tuple[str, ...],
    absolute_tolerance: Decimal,
    relative_tolerance: Decimal,
) -> None:
    names = (left_metric, *right_metrics)
    present = [metrics[name] for name in names if name in metrics]
    if not present:
        return
    if len(present) != len(names):
        checks.append(
            _check_result(
                check_id=f"{check_name}-{_scope_key(business_scope)}",
                code="DETERMINISTIC_CHECK_UNAVAILABLE",
                status="unavailable",
                candidates=present,
                details={"missing_metrics": [name for name in names if name not in metrics]},
            )
        )
        return
    left = metrics[left_metric].normalized_value
    right = sum((metrics[name].normalized_value for name in right_metrics), Decimal("0"))
    difference = left - right
    tolerance = max(absolute_tolerance, relative_tolerance * max(abs(left), abs(right)))
    checks.append(
        _check_result(
            check_id=f"{check_name}-{_scope_key(business_scope)}",
            code=code if abs(difference) > tolerance else "BALANCE_EQUATION_MATCHED",
            status="failed" if abs(difference) > tolerance else "passed",
            candidates=[metrics[name] for name in names],
            details={
                "left": str(left),
                "right": str(right),
                "difference": str(difference),
                "tolerance": str(tolerance),
            },
        )
    )


def _append_ratio_check(
    checks: list[dict[str, Any]],
    business_scope: tuple[str | None, str | None, str | None],
    metrics: Mapping[str, NormalizedFactCandidate],
    *,
    check_name: str,
    result_metric: str,
    numerator_metric: str,
    denominator_metric: str,
) -> None:
    names = (result_metric, numerator_metric, denominator_metric)
    present = [metrics[name] for name in names if name in metrics]
    if not present:
        return
    if len(present) != len(names) or metrics[denominator_metric].normalized_value == 0:
        details: dict[str, Any] = {"missing_metrics": [name for name in names if name not in metrics]}
        if denominator_metric in metrics and metrics[denominator_metric].normalized_value == 0:
            details["reason"] = "denominator is zero"
        checks.append(
            _check_result(
                check_id=f"{check_name}-{_scope_key(business_scope)}",
                code="DETERMINISTIC_CHECK_UNAVAILABLE",
                status="unavailable",
                candidates=present,
                details=details,
            )
        )
        return
    reported = metrics[result_metric].normalized_value
    recomputed = metrics[numerator_metric].normalized_value / abs(metrics[denominator_metric].normalized_value) * 100
    difference = reported - recomputed
    tolerance = Decimal("0.1")
    checks.append(
        _check_result(
            check_id=f"{check_name}-{_scope_key(business_scope)}",
            code="REPORTED_GROWTH_MISMATCH" if abs(difference) > tolerance else "REPORTED_RATIO_MATCHED",
            status="failed" if abs(difference) > tolerance else "passed",
            candidates=[metrics[name] for name in names],
            details={
                "reported": str(reported),
                "recomputed": str(recomputed),
                "difference": str(difference),
                "tolerance_percentage_points": str(tolerance),
            },
        )
    )


def _append_gross_margin_check(
    checks: list[dict[str, Any]],
    business_scope: tuple[str | None, str | None, str | None],
    metrics: Mapping[str, NormalizedFactCandidate],
) -> None:
    names = ("gross_profit_margin", "total_operating_revenue", "operating_expense_cost_of_sales")
    present = [metrics[name] for name in names if name in metrics]
    if not present:
        return
    revenue = metrics.get("total_operating_revenue")
    if len(present) != len(names) or revenue is None or revenue.normalized_value == 0:
        checks.append(
            _check_result(
                check_id=f"gross-profit-margin-{_scope_key(business_scope)}",
                code="DETERMINISTIC_CHECK_UNAVAILABLE",
                status="unavailable",
                candidates=present,
                details={"missing_metrics": [name for name in names if name not in metrics]},
            )
        )
        return
    reported = metrics["gross_profit_margin"].normalized_value
    cost = metrics["operating_expense_cost_of_sales"].normalized_value
    recomputed = (revenue.normalized_value - cost) / abs(revenue.normalized_value) * 100
    difference = reported - recomputed
    tolerance = Decimal("0.1")
    checks.append(
        _check_result(
            check_id=f"gross-profit-margin-{_scope_key(business_scope)}",
            code="REPORTED_GROWTH_MISMATCH" if abs(difference) > tolerance else "REPORTED_RATIO_MATCHED",
            status="failed" if abs(difference) > tolerance else "passed",
            candidates=[metrics[name] for name in names],
            details={
                "reported": str(reported),
                "recomputed": str(recomputed),
                "difference": str(difference),
                "tolerance_percentage_points": str(tolerance),
            },
        )
    )


def _check_result(
    *,
    check_id: str,
    code: str,
    status: str,
    candidates: Sequence[NormalizedFactCandidate],
    details: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "code": code,
        "status": status,
        "candidate_ids": [candidate.candidate_id for candidate in candidates],
        "details": dict(details),
    }


def _scope_key(scope: tuple[str | None, str | None, str | None]) -> str:
    return "-".join(value or "unknown" for value in scope)


def _normalized_candidate_dict(candidate: NormalizedFactCandidate) -> dict[str, Any]:
    return {
        "candidate_id": candidate.candidate_id,
        "normalized_value": str(candidate.normalized_value),
        "conversion_rule_id": candidate.conversion_rule_id,
        "fact": candidate.fact.model_dump(mode="json"),
    }


def _document_lineage_dict(lineage: DocumentLineage) -> dict[str, Any]:
    rendered = lineage.rendered_pdf
    return {
        "source_document": {
            "file_name": lineage.source_file_name,
            "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "sha256": lineage.source_content_sha256,
        },
        "rendered_pdf": {
            "file_name": rendered.pdf_path.name,
            "mime_type": "application/pdf",
            "sha256": rendered.pdf_sha256,
            "page_count": rendered.page_count,
        },
        "renderer": {
            "name": rendered.renderer_name,
            "version": rendered.renderer_version,
            "binary_sha256": rendered.renderer_binary_sha256,
        },
        "review_codes": list(rendered.review_codes),
    }


def _append_stage_audit(audit: dict[str, Any], result: StageExecutionResult) -> None:
    audit["stages"].append(
        {
            "stage": result.manifest.stage,
            "prompt_version": result.manifest.prompt_version,
            "request_id": result.manifest.request_id,
            "input_scope_id": result.manifest.input_scope_id,
            "request_manifest": result.manifest.model_dump(mode="json"),
            "response_id": result.response.response_id,
            "model": result.response.model,
            "usage": result.response.usage,
            "latency_ms": result.response.latency_ms,
            "finish_reason": result.response.finish_reason,
            "raw_response": result.response.raw_json,
            "repair_response_id": result.repair_response.response_id if result.repair_response else None,
            "repair_response": result.repair_response.raw_json if result.repair_response else None,
            "envelope": result.envelope.model_dump(mode="json"),
        }
    )


def _require_payload(result: StageExecutionResult, expected_type: type[Any]) -> Any:
    payload = result.envelope.payload
    if not isinstance(payload, expected_type):
        raise TypeError(f"stage {result.manifest.stage} returned unexpected payload type {type(payload).__name__}")
    return payload


def _write_audit(path: Path, audit: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
