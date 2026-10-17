"""FastAPI application entry point for the UAE VAT Audit MVP.

Phase 1 provided the application skeleton and ``GET /health``; Phase 2 added PDF
upload and page-level text extraction; Phase 3 (Revision 2) added structured invoice
field extraction through a provider-agnostic LLM layer configured entirely through
``.env``; Phase 4 added the configurable VAT rules engine; Phase 5 added the agentic
(LangGraph) audit engine; Phase 6 exposes it as ``POST /audit/{document_no}``.
Phase 8 adds the list/read endpoints the Next.js UI needs (``GET /documents`` and
``GET /audit/{document_no}``) and the CORS policy that lets a browser call the API.
Phase 9 moves extraction to pdfplumber (text **and** tables), adds the extraction
quality check and the optional PaddleOCR fallback for scanned pages.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.api.audits import router as audits_router
from app.api.documents import router as documents_router
from app.config import get_cors_allow_origins

APP_TITLE = "UAE VAT Audit API"
APP_VERSION = "0.2.0"
APP_DESCRIPTION = """
Minimal API for auditing UAE VAT invoices.

Workflow: upload an invoice PDF, receive a generated document number, extract its
structured invoice fields, retrieve the document data, then audit it by document
number and inspect the structured JSON findings in Swagger UI. ``GET /documents``
lists every uploaded document with its extraction and audit status so a UI can drive
the same workflow.

**Current scope (Phase 6):** text extraction from uploaded PDFs, structured invoice
field extraction through any OpenAI-compatible LLM configured via `.env`, a
configurable VAT ruleset, and an agentic audit engine that recomputes monetary
arithmetic deterministically and returns findings with confidence and human-review
flags.

**Disclaimer:** the audit engine performs only the configured checks. Monetary
arithmetic is recomputed in Python, but a failing check is an audit finding, not proof
of tax non-compliance, and passing every implemented check is not a VAT compliance
certification.
"""


class HealthResponse(BaseModel):
    """Response schema for the health check endpoint."""

    status: str = "ok"


app = FastAPI(
    title=APP_TITLE,
    version=APP_VERSION,
    description=APP_DESCRIPTION,
)

# The Next.js UI (`frontend/`) is served from a different origin than the API, so a
# browser call is cross-origin and needs this policy. Origins come from
# VAT_AUDIT_CORS_ALLOW_ORIGINS and default to the local Next.js dev server.
# Credentials stay disabled: the API has no cookies, sessions or authentication.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_allow_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(documents_router)
app.include_router(audits_router)



@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["Health"],
    summary="Check that the API is running",
    description="Returns `{\"status\": \"ok\"}` when the service is reachable.",
)
def health() -> HealthResponse:
    """Return the service liveness status."""

    return HealthResponse(status="ok")
