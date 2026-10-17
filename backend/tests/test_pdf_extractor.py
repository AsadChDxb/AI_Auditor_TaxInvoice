"""Tests for the Phase 9 extraction pipeline (pdfplumber + quality check + OCR).

The suite never installs PaddleOCR: the fallback is driven through
:class:`tests.ocr_stubs.StubOcrEngine`, and pdfplumber runs on the in-memory fixtures
built by :mod:`tests.pdf_factory`.
"""

import pytest

from app.config import settings
from app.services import pdf_extractor
from app.services.ocr_fallback import OcrError
from app.services.pdf_extractor import (
    NEEDS_OCR_ERROR,
    PADDLEOCR_SOURCE,
    PDFPLUMBER_SOURCE,
    CorruptedPdfError,
    ExtractedPage,
    ExtractedTable,
    UnsupportedFileTypeError,
    build_extraction_quality,
    extract_pdf_text,
    looks_like_pdf,
    page_needs_ocr,
)
from tests.ocr_stubs import StubOcrEngine, make_line, scanned_invoice_lines
from tests.pdf_factory import (
    build_blank_pdf,
    build_encrypted_pdf,
    build_scanned_pdf,
    build_table_pdf,
    build_text_pdf,
)

INVOICE_TEXT = "\n".join(
    [
        "TAX INVOICE INV-2024-0001",
        "Date: 2024-05-03",
        "Supplier: Al Noor Trading LLC",
        "Supplier TRN: 100123456700003",
        "Taxable Amount: 1000.00",
        "VAT Amount: 50.00",
        "Total: 1050.00",
    ]
)
TABLE_ROWS = [
    ["Description", "Qty", "Amount"],
    ["Consulting services", "1", "1,000.00"],
    ["VAT 5%", "", "50.00"],
    ["Total", "", "1,050.00"],
]


@pytest.fixture()
def no_configured_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend PaddleOCR is not installed, so no OCR engine is available."""

    monkeypatch.setattr(pdf_extractor, "get_ocr_engine", lambda language=None: None)


# --- validation -------------------------------------------------------------------


def test_looks_like_pdf_searches_the_leading_bytes() -> None:
    assert looks_like_pdf(b"%PDF-1.7\n") is True

    assert looks_like_pdf(b"junk%PDF-1.7\n") is True
    assert looks_like_pdf(b"This is plain text") is False
    assert looks_like_pdf(b"") is False


def test_empty_and_non_pdf_bytes_are_rejected() -> None:
    with pytest.raises(CorruptedPdfError):
        extract_pdf_text(b"")
    with pytest.raises(UnsupportedFileTypeError):
        extract_pdf_text(b"a plain text file")


def test_corrupted_and_encrypted_pdfs_are_rejected() -> None:
    corrupted = b"%PDF-1.4\n" + bytes([0x00, 0xFF, 0x7F]) * 200

    with pytest.raises(CorruptedPdfError) as corrupted_error:
        extract_pdf_text(corrupted)
    with pytest.raises(CorruptedPdfError) as encrypted_error:
        extract_pdf_text(build_encrypted_pdf())

    assert "corrupt" in str(corrupted_error.value).lower()
    assert "password protected" in str(encrypted_error.value)


# --- pdfplumber extraction --------------------------------------------------------


def test_pages_come_from_pdfplumber_with_text_and_provenance() -> None:
    result = extract_pdf_text(build_text_pdf([INVOICE_TEXT, "Page two: payment terms."]))

    assert result.page_count == 2
    assert result.pages_with_text == 2
    assert result.has_text is True
    assert "INV-2024-0001" in result.pages[0].text
    assert all(page.source == PDFPLUMBER_SOURCE for page in result.pages)
    assert result.used_ocr is False
    assert result.ocr_pages == []
    assert result.total_characters > 0


def test_ruled_table_is_rebuilt_and_reported() -> None:
    result = extract_pdf_text(build_table_pdf(TABLE_ROWS))

    assert result.table_count == 1
    table = result.tables[0]
    assert table.source == PDFPLUMBER_SOURCE
    assert (table.row_count, table.column_count) == (4, 3)
    assert table.rows[1] == ["Consulting services", "1", "1,000.00"]
    assert "Description | Qty | Amount" in table.to_text()
    assert result.pages[0].has_ruling_lines is True
    assert result.quality.table_pages == [1]


def test_quality_check_reports_required_fields() -> None:
    complete = extract_pdf_text(build_text_pdf([INVOICE_TEXT]))
    incomplete = extract_pdf_text(build_text_pdf(["Total: 100.00"]))

    assert complete.quality.missing_required_fields == []
    assert "invoice_number" in complete.quality.required_fields_found
    assert "quality" in complete.quality.summary()

    assert incomplete.quality.missing_required_fields == [
        "invoice_number",
        "invoice_date",
        "supplier_trn",
        "taxable_amount",
        "vat_amount",
    ]
    assert any(
        "Required invoice fields were not found" in issue
        for issue in incomplete.quality.issues
    )


def test_quality_check_flags_an_incomplete_table() -> None:
    page = ExtractedPage(page_number=1, text=INVOICE_TEXT, has_ruling_lines=True)

    quality = build_extraction_quality([page])

    assert any("Ruling lines were found on page(s) 1" in issue for issue in quality.issues)
    assert quality.passed is False


def test_quality_check_passes_for_a_complete_invoice() -> None:
    result = extract_pdf_text(build_table_pdf(TABLE_ROWS))

    assert result.quality.passed is False  # the amount table has no invoice header
    # A document with every required field, full text and a rebuilt table passes.
    page = ExtractedPage(
        page_number=1,
        text=INVOICE_TEXT,
        tables=[ExtractedTable(page_number=1, rows=TABLE_ROWS)],
    )
    assert build_extraction_quality([page]).passed is True


# --- OCR candidate detection ------------------------------------------------------


def test_page_needs_ocr_when_the_text_layer_is_missing() -> None:
    assert page_needs_ocr(ExtractedPage(page_number=1, text="")) is True
    assert page_needs_ocr(ExtractedPage(page_number=1, text="   \n  ")) is True
    assert page_needs_ocr(ExtractedPage(page_number=1, text="Total")) is True


def test_page_with_real_text_is_never_an_ocr_candidate() -> None:
    with_text = ExtractedPage(page_number=1, text="Total: 1,050.00")

    assert page_needs_ocr(with_text) is False
    # A page that merely holds a logo next to a text layer is not a scan either.
    with_logo = ExtractedPage(page_number=1, text=INVOICE_TEXT, image_coverage=0.9)
    assert page_needs_ocr(with_logo) is False


def test_image_dominated_page_with_a_stamp_is_an_ocr_candidate() -> None:
    scanned = ExtractedPage(
        page_number=1, text="Scanned by Office Scanner", image_coverage=0.94
    )

    assert page_needs_ocr(scanned) is True


# --- OCR fallback -----------------------------------------------------------------


def test_scanned_page_without_an_engine_needs_ocr(no_configured_engine: None) -> None:
    result = extract_pdf_text(build_blank_pdf(page_count=1))

    assert result.has_text is False
    assert result.used_ocr is False
    assert result.quality.pages_needing_ocr == [1]
    assert result.quality.ocr_attempted is False
    assert any(
        "PaddleOCR fallback is not installed" in issue for issue in result.quality.issues
    )


def test_ocr_fallback_reads_a_scanned_page() -> None:
    engine = StubOcrEngine()

    result = extract_pdf_text(build_scanned_pdf(), ocr_engine=engine)

    page = result.pages[0]
    assert page.from_ocr is True
    assert page.source == PADDLEOCR_SOURCE
    assert "Invoice No: INV-2024-0001" in page.text
    assert "Subtotal | 1,000.00" in page.text
    assert result.used_ocr is True
    assert result.ocr_pages == [1]
    assert result.has_text is True

    # The recovered layout is reported as a table and the quality check now passes.
    assert result.table_count == 1
    table = result.tables[0]
    assert table.source == PADDLEOCR_SOURCE
    assert table.rows[-1] == ["Total", "1,050.00"]
    assert result.quality.ocr_attempted is True
    assert result.quality.pages_needing_ocr == []
    assert result.quality.missing_required_fields == []
    assert result.quality.passed is True


def test_ocr_fallback_renders_the_page_image() -> None:
    engine = StubOcrEngine()

    extract_pdf_text(build_scanned_pdf(), ocr_engine=engine)

    assert len(engine.images) == 1
    # 150 dpi rendering of an A4 page is far wider than the PDF point size.
    assert engine.images[0].width > 1000


def test_ocr_fallback_does_not_touch_a_page_that_already_has_text() -> None:
    engine = StubOcrEngine()

    result = extract_pdf_text(build_text_pdf([INVOICE_TEXT]), ocr_engine=engine)

    assert engine.images == []
    assert result.used_ocr is False
    assert result.quality.ocr_attempted is False
    assert result.pages[0].source == PDFPLUMBER_SOURCE


def test_ocr_fallback_keeps_the_text_layer_when_ocr_reads_less() -> None:
    engine = StubOcrEngine([make_line("nonsense", 10.0, 10.0)])

    result = extract_pdf_text(build_scanned_pdf(), ocr_engine=engine)

    assert result.pages[0].text == "Scanned by Office Scanner"
    assert result.pages[0].source == PDFPLUMBER_SOURCE
    assert result.used_ocr is False
    assert any("original text was kept" in issue for issue in result.quality.issues)


def test_ocr_failure_is_reported_and_never_raised() -> None:
    engine = StubOcrEngine(error=OcrError("the model could not be loaded"))

    result = extract_pdf_text(build_scanned_pdf(), ocr_engine=engine)

    assert result.has_text is True  # the scanner stamp is still readable
    assert result.used_ocr is False
    assert result.quality.ocr_attempted is True
    assert any("could not read page 1" in issue for issue in result.quality.issues)


def test_ocr_fallback_can_be_disabled_by_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ocr_enabled", False)
    engine = StubOcrEngine()

    result = extract_pdf_text(build_scanned_pdf(), ocr_engine=engine)

    assert engine.images == []
    assert any(
        "ocr fallback is disabled" in issue.lower() for issue in result.quality.issues
    )


def test_ocr_page_budget_bounds_the_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ocr_max_pages", 1)
    engine = StubOcrEngine()

    result = extract_pdf_text(build_blank_pdf(page_count=2), ocr_engine=engine)

    assert result.ocr_pages == [1]
    assert len(engine.images) == 1
    assert any("first 1 of 2 pages" in issue for issue in result.quality.issues)


def test_injected_engine_is_used_instead_of_the_configured_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def must_not_be_called(language: str | None = None) -> None:
        raise AssertionError("the configured engine must not be resolved")

    monkeypatch.setattr(pdf_extractor, "get_ocr_engine", must_not_be_called)

    result = extract_pdf_text(build_scanned_pdf(), ocr_engine=StubOcrEngine())

    assert result.ocr_pages == [1]


def test_needs_ocr_error_wording_points_at_the_fallback() -> None:
    assert "OCR" in NEEDS_OCR_ERROR
    assert "scanned" in NEEDS_OCR_ERROR


def test_stub_lines_are_a_usable_scan_fixture() -> None:
    # Guards the fixture itself: the canned layout must stay parseable.
    assert len(scanned_invoice_lines()) == 15
