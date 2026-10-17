"""UAE VAT Audit MVP application package.

Phase 1 provided the FastAPI skeleton and ``GET /health``; Phase 2 added in-memory
PDF upload and text extraction; Phase 3 (Revision 2) adds structured invoice field
extraction through a provider-agnostic LLM layer. The configurable VAT rules engine
and the agentic (LangGraph) audit endpoint arrive in later phases. No database, ORM,
frontend, authentication, LLM-based VAT decisions, or OCR is part of this MVP.
"""

__version__ = "0.2.0"
