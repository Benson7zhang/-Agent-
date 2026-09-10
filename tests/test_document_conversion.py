from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from smart_finqa.document_conversion import (
    DocxRendererUnavailableError,
    DocxValidationError,
    inspect_docx_package,
    render_docx_to_pdf,
)

CONTENT_TYPES = (
    b"""<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>"""
)
ROOT_RELS = (
    b"""<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>"""
)
DOCUMENT = b"""<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p/></w:body></w:document>"""


def _write_docx(path: Path, *, parts: dict[str, bytes] | None = None) -> Path:
    values = {
        "[Content_Types].xml": CONTENT_TYPES,
        "_rels/.rels": ROOT_RELS,
        "word/document.xml": DOCUMENT,
    }
    values.update(parts or {})
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as package:
        for name, content in values.items():
            package.writestr(name, content)
    return path


def test_inspect_docx_package_returns_hash_and_detects_review_content(tmp_path: Path) -> None:
    document = DOCUMENT.replace(b"<w:p/>", b"<w:ins><w:p/></w:ins><w:txbxContent/>")
    path = _write_docx(tmp_path / "report.docx", parts={"word/document.xml": document, "word/comments.xml": b"<x/>"})

    result = inspect_docx_package(path)

    assert len(result.source_sha256) == 64
    assert result.entry_count == 4
    assert result.review_codes == ("DOCX_COMMENTS_PRESENT", "DOCX_TEXTBOX_PRESENT", "REVISION_CONFLICT")


@pytest.mark.parametrize("part_name", ["word/vbaProject.bin", "word/embeddings/oleObject1.bin"])
def test_inspect_docx_package_rejects_executable_or_embedded_parts(tmp_path: Path, part_name: str) -> None:
    path = _write_docx(tmp_path / "unsafe.docx", parts={part_name: b"payload"})

    with pytest.raises(DocxValidationError, match="executable or embedded"):
        inspect_docx_package(path)


def test_inspect_docx_package_rejects_external_relationships(tmp_path: Path) -> None:
    external = b"""<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r1" Target="https://example.test" TargetMode="External" Type="x"/></Relationships>"""
    path = _write_docx(tmp_path / "external.docx", parts={"word/_rels/document.xml.rels": external})

    with pytest.raises(DocxValidationError, match="external relationship"):
        inspect_docx_package(path)


def test_render_docx_fails_explicitly_when_renderer_is_unavailable(monkeypatch, tmp_path: Path) -> None:
    path = _write_docx(tmp_path / "report.docx")
    monkeypatch.setattr("smart_finqa.document_conversion.shutil.which", lambda _name: None)

    with pytest.raises(DocxRendererUnavailableError, match="DOCX_RENDERER_UNAVAILABLE"):
        render_docx_to_pdf(path, tmp_path / "output")


def test_render_docx_refuses_existing_output_before_starting_renderer(monkeypatch, tmp_path: Path) -> None:
    path = _write_docx(tmp_path / "report.docx")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "report.pdf").write_bytes(b"stale")
    renderer = tmp_path / "soffice.exe"
    renderer.write_bytes(b"binary")

    with pytest.raises(FileExistsError, match="render target already exists"):
        render_docx_to_pdf(path, output_dir, renderer_path=renderer)
