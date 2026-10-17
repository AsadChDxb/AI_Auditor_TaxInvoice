"""Document upload, metadata and invoice-extraction endpoints (Phases 2-9).

Routes stay thin: PDF validation, text/table extraction, the extraction quality check
and the PaddleOCR fallback live in ``app.services.pdf_extractor`` and
``app.services.ocr_fallback``, LLM extraction lives in
``app.services.invoice_extractor`` and the provider-agnostic ``app.services.llm``
layer, storage lives in ``app.storage.memory``, the VAT ruleset in
``app.audit.rules_config`` and the audit engine in ``app.audit.ai_auditor``
(``POST /audit/{document_no}``, see ``app.api.audits``).
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status

from app.audit.ai_auditor import report_store
from app.config import settings
from app.models.audit import AuditReport
from app.models.document import (
    DocumentDetail,
    DocumentListItem,
    DocumentListResponse,
    DocumentSummary,
    ExtractionQualityInfo,
    PageContent,
)
from app.models.invoice import InvoiceData, InvoiceExtractionResponse
from app.services.invoice_extractor import (
    DocumentNotExtractableError,
    InvoiceExtractionError,
    InvoiceExtractionService,
)
from app.services.llm_errors import (
    LLMConfigurationError,
    LLMInvalidResponseError,
    LLMProviderError,
    LLMTimeoutError,
)
from app.services.ocr_fallback import OcrEngine, get_ocr_engine
from app.services.pdf_extractor import (
    NEEDS_OCR_ERROR,
    CorruptedPdfError,
    ExtractionQuality,
    PdfValidationError,
    extract_pdf_text,
)
from app.storage.memory import DocumentRecord, document_registry, generate_document_no

router = APIRouter(prefix="/documents", tags=["Documents"])

PREVIEW_CHARACTERS = 80
MEMORY_NOTE = (
    "Extracted text is kept in memory only and is lost when the service restarts."
)
LIST_MEMORY_NOTE = (
    "Documents and audit reports are held in memory only and are lost when the service "
    "restarts, so a document number from an earlier run no longer resolves."
)
LIST_WORKFLOW_NOTE = (
    "Row status follows the workflow: upload (text extraction), then POST "
    "/documents/{document_no}/extract (structured invoice fields), then POST "
    "/audit/{document_no} (VAT audit report). Only the latest report per document is "
    "kept, so the audit columns always describe the most recent run."
)
LIST_MESSAGE = (
    "One row per document, newest upload first. A document must have its text "
    "extracted and its invoice fields extracted before it can be audited; a document "
    "that needs OCR cannot be audited at all."
)
EXTRACTED_MESSAGE = (
    "Text extracted successfully. Continue with POST /documents/{document_no}/extract "
    "to extract structured invoice fields, then POST /audit/{document_no} to audit the "
    "invoice, or GET /documents/{document_no} to inspect extraction metadata."
)
NEEDS_OCR_MESSAGE = (
    "No extractable text was found on any page, so this document is flagged as needing "
    "OCR. The PDF is probably scanned or image-only and the PaddleOCR fallback (an "
    "optional dependency, see requirements-ocr.txt) recovered no text; auditing cannot "
    "proceed reliably."
)
OCR_FALLBACK_NOTE = (
    "Page(s) {pages} had no usable text layer, so the PaddleOCR fallback read them and "
    "recovered their layout/table structure. OCR text is a best-effort reading of the "
    "image and must be verified against the source invoice."
)
QUALITY_FLAGGED_NOTE = (
    "The extraction quality check flagged this document: {issues} Fields that were not "
    "found are reported, never guessed."
)
NOT_EXTRACTED_NOTE = (
    "Structured invoice fields have not been extracted yet; call POST "
    "/documents/{document_no}/extract to run LLM extraction."
)
INVOICE_PRESENT_NOTE = (
    "Structured invoice fields were produced by an LLM and must be verified against "
    "the source invoice before they are relied on."
)
AUDIT_NOTE = (
    "POST /audit/{document_no} evaluates this document against the enabled VAT rules "
    "and returns findings with confidence and human-review flags."
)
EXTRACTION_MESSAGE = (
    "Structured invoice fields extracted with the configured LLM. Review the warnings "
    "and the per-field evidence before relying on the values."
)
REUSED_EXTRACTION_MESSAGE = (
    "An existing extraction was returned. Pass force=true to call the provider again."
)



def _preview(text: str) -> str:
    """Return a whitespace-collapsed, length-capped excerpt of `text`."""

    collapsed = " ".join(text.split())
    if len(collapsed) <= PREVIEW_CHARACTERS:
        return collapsed
    return collapsed[:PREVIEW_CHARACTERS].rstrip() + "..."


async def _read_upload_within_limit(upload: UploadFile) -> bytes:
    """Stream the upload into memory, rejecting anything over the configured limit."""

    limit = settings.max_upload_size_bytes
    buffer = bytearray()

    while True:
        chunk = await upload.read(settings.read_chunk_size_bytes)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > limit:
            raise HTTPException(
                # 413 Request Entity Too Large (named HTTP_413_CONTENT_TOO_LARGE in
                # newer Starlette releases); the literal avoids the deprecated alias.
                status_code=413,
                detail=(
                    "The uploaded file exceeds the maximum allowed size of "
                    f"{settings.max_upload_size_mb} MB ({limit} bytes)."
                ),
            )

    return bytes(buffer)


async def extract_document_record(
    file: UploadFile,
    ocr_engine: OcrEngine | None,
    *,
    document_no: str | None = None,
) -> tuple[DocumentRecord, str]:
    """Read, validate and extract one uploaded PDF, then register the record.

    Shared by ``POST /documents/upload`` (text extraction only) and
    ``POST /analysis`` (the stateless single-request workflow), so both endpoints
    accept exactly the same files and build exactly the same record. Supplying
    `document_no` reuses an existing row, which lets a client re-extract or re-audit
    a document it already holds without creating a duplicate.

    Returns the stored record and the user-facing message describing the
    text-extraction outcome. HTTP errors match the upload route: 400 for an empty or
    corrupted PDF, 413 for an oversized upload and 415 for non-PDF content.
    """

    content_type = file.content_type
    filename = file.filename or "unnamed"
    data = await _read_upload_within_limit(file)
    await file.close()

    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The uploaded file is empty.",
        )

    try:
        extraction = extract_pdf_text(data, ocr_engine=ocr_engine)
    except PdfValidationError as exc:
        error_code = (
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
            if not isinstance(exc, CorruptedPdfError)
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=error_code, detail=str(exc)) from exc

    if extraction.has_text:
        extraction_status = "extracted"
        extraction_error: str | None = None
        message = EXTRACTED_MESSAGE
        if extraction.used_ocr:
            message = (
                OCR_FALLBACK_NOTE.format(
                    pages=", ".join(str(page) for page in extraction.ocr_pages)
                )
                + " "
                + message
            )
    else:
        extraction_status = "needs_ocr"
        extraction_error = NEEDS_OCR_ERROR
        message = NEEDS_OCR_MESSAGE

    record = document_registry.add(
        DocumentRecord(
            document_no=document_no or generate_document_no(),
            original_filename=filename,
            content_type=content_type,
            size_bytes=len(data),
            uploaded_at=datetime.now(timezone.utc),
            page_count=extraction.page_count,
            pages=extraction.pages,
            extraction_status=extraction_status,
            extraction_error=extraction_error,
            extraction_quality=extraction.quality,
        )
    )

    return record, message


def get_invoice_extraction_service() -> InvoiceExtractionService:
    """Provide the invoice extraction service.

    Declared as a FastAPI dependency so tests can replace it with a service backed
    by a scripted chat model, keeping the suite offline.
    """

    return InvoiceExtractionService()


def get_document_ocr_engine() -> OcrEngine | None:
    """Provide the OCR fallback engine used by the upload route.

    Returns ``None`` when PaddleOCR is not installed, which is a supported deployment:
    scanned pages are then simply reported as ``needs_ocr``. Declared as a FastAPI
    dependency so tests can inject a stub engine instead of installing PaddleOCR.
    """

    return get_ocr_engine()


def _quality_info(quality: ExtractionQuality | None) -> ExtractionQualityInfo | None:
    """Convert the extractor's quality report into its response schema."""

    if quality is None:
        return None
    return ExtractionQualityInfo(
        passed=quality.passed,
        required_fields_found=list(quality.required_fields_found),
        missing_required_fields=list(quality.missing_required_fields),
        pages_needing_ocr=list(quality.pages_needing_ocr),
        table_pages=list(quality.table_pages),
        ocr_attempted=quality.ocr_attempted,
        ocr_pages=list(quality.ocr_pages),
        issues=list(quality.issues),
        summary=quality.summary(),
    )


def _quality_notes(record: DocumentRecord) -> list[str]:
    """Notes describing the OCR fallback and any quality finding, for the detail view."""

    notes: list[str] = []
    if record.ocr_pages:
        notes.append(
            OCR_FALLBACK_NOTE.format(pages=", ".join(str(page) for page in record.ocr_pages))
        )
    quality = record.extraction_quality
    if quality is not None and quality.issues:
        notes.append(
            QUALITY_FLAGGED_NOTE.format(issues=" ".join(quality.issues))
        )
    return notes


def _require_document(document_no: str) -> DocumentRecord:
    """Return the stored document, or raise 404 when the number is unknown."""

    record = document_registry.get(document_no)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Document '{document_no}' was not found. Documents are held in "
                "memory only, so records are lost when the service restarts."
            ),
        )
    return record


def to_document_detail(record: DocumentRecord, include_text: bool) -> DocumentDetail:
    """Build the GET response for `record`, omitting full text unless requested."""

    pages = [
        PageContent(
            page_number=page.page_number,
            character_count=page.character_count,
            has_text=page.has_text,
            preview=_preview(page.text),
            text=page.text if include_text else None,
            source=page.source,
            table_count=page.table_count,
            tables=(
                [table.rows for table in page.tables] if include_text else None
            ),
        )
        for page in record.pages
    ]

    notes = [MEMORY_NOTE]
    if record.extraction_status == "needs_ocr":
        notes.append(NEEDS_OCR_ERROR)
    notes.extend(_quality_notes(record))
    if record.invoice is not None:
        notes.append(INVOICE_PRESENT_NOTE)
        notes.append(AUDIT_NOTE)
    elif record.extraction_status == "extracted":
        notes.append(NOT_EXTRACTED_NOTE)

    return DocumentDetail(
        document_no=record.document_no,
        original_filename=record.original_filename,
        content_type=record.content_type,
        size_bytes=record.size_bytes,
        uploaded_at=record.uploaded_at,
        page_count=record.page_count,
        extraction_status=record.extraction_status,
        extraction_error=record.extraction_error,
        pages_with_text=record.pages_with_text,
        total_characters=record.total_characters,
        raw_text_included=include_text,
        used_ocr=record.used_ocr,
        ocr_pages=record.ocr_pages,
        table_count=record.table_count,
        quality=_quality_info(record.extraction_quality),
        pages=pages,
        invoice=record.invoice,
        notes=notes,
    )


def to_list_item(
    record: DocumentRecord, report: AuditReport | None
) -> DocumentListItem:
    """Build one `GET /documents` row from a stored record and its latest report."""

    invoice = record.invoice
    has_text = record.extraction_status == "extracted" and any(
        page.text.strip() for page in record.pages
    )

    return DocumentListItem(
        document_no=record.document_no,
        original_filename=record.original_filename,
        content_type=record.content_type,
        size_bytes=record.size_bytes,
        uploaded_at=record.uploaded_at,
        page_count=record.page_count,
        extraction_status=record.extraction_status,
        extraction_error=record.extraction_error,
        pages_with_text=record.pages_with_text,
        total_characters=record.total_characters,
        invoice_extracted=invoice is not None,
        invoice_number=invoice.invoice_number if invoice is not None else None,
        invoice_total_including_vat=(
            None
            if invoice is None or invoice.total_including_vat is None
            else str(invoice.total_including_vat)
        ),
        vat_classification=invoice.vat_classification if invoice is not None else None,
        audited=report is not None,
        audit_status=report.audit_status if report is not None else None,
        audit_overall_confidence_percent=(
            report.overall_confidence_percent if report is not None else None
        ),
        audit_human_review_required=(
            report.human_review_required if report is not None else None
        ),
        audit_summary=report.summary if report is not None else None,
        audit_ruleset_version=report.ruleset_version if report is not None else None,
        audited_at=report.audited_at if report is not None else None,
        can_extract=has_text,
        can_audit=has_text and invoice is not None,
    )


def to_summary(record: DocumentRecord, message: str) -> DocumentSummary:
    """Build the ``POST /documents/upload`` response for a stored record."""

    return DocumentSummary(
        document_no=record.document_no,
        original_filename=record.original_filename,
        content_type=record.content_type,
        size_bytes=record.size_bytes,
        uploaded_at=record.uploaded_at,
        page_count=record.page_count,
        extraction_status=record.extraction_status,
        extraction_error=record.extraction_error,
        pages_with_text=record.pages_with_text,
        total_characters=record.total_characters,
        used_ocr=record.used_ocr,
        ocr_pages=record.ocr_pages,
        table_count=record.table_count,
        quality=_quality_info(record.extraction_quality),
        message=message,
    )


def run_invoice_extraction(
    service: InvoiceExtractionService, record: DocumentRecord
) -> InvoiceData:
    """Extract `record`'s structured fields, mapping failures onto HTTP errors.

    Shared by ``POST /documents/{document_no}/extract`` (stored document) and
    ``POST /analysis`` (self-contained request), so both report 409 for a document
    that needs OCR, 503 for missing LLM configuration, 504 for a provider timeout and
    502 for any other provider or validation failure.
    """

    try:
        return service.extract(record)
    except DocumentNotExtractableError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except LLMConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except LLMTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc)
        ) from exc
    except (
        LLMProviderError,
        LLMInvalidResponseError,
        InvoiceExtractionError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc


@router.post(
    "/upload",
    response_model=DocumentSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Upload an invoice PDF and extract its text",
    description=(
        "Accepts one PDF as multipart/form-data and validates it by content (PDF "
        "signature plus a real pdfplumber read), never by filename or declared "
        "content type. Text and tables are extracted page by page in memory, an "
        "extraction quality check looks for the required invoice fields and for "
        "tables that could not be rebuilt, and pages without a usable text layer "
        "are read with the PaddleOCR fallback when it is installed. Scanned or "
        "image-only PDFs whose pages cannot be read end up with "
        "extraction_status='needs_ocr' instead of being "
        "treated as readable.\n\n"
        "The response reports what happened (`used_ocr`, `ocr_pages`, `table_count` "
        "and the `quality` report).\n\n"
        "Full workflow: this call, then POST /documents/{document_no}/extract for the "
        "structured invoice fields, then POST /audit/{document_no} for the VAT audit "
        "report. The returned `message` states whether auditing can proceed."
    ),
    responses={
        400: {"description": "Empty or corrupted/encrypted PDF."},
        413: {"description": "Upload exceeds the configured maximum size."},
        415: {"description": "Uploaded content is not a PDF."},
    },
)
async def upload_document(
    file: UploadFile = File(..., description="A text-based or scanned invoice PDF."),
    ocr_engine: OcrEngine | None = Depends(get_document_ocr_engine),
) -> DocumentSummary:
    """Validate the upload, extract its text, store it, and return a summary."""

    record, message = await extract_document_record(file, ocr_engine)
    return to_summary(record, message)


@router.get(
    "",
    response_model=DocumentListResponse,
    summary="List every uploaded document with its extraction and audit status",
    description=(
        "Returns one row per stored document, newest upload first, so a UI can show the "
        "whole workflow (upload -> text extraction -> structured invoice fields -> VAT "
        "audit) in a single table. Each row carries the text-extraction status, whether "
        "the structured invoice fields exist, and the latest audit status, confidence "
        "and pass/fail counts, plus `can_extract`/`can_audit` flags so the client does "
        "not have to re-derive which action is allowed. Reading the list never calls the "
        "LLM.\\n\\n"
        "Documents and reports live in memory only, so the list is empty after a "
        "restart and only the latest report per document number is reflected."
    ),
)
def list_documents() -> DocumentListResponse:
    """Return one row per stored document, newest upload first."""

    items = [
        to_list_item(record, report_store.get(record.document_no))
        for record in document_registry.list_records()
    ]

    return DocumentListResponse(
        total=len(items),
        documents=items,
        notes=[LIST_MEMORY_NOTE, LIST_WORKFLOW_NOTE, LIST_MESSAGE],
    )


@router.get(
    "/{document_no}",
    response_model=DocumentDetail,
    summary="Retrieve document metadata, extraction status and invoice fields",
    description=(
        "Returns stored metadata, per-page extraction summaries (including which engine "
        "produced each page's text and how many tables were recovered) and the "
        "`quality` report of the extraction quality check. The full extracted text and "
        "the recovered table rows are withheld by default and returned only when "
        "include_text=true is supplied. Once POST "
        "/documents/{document_no}/extract has run, the structured `invoice` block "
        "(fields, per-field evidence and warnings) is included as well, and POST "
        "/audit/{document_no} can be called to audit those fields against the enabled "
        "VAT rules."
    ),
    responses={404: {"description": "Unknown document number."}},
)
def get_document(
    document_no: str,
    include_text: bool = Query(
        False,
        description="When true, include the full extracted text of every page.",
    ),
) -> DocumentDetail:
    """Return metadata for a stored document, or 404 when it is unknown."""

    return to_document_detail(_require_document(document_no), include_text)


@router.post(
    "/{document_no}/extract",
    response_model=InvoiceExtractionResponse,
    summary="Extract structured invoice fields with the configured LLM",
    description=(
        "Sends the page-level text already extracted by Phase 2 to the configured "
        "LLM (any OpenAI-compatible provider selected through .env) and "
        "stores the validated structured invoice fields (invoice number, date, "
        "supplier/customer details and TRNs, currency, amounts, VAT rate and "
        "classification) together with per-field evidence, confidences and "
        "extraction warnings. Missing or unreadable values are returned as null "
        "rather than guessed. No VAT audit rules are applied at this stage."
    ),
    responses={
        404: {"description": "Unknown document number."},
        409: {"description": "The document has no extractable text and needs OCR."},
        502: {"description": "The provider failed, timed out or returned unusable data."},
        503: {"description": "The LLM is not configured (LLM_API_KEY / LLM_MODEL)."},
    },
)
def extract_invoice_fields(
    document_no: str,
    force: bool = Query(
        False,
        description=(
            "Call the provider again even when an extraction is already stored for "
            "this document."
        ),
    ),
    service: InvoiceExtractionService = Depends(get_invoice_extraction_service),
) -> InvoiceExtractionResponse:
    """Extract and store structured invoice fields for a stored document."""

    record = _require_document(document_no)

    if record.invoice is not None and not force:
        return InvoiceExtractionResponse(
            document_no=record.document_no,
            reused_existing=True,
            invoice=record.invoice,
            message=REUSED_EXTRACTION_MESSAGE,
        )

    invoice = run_invoice_extraction(service, record)

    document_registry.set_invoice(record.document_no, invoice)

    return InvoiceExtractionResponse(
        document_no=record.document_no,
        reused_existing=False,
        invoice=invoice,
        message=EXTRACTION_MESSAGE,
    )
