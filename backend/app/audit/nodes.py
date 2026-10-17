"""Audit nodes for the LangGraph state machine (Phase 5).

The node set separates what must stay deterministic from what a model is allowed to
say:

``load_context`` / ``select_rules``
    Read the stored document and pick the enabled rules for the reported treatment.
``deterministic_checks``
    Recompute every rate, VAT, total and TRN check in Python with ``Decimal``.
``agent_review``
    Ask the model for one finding per applicable rule, following the tools it calls.
``guardrail_validate``
    Validate model output against the ruleset, drop anything invented, and treat the
    deterministic result as authoritative whenever the two disagree.
``re_extract_fields``
    One bounded re-extraction/retry pass when evidence is missing or the model failed.
``human_review`` / ``cannot_audit`` / ``aggregate_report``
    Decide the final status, confidence and review flags.

No node trusts the model for arithmetic, rule identity or legal conclusions.
"""

import json
import re
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from pydantic import ValidationError

from app.audit.prompts import (
    AUDITOR_SYSTEM_PROMPT,
    MAX_EXPLANATION_CHARACTERS,
    build_auditor_prompt,
)
from app.audit.rules_config import (
    TRN_FORMAT_PATTERN,
    resolve_expected_vat_rate,
)
from app.audit.state import (
    MODEL_FAILURE_KINDS,
    AuditState,
    findings_from_state,
    invoice_from_state,
)
from app.audit.tools import (
    AuditToolbelt,
    mask_identifiers,
    parse_amount,
    round_monetary,
)
from app.models.audit import (
    SEVERITY_ORDER,
    SEVERITY_WEIGHTS,
    ZERO_TREATMENTS,
    AuditFinding,
    ModelAuditReview,
    ModelFinding,
    RulesConfig,
    severity_rank,
)
from app.models.invoice import INVOICE_FIELD_NAMES, InvoiceData
from app.services.invoice_extractor import (
    DocumentNotExtractableError,
    build_invoice_data,
)
from app.services.llm import content_text, strip_code_fences
from app.services.llm_errors import (
    LLMError,
    LLMInvalidResponseError,
    map_exception,
    summarise_validation_error,
)
from app.storage.memory import DocumentRecord

VALID_STATUSES: tuple[str, ...] = (
    "pass",
    "fail",
    "warning",
    "not_applicable",
    "review_required",
)
"""Statuses a finding may carry; anything else is coerced to ``review_required``."""

DETERMINISTIC_CONFIDENCE_KNOWN = 95.0
"""Confidence for arithmetic whose inputs were all extracted."""

DETERMINISTIC_CONFIDENCE_MISSING = 60.0
"""Confidence for a check that cannot run because evidence is missing."""

MODEL_CONFIDENCE_FALLBACK = 50.0
"""Confidence assumed for a model finding that gave none (and therefore needs review)."""

CONFLICT_CONFIDENCE_CAP = 70.0
"""Overall-confidence cap when the model disagreed with our arithmetic."""

MISSING_EVIDENCE_CONFIDENCE_CAP = 70.0
"""Overall-confidence cap when key evidence is missing."""

MODEL_FAILURE_CONFIDENCE_CAP = 40.0
"""Overall-confidence cap when the model could not be used at all."""

RATE_TOLERANCE = Decimal("0.01")
"""Tolerance (percentage points) when comparing a printed rate with the expected rate."""

MAX_TOOL_ROUNDS = 3
"""Maximum tool-calling rounds per model invocation."""

MAX_TOOL_RESULT_CHARACTERS = 2_000
"""Bound on a tool result handed back to the model."""

MAX_EVIDENCE_CHARACTERS = 240
"""Bound on an evidence snippet stored in a finding."""


def _confidence_text(value: float) -> str:
    """Render a confidence without a trailing ``.0``."""

    return f"{value:g}"


def _bounded_explanation(text: str | None) -> str:
    """Collapse, mask and cap an explanation before it is stored."""

    collapsed = " ".join(str(text or "").split())
    if len(collapsed) > MAX_EXPLANATION_CHARACTERS:
        collapsed = collapsed[:MAX_EXPLANATION_CHARACTERS].rstrip() + "..."
    return collapsed


def make_finding(
    rule: Any,
    *,
    status: str,
    explanation: str,
    confidence: float,
    expected: str | None = None,
    actual: str | None = None,
    page_number: int | None = None,
    evidence: str | None = None,
    review: bool = False,
    review_reason: str | None = None,
    action: str | None = None,
    source: str = "deterministic",
) -> AuditFinding:
    """Build a validated finding from a rule and an outcome."""

    snippet = None
    if evidence:
        snippet = mask_identifiers(_bounded_explanation(evidence))
    return AuditFinding(
        rule_id=rule.rule_id,
        rule_title=rule.title,
        status=status,
        severity=rule.severity,
        explanation=_bounded_explanation(explanation),
        expected_value=expected,
        actual_value=actual,
        page_number=page_number,
        evidence=snippet,
        confidence_percent=confidence,
        human_review_required=review,
        human_review_reason=_bounded_explanation(review_reason) if review_reason else None,
        recommended_action=action,
        source=source,
    )


# --- deterministic checks --------------------------------------------------------


def _check_expected_vat_rate(rule, invoice: InvoiceData, rules: RulesConfig) -> AuditFinding:
    """Compare the printed rate with the configured expected rate."""

    classification = invoice.vat_classification
    expected_rate = resolve_expected_vat_rate(rule, classification)
    if expected_rate is None:
        return make_finding(
            rule,
            status="review_required",
            confidence=DETERMINISTIC_CONFIDENCE_MISSING,
            explanation=(
                "The VAT treatment could not be established, so no expected rate was "
                "assumed: a 5% rate is never applied to an unknown treatment."
            ),
            actual=None if invoice.vat_rate_percent is None else str(invoice.vat_rate_percent),
            review=True,
            review_reason="The VAT treatment is unknown, so no rate could be checked.",
            action="Confirm the VAT treatment from the invoice and its supporting evidence.",
        )

    printed = invoice.vat_rate_percent
    if printed is None:
        if expected_rate == 0:
            return make_finding(
                rule,
                status="not_applicable",
                confidence=DETERMINISTIC_CONFIDENCE_KNOWN,
                explanation=(
                    f"A {classification} supply does not carry a positive VAT rate, and no "
                    "rate was printed."
                ),
                expected=str(expected_rate),
                actual="not printed",
            )
        return make_finding(
            rule,
            status="review_required",
            confidence=DETERMINISTIC_CONFIDENCE_MISSING,
            explanation="The VAT rate printed on the invoice was not extracted.",
            expected=str(expected_rate),
            actual=None,
            review=True,
            review_reason="The printed VAT rate is missing from the extracted fields.",
            action="Read the VAT rate from the invoice and compare it with the expected rate.",
        )

    difference = printed - expected_rate
    if abs(difference) <= RATE_TOLERANCE:
        return make_finding(
            rule,
            status="pass",
            confidence=DETERMINISTIC_CONFIDENCE_KNOWN,
            explanation=(
                f"The printed rate matches the expected rate for a {classification} supply."
            ),
            expected=str(expected_rate),
            actual=str(printed),
        )
    return make_finding(
        rule,
        status="fail",
        confidence=DETERMINISTIC_CONFIDENCE_KNOWN,
        explanation=(
            f"The printed rate of {printed}% does not match the expected rate of "
            f"{expected_rate}% for a {classification} supply."
        ),
        expected=str(expected_rate),
        actual=str(printed),
        review=True,
        review_reason="The printed VAT rate does not match the configured expected rate.",
        action="Check which VAT treatment applies and, if the rate is wrong, request a corrected invoice.",
    )


def _check_taxable_amount_vs_vat(
    rule, invoice: InvoiceData, rules: RulesConfig
) -> AuditFinding:
    """Recompute VAT from the taxable amount with Decimal arithmetic."""

    rate = resolve_expected_vat_rate(rule, invoice.vat_classification)
    taxable = invoice.subtotal_excluding_vat
    reported = invoice.vat_amount

    if rate is None:
        return make_finding(
            rule,
            status="review_required",
            confidence=DETERMINISTIC_CONFIDENCE_MISSING,
            explanation=(
                "Expected VAT cannot be computed because the VAT treatment is unknown."
            ),
            actual=None if reported is None else str(reported),
            review=True,
            review_reason="The VAT treatment is unknown, so VAT cannot be recomputed.",
            action="Confirm the VAT treatment before recomputing VAT.",
        )
    if taxable is None or reported is None:
        return make_finding(
            rule,
            status="review_required",
            confidence=DETERMINISTIC_CONFIDENCE_MISSING,
            explanation=(
                "The taxable amount or the VAT amount was not extracted, so VAT cannot be "
                "recomputed and no value is assumed."
            ),
            expected=None,
            actual=None if reported is None else str(reported),
            review=True,
            review_reason="The taxable amount or VAT amount is missing from the extracted fields.",
            action="Confirm the missing amount on the invoice before relying on this check.",
        )

    expected = round_monetary(taxable * rate / Decimal("100"), rules.rounding_policy)
    difference = round_monetary(reported - expected, rules.rounding_policy)
    if abs(difference) <= rules.monetary_tolerance:
        return make_finding(
            rule,
            status="pass",
            confidence=DETERMINISTIC_CONFIDENCE_KNOWN,
            explanation=(
                f"Reported VAT equals {taxable} x {rate}% within the AED "
                f"{rules.monetary_tolerance} tolerance."
            ),
            expected=str(expected),
            actual=str(reported),
        )
    return make_finding(
        rule,
        status="fail",
        confidence=DETERMINISTIC_CONFIDENCE_KNOWN,
        explanation=(
            f"Reported VAT of {reported} differs from {taxable} x {rate}% = {expected} by "
            f"{difference}, which exceeds the AED {rules.monetary_tolerance} tolerance."
        ),
        expected=str(expected),
        actual=str(reported),
        review=True,
        review_reason="Reported VAT does not match the taxable amount multiplied by the applicable rate.",
        action="Recheck the taxable amount and VAT on the invoice; treat any difference as a review point, not a legal conclusion.",
    )


def _check_invoice_total(rule, invoice: InvoiceData, rules: RulesConfig) -> AuditFinding:
    """Reconcile the invoice total with subtotal + VAT."""

    subtotal = invoice.subtotal_excluding_vat
    vat = invoice.vat_amount
    total = invoice.total_including_vat

    if subtotal is None or vat is None or total is None:
        missing = [
            name
            for name, value in (
                ("subtotal", subtotal),
                ("VAT amount", vat),
                ("total", total),
            )
            if value is None
        ]
        return make_finding(
            rule,
            status="review_required",
            confidence=DETERMINISTIC_CONFIDENCE_MISSING,
            explanation=(
                "The invoice total cannot be reconciled because the following amount(s) "
                "were not extracted: " + ", ".join(missing) + "."
            ),
            expected=None,
            actual=None if total is None else str(total),
            review=True,
            review_reason="A required amount for the total reconciliation is missing.",
            action="Read the missing amount from the invoice and re-run the audit.",
        )

    expected = round_monetary(subtotal + vat, rules.rounding_policy)
    difference = round_monetary(total - expected, rules.rounding_policy)
    if abs(difference) <= rules.monetary_tolerance:
        return make_finding(
            rule,
            status="pass",
            confidence=DETERMINISTIC_CONFIDENCE_KNOWN,
            explanation=(
                f"Total equals subtotal + VAT ({subtotal} + {vat}) within the AED "
                f"{rules.monetary_tolerance} tolerance; no separately extracted discounts "
                "or charges were applied."
            ),
            expected=str(expected),
            actual=str(total),
        )
    return make_finding(
        rule,
        status="fail",
        confidence=DETERMINISTIC_CONFIDENCE_KNOWN,
        explanation=(
            f"Total of {total} differs from subtotal + VAT = {expected} by {difference}, "
            f"which exceeds the AED {rules.monetary_tolerance} tolerance. Discounts, deposits "
            "or charges that were not extracted would explain part of this."
        ),
        expected=str(expected),
        actual=str(total),
        review=True,
        review_reason="The invoice total does not reconcile with subtotal + VAT.",
        action="Check for unextracted discounts or charges, then confirm the printed total.",
    )


def _check_trn_presence(rule, invoice: InvoiceData, rules: RulesConfig) -> AuditFinding:
    """Check TRN presence and 15-digit format for the configured role."""

    role = rule.trn_role or "supplier"
    value = getattr(invoice, f"{role}_trn", None)
    masked = None if value is None else mask_identifiers(str(value))

    if not rule.trn_required:
        return make_finding(
            rule,
            status="not_applicable",
            confidence=DETERMINISTIC_CONFIDENCE_KNOWN,
            explanation=(
                f"The configured scenario does not require a {role} TRN, so a missing one is "
                "not treated as a finding."
            ),
            actual=masked,
        )
    if value is None or not str(value).strip():
        return make_finding(
            rule,
            status="fail",
            confidence=DETERMINISTIC_CONFIDENCE_KNOWN - 10,
            explanation=(
                f"The configured scenario requires a {role} TRN, but none was extracted."
            ),
            expected="15-digit TRN",
            actual="missing",
            review=True,
            review_reason=f"A required {role} TRN is missing or could not be read.",
            action=f"Obtain the {role}'s TRN from the invoice or the counterparty's records.",
        )
    candidate = str(value).strip()
    if not re.fullmatch(TRN_FORMAT_PATTERN, candidate):
        return make_finding(
            rule,
            status="fail",
            confidence=DETERMINISTIC_CONFIDENCE_KNOWN - 5,
            explanation=(
                f"The {role} TRN is not a 15-digit number. This is a format check only: it does "
                "not establish whether the number is registered with the Federal Tax Authority."
            ),
            expected="15 digits",
            actual=masked,
            review=True,
            review_reason=f"The {role} TRN format is invalid.",
            action=f"Confirm the {role} TRN on the original invoice.",
        )
    return make_finding(
        rule,
        status="pass",
        confidence=DETERMINISTIC_CONFIDENCE_KNOWN - 5,
        explanation=(
            f"A 15-digit {role} TRN is present. Format only: registration with the Federal Tax "
            "Authority is not verified."
        ),
        expected="15 digits",
        actual=masked,
    )


def _check_classification_review(
    rule, invoice: InvoiceData, rules: RulesConfig
) -> AuditFinding:
    """Flag ambiguous or unsupported VAT treatment for human review."""

    classification = invoice.vat_classification
    vat = invoice.vat_amount

    if classification == "unknown":
        return make_finding(
            rule,
            status="review_required",
            confidence=80.0,
            explanation=(
                "The invoice does not state a VAT treatment that could be established, so the "
                "treatment is ambiguous. No rate or treatment was assumed."
            ),
            actual="unknown",
            review=True,
            review_reason="The VAT treatment is unknown or ambiguous.",
            action="Confirm the treatment from the invoice wording and the nature of the supply.",
        )
    if classification in ZERO_TREATMENTS:
        if vat is not None and vat > rules.monetary_tolerance:
            return make_finding(
                rule,
                status="fail",
                confidence=DETERMINISTIC_CONFIDENCE_KNOWN,
                explanation=(
                    f"VAT of {vat} was charged on a supply reported as {classification}, which "
                    "carries no VAT."
                ),
                expected="0.00",
                actual=str(vat),
                review=True,
                review_reason=f"VAT was charged on a supply reported as {classification}.",
                action="Check the treatment and the VAT charged; the supply may be misclassified.",
            )
        return make_finding(
            rule,
            status="review_required",
            confidence=75.0,
            explanation=(
                f"The invoice reports {classification} treatment with no VAT charged. The audit "
                "cannot verify from the extracted fields that the condition for this treatment is "
                "met, so a reviewer must confirm the supporting evidence."
            ),
            expected="0.00",
            actual="0.00" if vat is None else str(vat),
            review=True,
            review_reason=f"{classification} treatment needs supporting evidence.",
            action="Confirm the documentary evidence for the reported treatment before filing.",
        )
    return make_finding(
        rule,
        status="not_applicable",
        confidence=DETERMINISTIC_CONFIDENCE_KNOWN,
        explanation="Standard-rated treatment does not require a classification review.",
        actual=str(classification),
    )


CHECK_IMPLEMENTATIONS: dict[str, Callable[[Any, InvoiceData, RulesConfig], AuditFinding]] = {
    "expected_vat_rate": _check_expected_vat_rate,
    "taxable_amount_vs_vat": _check_taxable_amount_vs_vat,
    "invoice_total": _check_invoice_total,
    "trn_presence": _check_trn_presence,
    "classification_review": _check_classification_review,
}
"""Deterministic implementation of every supported check kind."""


def run_deterministic_checks(
    rules: RulesConfig, invoice: InvoiceData
) -> list[AuditFinding]:
    """Run every deterministic check that applies to the invoice's treatment."""

    findings: list[AuditFinding] = []
    for rule in rules.applicable_rules(invoice.vat_classification):
        implementation = CHECK_IMPLEMENTATIONS.get(rule.check)
        if implementation is None:
            findings.append(
                make_finding(
                    rule,
                    status="review_required",
                    confidence=DETERMINISTIC_CONFIDENCE_MISSING,
                    explanation=(
                        f"No deterministic implementation is configured for the "
                        f"'{rule.check}' check, so this rule could not be evaluated."
                    ),
                    review=True,
                    review_reason=f"Rule {rule.rule_id} has no deterministic implementation.",
                    action="Upgrade the audit engine or disable this rule before relying on the report.",
                )
            )
            continue
        findings.append(implementation(rule, invoice, rules))
    return findings


def overall_confidence(
    findings: Sequence[AuditFinding],
    *,
    threshold: float,
    model_available: bool = True,
    missing_evidence: bool = False,
    conflicts: int = 0,
) -> tuple[float, str]:
    """Compute the overall confidence and explain how it was reached.

    The value is a **severity-weighted mean** of the finding confidences: attention
    findings (``fail``, ``warning``, ``review_required``) count double, so a
    low-confidence critical finding cannot be hidden by many confident passes. The
    mean is then capped:

    * below the review threshold when any finding is below that threshold or is
      ``review_required`` (a report with a review point must not read as a confident
      pass),
    * at :data:`CONFLICT_CONFIDENCE_CAP` when the model disagreed with our arithmetic,
    * at :data:`MISSING_EVIDENCE_CONFIDENCE_CAP` when evidence is missing, and
    * at :data:`MODEL_FAILURE_CONFIDENCE_CAP` when the model could not be used.

    Args:
        findings: Final, validated findings.
        threshold: Configured human-review confidence threshold.
        model_available: False when the model failed or returned unusable output.
        missing_evidence: True when required fields were not extracted.
        conflicts: Number of deterministic/model disagreements.

    Returns:
        ``(confidence, explanation)``; the explanation names the caps that applied.
    """

    considered = [finding for finding in findings if finding.status != "not_applicable"]
    notes: list[str] = []

    if not considered:
        base = 0.0
        notes.append(
            "no applicable rule produced a finding, so there is nothing to be confident about"
        )
    else:
        weights = [
            SEVERITY_WEIGHTS.get(finding.severity, 2.0)
            * (2.0 if finding.needs_attention else 1.0)
            for finding in considered
        ]
        base = sum(
            weight * finding.confidence_percent
            for weight, finding in zip(weights, considered)
        ) / sum(weights)
        notes.append(
            f"severity-weighted mean of {len(considered)} finding(s), with failures, "
            "warnings and review_required findings weighted double"
        )

    caps: list[tuple[float, str]] = []
    low = [f for f in considered if f.confidence_percent < threshold]
    if low:
        lowest = min(f.confidence_percent for f in low)
        caps.append(
            (
                max(0.0, threshold - 1.0),
                f"below the {threshold:g}% review threshold because {len(low)} finding(s) are "
                f"below it (lowest {lowest:g}%)",
            )
        )
    flagged = [
        f
        for f in considered
        if f.human_review_required or f.status == "review_required"
    ]
    if flagged:
        caps.append(
            (
                max(0.0, threshold - 1.0),
                f"below the review threshold because {len(flagged)} finding(s) are flagged for "
                "human review",
            )
        )
    if conflicts:
        caps.append(
            (
                CONFLICT_CONFIDENCE_CAP,
                f"at {CONFLICT_CONFIDENCE_CAP:g}% because {conflicts} deterministic/model "
                "conflict(s) need a human decision",
            )
        )
    if missing_evidence:
        caps.append(
            (
                MISSING_EVIDENCE_CONFIDENCE_CAP,
                f"at {MISSING_EVIDENCE_CONFIDENCE_CAP:g}% because key evidence is missing",
            )
        )
    if not model_available:
        caps.append(
            (
                MODEL_FAILURE_CONFIDENCE_CAP,
                f"at {MODEL_FAILURE_CONFIDENCE_CAP:g}% because the model could not be used",
            )
        )

    value = base
    applied: list[str] = []
    for cap, reason in caps:
        value = min(value, cap)
        applied.append(reason)

    explanation = "Overall confidence: " + "; ".join(notes)
    if applied:
        explanation += ". Capping applied: " + "; ".join(applied)
    else:
        explanation += ". No capping was necessary."
    return round(max(0.0, min(100.0, value)), 1), explanation


# --- graph nodes -----------------------------------------------------------------


def load_context(state: AuditState, record: DocumentRecord) -> dict[str, Any]:
    """Read the stored document, or decide that it cannot be audited."""

    node = ["load_context"]
    has_text = any(page.text.strip() for page in record.pages)
    if record.extraction_status != "extracted" or not has_text:
        return {
            "nodes": node,
            "audit_status": "cannot_audit",
            "failure_kind": "needs_ocr",
            "error": (
                "The document has no extractable text (it needs OCR), so no audit was "
                "performed."
            ),
            "warnings": ["The audit was not performed because the document needs OCR."],
        }
    if record.invoice is None:
        return {
            "nodes": node,
            "audit_status": "cannot_audit",
            "failure_kind": "missing_extraction",
            "error": (
                "Structured invoice fields have not been extracted for this document, so "
                "no audit was performed."
            ),
            "warnings": ["Invoice fields must be extracted before an audit can run."],
        }

    invoice = record.invoice
    return {
        "nodes": node,
        "page_count": record.page_count,
        "invoice": invoice.model_dump(mode="json"),
        "classification": invoice.vat_classification,
        "missing_fields": list(invoice.missing_fields),
    }


def select_rules(state: AuditState, rules: RulesConfig) -> dict[str, Any]:
    """Select the enabled rules applicable to the reported VAT treatment."""

    node = {"nodes": ["select_rules"]}
    invoice = invoice_from_state(state)
    if invoice is None:
        return {
            **node,
            "audit_status": "cannot_audit",
            "failure_kind": "missing_extraction",
            "error": "No extracted invoice fields are available to audit.",
        }

    applicable = rules.applicable_rules(invoice.vat_classification)
    if not applicable:
        return {
            **node,
            "audit_status": "cannot_audit",
            "failure_kind": "no_applicable_rules",
            "error": (
                "No enabled rule applies to this invoice, so no audit could be performed."
            ),
            "warnings": [
                f"Ruleset {rules.version} has no enabled rule for "
                f"'{invoice.vat_classification}' treatment."
            ],
        }

    return {
        **node,
        "applicable_rule_ids": [rule.rule_id for rule in applicable],
        "classification": invoice.vat_classification,
        "failure_kind": None,
    }


def deterministic_checks(state: AuditState, rules: RulesConfig) -> dict[str, Any]:
    """Recompute every rate, VAT, total and TRN check in Python."""

    invoice = invoice_from_state(state)
    if invoice is None:
        return {
            "nodes": ["deterministic_checks"],
            "audit_status": "cannot_audit",
            "failure_kind": "missing_extraction",
        }
    findings = run_deterministic_checks(rules, invoice)
    return {
        "nodes": ["deterministic_checks"],
        "deterministic_findings": [item.model_dump(mode="json") for item in findings],
    }


def human_review(state: AuditState) -> dict[str, Any]:
    """Collect the reasons a human must look at this report."""

    reasons: list[str] = list(state.get("review_reasons", []))
    for finding in findings_from_state(state):
        if finding.human_review_required:
            reasons.append(
                f"{finding.rule_id}: "
                + (finding.human_review_reason or "flagged for human review.")
            )
    if state.get("missing_fields"):
        reasons.append(
            "Missing or unreadable fields: " + ", ".join(state.get("missing_fields", [])) + "."
        )
    if state.get("failure_kind") in MODEL_FAILURE_KINDS and state.get("error"):
        reasons.append("Model assessment unavailable: " + str(state.get("error")))
    return {"nodes": ["human_review"], "review_reasons": list(dict.fromkeys(reasons))}


def cannot_audit(state: AuditState) -> dict[str, Any]:
    """Record that the document could not be audited at all."""

    return {
        "nodes": ["cannot_audit"],
        "audit_status": "cannot_audit",
        "overall_confidence_percent": 0.0,
        "confidence_explanation": "Overall confidence is 0% because no audit was performed.",
        "overall_assessment": state.get("error") or "The document could not be audited.",
        "human_review_required": True,
    }


def aggregate_report(state: AuditState, rules: RulesConfig) -> dict[str, Any]:
    """Decide the final status, confidence and assessment."""

    findings = findings_from_state(state)
    model_available = state.get("failure_kind") not in MODEL_FAILURE_KINDS
    conflicts = int(state.get("conflicts", 0) or 0)
    missing_evidence = bool(state.get("missing_fields")) or any(
        finding.status == "review_required"
        and (
            "missing" in (finding.explanation or "").lower()
            or "not extracted" in (finding.explanation or "").lower()
        )
        for finding in findings
    )
    value, explanation = overall_confidence(
        findings,
        threshold=float(rules.human_review_confidence_threshold),
        model_available=model_available,
        missing_evidence=missing_evidence,
        conflicts=conflicts,
    )

    warnings = list(state.get("warnings", []))
    reasons = list(state.get("review_reasons", []))
    needs_review = (
        bool(reasons)
        or any(finding.human_review_required for finding in findings)
        or any(finding.status == "review_required" for finding in findings)
        or not model_available
    )

    if state.get("audit_status") == "cannot_audit":
        status = "cannot_audit"
    elif needs_review:
        status = "requires_review"
    elif warnings:
        status = "completed_with_warnings"
    else:
        status = "completed"

    return {
        "nodes": ["aggregate_report"],
        "audit_status": status,
        "overall_confidence_percent": value,
        "confidence_explanation": explanation,
        "human_review_required": needs_review,
    }


# --- model invocation ------------------------------------------------------------


def bind_tools_if_supported(chat_model: BaseChatModel, toolbelt: AuditToolbelt) -> Any:
    """Bind the deterministic tools when the provider supports tool calling."""

    try:
        return chat_model.bind_tools(toolbelt.tools)
    except Exception:  # noqa: BLE001 - a provider without tool calling uses the prompt only
        return None


def tool_calls_of(message: BaseMessage) -> list[dict[str, Any]]:
    """Tool calls requested by a chat message (empty for a normal reply)."""

    raw = getattr(message, "tool_calls", None)
    if not raw:
        return []
    calls: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            calls.append(item)
        else:
            calls.append(
                {
                    "name": getattr(item, "name", None),
                    "args": getattr(item, "args", {}) or {},
                    "id": getattr(item, "id", None),
                }
            )
    return calls


def invoke_chat_model(
    model: Any, messages: Sequence[BaseMessage], *, api_key: str | None = None
) -> BaseMessage:
    """Invoke a chat model, mapping any failure to a sanitised :class:`LLMError`."""

    try:
        return model.invoke(list(messages))
    except Exception as exc:
        raise map_exception(exc, (api_key,)) from exc


def parse_audit_review(content: str) -> ModelAuditReview:
    """Parse and validate the model's audit review JSON.

    Raises:
        LLMInvalidResponseError: the content was empty, not JSON, or did not match the
            review schema.
    """

    text = strip_code_fences(content or "")
    if not text:
        raise LLMInvalidResponseError(
            "The audit model returned an empty response instead of a JSON review."
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMInvalidResponseError(
            "The audit model returned content that is not valid JSON "
            f"(line {exc.lineno}, column {exc.colno}: {exc.msg})."
        ) from exc

    if isinstance(payload, list):
        payload = {"findings": payload}
    elif not isinstance(payload, dict):
        raise LLMInvalidResponseError(
            "The audit model returned JSON that is neither an object nor an array."
        )
    elif "findings" not in payload and isinstance(payload.get("rule_id"), str):
        payload = {"findings": [payload]}

    try:
        return ModelAuditReview.model_validate(payload)
    except ValidationError as exc:
        raise LLMInvalidResponseError(
            "The audit model output did not match the expected review schema: "
            f"{summarise_validation_error(exc)}"
        ) from exc


def invoke_audit_model(
    chat_model: BaseChatModel,
    messages: Sequence[BaseMessage],
    toolbelt: AuditToolbelt,
    *,
    max_tool_rounds: int = MAX_TOOL_ROUNDS,
    api_key: str | None = None,
) -> tuple[ModelAuditReview, list[str]]:
    """Ask the model for the audit review, following the tools it calls.

    The tool budget is bounded: once it is exhausted the model is asked, without tools,
    for its final JSON review. Tool *names* are returned for the trace; tool arguments
    and results are never stored.

    Raises:
        LLMError: the provider failed, timed out, or returned unusable content.
    """

    requested_names: list[str] = []
    conversation: list[BaseMessage] = list(messages)
    bound = bind_tools_if_supported(chat_model, toolbelt)
    rounds = 0

    while True:
        reply = invoke_chat_model(
            bound if bound is not None else chat_model, conversation, api_key=api_key
        )
        requested = tool_calls_of(reply)
        if not requested:
            return parse_audit_review(content_text(reply)), requested_names

        if rounds >= max_tool_rounds:
            conversation.append(reply)
            conversation.append(
                HumanMessage(
                    content=(
                        "Tool budget exhausted. Return the final JSON review now, using the "
                        "results you already have, and make no further tool calls."
                    )
                )
            )
            final = invoke_chat_model(chat_model, conversation, api_key=api_key)
            return parse_audit_review(content_text(final)), requested_names

        rounds += 1
        conversation.append(reply)
        for call in requested:
            name = str(call.get("name") or "")
            requested_names.append(name)
            output = toolbelt.invoke(name, call.get("args") or {})
            conversation.append(
                ToolMessage(
                    content=json.dumps(output, default=str)[:MAX_TOOL_RESULT_CHARACTERS],
                    tool_call_id=str(call.get("id") or name or "tool-call"),
                )
            )


def make_agent_node(
    *,
    rules: RulesConfig,
    chat_model: BaseChatModel,
    settings: Any,
    toolbelt_factory: Callable[[InvoiceData, Sequence[str]], AuditToolbelt],
    max_tool_rounds: int = MAX_TOOL_ROUNDS,
) -> Callable[[AuditState], dict[str, Any]]:
    """Build the model-review node for one document and model.

    The toolbelt is rebuilt for every invocation from the invoice currently in the
    state, so a re-extraction pass cannot leave the model looking at stale values.
    """

    api_key = getattr(settings, "api_key", None) if settings is not None else None
    max_characters = int(getattr(settings, "llm_max_input_characters", 0) or 24_000)

    def agent_review(state: AuditState) -> dict[str, Any]:
        """Ask the model for one finding per applicable rule."""

        node = ["agent_review"]
        invoice = invoice_from_state(state)
        if invoice is None:
            return {
                "nodes": node,
                "audit_status": "cannot_audit",
                "failure_kind": "missing_extraction",
                "error": "No extracted invoice fields are available for the model review.",
            }

        toolbelt = toolbelt_factory(invoice, state.get("missing_fields", []))
        deterministic = [
            AuditFinding.model_validate(item)
            for item in state.get("deterministic_findings", [])
        ]
        applicable = rules.applicable_rules(invoice.vat_classification)
        rules_payload = [
            rule.as_tool_payload(
                tolerance=rules.monetary_tolerance,
                review_threshold=rules.human_review_confidence_threshold,
            )
            for rule in applicable
        ]
        messages = [
            SystemMessage(content=AUDITOR_SYSTEM_PROMPT),
            HumanMessage(
                content=build_auditor_prompt(
                    invoice=invoice,
                    rules_payload=rules_payload,
                    deterministic_findings=deterministic,
                    audit_context=toolbelt.audit_context,
                    evidence_lines=toolbelt.evidence_lines(),
                    max_characters=max_characters,
                )
            ),
        ]
        model_calls = int(state.get("model_calls", 0) or 0) + 1

        try:
            review, tool_names = invoke_audit_model(
                chat_model,
                messages,
                toolbelt,
                max_tool_rounds=max_tool_rounds,
                api_key=api_key,
            )
        except LLMError as exc:
            failure = (
                "invalid_model_output"
                if isinstance(exc, LLMInvalidResponseError)
                else "model_error"
            )
            return {
                "nodes": node,
                "model_calls": model_calls,
                "model_findings": [],
                "failure_kind": failure,
                "error": str(exc),
                "warnings": [f"The audit model could not be used: {exc}"],
            }

        return {
            "nodes": node,
            "model_calls": model_calls,
            "tool_calls": tool_names,
            "model_findings": [item.model_dump(mode="json") for item in review.findings],
            "overall_assessment": _bounded_explanation(review.overall_assessment),
            "failure_kind": None,
            "review_reasons": toolbelt.review_reasons,
        }

    return agent_review


# --- guardrails ------------------------------------------------------------------


def confidence_from(value: str | int | float | None) -> tuple[float, str | None]:
    """Parse a model confidence into 0-100, with a warning note when it is adjusted."""

    parsed = parse_amount(value)
    if parsed is None:
        return (
            MODEL_CONFIDENCE_FALLBACK,
            "The model gave no usable confidence for {rule_id}; "
            f"{_confidence_text(MODEL_CONFIDENCE_FALLBACK)}% was assumed and the finding "
            "therefore needs review.",
        )
    number = float(parsed)
    if number < 0 or number > 100:
        clamped = min(100.0, max(0.0, number))
        return (
            clamped,
            "The model returned a confidence of {rule_id}="
            f"{number:g} which is outside 0-100; it was clamped to {clamped:g}.",
        )
    return number, None


def validate_model_finding(
    item: Any,
    rules: RulesConfig,
    applicable_ids: Sequence[str],
    warnings: list[str],
) -> AuditFinding | None:
    """Validate one model finding against the configured ruleset.

    Returns the finding, or None when it must be discarded: an unknown, disabled or
    inapplicable rule ID, or a shape Pydantic rejects. Every rejection is recorded as
    a warning, never silently dropped.
    """

    try:
        raw = item if isinstance(item, ModelFinding) else ModelFinding.model_validate(item)
    except ValidationError as exc:
        warnings.append(
            "A model finding was discarded because its shape was invalid: "
            f"{summarise_validation_error(exc)}."
        )
        return None

    rule_id = str(raw.rule_id or "").strip().upper()
    if not rule_id:
        warnings.append("A model finding was discarded because it named no rule ID.")
        return None
    if rule_id not in applicable_ids:
        configured = rules.find(rule_id)
        if configured is None:
            reason = "it is not part of the configured ruleset"
        elif not configured.enabled:
            reason = "it is disabled in this deployment"
        else:
            reason = "it does not apply to the reported VAT treatment"
        warnings.append(
            f"A model finding for {rule_id} was discarded because {reason}; rules come "
            "from configuration only."
        )
        return None

    rule = rules.require(rule_id)
    status = str(raw.status or "").strip().lower()
    if status not in VALID_STATUSES:
        warnings.append(
            f"The model returned the invalid status {raw.status!r} for {rule_id}; the "
            "finding was recorded as review_required."
        )
        status = "review_required"

    severity = str(raw.severity or "").strip().lower()
    if severity not in SEVERITY_ORDER:
        if severity:
            warnings.append(
                f"The model returned the unknown severity {raw.severity!r} for {rule_id}; "
                f"the configured severity {rule.severity} was used."
            )
        severity = rule.severity

    confidence, note = confidence_from(raw.confidence_percent)
    if note:
        warnings.append(note.replace("{rule_id}", rule_id))

    review_reason = raw.human_review_reason
    if status == "review_required" and not review_reason:
        review_reason = "The model reported that this rule needs human review."
    if status == "not_applicable" and not review_reason:
        review_reason = None

    return AuditFinding(
        rule_id=rule_id,
        rule_title=rule.title,
        status=status,
        severity=severity,
        explanation=_bounded_explanation(raw.explanation)
        or f"The model returned no explanation for {rule_id}.",
        expected_value=None if raw.expected_value is None else str(raw.expected_value),
        actual_value=None if raw.actual_value is None else str(raw.actual_value),
        page_number=raw.page_number,
        evidence=(
            mask_identifiers(_bounded_explanation(raw.evidence)) if raw.evidence else None
        ),
        confidence_percent=confidence,
        human_review_required=bool(raw.human_review_required)
        or status == "review_required",
        human_review_reason=(
            _bounded_explanation(review_reason) if review_reason else None
        ),
        recommended_action=(
            _bounded_explanation(raw.recommended_action) if raw.recommended_action else None
        ),
        source="model",
    )


def merge_findings(
    rule: Any,
    deterministic: AuditFinding | None,
    model_finding: AuditFinding | None,
    *,
    threshold: float,
    warnings: list[str],
    review_reasons: list[str],
) -> tuple[AuditFinding, bool]:
    """Combine the deterministic result with the model's finding for one rule.

    The deterministic result is authoritative for status and values. When the two
    disagree decisively (one says pass, the other does not), the finding becomes
    ``review_required``, the conflict is recorded, and a human decides - a clean pass is
    never reported while the model disagrees.

    Returns:
        ``(finding, conflicted)``.
    """

    if deterministic is None and model_finding is None:
        reason = (
            f"Neither the deterministic checks nor the model produced a result for "
            f"{rule.rule_id}; the rule was recorded as review_required."
        )
        warnings.append(reason)
        review_reasons.append(f"{rule.rule_id}: no result was produced for this rule.")
        return (
            make_finding(
                rule,
                status="review_required",
                confidence=MODEL_CONFIDENCE_FALLBACK,
                explanation=(
                    "No result was produced for this applicable rule by the deterministic "
                    "checks or the model."
                ),
                review=True,
                review_reason="No result was produced for this rule.",
                action="Re-run the audit or evaluate this rule manually.",
                source="guardrail",
            ),
            False,
        )

    if deterministic is None:
        assert model_finding is not None
        return model_finding, False

    if model_finding is None:
        warnings.append(
            f"The model returned no result for {rule.rule_id}; the deterministic result "
            "was used."
        )
        return deterministic, False

    decisive = deterministic.status in ("pass", "fail")
    disagrees = (
        model_finding.status in ("pass", "fail")
        and model_finding.status != deterministic.status
    )
    if decisive and disagrees:
        warnings.append(
            f"The model reported '{model_finding.status}' for {rule.rule_id} while the "
            f"deterministic calculation reported '{deterministic.status}'; the "
            "deterministic result was kept and the case needs human review."
        )
        review_reasons.append(
            f"{rule.rule_id}: the model disagreed with the deterministic calculation."
        )
        conflicted = deterministic.model_copy(deep=True)
        conflicted.status = "review_required"
        conflicted.human_review_required = True
        conflicted.human_review_reason = (
            f"The model reported '{model_finding.status}' while the deterministic "
            f"calculation reported '{deterministic.status}'."
        )
        conflicted.explanation = _bounded_explanation(
            f"{deterministic.explanation} Model assessment: {model_finding.explanation}"
        )
        if model_finding.evidence and not conflicted.evidence:
            conflicted.evidence = model_finding.evidence
        if model_finding.page_number and not conflicted.page_number:
            conflicted.page_number = model_finding.page_number
        return conflicted, True

    merged = deterministic.model_copy(deep=True)
    if model_finding.evidence and not merged.evidence:
        merged.evidence = model_finding.evidence
    if model_finding.page_number and not merged.page_number:
        merged.page_number = model_finding.page_number
    if model_finding.recommended_action and not merged.recommended_action:
        merged.recommended_action = model_finding.recommended_action
    if model_finding.confidence_percent < threshold and not merged.human_review_required:
        merged.human_review_required = True
        merged.human_review_reason = (
            f"The model's own confidence in this rule "
            f"({_confidence_text(model_finding.confidence_percent)}%) is below the review "
            f"threshold of {threshold:g}%."
        )
    if model_finding.human_review_required and not merged.human_review_required:
        merged.human_review_required = True
        merged.human_review_reason = (
            model_finding.human_review_reason
            or "The model flagged this finding for human review."
        )
    if model_finding.status == "review_required" and merged.status not in (
        "fail",
        "review_required",
    ):
        merged.status = "review_required"
        merged.human_review_required = True
        merged.human_review_reason = (
            model_finding.human_review_reason
            or "The model could not verify this rule from the available evidence."
        )
    return merged, False


def guardrail_validate(state: AuditState, rules: RulesConfig) -> dict[str, Any]:
    """Validate model output against the ruleset and merge it with the arithmetic."""

    node = ["guardrail_validate"]
    invoice = invoice_from_state(state)
    if invoice is None:
        return {
            "nodes": node,
            "audit_status": "cannot_audit",
            "failure_kind": "missing_extraction",
            "error": "No extracted invoice fields are available to validate findings against.",
        }

    applicable = rules.applicable_rules(invoice.vat_classification)
    applicable_ids = [rule.rule_id for rule in applicable]
    deterministic = {
        item.rule_id: item
        for item in (
            AuditFinding.model_validate(raw)
            for raw in state.get("deterministic_findings", [])
        )
    }
    warnings: list[str] = []
    review_reasons: list[str] = list(state.get("review_reasons", []))

    model_by_rule: dict[str, AuditFinding] = {}
    for raw in state.get("model_findings", []):
        finding = validate_model_finding(raw, rules, applicable_ids, warnings)
        if finding is None:
            continue
        existing = model_by_rule.get(finding.rule_id)
        if existing is not None:
            warnings.append(
                f"The model returned more than one finding for {finding.rule_id}; only the "
                "most confident one was kept."
            )
            if finding.confidence_percent <= existing.confidence_percent:
                continue
        model_by_rule[finding.rule_id] = finding

    merged: list[AuditFinding] = []
    conflicts = 0
    threshold = float(rules.human_review_confidence_threshold)
    for rule in applicable:
        finding, conflicted = merge_findings(
            rule,
            deterministic.get(rule.rule_id),
            model_by_rule.get(rule.rule_id),
            threshold=threshold,
            warnings=warnings,
            review_reasons=review_reasons,
        )
        conflicts += 1 if conflicted else 0
        merged.append(finding)

    for finding in merged:
        if finding.status == "review_required" and not finding.human_review_required:
            finding.human_review_required = True
            finding.human_review_reason = (
                finding.human_review_reason
                or "This rule could not be verified from the available evidence."
            )
        if finding.confidence_percent < threshold and not finding.human_review_required:
            finding.human_review_required = True
            finding.human_review_reason = (
                f"Confidence {_confidence_text(finding.confidence_percent)}% is below the "
                f"review threshold of {threshold:g}%."
            )
        if finding.human_review_required:
            review_reasons.append(
                f"{finding.rule_id}: "
                + (finding.human_review_reason or "flagged for human review.")
            )

    order = {rule.rule_id: index for index, rule in enumerate(applicable)}
    merged.sort(
        key=lambda f: (severity_rank(f.severity), order.get(f.rule_id, len(order)))
    )

    if state.get("failure_kind") in MODEL_FAILURE_KINDS:
        # Derived here rather than in the model node, so a successful retry does not
        # leave a stale "no model assessment" review reason behind.
        review_reasons.append(
            "No model assessment is available for this audit; only the deterministic "
            "checks were applied."
        )
    if state.get("extraction_note"):
        review_reasons.append(str(state["extraction_note"]))

    return {
        "nodes": node,
        "findings": [finding.model_dump(mode="json") for finding in merged],
        "warnings": warnings,
        "review_reasons": list(dict.fromkeys(review_reasons)),
        "conflicts": conflicts,
    }


# --- re-extraction ---------------------------------------------------------------


def merge_invoice_values(
    previous: Mapping[str, Any] | None, refreshed: InvoiceData
) -> tuple[InvoiceData, list[str]]:
    """Keep the first extraction's values and fill only the gaps from a second pass.

    A re-extraction never overwrites a value that was already read: the existing value
    may carry evidence that the second pass did not reproduce.

    Returns:
        ``(merged_invoice, filled_field_names)``.
    """

    base = InvoiceData.model_validate(dict(previous)) if previous else InvoiceData()
    updates: dict[str, Any] = {}
    filled: list[str] = []
    for name in INVOICE_FIELD_NAMES:
        if getattr(base, name, None) is None:
            candidate = getattr(refreshed, name, None)
            if candidate is not None:
                updates[name] = candidate
                filled.append(name)
    if not updates:
        return base, []

    merged = base.model_copy(update=updates)
    evidence = dict(base.field_evidence)
    for name in filled:
        entry = refreshed.field_evidence.get(name)
        if entry is not None:
            evidence[name] = entry
    merged.field_evidence = evidence
    merged.missing_fields = [
        name for name in INVOICE_FIELD_NAMES if getattr(merged, name, None) is None
    ]
    merged.warnings = list(
        dict.fromkeys(
            [
                *base.warnings,
                f"A second extraction pass supplied: {', '.join(filled)}.",
                *refreshed.warnings,
            ]
        )
    )[:25]
    return merged, filled


def make_re_extract_node(
    *, record: DocumentRecord, extractor: Any, api_key: str | None = None
) -> Callable[[AuditState], dict[str, Any]]:
    """Build the bounded re-extraction node for one document."""

    pages = [
        (page.page_number, page.text) for page in record.pages if page.text.strip()
    ]
    provider = getattr(extractor, "provider_label", None) or "openai-compatible"

    def re_extract_fields(state: AuditState) -> dict[str, Any]:
        """Try once more for the fields that were missing, then loop back."""

        iterations = int(state.get("iterations", 0) or 0) + 1
        model_calls = int(state.get("model_calls", 0) or 0)
        update: dict[str, Any] = {
            "nodes": ["re_extract_fields"],
            "iterations": iterations,
            "re_extraction_performed": True,
        }
        missing = list(state.get("missing_fields", []))
        if not missing:
            update["warnings"] = [
                "The audit model was retried once because its first response could not be used."
            ]
            return update

        failure: str | None = None
        raw: Any = None
        try:
            raw = extractor.extract_invoice_fields(pages)
        except DocumentNotExtractableError as exc:
            failure = str(exc)
        except Exception as exc:  # noqa: BLE001 - reduced to a sanitised message
            failure = str(map_exception(exc, (api_key,)))

        if failure is not None:
            note = (
                "The missing fields could not be re-extracted, so the report relies on the "
                "first extraction."
            )
            update.update(
                {
                    "model_calls": model_calls,
                    "warnings": [f"The missing fields could not be re-extracted: {failure}"],
                    "extraction_note": note,
                }
            )
            return update

        model_calls += 1
        refreshed = build_invoice_data(
            raw, provider=provider, page_count=record.page_count
        )
        merged, filled = merge_invoice_values(state.get("invoice"), refreshed)

        update.update(
            {
                "model_calls": model_calls,
                "invoice": merged.model_dump(mode="json"),
                "missing_fields": list(merged.missing_fields),
                "classification": merged.vat_classification,
            }
        )
        if filled:
            update["warnings"] = [
                f"A second extraction pass supplied: {', '.join(filled)}."
            ]
            update["extraction_note"] = None
        else:
            update["warnings"] = [
                "A second extraction pass found no additional values for the missing fields."
            ]
            update["extraction_note"] = (
                "The missing fields were not found by a second extraction pass."
            )
        return update

    return re_extract_fields
