"""OCR stubs for the offline test suite.

PaddleOCR is an optional dependency and is never installed for the test run, so the
fallback is exercised through these stubs: a deterministic engine that returns canned
text lines with bounding boxes (the shape PaddleOCR produces) and records the page
images it was handed.
"""

from collections.abc import Sequence
from typing import Any

from app.services.ocr_fallback import OcrEngine, OcrLine

STUB_ENGINE_LABEL = "stub-ocr"
"""Label of the stub engine, so a test can tell which engine produced a page."""

SCANNED_PAGE_WIDTH = 595.0
"""A4 width in points, which is what the scanned fixtures are built on."""

LINE_HEIGHT = 12.0
COLUMN_LEFT = 100.0
COLUMN_RIGHT = 300.0


def make_line(
    text: str,
    left: float,
    top: float,
    *,
    right: float | None = None,
    confidence: float = 0.97,
) -> OcrLine:
    """Build one OCR line with a plausible bounding box."""

    end = left + 8.0 * len(text) if right is None else right
    return OcrLine(
        text=text,
        left=left,
        top=top,
        right=end,
        bottom=top + LINE_HEIGHT,
        confidence=confidence,
    )


def _row(top: float, left_text: str, right_text: str = "") -> list[OcrLine]:
    """One OCR row: a left column line and an optional right column line."""

    lines = [make_line(left_text, COLUMN_LEFT, top)]
    if right_text:
        lines.append(make_line(right_text, COLUMN_RIGHT, top))
    return lines


def scanned_invoice_lines() -> list[OcrLine]:
    """A small scanned invoice: header lines plus a two-column amount table.

    The rows are deliberately ungrouped and out of order (that is what OCR returns), so
    the tests also cover the row/column recovery in ``app.services.ocr_fallback``.
    """

    rows = [
        _row(60.0, "TAX INVOICE", "Al Noor Trading LLC"),
        _row(90.0, "Invoice No: INV-2024-0001", "Date: 2024-05-03"),
        _row(120.0, "Supplier TRN: 100123456700003"),
        _row(150.0, "Description", "Amount"),
        _row(180.0, "Consulting services", "1,000.00"),
        _row(210.0, "Subtotal", "1,000.00"),
        _row(240.0, "VAT 5%", "50.00"),
        _row(270.0, "Total", "1,050.00"),
    ]
    lines = [line for row in rows for line in row]
    lines.reverse()  # OCR order is not reading order
    return lines


class StubOcrEngine(OcrEngine):
    """Deterministic OCR engine used instead of PaddleOCR in tests."""

    label = STUB_ENGINE_LABEL

    def __init__(
        self,
        lines: Sequence[OcrLine] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        """Create the stub.

        Args:
            lines: Lines to return; defaults to
                :func:`scanned_invoice_lines`.
            error: Exception to raise instead of returning text, so the caller's
                failure handling can be tested.
        """

        self.lines: list[OcrLine] = list(
            scanned_invoice_lines() if lines is None else lines
        )
        self.error = error
        self.images: list[Any] = []

    def recognise(self, image: Any) -> list[OcrLine]:
        """Record the rendered page image and return the canned lines."""

        self.images.append(image)
        if self.error is not None:
            raise self.error
        return list(self.lines)
