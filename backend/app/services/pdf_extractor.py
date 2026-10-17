"""PDF validation, text/table extraction and the OCR fallback (Phase 9).

The uploaded bytes are validated by content, never by file extension or the
declared content type. Nothing is written to disk: ``pdfplumber`` reads the document
from an in-memory byte stream and the page images needed by OCR are rendered in
memory as well.

Pipeline (the flow the project agreed on):

1. **Content validation** — the ``%PDF-`` signature must be present and
   ``pdfplumber`` must really be able to open and read the document.
2. **pdfplumber** extracts the text and the tables of every page.
3. **Extraction quality check** — the extracted text is checked for the required
   invoice fields (number, date, TRN, taxable amount, VAT amount, total) and for
   tables that could not be rebuilt, and every page whose text layer is too thin to
   be usable is marked as an OCR candidate.
4. **PaddleOCR fallback** — those pages are rendered, read with PaddleOCR (an
   optional dependency, see ``requirements-ocr.txt``) and their layout/table
   structure is recovered from the recognised line geometry. A missing or failing
   engine never fails the upload: the page keeps its pdfplumber text and the reason
   is recorded in the quality report.
5. The resulting :class:`PdfExtraction` (per-page text, recovered tables and the
   quality report) is what the LLM invoice extraction and the VAT audit consume.

Tables are only ever *reported*, never invented: ``pdfplumber`` returns the cells of
ruled tables, and the OCR path rebuilds a grid from line positions.
"""

import contextlib
import io
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import pdfplumber
from pdfminer.pdfdocument import PDFEncryptionError

from app.config import settings
from app.services.ocr_fallback import (
    OcrEngine,
    OcrError,
    get_ocr_engine,
    ocr_lines_to_text,
    recover_table_rows,
)

PDF_SIGNATURE = b"%PDF-"

ExtractionSource = Literal["pdfplumber", "paddleocr"]
"""Which engine produced a page's text: ``pdfplumber`` first, ``paddleocr`` on fallback."""

PDFPLUMBER_SOURCE: ExtractionSource = "pdfplumber"
PADDLEOCR_SOURCE: ExtractionSource = "paddleocr"

ENCRYPTED_MESSAGE = (
    "The uploaded PDF is password protected or encrypted, which is not supported."
)

NEEDS_OCR_ERROR = (
    "No extractable text was found on any page: the PDF is probably scanned or "
    "image-only and the PaddleOCR fallback recovered nothing (or is not installed, "
    "see requirements-ocr.txt). Reliable auditing cannot proceed for this document."
)

OCR_UNAVAILABLE_NOTE = (
    "The optional PaddleOCR fallback is not installed, so the scanned pages could not "
    "be read. Install it with 'pip install -r requirements-ocr.txt' to OCR image-only "
    "invoices."
)

OCR_DISABLED_NOTE = (
    "The OCR fallback is disabled (VAT_AUDIT_OCR_ENABLED=false), so scanned pages were "
    "left without text."
)

INVOICE_FIELD_PATTERNS: dict[str, re.Pattern[str]] = {
    "invoice_number": re.compile(
        r"(?i)\binvoice\s*(?:no|nr|number|num|#)\b|\binv[\s:.\-/#]*[a-z0-9]{2,}"
    ),
    "invoice_date": re.compile(
        r"(?i)\b(?:invoice\s+)?date\b|\b\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}\b"
        r"|\b\d{4}-\d{2}-\d{2}\b"
    ),
    "supplier_trn": re.compile(
        r"(?i)\btrn\b|tax\s*registration\s*(?:number|no)|(?<!\d)\d{15}(?!\d)"
    ),
    "taxable_amount": re.compile(
        r"(?i)sub\s*-?\s*total|taxable\s*(?:amount|value)|net\s*amount"
        r"|amount\s*excluding|amount\s*before\s*vat"
    ),
    "vat_amount": re.compile(r"(?i)\bvat\b[^\n]{0,25}?\d|\b(?:tax|vat)\s*amount\b"),
    "total_amount": re.compile(r"(?i)\bgrand\s+total\b|\btotal\b|\bamount\s+due\b"),
}
"""Signals that prove an invoice field is present in the extracted text.

A signal is deliberately layout-independent: it says "this document mentions a VAT
amount", not "this is the correct VAT amount". Turning text into values stays the
LLM's job (Phase 3) and judging those values stays the VAT rules' job (Phase 4).
"""

REQUIRED_INVOICE_FIELDS: tuple[str, ...] = tuple(INVOICE_FIELD_PATTERNS)
"""Fields a UAE tax invoice must show; a missing one is a quality finding."""

SCAN_IMAGE_COVERAGE_THRESHOLD = 0.6
"""Image coverage above which a page counts as an image-dominated (scanned) page."""

SCAN_TEXT_MULTIPLIER = 10
"""An image-dominated page is an OCR candidate while its text stays below
``ocr_min_page_characters * SCAN_TEXT_MULTIPLIER``, which is what separates a real
scanned invoice (a full-page image plus a scanner stamp) from a page that merely
contains a logo or a signature image next to a normal text layer."""


class PdfValidationError(ValueError):
    """Base class for rejected uploads."""


class UnsupportedFileTypeError(PdfValidationError):
    """The bytes are not a PDF at all (no PDF signature)."""


class CorruptedPdfError(PdfValidationError):
    """The bytes look like a PDF but pdfplumber cannot read them, or they are encrypted."""


@dataclass(frozen=True)
class ExtractedTable:
    """One table recovered from a page, as rows of string cells."""

    page_number: int
    rows: list[list[str]]
    source: ExtractionSource = PDFPLUMBER_SOURCE

    @property
    def row_count(self) -> int:
        """Number of rows in the table."""

        return len(self.rows)

    @property
    def column_count(self) -> int:
        """Number of columns in the widest row."""

        return max((len(row) for row in self.rows), default=0)

    def to_text(self, separator: str = " | ") -> str:
        """Render the grid as text, one row per line."""

        return "\n".join(separator.join(cell for cell in row) for row in self.rows)


@dataclass
class ExtractedPage:
    """Text, recovered tables and provenance for one page of the PDF."""

    page_number: int
    text: str
    tables: list[ExtractedTable] = field(default_factory=list)
    source: ExtractionSource = PDFPLUMBER_SOURCE
    has_ruling_lines: bool = False
    """True when the page draws lines/rectangles, i.e. it may hold a ruled table."""

    image_coverage: float = 0.0
    """Fraction of the page area covered by embedded images (1.0 = full-page scan)."""

    @property
    def character_count(self) -> int:
        """Number of characters extracted from this page."""

        return len(self.text)

    @property
    def has_text(self) -> bool:
        """True when the page yielded non-whitespace text."""

        return bool(self.text.strip())

    @property
    def table_count(self) -> int:
        """Number of tables recovered from this page."""

        return len(self.tables)

    @property
    def from_ocr(self) -> bool:
        """True when this page's text came from the OCR fallback."""

        return self.source == PADDLEOCR_SOURCE


@dataclass
class ExtractionQuality:
    """Result of the extraction quality check that drives the OCR fallback.

    ``pages_needing_ocr`` holds the pages whose text layer is too thin to be usable —
    exactly the pages the fallback is asked to read — and is re-evaluated on the final
    text, so a page that OCR rescued no longer appears there.
    """

    required_fields_found: list[str] = field(default_factory=list)
    missing_required_fields: list[str] = field(default_factory=list)
    pages_needing_ocr: list[int] = field(default_factory=list)
    table_pages: list[int] = field(default_factory=list)
    ocr_attempted: bool = False
    ocr_pages: list[int] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True when every required field, page text and table check passed."""

        return not (
            self.issues or self.missing_required_fields or self.pages_needing_ocr
        )

    def summary(self) -> str:
        """One-line, secret-free summary for responses and logs."""

        found = ", ".join(self.required_fields_found) or "none"
        missing = ", ".join(self.missing_required_fields) or "none"
        ocr = ", ".join(str(page) for page in self.ocr_pages) or "none"
        return (
            f"quality={'passed' if self.passed else 'flagged'} fields_found={found} "
            f"fields_missing={missing} pages_needing_ocr={len(self.pages_needing_ocr)} "
            f"table_pages={len(self.table_pages)} ocr_pages={ocr} "
            f"issues={len(self.issues)}"
        )


@dataclass
class OcrFallbackResult:
    """What the OCR fallback did: whether it ran, which pages it read and what failed."""

    attempted: bool = False
    pages: list[int] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


@dataclass
class PdfExtraction:
    """Result of extracting a PDF: one entry per page, in page order."""

    page_count: int
    pages: list[ExtractedPage] = field(default_factory=list)
    quality: ExtractionQuality = field(default_factory=ExtractionQuality)

    @property
    def pages_with_text(self) -> int:
        """Number of pages that yielded non-whitespace text."""

        return sum(1 for page in self.pages if page.has_text)

    @property
    def total_characters(self) -> int:
        """Total extracted character count across all pages."""

        return sum(page.character_count for page in self.pages)

    @property
    def has_text(self) -> bool:
        """True when at least one page yielded usable text."""

        return self.pages_with_text > 0

    @property
    def tables(self) -> list[ExtractedTable]:
        """Every table recovered anywhere in the document, in page order."""

        return [table for page in self.pages for table in page.tables]

    @property
    def table_count(self) -> int:
        """Number of tables recovered across the document."""

        return len(self.tables)

    @property
    def used_ocr(self) -> bool:
        """True when at least one page was read by the OCR fallback."""

        return any(page.from_ocr for page in self.pages)

    @property
    def ocr_pages(self) -> list[int]:
        """Page numbers whose text came from the OCR fallback."""

        return [page.page_number for page in self.pages if page.from_ocr]


def looks_like_pdf(data: bytes) -> bool:
    """Return True when the PDF signature appears in the leading bytes.

    Files produced by some tools contain a few bytes before ``%PDF-``, so the
    signature is searched in a small leading window rather than only at offset 0.
    """

    return PDF_SIGNATURE in data[: settings.pdf_signature_scan_bytes]


def collapse_whitespace(text: str) -> str:
    """Collapse every whitespace run to a single space and trim the ends."""

    return " ".join(text.split())


def _page_list(pages: Sequence[int]) -> str:
    """Render page numbers for a message, for example ``1, 2 and 5``."""

    numbers = [str(page) for page in pages]
    if len(numbers) == 1:
        return numbers[0]
    return ", ".join(numbers[:-1]) + f" and {numbers[-1]}"


def page_needs_ocr(page: ExtractedPage, *, min_characters: int | None = None) -> bool:
    """True when a page has no usable text layer, so the OCR fallback should read it.

    Two signals are used, both configurable:

    * the page's collapsed text is shorter than ``VAT_AUDIT_OCR_MIN_PAGE_CHARACTERS``
      (a scanned page has no text layer at all, so it collapses to zero characters), or
    * the page is image-dominated (an embedded scan covers most of it) and its text is
      still very short, which catches scans whose text layer holds only a scanner
      stamp or a watermark.

    A page that already carries real text is never an OCR candidate, so a genuine text
    layer is never replaced by a guess.
    """

    threshold = (
        settings.ocr_min_page_characters if min_characters is None else min_characters
    )
    length = len(collapse_whitespace(page.text))
    if length < threshold:
        return True
    image_dominated = page.image_coverage >= SCAN_IMAGE_COVERAGE_THRESHOLD
    return image_dominated and length < threshold * SCAN_TEXT_MULTIPLIER


def build_extraction_quality(
    pages: Sequence[ExtractedPage],
    *,
    ocr_attempted: bool = False,
    ocr_pages: Sequence[int] = (),
    extra_issues: Sequence[str] = (),
    min_page_characters: int | None = None,
) -> ExtractionQuality:
    """Run the extraction quality check on the final page text.

    Checks, in the order the flow asks for them:

    * which required invoice fields the text mentions (and which are missing),
    * which pages still have no usable text layer after the OCR fallback,
    * whether a page with ruling lines produced a table (table completeness).

    ``extra_issues`` carries the OCR fallback's own notes, so the caller does not have
    to merge two reports. Missing fields are reported, never guessed: a document that
    mentions a VAT amount but not a total is flagged, not filled in.
    """

    threshold = (
        settings.ocr_min_page_characters if min_page_characters is None else min_page_characters
    )
    combined = "\n".join(page.text for page in pages)
    found = [
        name for name, pattern in INVOICE_FIELD_PATTERNS.items() if pattern.search(combined)
    ]
    missing = [name for name in REQUIRED_INVOICE_FIELDS if name not in found]
    needing_ocr = [
        page.page_number
        for page in pages
        if page_needs_ocr(page, min_characters=threshold)
    ]
    table_pages = [page.page_number for page in pages if page.tables]

    issues = list(extra_issues)
    if needing_ocr:
        issues.append(
            f"Page(s) {_page_list(needing_ocr)} have no usable text layer "
            "(OCR candidates)."
        )
    if missing:
        issues.append(
            "Required invoice fields were not found in the extracted text: "
            + ", ".join(missing)
            + "."
        )
    incomplete_tables = [
        page.page_number for page in pages if page.has_ruling_lines and not page.tables
    ]
    if incomplete_tables:
        issues.append(
            f"Ruling lines were found on page(s) {_page_list(incomplete_tables)} but no "
            "table could be reconstructed, so the table structure may be incomplete."
        )

    return ExtractionQuality(
        required_fields_found=found,
        missing_required_fields=missing,
        pages_needing_ocr=needing_ocr,
        table_pages=table_pages,
        ocr_attempted=ocr_attempted,
        ocr_pages=list(ocr_pages),
        issues=issues,
    )


def _clean_cell(cell: Any) -> str:
    """Normalise one table cell: ``None`` becomes empty and whitespace is collapsed."""

    if cell is None:
        return ""
    return collapse_whitespace(str(cell))


def _has_ruling_lines(page: Any) -> bool:
    """True when the page draws lines/rectangles/curves, so it may hold a ruled table.

    pdfplumber builds table edges from all three object types (a stroked rectangle often
    arrives as four curves), so all of them count.
    """

    try:
        return any(
            bool(getattr(page, attribute, None))
            for attribute in ("lines", "rects", "curves")
        )
    except Exception:  # noqa: BLE001 - defensive: pdfplumber internals vary
        return False


def extract_page_tables(page: Any, page_number: int) -> list[ExtractedTable]:
    """Return the tables pdfplumber could rebuild on one page.

    A table that cannot be parsed is dropped (and reported by the quality check through
    ``has_ruling_lines``) instead of failing the whole page, so a bad table never costs
    the invoice its text.
    """

    try:
        raw_tables = page.extract_tables()
    except Exception:  # noqa: BLE001 - a bad table must not cost the page its text
        return []

    tables: list[ExtractedTable] = []
    for raw_rows in raw_tables or []:
        rows = [[_clean_cell(cell) for cell in row or []] for row in raw_rows or []]
        rows = [row for row in rows if any(cell for cell in row)]
        if len(rows) < 2 or max(len(row) for row in rows) < 2:
            continue
        tables.append(ExtractedTable(page_number=page_number, rows=rows))
    return tables


def _bounds_from_mapping(image: Any) -> tuple[float, float] | None:
    """Width/height of a mapping-style image (``x0``/``x1``/``top``/``bottom``)."""

    try:
        return (
            abs(float(image["x1"]) - float(image["x0"])),
            abs(float(image["bottom"]) - float(image["top"])),
        )
    except Exception:  # noqa: BLE001 - not a mapping-style image
        return None


def _bounds_from_attributes(image: Any) -> tuple[float, float] | None:
    """Width/height of an attribute-style image (``width``/``height``)."""

    try:
        return abs(float(image.width)), abs(float(image.height))
    except Exception:  # noqa: BLE001 - an unreadable image contributes no area
        return None


def _image_bounds(image: Any) -> tuple[float, float] | None:
    """Return the (width, height) of an embedded image, or None when unreadable.

    pdfplumber exposes an image either as a mapping or as an object with ``width`` and
    ``height``, depending on the version, so both spellings are attempted.
    """

    return _bounds_from_mapping(image) or _bounds_from_attributes(image)


def page_image_coverage(page: Any) -> float:
    """Fraction of the page area covered by embedded images (1.0 = a full-page scan).

    Scanned invoices embed one image covering the whole page. The value is only used to
    decide whether a page is a scan; it is clamped to ``1.0``.
    """

    try:
        images = list(getattr(page, "images", None) or [])
        page_width = float(page.width)
        page_height = float(page.height)
    except Exception:  # noqa: BLE001 - defensive: pdfplumber internals vary
        return 0.0

    if not images or page_width <= 0 or page_height <= 0:
        return 0.0

    covered = 0.0
    for image in images:
        bounds = _image_bounds(image)
        if bounds is None:
            continue
        covered += max(bounds[0], 0.0) * max(bounds[1], 0.0)
    return min(covered / (page_width * page_height), 1.0)


def extract_page(page: Any, index: int) -> ExtractedPage:
    """Read one pdfplumber page: its text plus every table that could be rebuilt."""

    try:
        page_number = int(getattr(page, "page_number", index + 1) or index + 1)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        page_number = index + 1

    try:
        text = page.extract_text() or ""
    except Exception as exc:
        raise CorruptedPdfError(
            f"Text extraction failed on page {page_number}: {exc}"
        ) from exc

    return ExtractedPage(
        page_number=page_number,
        text=text,
        tables=extract_page_tables(page, page_number),
        source=PDFPLUMBER_SOURCE,
        has_ruling_lines=_has_ruling_lines(page),
        image_coverage=page_image_coverage(page),
    )


def render_page_image(page: Any, *, resolution: int | None = None) -> Any:
    """Render one page to an in-memory RGB image (nothing is written to disk)."""

    dpi = settings.ocr_dpi if resolution is None else resolution
    return page.to_image(resolution=dpi).original


def _ocr_page(
    document: Any, page: ExtractedPage, engine: OcrEngine
) -> tuple[str, list[list[str]]]:
    """OCR one page and return its text plus any recovered table grid."""

    image = render_page_image(document.pages[page.page_number - 1])
    lines = engine.recognise(image)
    text = ocr_lines_to_text(lines)
    rows = recover_table_rows(lines) if text.strip() else []
    return text, rows


def apply_ocr_fallback(
    document: Any,
    pages: list[ExtractedPage],
    *,
    candidates: Sequence[int],
    engine: OcrEngine | None = None,
) -> OcrFallbackResult:
    """Read the pages whose text layer is unusable with the PaddleOCR fallback.

    The fallback never raises: a disabled or missing engine, a page that cannot be
    rendered and an engine error are all recorded as issues, and the page keeps the
    ``pdfplumber`` text. That is what keeps a scanned upload working (it simply ends up
    ``needs_ocr``) instead of failing with a 500.

    Args:
        document: The open pdfplumber document (used to render page images).
        pages: Pages extracted so far; pages read by OCR are updated in place.
        candidates: Page numbers the quality check marked as needing OCR.
        engine: OCR engine to use; resolved through :func:`get_ocr_engine` when omitted.
    """

    result = OcrFallbackResult()
    if not candidates:
        return result

    if not settings.ocr_enabled:
        result.issues.append(
            f"{OCR_DISABLED_NOTE} Affected page(s): {_page_list(candidates)}."
        )
        return result

    limited = list(candidates)[: settings.ocr_max_pages]
    if len(candidates) > len(limited):
        result.issues.append(
            f"OCR ran on the first {len(limited)} of {len(candidates)} pages that need "
            "it (VAT_AUDIT_OCR_MAX_PAGES)."
        )

    active_engine = engine if engine is not None else get_ocr_engine()
    if active_engine is None:
        result.issues.append(OCR_UNAVAILABLE_NOTE)
        return result

    result.attempted = True
    for page_number in limited:
        page = next((item for item in pages if item.page_number == page_number), None)
        if page is None:  # pragma: no cover - candidates always come from `pages`
            continue
        try:
            text, rows = _ocr_page(document, page, active_engine)
        except OcrError as exc:
            result.issues.append(
                f"The OCR fallback could not read page {page_number}: {exc}"
            )
            continue
        except Exception as exc:  # noqa: BLE001 - rendering depends on the PDF internals
            result.issues.append(
                f"Page {page_number} could not be prepared for OCR "
                f"({type(exc).__name__}: {exc})."
            )
            continue

        if not text.strip():
            result.issues.append(
                f"The OCR fallback found no text on page {page_number}, so that page "
                "remains unreadable."
            )
            continue

        existing = collapse_whitespace(page.text)
        if existing and len(collapse_whitespace(text)) <= len(existing):
            # OCR is only ever an improvement: a page that already had a real text
            # layer keeps it when OCR finds nothing more.
            result.issues.append(
                f"The OCR fallback read less text on page {page_number} than the "
                "existing text layer, so the original text was kept."
            )
            continue

        page.text = text
        page.source = PADDLEOCR_SOURCE
        if rows:
            page.tables = [
                ExtractedTable(
                    page_number=page_number, rows=rows, source=PADDLEOCR_SOURCE
                )
            ]
        result.pages.append(page_number)

    return result


def _is_encryption_failure(exc: BaseException) -> bool:
    """True when an exception (or the one it wraps) means the PDF is encrypted.

    pdfplumber wraps pdfminer errors in its own ``PdfminerException``, so the original
    ``PDFPasswordIncorrect`` is only reachable through the exception chain.
    """

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, PDFEncryptionError):
            return True
        current = current.__cause__ or current.__context__
    return False


def _corrupted_error(exc: BaseException, action: str) -> CorruptedPdfError:
    """Build the typed error for a document that could not be opened or read."""

    detail = str(exc).strip()
    message = f"The uploaded PDF could not be {action} and appears to be corrupted."
    if detail:
        message = (
            f"The uploaded PDF could not be {action} and appears to be corrupted: "
            f"{type(exc).__name__}: {detail}"
        )
    return CorruptedPdfError(message)


def _open_document(data: bytes) -> Any:
    """Open the in-memory PDF with pdfplumber, mapping failures to typed errors."""

    try:
        return pdfplumber.open(io.BytesIO(data))
    except Exception as exc:
        if _is_encryption_failure(exc):
            raise CorruptedPdfError(ENCRYPTED_MESSAGE) from exc
        raise _corrupted_error(exc, "opened") from exc


def _read_pages(document: Any) -> list[ExtractedPage]:
    """Read every page with pdfplumber, rejecting documents with no readable page."""

    try:
        pdf_pages = list(document.pages)
    except Exception as exc:
        if _is_encryption_failure(exc):
            raise CorruptedPdfError(ENCRYPTED_MESSAGE) from exc
        raise _corrupted_error(exc, "read") from exc

    if not pdf_pages:
        raise CorruptedPdfError(
            "The uploaded PDF contains no readable pages and appears to be corrupted."
        )
    return [extract_page(page, index) for index, page in enumerate(pdf_pages)]


def _close_document(document: Any) -> None:
    """Close the pdfplumber document; a failed close must not fail the upload."""

    with contextlib.suppress(Exception):  # closing is best-effort
        document.close()


def extract_pdf_text(
    data: bytes, *, ocr_engine: OcrEngine | None = None
) -> PdfExtraction:
    """Validate `data` as a PDF and run the full extraction pipeline.

    Steps: ``pdfplumber`` text/table extraction, the extraction quality check and the
    PaddleOCR fallback for pages with no usable text layer. The quality report is
    returned alongside the pages so the caller can surface what was (and was not)
    readable.

    Args:
        data: The uploaded bytes; nothing is written to disk.
        ocr_engine: OCR fallback engine. Omitted (``None``) means "use the configured
            PaddleOCR engine if it is installed", which lets tests inject a stub and
            keeps deployments without the optional OCR dependency working.

    Raises:
        UnsupportedFileTypeError: the bytes have no PDF signature.
        CorruptedPdfError: the signature is present but the document is empty, cannot
            be opened (corrupted) or requires a password.
    """

    if not data:
        raise CorruptedPdfError("The uploaded file is empty.")

    if not looks_like_pdf(data):
        raise UnsupportedFileTypeError(
            "The uploaded file is not a PDF (the PDF '%PDF-' signature was not found). "
            "Only PDF invoices are supported."
        )

    document = _open_document(data)
    try:
        pages = _read_pages(document)
        candidates = [
            page.page_number
            for page in pages
            if page_needs_ocr(page)
        ]
        fallback = apply_ocr_fallback(
            document, pages, candidates=candidates, engine=ocr_engine
        )
    finally:
        _close_document(document)

    quality = build_extraction_quality(
        pages,
        ocr_attempted=fallback.attempted,
        ocr_pages=fallback.pages,
        extra_issues=fallback.issues,
    )
    return PdfExtraction(page_count=len(pages), pages=pages, quality=quality)
