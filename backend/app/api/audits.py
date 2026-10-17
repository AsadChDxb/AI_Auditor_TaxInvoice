"""Audit endpoint (Phase 6).

Thin route: rule selection, the deterministic Decimal checks and the LangGraph state
machine all live in :mod:`app.audit.ai_auditor`; the ruleset comes from
:mod:`app.audit.rules_config` and the response schema from :mod:`app.models.audit`.

Nothing in this module decides accounting or legal questions, and no stack trace,
secret or full invoice text is ever returned.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.audit.ai_auditor import (
    AiAuditor,
    DocumentNotAuditableError,
    UnknownDocumentError,
)
from app.models.audit import AuditReport
from app.storage.memory import document_registry

router = APIRouter(prefix="/audit", tags=["Audit"])

AUDIT_MESSAGE = (
    "Audit completed from the extracted invoice fields and the enabled rules. Read the "
    "review reasons and the per-finding evidence before relying on the result."
)
REPEATED_AUDIT_MESSAGE = (
    "A previous report for this document was replaced: only the latest report is stored, "
    "and this call re-ran the deterministic checks and the model review."
)
MEMORY_NOTE = "Reports are kept in memory only and are lost when the service restarts."
UNKNOWN_DOCUMENT_DETAIL = (
    "Document '{document_no}' was not found. Documents are held in memory only, so "
    "records are lost when the service restarts."
)
NO_REPORT_DETAIL = (
    "No audit report is stored for document '{document_no}'. Run POST "
    "/audit/{document_no} first: only the latest report per document number is kept, "
    "and reports are held in memory only."
)


def get_ai_auditor() -> AiAuditor:
    """Provide the agentic audit service.

    Declared as a FastAPI dependency so tests can inject an auditor backed by a
    scripted chat model, keeping the suite offline.
    """

    return AiAuditor()


@router.post(
    "/{document_no}",
    response_model=AuditReport,
    summary="Audit a stored document against the enabled VAT rules",
    description=(
        "Runs the agentic audit engine for a document that has already been uploaded "
        "(POST /documents/upload) and had its structured invoice fields extracted "
        "(POST /documents/{document_no}/extract). The path document number is the only "
        "input.\n\n"
        "Every applicable enabled rule produces exactly one finding. Monetary "
        "arithmetic (rate, VAT, total) is recomputed in Python with `Decimal` and an "
        "explicit AED tolerance, and the deterministic result always wins a conflict "
        "with the model; a conflict, missing evidence, an uncertain zero-rated/exempt "
        "treatment or any provider failure sets `human_review_required`.\n\n"
        "`audit_status` is `completed`, `completed_with_warnings` (e.g. a model finding "
        "was discarded), `requires_review` (a human must decide) or `cannot_audit` (no "
        "audit was performed). A document that needs OCR, or whose invoice fields have "
        "not been extracted yet, is refused with 409 instead of a passing report. A "
        "provider outage is reported as `requires_review`, never as success.\n\n"
        "Only the latest report per document number is kept in memory: a repeated call "
        "re-runs the audit and replaces the stored report."
    ),
    responses={
        404: {"description": "Unknown document number."},
        409: {
            "description": (
                "The document cannot be audited: it needs OCR, or its structured invoice "
                "fields have not been extracted yet."
            )
        },
        500: {
            "description": (
                "An unexpected server error. No stack trace, secret or invoice text is "
                "returned."
            )
        },
    },
)
def audit_document(
    document_no: str,
    auditor: AiAuditor = Depends(get_ai_auditor),
) -> AuditReport:
    """Audit a stored document and return the structured report."""

    record = document_registry.get(document_no)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=UNKNOWN_DOCUMENT_DETAIL.format(document_no=document_no),
        )

    try:
        report = auditor.audit_record(record)
    except DocumentNotAuditableError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except UnknownDocumentError as exc:  # pragma: no cover - registry checked above
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    report.message = (
        REPEATED_AUDIT_MESSAGE
        if auditor.latest(record.document_no) is not None
        else AUDIT_MESSAGE
    )
    return auditor.remember(report)


@router.get(
    "/{document_no}",
    response_model=AuditReport,
    summary="Retrieve the latest stored audit report for a document",
    description=(
        "Returns the most recent report produced by POST /audit/{document_no} without "
        "re-running the audit, so a client can show a report it already has (for "
        "example after a page refresh) instead of paying for another model call.\\n\\n"
        "Only the latest report per document number is kept, in memory only: a report "
        "disappears when the service restarts, and an earlier report is never returned. "
        "Use POST /audit/{document_no} to (re-)run the audit."
    ),
    responses={
        404: {
            "description": (
                "Unknown document number, or no report is stored for it yet because "
                "POST /audit/{document_no} has not been called."
            )
        },
    },
)
def get_audit_report(
    document_no: str,
    auditor: AiAuditor = Depends(get_ai_auditor),
) -> AuditReport:
    """Return the latest stored report, or 404 when there is none."""

    if document_registry.get(document_no) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=UNKNOWN_DOCUMENT_DETAIL.format(document_no=document_no),
        )

    report = auditor.latest(document_no)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=NO_REPORT_DETAIL.format(document_no=document_no),
        )

    return report
