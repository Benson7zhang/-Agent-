from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import warnings

from openpyxl import load_workbook
from pypdf import PdfReader

from .core import parse_report_period
from .extraction import extract_metric_snapshot_from_text


@dataclass(slots=True)
class ReportRecord:
    source_path: Path
    stock_code: str
    stock_abbr: str
    report_period: str
    report_year: int
    text: str
    snapshot: dict[str, float | None]


@dataclass(slots=True)
class CompanyIndex:
    code_to_abbr: dict[str, str]
    abbr_to_code: dict[str, str]

    @classmethod
    def from_xlsx(cls, path: Path) -> "CompanyIndex":
        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb[wb.sheetnames[0]]
        code_to_abbr: dict[str, str] = {}
        abbr_to_code: dict[str, str] = {}
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row:
                continue
            code = row[1]
            abbr = row[2]
            if code is None or not abbr:
                continue
            code_str = str(int(code)).zfill(6)
            abbr_str = str(abbr).strip()
            code_to_abbr[code_str] = abbr_str
            abbr_to_code[abbr_str] = code_str
        return cls(code_to_abbr=code_to_abbr, abbr_to_code=abbr_to_code)


def extract_pdf_text(pdf_path: Path) -> str:
    text = ""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            reader = PdfReader(str(pdf_path))
            if getattr(reader, "is_encrypted", False):
                try:
                    reader.decrypt("")
                except Exception:
                    pass
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        text = ""
    if len(text.strip()) >= 80:
        return text
    text_alt = _pdftotext_fallback(pdf_path)
    if len(text_alt.strip()) >= 80:
        return text_alt
    ocr_text = _ocr_fallback(pdf_path)
    if ocr_text.strip():
        return ocr_text
    if text_alt.strip():
        return text_alt
    return text


def _pdftotext_fallback(pdf_path: Path) -> str:
    if not shutil.which("pdftotext"):
        return ""
    try:
        proc = subprocess.run(
            ["pdftotext", str(pdf_path), "-"],
            check=False,
            capture_output=True,
            text=True,
            errors="ignore",
        )
        if proc.stdout:
            return proc.stdout
    except Exception:
        return ""
    return ""


def _ocr_fallback(pdf_path: Path) -> str:
    """Best-effort OCR fallback when PDF text layer is missing."""
    if not shutil.which("pdftoppm") or not shutil.which("tesseract"):
        return ""
    with tempfile.TemporaryDirectory(prefix="pdf_ocr_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        image_prefix = tmp_path / "page"
        try:
            subprocess.run(
                ["pdftoppm", "-f", "1", "-l", "8", "-r", "200", "-png", str(pdf_path), str(image_prefix)],
                check=True,
                capture_output=True,
            )
        except Exception:
            return ""
        ocr_chunks: list[str] = []
        for image_file in sorted(tmp_path.glob("page-*.png")):
            try:
                proc = subprocess.run(
                    ["tesseract", str(image_file), "stdout", "-l", "chi_sim+eng"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except Exception:
                continue
            if proc.stdout:
                ocr_chunks.append(proc.stdout)
        return "\n".join(ocr_chunks)


def _infer_stock_code(file_name: str, text: str, company_index: CompanyIndex) -> str:
    code_match = re.search(r"\b(\d{6})\b", file_name)
    if code_match:
        return code_match.group(1)
    if "：" in file_name:
        candidate = file_name.split("：", 1)[0]
        if candidate in company_index.abbr_to_code:
            return company_index.abbr_to_code[candidate]
    text_match = re.search(r"公司代码[:：]\s*(\d{6})", text)
    if text_match:
        return text_match.group(1)
    for abbr, code in company_index.abbr_to_code.items():
        if abbr in text:
            return code
    return "000000"


def _infer_stock_abbr(file_name: str, text: str, stock_code: str, company_index: CompanyIndex) -> str:
    if stock_code in company_index.code_to_abbr:
        return company_index.code_to_abbr[stock_code]
    if "：" in file_name:
        return file_name.split("：", 1)[0]
    match = re.search(r"公司简称[:：]\s*([^\s]+)", text)
    if match:
        return match.group(1).strip()
    return "未知公司"


def extract_report_record(pdf_path: Path, company_index: CompanyIndex) -> ReportRecord:
    text = extract_pdf_text(pdf_path)
    text_head = text[:5000]
    stock_code = _infer_stock_code(pdf_path.name, text_head, company_index)
    stock_abbr = _infer_stock_abbr(pdf_path.name, text_head, stock_code, company_index)
    report_period, report_year = parse_report_period(pdf_path.name, text_head)
    snapshot = extract_metric_snapshot_from_text(text)
    return ReportRecord(
        source_path=pdf_path,
        stock_code=stock_code,
        stock_abbr=stock_abbr,
        report_period=report_period,
        report_year=report_year,
        text=text,
        snapshot=snapshot,
    )
