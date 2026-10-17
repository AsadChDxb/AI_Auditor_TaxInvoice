"""Response schema for the stateless one-shot analysis endpoint (``POST /analysis``).

The classic workflow stores a document in process memory and refers to it by number
across three requests. On a serverless platform (Vercel, Lambda, an autoscaled
container) the follow-up request can be served by an instance that never saw the
upload, which is why :class:`AnalysisResponse` returns everything in one payload: the
document detail (with the full page text and the structured invoice fields), the audit
report and the ready-made table row. The client keeps that payload, so no request
depends on another request's memory.
"""

from pydantic import BaseModel, Field

from app.models.audit import AuditReport
from app.models.document import DocumentDetail, DocumentListItem


class AnalysisResponse(BaseModel):
    """Everything one ``POST /analysis`` call produced, in a single response."""

    document: DocumentDetail = Field(
        ...,
        description=(
            "The uploaded document, its page-level text and (when extraction ran) the "
            "structured invoice fields."
        ),
    )
    report: AuditReport | None = Field(
        None,
        description=(
            "The audit report, or null when the request asked for extraction only "
            "(`run_audit=false`) or the document could not be read (it needs OCR)."
        ),
    )
    row: DocumentListItem = Field(
        ...,
        description=(
            "The same row `GET /documents` would return for this document, so a client "
            "can render or update its table without another call."
        ),
    )
    message: str = Field(
        "", description="One-line, secret-free summary of what this call did."
    )
    notes: list[str] = Field(
        default_factory=list,
        description=(
            "Storage and workflow notes, including the fact that this response is "
            "self-contained because the API does not rely on shared memory."
        ),
    )


__all__ = ["AnalysisResponse"]
