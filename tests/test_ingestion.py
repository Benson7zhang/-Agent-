from pathlib import Path

import pytest

from smart_finqa.config import OCRConfig
from smart_finqa.core import MetadataRecognitionError
from smart_finqa.ingestion import (
    CompanyIndex,
    PageText,
    PdfPageExtractor,
    ReportFileMetadata,
    ReportRevisionKind,
    _infer_stock_abbr,
    _infer_stock_code,
    classify_report_file_name,
    extract_pdf_text,
    extract_report_record,
    inspect_report_file_metadata,
    select_authoritative_reports,
)
from smart_finqa.ocr import OCRPage, OCRToken
from tests.helpers import write_company_workbook


def test_infer_company_from_file_name_and_workbook(tmp_path) -> None:
    company_index = CompanyIndex.from_xlsx(write_company_workbook(tmp_path / "company.xlsx"))
    stock_code = _infer_stock_code("report-600080-20251030-Q3.pdf", "", company_index)
    stock_abbr = _infer_stock_abbr("report-600080-20251030-Q3.pdf", "", stock_code, company_index)
    assert stock_code == "600080"
    assert stock_abbr == "金花股份"


def test_infer_stock_code_from_underscore_delimited_exchange_file_name(tmp_path) -> None:
    company_index = CompanyIndex.from_xlsx(write_company_workbook(tmp_path / "company.xlsx"))

    stock_code = _infer_stock_code("600080_20250425_P7ID.pdf", "", company_index)

    assert stock_code == "600080"


def test_infer_company_from_securities_labels(tmp_path) -> None:
    company_index = CompanyIndex.from_xlsx(write_company_workbook(tmp_path / "company.xlsx"))

    stock_code = _infer_stock_code("opaque.pdf", "证券代码：600080", company_index)
    stock_abbr = _infer_stock_abbr("opaque.pdf", "证券简称：金花股份", stock_code, company_index)

    assert stock_code == "600080"
    assert stock_abbr == "金花股份"


def test_infer_stock_code_rejects_unknown_company(tmp_path) -> None:
    company_index = CompanyIndex.from_xlsx(write_company_workbook(tmp_path / "company.xlsx"))

    with pytest.raises(MetadataRecognitionError, match="stock_code") as exc_info:
        _infer_stock_code("annual-report.pdf", "无可识别的公司元数据", company_index)

    assert exc_info.value.field == "stock_code"


def test_infer_stock_abbr_rejects_unknown_company(tmp_path) -> None:
    company_index = CompanyIndex.from_xlsx(write_company_workbook(tmp_path / "company.xlsx"))

    with pytest.raises(MetadataRecognitionError, match="company") as exc_info:
        _infer_stock_abbr("annual-report.pdf", "无可识别的公司元数据", "123456", company_index)

    assert exc_info.value.field == "company"


def test_inference_rejects_unknown_placeholders(tmp_path) -> None:
    company_index = CompanyIndex.from_xlsx(write_company_workbook(tmp_path / "company.xlsx"))

    with pytest.raises(MetadataRecognitionError, match="stock_code"):
        _infer_stock_code("report-000000.pdf", "公司代码：000000", company_index)
    with pytest.raises(MetadataRecognitionError, match="company"):
        _infer_stock_abbr("未知公司：年度报告.pdf", "公司简称：未知公司", "123456", company_index)


def test_extract_report_record_keeps_page_text(monkeypatch, tmp_path) -> None:
    company_index = CompanyIndex.from_xlsx(write_company_workbook(tmp_path / "company.xlsx"))
    pages = (
        PageText(page_no=1, text="公司代码：600080\n公司简称：金花股份"),
        PageText(page_no=2, text="2024 年第三季度报告\n利润总额 1,000,000"),
    )
    monkeypatch.setattr("smart_finqa.ingestion.extract_pdf_pages", lambda _: pages)

    record = extract_report_record(Path("annual-report.pdf"), company_index)

    assert record.pages == pages
    assert record.report_period == "2024Q3"
    assert "利润总额" in record.text


def test_extract_pdf_text_keeps_flat_text_compatibility(monkeypatch) -> None:
    pages = (PageText(page_no=1, text="第一页"), PageText(page_no=2, text="第二页"))
    monkeypatch.setattr("smart_finqa.ingestion.extract_pdf_pages", lambda _: pages)

    assert extract_pdf_text(Path("annual-report.pdf")) == "第一页\n第二页"


def test_pdf_page_extractor_ocrs_only_pages_with_insufficient_native_text(monkeypatch) -> None:
    native_pages = (
        PageText(page_no=1, text="A" * 80),
        PageText(page_no=2, text="扫描页"),
        PageText(page_no=3, text="B" * 80),
    )
    monkeypatch.setattr("smart_finqa.ingestion._read_native_pdf_pages", lambda _path: native_pages)
    monkeypatch.setattr("smart_finqa.ingestion._pdftotext_pages_fallback", lambda _path: ())

    class FakeEngine:
        name = "fake-ocr"
        version = "1.0"

        def __init__(self) -> None:
            self.requested_pages: list[int] = []

        def recognize_page(self, _path: Path, page_no: int, *, dpi: int) -> OCRPage:
            assert dpi == 220
            self.requested_pages.append(page_no)
            token = OCRToken(
                token_id="ocr-p2-0001",
                page_no=2,
                text="营业收入 1,000.00",
                confidence=0.98,
                bbox=(0.1, 0.2, 0.8, 0.3),
                page_image_sha256="d" * 64,
            )
            return OCRPage(2, token.text, (token,), "d" * 64)

    engine = FakeEngine()
    pages = PdfPageExtractor(
        OCRConfig(engine="rapidocr", min_page_text_chars=40),
        ocr_engine=engine,
    ).extract(Path("report.pdf"))

    assert engine.requested_pages == [2]
    assert [page.page_no for page in pages] == [1, 2, 3]
    assert [page.source_type for page in pages] == ["native_text", "mixed", "native_text"]
    assert pages[1].text_layer_text == "扫描页"
    assert pages[1].ocr_tokens[0].bbox == (0.1, 0.2, 0.8, 0.3)


def test_pdf_page_extractor_ocrs_financial_statement_page_and_preserves_text_layer(monkeypatch) -> None:
    native_text = "合并利润表\n" + "页眉及说明" * 20
    native_pages = (PageText(page_no=1, text=native_text),)
    monkeypatch.setattr("smart_finqa.ingestion._read_native_pdf_pages", lambda _path: native_pages)
    monkeypatch.setattr("smart_finqa.ingestion._pdftotext_pages_fallback", lambda _path: ())

    class FakeEngine:
        name = "fake-ocr"
        version = "1.0"

        def recognize_page(self, _path: Path, page_no: int, *, dpi: int) -> OCRPage:
            token = OCRToken(
                token_id="ocr-p1-0001",
                page_no=page_no,
                text="净利润 1,000.00",
                confidence=0.98,
                bbox=(0.1, 0.2, 0.8, 0.3),
                page_image_sha256="e" * 64,
            )
            return OCRPage(page_no, token.text, (token,), "e" * 64)

    pages = PdfPageExtractor(
        OCRConfig(engine="rapidocr", policy="financial_pages_and_low_text"),
        ocr_engine=FakeEngine(),
    ).extract(Path("report.pdf"))

    assert pages[0].source_type == "mixed"
    assert pages[0].text_layer_text == native_text
    assert native_text in pages[0].text
    assert "净利润 1,000.00" in pages[0].text
    assert [token.text for token in pages[0].ocr_tokens] == ["净利润 1,000.00"]


def test_pdf_page_extractor_ocrs_mixed_page_with_large_raster_image(monkeypatch) -> None:
    native_pages = (PageText(page_no=1, text="页眉及说明" * 20, has_large_raster_image=True),)
    monkeypatch.setattr("smart_finqa.ingestion._read_native_pdf_pages", lambda _path: native_pages)
    monkeypatch.setattr("smart_finqa.ingestion._pdftotext_pages_fallback", lambda _path: ())

    class FakeEngine:
        name = "fake-ocr"
        version = "1.0"

        def recognize_page(self, _path: Path, page_no: int, *, dpi: int) -> OCRPage:
            token = OCRToken(
                token_id="ocr-p1-0001",
                page_no=page_no,
                text="资产总计 1,000.00",
                confidence=0.98,
                bbox=(0.1, 0.2, 0.8, 0.3),
                page_image_sha256="a" * 64,
            )
            return OCRPage(page_no, token.text, (token,), "a" * 64)

    pages = PdfPageExtractor(
        OCRConfig(engine="rapidocr", policy="financial_pages_and_low_text"),
        ocr_engine=FakeEngine(),
    ).extract(Path("report.pdf"))

    assert pages[0].source_type == "mixed"
    assert pages[0].has_large_raster_image is True
    assert [token.text for token in pages[0].ocr_tokens] == ["资产总计 1,000.00"]


def test_pdf_page_extractor_does_not_erase_text_when_ocr_returns_no_tokens(monkeypatch) -> None:
    native_pages = (PageText(page_no=1, text="扫描页说明"),)
    monkeypatch.setattr("smart_finqa.ingestion._read_native_pdf_pages", lambda _path: native_pages)
    monkeypatch.setattr("smart_finqa.ingestion._pdftotext_pages_fallback", lambda _path: ())

    class EmptyEngine:
        name = "fake-ocr"
        version = "1.0"

        def recognize_page(self, _path: Path, page_no: int, *, dpi: int) -> OCRPage:
            return OCRPage(page_no, "", (), "f" * 64)

    pages = PdfPageExtractor(OCRConfig(engine="rapidocr"), ocr_engine=EmptyEngine()).extract(Path("report.pdf"))

    assert pages[0].source_type == "mixed"
    assert pages[0].text == "扫描页说明"
    assert pages[0].text_layer_text == "扫描页说明"
    assert pages[0].ocr_tokens == ()
    assert pages[0].page_image_sha256 == "f" * 64


def test_pdf_page_extractor_preserves_blank_physical_pages_after_ocr(monkeypatch) -> None:
    native_pages = tuple(PageText(page_no=page_no, text="") for page_no in range(1, 11))
    monkeypatch.setattr("smart_finqa.ingestion._read_native_pdf_pages", lambda _path: native_pages)
    monkeypatch.setattr("smart_finqa.ingestion._pdftotext_pages_fallback", lambda _path: ())

    class FakeEngine:
        name = "fake-ocr"
        version = "1.0"

        def recognize_page(self, _path: Path, page_no: int, *, dpi: int) -> OCRPage:
            return OCRPage(page_no, "", (), f"{page_no:064x}")

    pages = PdfPageExtractor(OCRConfig(engine="rapidocr"), ocr_engine=FakeEngine()).extract(Path("report.pdf"))

    assert len(pages) == 10
    assert [page.page_no for page in pages] == list(range(1, 11))


def test_metadata_inspection_uses_only_the_head_ocr_path(tmp_path: Path) -> None:
    company_index = CompanyIndex.from_xlsx(write_company_workbook(tmp_path / "company.xlsx"))

    class FakeExtractor:
        def __init__(self) -> None:
            self.max_pages: int | None = None

        def extract_head(self, _path: Path, *, max_pages: int) -> tuple[tuple[PageText, ...], int]:
            self.max_pages = max_pages
            return (
                (
                    PageText(1, "金花股份 公司代码：600080 2024年年度报告"),
                    PageText(2, "目录"),
                ),
                127,
            )

    extractor = FakeExtractor()
    metadata = inspect_report_file_metadata(
        Path("opaque.pdf"),
        company_index,
        page_extractor=extractor,
    )

    assert extractor.max_pages == 2
    assert metadata.page_count == 127
    assert metadata.stock_code == "600080"


@pytest.mark.parametrize(
    ("file_name", "expected_code", "expected_period", "is_summary", "is_english", "revision_kind", "copy_index"),
    [
        ("华润三九：2024年年度报告摘要.pdf", "000999", "2024FY", True, False, ReportRevisionKind.ORIGINAL, 0),
        ("华润三九：2024年年度报告（英文版）.pdf", "000999", "2024FY", False, True, ReportRevisionKind.ORIGINAL, 0),
        ("桂林三金：2024年第一季度报告（更正后）.pdf", "002275", "2024Q1", False, False, ReportRevisionKind.REVISED, 0),
        (
            "桂林三金：2024年第一季度报告（更新前）.pdf",
            "002275",
            "2024Q1",
            False,
            False,
            ReportRevisionKind.PRE_REVISION,
            0,
        ),
        (
            "桂林三金：2024年第一季度报告（更正后） (1).pdf",
            "002275",
            "2024Q1",
            False,
            False,
            ReportRevisionKind.REVISED,
            1,
        ),
    ],
)
def test_classify_report_file_name_recognizes_formal_variants(
    tmp_path: Path,
    file_name: str,
    expected_code: str,
    expected_period: str,
    is_summary: bool,
    is_english: bool,
    revision_kind: ReportRevisionKind,
    copy_index: int,
) -> None:
    company_index = CompanyIndex.from_xlsx(write_company_workbook(tmp_path / "company.xlsx"))

    metadata = classify_report_file_name(Path(file_name), company_index)

    assert metadata.stock_code == expected_code
    assert metadata.report_period == expected_period
    assert metadata.is_summary is is_summary
    assert metadata.is_english is is_english
    assert metadata.revision_kind is revision_kind
    assert metadata.copy_index == copy_index


def test_classify_opaque_exchange_file_uses_head_metadata(tmp_path: Path) -> None:
    company_index = CompanyIndex.from_xlsx(write_company_workbook(tmp_path / "company.xlsx"))

    metadata = classify_report_file_name(
        Path("600080_20250425_P7ID.pdf"),
        company_index,
        text_head="公司代码：600080 金花股份 2024年年度报告摘要",
        page_count=8,
        head_text_length=31,
    )

    assert metadata.stock_code == "600080"
    assert metadata.report_period == "2024FY"
    assert metadata.published_date == "20250425"
    assert metadata.is_summary is True
    assert metadata.page_count == 8
    assert metadata.head_text_length == 31


def _metadata(
    name: str,
    *,
    period: str = "2024FY",
    summary: bool = False,
    english: bool = False,
    revision: ReportRevisionKind = ReportRevisionKind.ORIGINAL,
    published_date: str | None = None,
    copy_index: int = 0,
) -> ReportFileMetadata:
    return ReportFileMetadata(
        path=Path(name),
        stock_code="600080",
        stock_abbr="金花股份",
        report_period=period,
        report_year=int(period[:4]),
        is_summary=summary,
        is_english=english,
        revision_kind=revision,
        published_date=published_date,
        copy_index=copy_index,
        page_count=100,
        head_text_length=1000,
    )


def test_authoritative_selection_skips_summary_english_and_superseded_versions() -> None:
    original = _metadata("original.pdf", published_date="20230301")
    summary = _metadata("summary.pdf", summary=True, published_date="20230301")
    english = _metadata("english.pdf", english=True, published_date="20230302")
    pre_revision = _metadata("before.pdf", revision=ReportRevisionKind.PRE_REVISION, published_date="20230303")
    revised = _metadata("revised.pdf", revision=ReportRevisionKind.REVISED, published_date="20230304")

    decisions = select_authoritative_reports([original, summary, english, pre_revision, revised])

    assert {decision.path.name: decision.status for decision in decisions} == {
        "original.pdf": "superseded",
        "summary.pdf": "skipped_summary",
        "english.pdf": "superseded",
        "before.pdf": "superseded",
        "revised.pdf": "selected",
    }
    assert next(decision.reason for decision in decisions if decision.path == revised.path) == "authoritative_version"


def test_authoritative_selection_prefers_unsuffixed_copy() -> None:
    canonical = _metadata("新光药业：2022年年度报告全文（更正后）.pdf", revision=ReportRevisionKind.REVISED)
    copied = _metadata(
        "新光药业：2022年年度报告全文（更正后） (1).pdf",
        revision=ReportRevisionKind.REVISED,
        copy_index=1,
    )

    decisions = select_authoritative_reports([copied, canonical])

    assert {decision.path.name: decision.status for decision in decisions} == {
        copied.path.name: "superseded",
        canonical.path.name: "selected",
    }


def test_authoritative_selection_marks_unresolved_tie_for_review() -> None:
    first = _metadata("opaque-a.pdf", published_date="20250425")
    second = _metadata("opaque-b.pdf", published_date="20250425")

    decisions = select_authoritative_reports([first, second])

    assert {decision.status for decision in decisions} == {"needs_review"}
    assert {decision.reason for decision in decisions} == {"ambiguous_authoritative_version"}
    assert {decision.path for decision in decisions} == {first.path, second.path}


def test_authoritative_selection_rejects_company_outside_master(tmp_path: Path) -> None:
    company_index = CompanyIndex.from_xlsx(write_company_workbook(tmp_path / "company.xlsx"))
    report = classify_report_file_name(
        Path("贵州百灵：2024年年度报告.pdf"),
        company_index,
        text_head="公司代码：002424",
    )

    decisions = select_authoritative_reports([report])

    assert report.company_in_master is False
    assert [(decision.status, decision.reason) for decision in decisions] == [
        ("needs_review", "outside_company_master")
    ]


def test_classify_outside_master_company_without_extractable_stock_code(tmp_path: Path) -> None:
    company_index = CompanyIndex.from_xlsx(write_company_workbook(tmp_path / "company.xlsx"))

    report = classify_report_file_name(Path("主数据外公司：2024年年度报告.pdf"), company_index)
    decisions = select_authoritative_reports([report])

    assert report.stock_code == ""
    assert report.stock_abbr == "主数据外公司"
    assert report.company_in_master is False
    assert [(decision.status, decision.reason) for decision in decisions] == [
        ("needs_review", "outside_company_master")
    ]
