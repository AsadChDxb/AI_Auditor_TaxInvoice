"""Stateless, single-request analysis endpoint (serverless-friendly workflow).

``POST /documents/upload`` -> ``POST /documents/{document_no}/extract`` ->
``POST /audit/{document_no}`` resolves a document number through
:mod:`app.storage.memory`, which is process-local. On a serverless platform (Vercel,
Lambda, Cloud Run) or any multi-worker deployment, the follow-up request can be served
by an instance that never saw the upload, so it answers 404 with
"Document 'DOC-...' was not found". That is exactly what happens on Vercel, where every
request may start a fresh function instance.

``POST /analysis`` removes the dependency: the PDF is sent with every call, the whole
pipeline (text extraction -> structured invoice extraction -> VAT audit) runs inside
that one request, and the response is self-contained. The client keeps the payload and
sends the same file back for a re-extraction or a re-audit, passing the document number
so the row keeps its identity. Nothing depends on another request's memory.

The stored registry is still written to as a best-effort cache, so the classic endpoints
keep working unchanged on a single long-running instance (local development, a VM).
"""

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from pydantic import ValidationError

from app.api.audits import AUDIT_MESSAGE, get_ai_auditor
from app.api.documents import (
    MEMORY_NOTE,
    extract_document_record,
    get_document_ocr_engine,
    get_invoice_extraction_service,
    run_invoice_extraction,
    to_document_detail,
    to_list_item,
)
from app.audit.ai_auditor import AiAuditor, DocumentNotAuditableError
from app.models.analysis import AnalysisResponse
from app.models.document import DocumentDetail
from app.models.invoice import InvoiceData
from app.services.invoice_extractor import InvoiceExtractionService
from app.services.llm_errors import LLMConfigurationError
from app.services.ocr_fallback import OcrEngine
from app.storage.memory import (
    DOCUMENT_NO_PREFIX,
    DocumentRecord,
    document_registry,
    generate_document_no,
    normalise_document_no,
)

router = APIRouter(prefix="/analysis", tags=["Analysis"])

ANALYSIS_MESSAGE = (
    "The document was read, its structured invoice fields were extracted with the "
    "configured LLM and the VAT audit ran, all in this one response. Every request "
    "must carry the payload it needs, so keep this JSON and send the PDF again to "
    "re-extract or re-audit."
)
EXTRACTION_ONLY_MESSAGE = (
    "The document was read and its structured invoice fields were extracted. No audit "
    "ran because run_audit=false; POST /analysis again with the same file (and these "
    "invoice fields) to audit them."
)
NEEDS_OCR_MESSAGE = (
    "The PDF was accepted, but no page yielded usable text even after the OCR "
    "fallback, so it is flagged as needing OCR and no invoice fields were extracted. "
    "The document is still returned so the client can show it."
)
STATELESS_NOTE = (
    "This response is self-contained: the API does not depend on the document still "
    "being in another instance's memory. Send the PDF (and the invoice fields) again "
    "to re-extract or re-audit."
)
CLIENT_INVOICE_NOTE = (
    "The structured invoice fields were supplied by the client instead of being "
    "extracted again in this request; verify them against the source PDF."
)
MAX_DOCUMENT_NO_LENGTH = 64


def analysis_detail(record: DocumentRecord) -> DocumentDetail:
    """Build the document block of an analysis response.

    ``to_document_detail`` explains that the text lives in server memory, which is
    misleading here: the full text is part of this response and the client keeps it.
    The stateless note replaces that one line; every other note is kept.
    """

    detail = to_document_detail(record, include_text=True)
    detail.notes = [STATELESS_NOTE, *[n for n in detail.notes if n != MEMORY_NOTE]]
    return detail


def resolve_document_no(supplied: str | None) -> str:
    """Return the document number to use for this request.

    A caller that already has a row sends its number back so a re-extraction or a
    re-audit keeps the same identity instead of creating a duplicate. Anything that
    does not look like a document number is refused: the value becomes an in-memory
    key and is echoed in responses.
    """

    if supplied is None or not supplied.strip():
        return generate_document_no()

    normalised = normalise_document_no(supplied)
    if (
        not normalised.startswith(DOCUMENT_NO_PREFIX)
        or len(normalised) > MAX_DOCUMENT_NO_LENGTH
        or not all(
            char.isascii() and (char.isalnum() or char == "-") for char in normalised
        )
    ):
        raise HTTPException(
            # 422 Unprocessable Content (named HTTP_422_UNPROCESSABLE_ENTITY in older
            # Starlette releases); the literal avoids the deprecated alias.
            status_code=422,
            detail=(
                "document_no must keep the form DOC- followed by letters, digits or "
                "dashes (the number returned by an earlier call), so a re-extraction "
                "or a re-audit updates the same row."
            ),
        )
    return normalised


def invoice_from_form(payload: str | None) -> InvoiceData | None:
    """Parse the optional ``invoice`` form field into validated invoice fields.

    The body is validated against the application-facing schema, so a client cannot
    inject unvalidated values into the audit: anything the API itself would not return
    is rejected with 422. The payload is never echoed back in the error.
    """

    if payload is None or not payload.strip():
        return None

    try:
        return InvoiceData.model_validate_json(payload)
    except ValidationError as exc:
        raise HTTPException(
            # 422 Unprocessable Content; the literal avoids the deprecated alias.
            status_code=422,
            detail=(
                "The invoice field is not a valid InvoiceData JSON object "
                f"({exc.error_count()} validation error(s)). Omit it to let the "
                "server extract the fields with the configured LLM."
            ),
        ) from exc


@router.post(
    "",
    response_model=AnalysisResponse,
    summary="Upload, extract and audit one PDF in a single stateless request",
    description=(
        "Runs the whole workflow for one PDF inside this request: PDF validation by "
        "content, page-level text and table extraction (with the optional PaddleOCR "
        "fallback), structured invoice extraction with the configured LLM, and the "
        "agentic VAT audit. The response carries the document detail (including the "
        "full page text and the invoice fields), the audit report and the ready-made "
        "`GET /documents` row.\n\n"
        "Use this endpoint instead of the three-step workflow on a serverless or "
        "multi-instance deployment (Vercel, Lambda, Cloud Run). The three-step "
        "workflow resolves a document number through process-local memory, so a "
        "follow-up request served by another instance answers 404.\n\n"
        "Re-extraction and re-audit: send the PDF again. Pass the previous "
        "`document_no` to keep the same row identity, and pass the previous `invoice` "
        "form field to re-audit the same fields without paying for another extraction "
        "call. `run_audit=false` stops after extraction (useful when only the "
        "structured fields are needed). A document that needs OCR is returned with "
        "`invoice`/`report` null instead of being rejected, because nothing failed: "
        "the PDF simply has no readable text."
    ),
    responses={
        400: {"description": "Empty or corrupted/encrypted PDF."},
        413: {"description": "Upload exceeds the configured maximum size."},
        415: {"description": "Uploaded content is not a PDF."},
        422: {"description": "document_no or invoice is not valid."},
        502: {"description": "The provider failed or returned unusable data."},
        503: {"description": "The LLM is not configured (LLM_API_KEY / LLM_MODEL)."},
        504: {"description": "The provider timed out."},
    },
)
async def analyse_document(
    file: UploadFile = File(..., description="A text-based or scanned invoice PDF."),
    invoice: str | None = Form(
        None,
        description=(
            "Optional InvoiceData JSON object from an earlier response. When supplied, "
            "the LLM extraction is skipped and these fields are audited instead."
        ),
    ),
    document_no: str | None = Form(
        None,
        description=(
            "Optional document number from an earlier response, so a re-extraction or "
            "re-audit updates that row instead of creating a new one."
        ),
    ),
    run_audit: bool = Query(
        True,
        description=(
            "When false, stop after structured invoice extraction and return "
            "`report: null`."
        ),
    ),
    ocr_engine: OcrEngine | None = Depends(get_document_ocr_engine),
    service: InvoiceExtractionService = Depends(get_invoice_extraction_service),
    auditor: AiAuditor = Depends(get_ai_auditor),
) -> AnalysisResponse:
    """Run the whole workflow for one PDF and return a self-contained response."""

    record, _ = await extract_document_record(
        file, ocr_engine, document_no=resolve_document_no(document_no)
    )

    notes = [STATELESS_NOTE]

    if record.extraction_status != "extracted":
        # Nothing failed: the PDF simply has no readable text, so the document (and the
        # needs-OCR flag) is returned instead of a bare 409.
        return AnalysisResponse(
            document=analysis_detail(record),
            report=None,
            row=to_list_item(record, None),
            message=NEEDS_OCR_MESSAGE,
            notes=notes,
        )

    supplied_invoice = invoice_from_form(invoice)
    if supplied_invoice is None:
        invoice_data = run_invoice_extraction(service, record)
    else:
        invoice_data = supplied_invoice
        notes.append(CLIENT_INVOICE_NOTE)

    stored = document_registry.set_invoice(record.document_no, invoice_data)
    if stored is not None:
        record = stored

    report = None
    message = EXTRACTION_ONLY_MESSAGE
    if run_audit:
        try:
            report = auditor.audit_record(record)
        except DocumentNotAuditableError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            ) from exc
        except LLMConfigurationError as exc:
            # Only reachable on a re-audit (a supplied invoice skips extraction), but
            # the answer must still be 503 rather than a bare 500.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        report.message = AUDIT_MESSAGE
        auditor.remember(report)
        message = ANALYSIS_MESSAGE

    return AnalysisResponse(
        document=analysis_detail(record),
        report=report,
        row=to_list_item(record, report),
        message=message,
        notes=notes,
    )
