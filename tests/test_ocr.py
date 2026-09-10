import pytest

from smart_finqa.config import OCRConfig
from smart_finqa.ocr import OCRDependencyError, OCRPage, OCRPageError, OCRToken, RapidOCREngine


def test_ocr_token_requires_normalized_ordered_bbox() -> None:
    with pytest.raises(ValueError, match="normalized"):
        OCRToken(
            token_id="ocr-p1-0001",
            page_no=1,
            text="营业收入",
            confidence=0.99,
            bbox=(0.1, 0.2, 1.2, 0.3),
            page_image_sha256="a" * 64,
        )


def test_ocr_page_requires_tokens_from_the_same_page() -> None:
    token = OCRToken(
        token_id="ocr-p2-0001",
        page_no=2,
        text="净利润",
        confidence=0.95,
        bbox=(0.1, 0.2, 0.3, 0.4),
        page_image_sha256="b" * 64,
    )

    with pytest.raises(ValueError, match="same page"):
        OCRPage(page_no=1, text="净利润", tokens=(token,), page_image_sha256="b" * 64)


def test_rapidocr_fails_explicitly_when_optional_dependencies_are_missing(monkeypatch) -> None:
    monkeypatch.setattr("smart_finqa.ocr.import_module", lambda _name: (_ for _ in ()).throw(ImportError("missing")))

    with pytest.raises(OCRDependencyError, match="OCR_ENGINE_UNAVAILABLE"):
        RapidOCREngine(OCRConfig(engine="rapidocr"))


def test_rapidocr_converts_boxes_to_stable_normalized_tokens() -> None:
    class Output:
        boxes = [
            [[100, 20], [180, 20], [180, 40], [100, 40]],
            [[10, 20], [90, 20], [90, 40], [10, 40]],
        ]
        txts = ("1,000.00", "营业收入")
        scores = (0.98, 0.99)

    page = RapidOCREngine.page_from_output(
        page_no=3,
        output=Output(),
        image_width=200,
        image_height=100,
        page_image_sha256="c" * 64,
    )

    assert page.text == "营业收入 1,000.00"
    assert [token.text for token in page.tokens] == ["营业收入", "1,000.00"]
    assert page.tokens[0].bbox == pytest.approx((0.05, 0.2, 0.45, 0.4))
    assert page.tokens[0].token_id == "ocr-p3-0001"


@pytest.mark.parametrize("invalid_coordinate", [float("nan"), float("inf"), float("-inf")])
def test_rapidocr_rejects_non_finite_detector_coordinates(invalid_coordinate: float) -> None:
    class Output:
        boxes = [[[invalid_coordinate, 20], [180, 20], [180, 40], [100, 40]]]
        txts = ("营业收入",)
        scores = (0.99,)

    with pytest.raises(OCRPageError, match="non-finite coordinates"):
        RapidOCREngine.page_from_output(
            page_no=1,
            output=Output(),
            image_width=200,
            image_height=100,
            page_image_sha256="d" * 64,
        )


def test_rapidocr_rejects_page_that_exceeds_pixel_limit_before_rendering(tmp_path) -> None:
    class FakePage:
        render_called = False

        @staticmethod
        def get_size() -> tuple[int, int]:
            return (100_000, 100_000)

        def render(self, *, scale: float):
            self.render_called = True
            raise AssertionError(f"oversized page must not render at scale {scale}")

        @staticmethod
        def close() -> None:
            return None

    page = FakePage()

    class FakeDocument:
        @staticmethod
        def __len__() -> int:
            return 1

        @staticmethod
        def __getitem__(_index: int) -> FakePage:
            return page

        @staticmethod
        def close() -> None:
            return None

    class FakePdfium:
        @staticmethod
        def PdfDocument(_path: str) -> FakeDocument:
            return FakeDocument()

    engine = object.__new__(RapidOCREngine)
    engine.config = OCRConfig(engine="rapidocr", max_page_pixels=10_000_000)
    engine._pdfium = FakePdfium()

    with pytest.raises(OCRPageError, match="OCR_PAGE_TOO_LARGE"):
        engine.recognize_page(tmp_path / "oversized.pdf", 1, dpi=220)

    assert page.render_called is False
