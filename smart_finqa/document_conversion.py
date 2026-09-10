"""Safe DOCX inspection and deterministic PDF rendering boundaries."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote
from xml.etree import ElementTree

from pypdf import PdfReader
from pypdf.errors import PyPdfError

MAX_DOCX_ENTRIES = 2048
MAX_DOCX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_DOCX_COMPRESSION_RATIO = 200
MAX_RELATIONSHIP_BYTES = 4 * 1024 * 1024
DEFAULT_RENDER_TIMEOUT_SECONDS = 120
REQUIRED_DOCX_PARTS = frozenset({"[Content_Types].xml", "_rels/.rels", "word/document.xml"})
DISALLOWED_PART_MARKERS = (
    "vbaproject.bin",
    "word/embeddings/",
    "word/activex/",
    "oleobject",
)
RELATIONSHIP_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/relationships"
WORDPROCESSING_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


class DocxValidationError(ValueError):
    """The DOCX package is unsafe or structurally invalid."""


class DocxRendererUnavailableError(RuntimeError):
    """No explicitly supported DOCX renderer is installed."""


class DocxRenderError(RuntimeError):
    """DOCX rendering did not produce a valid PDF."""


@dataclass(frozen=True, slots=True)
class DocxInspection:
    source_sha256: str
    entry_count: int
    uncompressed_bytes: int
    review_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RenderedPdf:
    source_path: Path
    source_sha256: str
    pdf_path: Path
    pdf_sha256: str
    page_count: int
    renderer_name: str
    renderer_version: str
    renderer_binary_sha256: str
    review_codes: tuple[str, ...]


def inspect_docx_package(path: Path) -> DocxInspection:
    """Validate an OOXML document without executing relationships or embedded content."""

    source = path.resolve(strict=True)
    if not source.is_file():
        raise DocxValidationError("DOCX source must be a regular file")
    if not zipfile.is_zipfile(source):
        raise DocxValidationError("DOCX source is not a valid ZIP package")

    with zipfile.ZipFile(source) as package:
        entries = package.infolist()
        if not entries:
            raise DocxValidationError("DOCX package is empty")
        if len(entries) > MAX_DOCX_ENTRIES:
            raise DocxValidationError(f"DOCX package contains too many entries: {len(entries)}")

        names: set[str] = set()
        total_uncompressed = 0
        for entry in entries:
            normalized_name = _validate_entry(entry)
            if normalized_name in names:
                raise DocxValidationError(f"DOCX package contains a duplicate entry: {normalized_name}")
            names.add(normalized_name)
            total_uncompressed += entry.file_size
            if total_uncompressed > MAX_DOCX_UNCOMPRESSED_BYTES:
                raise DocxValidationError("DOCX package exceeds the uncompressed size limit")

        missing = REQUIRED_DOCX_PARTS - names
        if missing:
            raise DocxValidationError(f"DOCX package is missing required parts: {', '.join(sorted(missing))}")
        lowered_names = {name.casefold() for name in names}
        disallowed = sorted(name for name in lowered_names if any(marker in name for marker in DISALLOWED_PART_MARKERS))
        if disallowed:
            raise DocxValidationError(f"DOCX package contains executable or embedded content: {disallowed[0]}")

        for name in sorted(names):
            if name.endswith(".rels"):
                _reject_external_relationships(package, name)

        document_xml = _read_bounded(package, "word/document.xml", MAX_RELATIONSHIP_BYTES)
        review_codes = _document_review_codes(document_xml, names)

    return DocxInspection(
        source_sha256=_sha256(source),
        entry_count=len(entries),
        uncompressed_bytes=total_uncompressed,
        review_codes=review_codes,
    )


def render_docx_to_pdf(
    source_path: Path,
    output_dir: Path,
    *,
    renderer_path: Path | None = None,
    timeout_seconds: int = DEFAULT_RENDER_TIMEOUT_SECONDS,
) -> RenderedPdf:
    """Render DOCX once with LibreOffice and return immutable lineage metadata."""

    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or timeout_seconds < 1:
        raise ValueError("timeout_seconds must be a positive integer")
    inspection = inspect_docx_package(source_path)
    renderer = _resolve_renderer(renderer_path)
    destination = output_dir.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    pdf_path = destination / f"{source_path.stem}.pdf"
    if pdf_path.exists() or pdf_path.is_symlink():
        raise FileExistsError(f"DOCX render target already exists: {pdf_path}")
    renderer_version = _renderer_version(renderer, timeout_seconds=min(timeout_seconds, 15))
    renderer_binary_sha256 = _sha256(renderer)

    with tempfile.TemporaryDirectory(prefix="smart-finqa-lo-profile-", dir=destination) as profile_dir:
        profile_uri = "file:///" + quote(Path(profile_dir).resolve().as_posix(), safe="/:")
        command = [
            str(renderer),
            "--headless",
            "--nologo",
            "--nodefault",
            "--nolockcheck",
            "--nofirststartwizard",
            f"-env:UserInstallation={profile_uri}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(destination),
            str(source_path.resolve(strict=True)),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise DocxRenderError(f"DOCX rendering timed out after {timeout_seconds} seconds") from exc
    if completed.returncode != 0:
        raise DocxRenderError(f"DOCX renderer exited with code {completed.returncode}")

    if not pdf_path.is_file() or pdf_path.stat().st_size < 5:
        raise DocxRenderError("DOCX renderer did not produce the expected PDF")
    with pdf_path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise DocxRenderError("DOCX renderer output is not a PDF")
    try:
        page_count = len(PdfReader(pdf_path).pages)
    except (OSError, PyPdfError, TypeError, ValueError) as exc:
        raise DocxRenderError("DOCX renderer output cannot be parsed as PDF") from exc
    if page_count < 1:
        raise DocxRenderError("DOCX renderer output contains no pages")
    if _sha256(source_path) != inspection.source_sha256:
        raise DocxRenderError("DOCX source changed during rendering")
    if _sha256(renderer) != renderer_binary_sha256:
        raise DocxRenderError("DOCX renderer binary changed during rendering")

    return RenderedPdf(
        source_path=source_path.resolve(strict=True),
        source_sha256=inspection.source_sha256,
        pdf_path=pdf_path.resolve(strict=True),
        pdf_sha256=_sha256(pdf_path),
        page_count=page_count,
        renderer_name=renderer.name,
        renderer_version=renderer_version,
        renderer_binary_sha256=renderer_binary_sha256,
        review_codes=inspection.review_codes,
    )


def _validate_entry(entry: zipfile.ZipInfo) -> str:
    name = entry.filename.replace("\\", "/")
    pure_path = PurePosixPath(name)
    if not name or pure_path.is_absolute() or ".." in pure_path.parts:
        raise DocxValidationError(f"DOCX package contains an unsafe entry path: {entry.filename!r}")
    if entry.flag_bits & 0x1:
        raise DocxValidationError(f"DOCX package contains an encrypted entry: {entry.filename!r}")
    if entry.file_size > MAX_DOCX_UNCOMPRESSED_BYTES:
        raise DocxValidationError(f"DOCX entry exceeds the size limit: {entry.filename!r}")
    compressed_size = max(entry.compress_size, 1)
    if entry.file_size > 1024 * 1024 and entry.file_size / compressed_size > MAX_DOCX_COMPRESSION_RATIO:
        raise DocxValidationError(f"DOCX entry exceeds the compression ratio limit: {entry.filename!r}")
    return pure_path.as_posix()


def _reject_external_relationships(package: zipfile.ZipFile, name: str) -> None:
    content = _read_bounded(package, name, MAX_RELATIONSHIP_BYTES)
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise DocxValidationError(f"DOCX relationship part is invalid XML: {name}") from exc
    for relation in root.findall(f"{{{RELATIONSHIP_NAMESPACE}}}Relationship"):
        if relation.attrib.get("TargetMode", "").casefold() == "external":
            raise DocxValidationError(f"DOCX package contains an external relationship: {name}")


def _read_bounded(package: zipfile.ZipFile, name: str, limit: int) -> bytes:
    info = package.getinfo(name)
    if info.file_size > limit:
        raise DocxValidationError(f"DOCX XML part exceeds the size limit: {name}")
    with package.open(info) as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise DocxValidationError(f"DOCX XML part exceeds the size limit: {name}")
    return content


def _document_review_codes(document_xml: bytes, names: set[str]) -> tuple[str, ...]:
    try:
        root = ElementTree.fromstring(document_xml)
    except ElementTree.ParseError as exc:
        raise DocxValidationError("word/document.xml is invalid XML") from exc
    codes: set[str] = set()
    if (
        root.find(f".//{{{WORDPROCESSING_NAMESPACE}}}ins") is not None
        or root.find(f".//{{{WORDPROCESSING_NAMESPACE}}}del") is not None
    ):
        codes.add("REVISION_CONFLICT")
    if root.find(f".//{{{WORDPROCESSING_NAMESPACE}}}txbxContent") is not None:
        codes.add("DOCX_TEXTBOX_PRESENT")
    if any(name.startswith("word/comments") for name in names):
        codes.add("DOCX_COMMENTS_PRESENT")
    return tuple(sorted(codes))


def _resolve_renderer(renderer_path: Path | None) -> Path:
    if renderer_path is not None:
        resolved = renderer_path.resolve(strict=True)
        if not resolved.is_file():
            raise DocxRendererUnavailableError(f"DOCX renderer is not a file: {resolved}")
        return resolved
    discovered = shutil.which("soffice") or shutil.which("libreoffice")
    if not discovered:
        raise DocxRendererUnavailableError("DOCX_RENDERER_UNAVAILABLE: install LibreOffice or configure a renderer")
    return Path(discovered).resolve(strict=True)


def _renderer_version(renderer: Path, *, timeout_seconds: int) -> str:
    try:
        completed = subprocess.run(
            [str(renderer), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise DocxRenderError("DOCX renderer version check timed out") from exc
    version = (completed.stdout or completed.stderr).strip()
    if completed.returncode != 0 or not version:
        raise DocxRenderError("DOCX renderer version could not be determined")
    return version[:500]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
