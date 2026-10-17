"""Response schemas for document upload and metadata retrieval (Phases 2-9).

Phase 8 adds the list schemas (``DocumentListItem``/``DocumentListResponse``) that back
``GET /documents``: one row per uploaded document carrying its text-extraction status,
its invoice-extraction status and the latest audit result, so a UI can render the whole
workflow in a single table.

Phase 9 adds the extraction provenance and the quality report: which engine produced a
page's text (``pdfplumber`` or the ``paddleocr`` fallback), how many tables were
recovered and what the extraction quality check had to flag.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.audit import AuditStatus, AuditSummary
from app.models.invoice import InvoiceData, VatClassification

ExtractionStatus = Literal["extracted", "needs_ocr"]
"""Outcome of text extraction.

``extracted`` means at least one page yielded text (from the PDF's own text layer or
from the PaddleOCR fallback). ``needs_ocr`` means no page could be read: the PDF is
scanned or image-only and the OCR fallback recovered no text (or is not installed), so
such documents are marked rather than silently treated as readable.
"""

ExtractionSource = Literal["pdfplumber", "paddleocr"]
"""Engine that produced a page's text: pdfplumber first, PaddleOCR on fallback."""


class ExtractionQualityInfo(BaseModel):
    """Report of the extraction quality check that runs before the OCR fallback."""

    passed: bool = Field(
        ..., description="True when no issue, no missing field and no unreadable page."
    )
    required_fields_found: list[str] = Field(
        default_factory=list, description="Required invoice fields found in the text."
    )
    missing_required_fields: list[str] = Field(
        default_factory=list,
        description="Required invoice fields absent from the text; never guessed.",
    )
    pages_needing_ocr: list[int] = Field(
        default_factory=list,
        description="Pages still without a usable text layer after the OCR fallback.",
    )
    table_pages: list[int] = Field(
        default_factory=list,
        description="Pages from which at least one table was rebuilt.",
    )
    ocr_attempted: bool = Field(
        False, description="True when the OCR fallback actually read a page."
    )
    ocr_pages: list[int] = Field(
        default_factory=list, description="Pages whose text came from the OCR fallback."
    )
    issues: list[str] = Field(
        default_factory=list, description="Human-readable findings of the quality check."
    )
    summary: str = Field("", description="One-line, secret-free summary of the check.")


class PageContent(BaseModel):
    """Extraction summary for a single PDF page (1-based page numbering)."""

    page_number: int = Field(..., description="Page number, starting at 1.")
    character_count: int = Field(..., description="Number of characters extracted from this page.")
    has_text: bool = Field(..., description="False when the page yielded no usable text.")
    preview: str = Field(
        "",
        description="Short, whitespace-collapsed excerpt used for quick inspection.",
    )
    text: str | None = Field(
        None,
        description="Full extracted page text; only returned when include_text=true.",
    )
    source: ExtractionSource = Field(
        "pdfplumber", description="Engine that produced this page's text."
    )
    table_count: int = Field(0, description="Tables recovered from this page.")
    tables: list[list[list[str]]] | None = Field(
        None,
        description=(
            "Recovered table rows (list of tables, each a list of rows of cells); "
            "only returned when include_text=true."
        ),
    )


class DocumentSummary(BaseModel):
    """Response returned after a successful upload."""

    document_no: str
    original_filename: str
    content_type: str | None = None
    size_bytes: int
    uploaded_at: datetime
    page_count: int
    extraction_status: ExtractionStatus
    extraction_error: str | None = None
    pages_with_text: int
    total_characters: int
    used_ocr: bool = Field(
        False, description="True when the PaddleOCR fallback read at least one page."
    )
    ocr_pages: list[int] = Field(
        default_factory=list, description="Pages whose text came from the OCR fallback."
    )
    table_count: int = Field(0, description="Tables recovered across the document.")
    quality: ExtractionQualityInfo | None = Field(
        None, description="Report of the extraction quality check."
    )
    message: str | None = Field(
        None, description="Next-step guidance, including whether auditing can proceed."
    )


class DocumentDetail(BaseModel):
    """Stored document metadata, without the full text unless requested."""

    document_no: str
    original_filename: str
    content_type: str | None = None
    size_bytes: int
    uploaded_at: datetime
    page_count: int
    extraction_status: ExtractionStatus
    extraction_error: str | None = None
    pages_with_text: int
    total_characters: int
    raw_text_included: bool = Field(
        False, description="True only when the full per-page text was requested."
    )
    used_ocr: bool = Field(
        False, description="True when the PaddleOCR fallback read at least one page."
    )
    ocr_pages: list[int] = Field(
        default_factory=list, description="Pages whose text came from the OCR fallback."
    )
    table_count: int = Field(0, description="Tables recovered across the document.")
    quality: ExtractionQualityInfo | None = Field(
        None, description="Report of the extraction quality check."
    )
    pages: list[PageContent] = Field(default_factory=list)
    invoice: InvoiceData | None = Field(
        None,
        description=(
            "Structured invoice fields, present once POST "
            "/documents/{document_no}/extract has run (Phase 3)."
        ),
    )
    notes: list[str] = Field(
        default_factory=list, description="Scope and storage limitations for this document."
    )


class DocumentListItem(BaseModel):
    """One row of ``GET /documents``: the whole workflow state for one document.

    The row is deliberately self-contained so a table can render the file, its text
    extraction, its invoice extraction and its latest audit result without a second
    call. ``audit_*`` fields are null until the document has been audited, and only
    the **latest** report is reflected because the report store keeps one per document.
    """

    document_no: str
    original_filename: str
    content_type: str | None = None
    size_bytes: int
    uploaded_at: datetime
    page_count: int

    extraction_status: ExtractionStatus
    extraction_error: str | None = None
    pages_with_text: int
    total_characters: int

    invoice_extracted: bool = Field(
        False, description="True once POST /documents/{document_no}/extract has run."
    )
    invoice_number: str | None = None
    invoice_total_including_vat: str | None = Field(
        None, description="Invoice total as text, so JSON cannot lose decimal precision."
    )
    vat_classification: VatClassification | None = None

    audited: bool = Field(
        False, description="True when a report is stored for this document."
    )
    audit_status: AuditStatus | None = None
    audit_overall_confidence_percent: float | None = Field(None, ge=0, le=100)
    audit_human_review_required: bool | None = None
    audit_summary: AuditSummary | None = Field(
        None, description="Pass/fail/review counts from the latest audit report."
    )
    audit_ruleset_version: str | None = None
    audited_at: datetime | None = None

    can_extract: bool = Field(
        False, description="True when the document has extractable text (not needs_ocr)."
    )
    can_audit: bool = Field(
        False,
        description=(
            "True when the document can be audited: text was extracted and the "
            "structured invoice fields exist."
        ),
    )


class DocumentListResponse(BaseModel):
    """Response of ``GET /documents``: every stored document, newest upload first."""

    total: int
    documents: list[DocumentListItem] = Field(default_factory=list)
    notes: list[str] = Field(
        default_factory=list, description="Scope and storage limitations for the list."
    )
