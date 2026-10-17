"""Deterministic, offline stubs for the Phase 5 audit tests.

No test calls a provider: the chat model is a scripted fake, settings and the ruleset
are built from explicit values (never from the developer's shell or ``.env``), and the
extractor is injected.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, Field

from app.audit.rules_config import build_rules_config
from app.config import AuditSettings
from app.models.audit import RulesConfig
from app.models.invoice import INVOICE_FIELD_NAMES, FieldEvidence, InvoiceData
from app.services.pdf_extractor import ExtractedPage
from app.storage.memory import DocumentRecord

AUDIT_DOCUMENT_NO = "DOC-AUDIT0000000000000000000000000"
_UNSET = object()
"""Sentinel so ``invoice=None`` means "no invoice", not "use the default"."""
SUPPLIER_TRN = "100123456700003"
CUSTOMER_TRN = "100987654300003"
PAGE_TEXT = (
    "TAX INVOICE INV-2024-0001 Supplier TRN: 100123456700003 "
    "Subtotal 1,000.00 VAT 50.00 Total 1,050.00"
)

EXTRACTION_JSON: dict[str, Any] = {
    "invoice_number": "INV-2024-0001",
    "invoice_date": "2024-05-03",
    "supplier_name": "Al Noor Trading LLC",
    "supplier_trn": SUPPLIER_TRN,
    "customer_name": "Gulf Retail FZE",
    "customer_trn": CUSTOMER_TRN,
    "currency": "AED",
    "subtotal_excluding_vat": "1000.00",
    "vat_amount": "50.00",
    "total_including_vat": "1050.00",
    "vat_rate_percent": "5%",
    "vat_classification": "Standard Rated",
}
"""A second-pass extraction payload, in the model-facing shape."""


def default_rules(**overrides: Any) -> RulesConfig:
    """The documented default ruleset, built without reading the environment."""

    return build_rules_config(settings=AuditSettings(**overrides))


def make_invoice(**overrides: Any) -> InvoiceData:
    """Build a validated, consistent standard-rated invoice."""

    values: dict[str, Any] = {
        "invoice_number": "INV-2024-0001",
        "invoice_date": "2024-05-03",
        "supplier_name": "Al Noor Trading LLC",
        "supplier_trn": SUPPLIER_TRN,
        "customer_name": "Gulf Retail FZE",
        "customer_trn": CUSTOMER_TRN,
        "currency": "AED",
        "subtotal_excluding_vat": Decimal("1000.00"),
        "vat_amount": Decimal("50.00"),
        "total_including_vat": Decimal("1050.00"),
        "vat_rate_percent": Decimal("5"),
        "vat_classification": "standard_rated",
        "field_evidence": {
            "vat_amount": FieldEvidence(
                value="50.00",
                confidence_percent=88,
                page_number=1,
                evidence="VAT 50.00",
            ),
            "supplier_trn": FieldEvidence(
                value=SUPPLIER_TRN,
                confidence_percent=95,
                page_number=1,
                evidence=f"Supplier TRN: {SUPPLIER_TRN}",
            ),
        },
    }
    values.update(overrides)
    values.setdefault(
        "missing_fields",
        [name for name in INVOICE_FIELD_NAMES if values.get(name) is None],
    )
    return InvoiceData(**values)


def make_audit_record(
    *,
    invoice: Any = _UNSET,
    page_texts: tuple[str, ...] = (PAGE_TEXT,),
    status: str = "extracted",
    document_no: str = AUDIT_DOCUMENT_NO,
) -> DocumentRecord:
    """Build a stored document ready for auditing.

    ``invoice`` defaults to a consistent standard-rated invoice; pass ``None`` to
    build a record whose structured fields have not been extracted.
    """

    return DocumentRecord(
        document_no=document_no,
        original_filename="invoice.pdf",
        content_type="application/pdf",
        size_bytes=2048,
        uploaded_at=datetime(2024, 5, 3, 12, 0, 0, tzinfo=timezone.utc),
        page_count=len(page_texts),
        pages=[
            ExtractedPage(page_number=index + 1, text=text)
            for index, text in enumerate(page_texts)
        ],
        extraction_status=status,
        extraction_error=None if status == "extracted" else "needs OCR",
        invoice=make_invoice() if invoice is _UNSET else invoice,
    )


def finding_payload(rule_id: str, **overrides: Any) -> dict[str, Any]:
    """A single model finding, in the shape the model is asked to return."""

    payload: dict[str, Any] = {
        "rule_id": rule_id,
        "status": "pass",
        "severity": None,
        "explanation": f"{rule_id} is consistent with the invoice.",
        "expected_value": "5",
        "actual_value": "5",
        "page_number": 1,
        "evidence": "VAT 50.00 on a taxable amount of 1,000.00",
        "confidence_percent": 92,
        "human_review_required": False,
        "human_review_reason": None,
        "recommended_action": "No action needed.",
    }
    payload.update(overrides)
    return payload


def review_payload(
    rule_ids: Any = (),
    *,
    assessment: str = "The invoice is consistent with the configured rules.",
    **finding_overrides: Any,
) -> dict[str, Any]:
    """A model review with one finding per supplied rule ID."""

    return {
        "findings": [
            finding_payload(rule_id, **finding_overrides) for rule_id in rule_ids
        ],
        "overall_assessment": assessment,
    }


def review_json(rule_ids: Any = (), **finding_overrides: Any) -> str:
    """The same review, serialised as JSON text."""

    return json.dumps(review_payload(rule_ids, **finding_overrides))


class ToolCallingChatModel(BaseChatModel):
    """Fake model that requests tools first, then replies with JSON.

    ``scripted`` holds one entry per invocation:

    * ``{"tool_calls": [{"name": ..., "args": {...}}]}`` requests tools, and
    * ``{"content": "<json>"}`` returns the final review.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    scripted: list[dict[str, Any]] = Field(default_factory=list)
    bound_tool_names: list[str] = Field(default_factory=list)
    calls: int = 0
    sent: list[Any] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted-tool-calling-model"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ToolCallingChatModel":
        """Record the tool names the graph bound, as a real provider would see them."""

        self.bound_tool_names = [getattr(item, "name", str(item)) for item in tools]
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Replay the next scripted step."""

        self.calls += 1
        self.sent.append(list(messages))
        if not self.scripted:
            raise AssertionError("ToolCallingChatModel has no scripted reply left")
        script = self.scripted.pop(0)
        if "tool_calls" in script:
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": call["name"],
                        "args": call.get("args", {}),
                        "id": call.get("id", f"call-{index}"),
                        "type": "tool_call",
                    }
                    for index, call in enumerate(script["tool_calls"])
                ],
            )
        else:
            message = AIMessage(content=script.get("content", ""))
        return ChatResult(generations=[ChatGeneration(message=message)])


def tool_calling_model(scripted: list[dict[str, Any]]) -> ToolCallingChatModel:
    """Build a tool-calling fake with the given script."""

    return ToolCallingChatModel(scripted=list(scripted))


__all__ = [
    "AUDIT_DOCUMENT_NO",
    "CUSTOMER_TRN",
    "EXTRACTION_JSON",
    "PAGE_TEXT",
    "SUPPLIER_TRN",
    "ToolCallingChatModel",
    "default_rules",
    "finding_payload",
    "make_audit_record",
    "make_invoice",
    "review_json",
    "review_payload",
    "tool_calling_model",
]