"""Audit state for the LangGraph state machine (Phase 5).

The state is deliberately JSON-safe: the invoice is carried as a ``mode="json"`` dump
(so ``Decimal`` amounts survive as strings and no arbitrary object needs serialising
by the checkpointer), and full page text never enters the state at all — the tools
read the stored pages through a per-run closure instead.
"""

import operator
from typing import Annotated, Any, TypedDict

from app.models.audit import AuditFinding
from app.models.invoice import InvoiceData

MODEL_FAILURE_KINDS = frozenset({"model_error", "invalid_model_output"})
"""Failure kinds that justify one bounded model retry through the loop."""


def merge_unique(left: list[str], right: list[str]) -> list[str]:
    """Reducer that appends messages while keeping them unique and in order."""

    merged = list(left)
    for item in right:
        if item not in merged:
            merged.append(item)
    return merged


class AuditState(TypedDict, total=False):
    """State passed between audit nodes.

    Attributes:
        document_no: Document being audited.
        page_count: Pages in the stored document (no page text is kept here).
        invoice: ``InvoiceData`` dump in JSON mode, or None when extraction failed.
        classification: Reported VAT treatment, normalised to the four known values.
        missing_fields: Fields the extractor could not establish.
        applicable_rule_ids: Enabled rules selected for this classification.
        deterministic_findings: Findings produced by our Decimal arithmetic.
        model_findings: Findings returned by the model, after Pydantic validation.
        findings: Final, guardrail-validated findings.
        warnings: Accumulated warnings (unique, order preserved).
        review_reasons: Reasons a human must look at the report. Recomputed by each pass
            (the guardrail and review nodes return the complete current list), so a retry
            that succeeds does not leave a stale reason behind.
        nodes: Names of the nodes that ran, in order.
        tool_calls: Names of the tools the model asked for.
        iterations: Completed agent/re-extraction loops.
        model_calls: Model invocations, including re-extraction.
        conflicts: Deterministic/model disagreements found by the guardrail; declared so
            the write survives routing and the conflict confidence cap can apply.
        re_extraction_performed: Whether a re-extraction pass ran.
        audit_status: One of :data:`app.models.audit.AuditStatus`.
        overall_assessment: Short narrative assessment.
        overall_confidence_percent: Overall confidence after capping.
        confidence_explanation: How the overall confidence was computed.
        human_review_required: Set by the review/aggregate nodes; a report is only
            returned as needing review when this is true or a finding demands it.
        error: Sanitised failure message, when something failed.
        failure_kind: Machine-readable failure reason (``needs_ocr``, ``model_error``,
            ``invalid_model_output``, ``missing_extraction``, ``no_applicable_rules``).
        extraction_note: Outcome of the re-extraction pass, kept in state so the guardrail
            can turn it into a review reason on the pass that follows.
    """

    document_no: str
    page_count: int
    invoice: dict[str, Any] | None
    classification: str
    missing_fields: list[str]
    applicable_rule_ids: list[str]

    deterministic_findings: list[dict[str, Any]]
    model_findings: list[dict[str, Any]]
    findings: list[dict[str, Any]]

    warnings: Annotated[list[str], merge_unique]
    review_reasons: list[str]
    nodes: Annotated[list[str], operator.add]
    tool_calls: Annotated[list[str], operator.add]

    iterations: int
    model_calls: int
    conflicts: int
    re_extraction_performed: bool

    audit_status: str
    overall_assessment: str
    overall_confidence_percent: float
    confidence_explanation: str
    human_review_required: bool
    error: str | None
    failure_kind: str | None
    extraction_note: str | None


def initial_state(
    *,
    document_no: str,
    page_count: int,
    invoice: dict[str, Any] | None,
    missing_fields: list[str] | None = None,
) -> AuditState:
    """Build the starting state for one audit run."""

    return AuditState(
        document_no=document_no,
        page_count=page_count,
        invoice=invoice,
        classification="unknown",
        missing_fields=list(missing_fields or []),
        applicable_rule_ids=[],
        deterministic_findings=[],
        model_findings=[],
        findings=[],
        warnings=[],
        review_reasons=[],
        nodes=[],
        tool_calls=[],
        iterations=0,
        model_calls=0,
        conflicts=0,
        re_extraction_performed=False,
        audit_status="requires_review",
        overall_assessment="",
        overall_confidence_percent=0.0,
        confidence_explanation="",
        human_review_required=False,
        error=None,
        failure_kind=None,
        extraction_note=None,
    )


def invoice_from_state(state: AuditState) -> InvoiceData | None:
    """Rebuild the validated invoice from the state, or None when it is absent."""

    payload = state.get("invoice")
    if not payload:
        return None
    return InvoiceData.model_validate(payload)


def findings_from_state(state: AuditState) -> list[AuditFinding]:
    """Rebuild the validated findings from the state."""

    return [AuditFinding.model_validate(item) for item in state.get("findings", [])]


def has_review_flag(state: AuditState) -> bool:
    """True when any finding or the model itself demands human review."""

    if state.get("failure_kind") in MODEL_FAILURE_KINDS:
        return True
    if state.get("review_reasons"):
        return True
    for item in state.get("findings", []):
        if item.get("human_review_required") or item.get("status") == "review_required":
            return True
    return False
