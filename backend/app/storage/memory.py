"""In-memory document registry (Phases 2-9).

Documents are held in process memory only: uploaded bytes are never written to
permanent storage, and every record disappears when the process restarts. This
is a deliberate MVP limitation, documented in the README and PROGRESS.md.

Phase 3 adds one optional field, ``invoice``, holding the validated structured
invoice fields once extraction has been run. Phase 9 adds ``extraction_quality``, the
report of the text-extraction quality check (including which pages the PaddleOCR
fallback had to read). Both default to ``None``, so earlier behaviour is unchanged.
"""

from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from uuid import uuid4

from app.models.document import ExtractionStatus
from app.models.invoice import InvoiceData
from app.services.pdf_extractor import ExtractedPage, ExtractedTable, ExtractionQuality

DOCUMENT_NO_PREFIX = "DOC-"


def generate_document_no() -> str:
    """Return a unique document number in the form ``DOC-<UUID hex>``."""

    return f"{DOCUMENT_NO_PREFIX}{uuid4().hex.upper()}"


def normalise_document_no(document_no: str) -> str:
    """Normalise a caller-supplied document number for lookup.

    Lookups are case-insensitive and ignore surrounding whitespace, because the
    number is typed or pasted by hand from Swagger.
    """

    return document_no.strip().upper()


@dataclass
class DocumentRecord:
    """A stored document: metadata plus page-level extracted text."""

    document_no: str
    original_filename: str
    content_type: str | None
    size_bytes: int
    uploaded_at: datetime
    page_count: int
    pages: list[ExtractedPage] = field(default_factory=list)
    extraction_status: ExtractionStatus = "extracted"
    extraction_error: str | None = None
    invoice: InvoiceData | None = None
    extraction_quality: ExtractionQuality | None = None

    @property
    def pages_with_text(self) -> int:
        """Number of stored pages that yielded non-whitespace text."""

        return sum(1 for page in self.pages if page.has_text)

    @property
    def total_characters(self) -> int:
        """Total stored character count across all pages."""

        return sum(page.character_count for page in self.pages)

    @property
    def used_ocr(self) -> bool:
        """True when the PaddleOCR fallback produced the text of at least one page."""

        return any(page.from_ocr for page in self.pages)

    @property
    def ocr_pages(self) -> list[int]:
        """Page numbers whose text came from the OCR fallback."""

        return [page.page_number for page in self.pages if page.from_ocr]

    @property
    def tables(self) -> list[ExtractedTable]:
        """Every table recovered anywhere in the document, in page order."""

        return [table for page in self.pages for table in page.tables]

    @property
    def table_count(self) -> int:
        """Number of tables recovered across the document."""

        return len(self.tables)


class InMemoryDocumentRegistry:
    """Thread-safe, process-local store of uploaded documents."""

    def __init__(self) -> None:
        self._documents: dict[str, DocumentRecord] = {}
        self._lock = Lock()

    def add(self, record: DocumentRecord) -> DocumentRecord:
        """Store `record` under its normalised document number."""

        with self._lock:
            self._documents[normalise_document_no(record.document_no)] = record
        return record

    def get(self, document_no: str) -> DocumentRecord | None:
        """Return the record for `document_no`, or None when unknown."""

        with self._lock:
            return self._documents.get(normalise_document_no(document_no))

    def exists(self, document_no: str) -> bool:
        """Return True when `document_no` is present in the registry."""

        with self._lock:
            return normalise_document_no(document_no) in self._documents

    def set_invoice(
        self, document_no: str, invoice: InvoiceData
    ) -> DocumentRecord | None:
        """Attach structured invoice data to a stored document.

        Returns the updated record, or None when `document_no` is unknown. The
        lookup and the update happen in one lock section, so a concurrent delete
        cannot leave the result orphaned.
        """

        with self._lock:
            record = self._documents.get(normalise_document_no(document_no))
            if record is None:
                return None
            record.invoice = invoice
            return record

    def clear(self) -> None:
        """Remove every record. Used by tests and for manual resets."""

        with self._lock:
            self._documents.clear()

    def list_document_numbers(self) -> list[str]:
        """Return the stored document numbers (unordered)."""

        with self._lock:
            return list(self._documents)

    def list_records(self) -> list[DocumentRecord]:
        """Return every stored record, newest upload first.

        Used by ``GET /documents`` so the UI can show one row per document. The
        list is a snapshot: later uploads do not mutate the returned sequence.
        """

        with self._lock:
            records = list(self._documents.values())
        return sorted(
            records,
            key=lambda record: (record.uploaded_at, record.document_no),
            reverse=True,
        )

    def __len__(self) -> int:
        with self._lock:
            return len(self._documents)


document_registry = InMemoryDocumentRegistry()
