"""Deterministic tools the auditor may call (Phase 5, Revision 2).

Every tool is a *deterministic* function over the stored document, the validated
invoice and the configured ruleset:

* the ruleset is only ever read through ``list_rules``/``get_rule``, so the model
  cannot invent, rename or extend a rule, and
* all money arithmetic happens here in Python with ``Decimal`` and the configured
  rounding policy, so the model can never be the sole authority for VAT arithmetic.

Full page text never leaves this module: ``get_page_evidence`` returns a bounded
excerpt, and TRNs and other long digit runs are masked before they are returned or
stored.
"""

import re
from collections.abc import Mapping, Sequence
from decimal import (
    ROUND_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
    Decimal,
    InvalidOperation,
)
from typing import Any

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from app.models.audit import RULE_ID_PATTERN, RulesConfig
from app.models.invoice import INVOICE_FIELD_NAMES, InvoiceData
from app.services.invoice_extractor import truncate

DEVICE_ID_RUN = re.compile(r"\b\d{15}\b")
LONG_DIGIT_RUN = re.compile(r"\b\d{9,}\b")
MASKED_SUFFIX_DIGITS = 4
MAX_REVIEW_REASONS = 10
MAX_TOOL_TEXT_CHARACTERS = 300

ROUNDING_MODES = {
    "ROUND_HALF_UP": ROUND_HALF_UP,
    "ROUND_HALF_EVEN": ROUND_HALF_EVEN,
    "ROUND_DOWN": ROUND_DOWN,
    "ROUND_UP": ROUND_UP,
}
"""Explicit rounding policies supported when reconciling monetary amounts."""


def mask_identifiers(text: str) -> str:
    """Mask TRNs and other long digit runs in free text.

    Tax registration numbers are identifiers, not audit evidence, so only a short
    prefix and suffix are kept. This keeps reports useful without spreading full
    identifiers through logs or prompts.
    """

    def _mask(match: re.Match[str]) -> str:
        digits = match.group(0)
        keep = max(0, len(digits) - MASKED_SUFFIX_DIGITS)
        prefix = digits[: max(0, keep - 8)] if keep > 8 else digits[:1]
        return f"{prefix}****{digits[-MASKED_SUFFIX_DIGITS:]}"

    masked = DEVICE_ID_RUN.sub(_mask, text)
    return LONG_DIGIT_RUN.sub(_mask, masked)


def round_monetary(amount: Decimal, policy: str | None = None) -> Decimal:
    """Round an amount to two decimals with the configured rounding policy."""

    mode = ROUNDING_MODES.get((policy or "ROUND_HALF_UP").upper(), ROUND_HALF_UP)
    return amount.quantize(Decimal("0.01"), rounding=mode)


def parse_amount(value: str | int | float | Decimal | None) -> Decimal | None:
    """Parse a tool argument into ``Decimal``, or None when it is not a number."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip().replace(",", "")
    text = text.replace("AED", "").replace("aed", "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


class RuleIdArgs(BaseModel):
    rule_id: str = Field(description="Configured rule ID, for example VAT_RATE_CHECK.")


class InvoiceFieldArgs(BaseModel):
    field_name: str = Field(
        description="One of: " + ", ".join(INVOICE_FIELD_NAMES) + "."
    )


class PageEvidenceArgs(BaseModel):
    page_number: int = Field(ge=1, description="1-based page number of the document.")
    max_characters: int = Field(
        default=200, ge=20, le=600, description="Maximum excerpt length."
    )


class ExpectedVatArgs(BaseModel):
    taxable_amount: str = Field(description="Taxable amount as a decimal string.")
    rate_percent: str = Field(description="VAT rate as a percentage, for example 5.")


class TotalsArgs(BaseModel):
    subtotal: str = Field(description="Subtotal excluding VAT.")
    vat_amount: str = Field(description="VAT amount.")
    total: str = Field(description="Total including VAT as printed on the invoice.")


class ReviewReasonArgs(BaseModel):
    reason: str = Field(description="Why a human must review this audit.")


class AuditToolbelt:
    """The deterministic tools available to the auditor for one document.

    The belt is built per audit run and closes over the stored pages, the validated
    invoice and the configured ruleset, so nothing sensitive has to travel inside the
    graph state.
    """

    def __init__(
        self,
        *,
        invoice: InvoiceData,
        pages: Sequence[tuple[int, str]] = (),
        rules: RulesConfig,
        missing_fields: Sequence[str] = (),
        max_evidence_characters: int = 200,
    ) -> None:
        self._invoice = invoice
        self._pages = {int(number): text for number, text in pages}
        self._rules = rules
        self._missing_fields = list(missing_fields)
        self._max_evidence_characters = max_evidence_characters
        self._review_reasons: list[str] = []
        self._rules_payload = rules.as_tool_payload()
        self._audit_context = rules.audit_context_payload()
        self._tools: dict[str, BaseTool] = {}
        self._build_tools()

    # --- read-only surface used by the graph ------------------------------------

    @property
    def tools(self) -> list[BaseTool]:
        """The tools, ready for ``bind_tools``."""

        return list(self._tools.values())

    def tool_names(self) -> list[str]:
        """Names of the available tools."""

        return list(self._tools)

    @property
    def review_reasons(self) -> list[str]:
        """Review reasons the model recorded through ``flag_human_review``."""

        return list(self._review_reasons)

    @property
    def rules_payload(self) -> list[dict[str, Any]]:
        """Compact payloads of the enabled rules."""

        return list(self._rules_payload)

    @property
    def audit_context(self) -> dict[str, Any]:
        """Ruleset-level context (version, tolerance, review threshold)."""

        return dict(self._audit_context)

    def evidence_lines(self) -> list[str]:
        """``field: masked snippet`` lines for the fields that carry evidence."""

        lines: list[str] = []
        for name, entry in self._invoice.field_evidence.items():
            if not entry.evidence:
                continue
            snippet = mask_identifiers(
                truncate(" ".join(str(entry.evidence).split()), MAX_TOOL_TEXT_CHARACTERS)
            )
            lines.append(f"{name}: {snippet}")
        return lines

    def invoke(
        self, name: str, args: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Run a tool by name. Failures become ``{"error": ...}``, never exceptions."""

        entry = self._tools.get(str(name))
        if entry is None:
            return {"error": f"unknown tool '{name}'", "available_tools": self.tool_names()}
        try:
            result = entry.invoke(dict(args or {}))
        except Exception as exc:  # noqa: BLE001 - a tool failure must not break the run
            return {"error": f"tool '{name}' failed ({type(exc).__name__})"}
        if isinstance(result, dict):
            return result
        return {"result": result}

    # --- tool construction ------------------------------------------------------

    def _build_tools(self) -> None:
        """Build the deterministic tools for this run."""

        rules = self._rules
        context = self._audit_context

        @tool("list_rules")
        def list_rules() -> dict[str, Any]:
            """List every enabled rule of the configured ruleset with its thresholds."""

            return {
                "ruleset_version": context["ruleset_version"],
                "monetary_tolerance": context["monetary_tolerance"],
                "review_confidence_threshold": context[
                    "human_review_confidence_threshold"
                ],
                "rounding_policy": context["rounding_policy"],
                "rules": list(self._rules_payload),
            }

        @tool("get_rule", args_schema=RuleIdArgs)
        def get_rule(rule_id: str) -> dict[str, Any]:
            """Return one configured rule, with its expected rate and guidance."""

            candidate = str(rule_id).strip().upper()
            if not re.fullmatch(RULE_ID_PATTERN, candidate):
                return {
                    "error": "malformed rule ID",
                    "rule_id": candidate,
                    "available_rule_ids": rules.enabled_rule_ids(),
                }
            configured = rules.find(candidate)
            if configured is None:
                return {
                    "error": "unknown rule ID: it is not part of the configured ruleset",
                    "rule_id": candidate,
                    "available_rule_ids": rules.enabled_rule_ids(),
                }
            if not configured.enabled:
                return {
                    "error": "disabled rule ID for this deployment",
                    "rule_id": candidate,
                    "available_rule_ids": rules.enabled_rule_ids(),
                }
            return configured.as_tool_payload(
                tolerance=rules.monetary_tolerance,
                review_threshold=rules.human_review_confidence_threshold,
            )

        @tool("get_invoice_field", args_schema=InvoiceFieldArgs)
        def get_invoice_field(field_name: str) -> dict[str, Any]:
            """Return one extracted invoice field with its masked evidence snippet."""

            name = str(field_name).strip().lower()
            if name not in INVOICE_FIELD_NAMES:
                return {
                    "error": "unknown field",
                    "available_fields": list(INVOICE_FIELD_NAMES),
                }
            value = getattr(self._invoice, name, None)
            evidence = self._invoice.field_evidence.get(name)
            snippet = None
            if evidence is not None and evidence.evidence:
                snippet = mask_identifiers(
                    truncate(
                        " ".join(str(evidence.evidence).split()),
                        MAX_TOOL_TEXT_CHARACTERS,
                    )
                )
            return {
                "field": name,
                "value": None if value is None else str(value),
                "present": value is not None,
                "confidence_percent": (
                    None if evidence is None else evidence.confidence_percent
                ),
                "page_number": None if evidence is None else evidence.page_number,
                "evidence": snippet,
                "missing_fields": list(self._missing_fields),
            }

        @tool("get_page_evidence", args_schema=PageEvidenceArgs)
        def get_page_evidence(page_number: int, max_characters: int = 200) -> dict[str, Any]:
            """Return a bounded, masked excerpt of one page; never the whole page."""

            text = self._pages.get(int(page_number))
            if text is None:
                return {"error": "unknown page", "available_pages": sorted(self._pages)}
            collapsed = " ".join(text.split())
            excerpt = mask_identifiers(truncate(collapsed, int(max_characters)))
            return {
                "page_number": int(page_number),
                "excerpt": excerpt,
                "excerpt_truncated": len(collapsed) > len(excerpt),
            }

        for entry in (list_rules, get_rule, get_invoice_field, get_page_evidence):
            self._tools[entry.name] = entry

        @tool("compute_expected_vat", args_schema=ExpectedVatArgs)
        def compute_expected_vat(taxable_amount: str, rate_percent: str) -> dict[str, Any]:
            """Compute taxable amount x rate with Decimal and the configured rounding."""

            taxable = parse_amount(taxable_amount)
            rate = parse_amount(rate_percent)
            if taxable is None or rate is None:
                return {"error": "taxable_amount and rate_percent must be decimal numbers"}
            expected = round_monetary(
                taxable * rate / Decimal("100"), rules.rounding_policy
            )
            return {
                "taxable_amount": str(taxable),
                "rate_percent": str(rate),
                "expected_vat": str(expected),
                "rounding_policy": rules.rounding_policy,
            }

        @tool("check_invoice_totals", args_schema=TotalsArgs)
        def check_invoice_totals(
            subtotal: str, vat_amount: str, total: str
        ) -> dict[str, Any]:
            """Compare total with subtotal + VAT inside the configured AED tolerance."""

            amounts = {
                "subtotal": parse_amount(subtotal),
                "vat_amount": parse_amount(vat_amount),
                "total": parse_amount(total),
            }
            if any(value is None for value in amounts.values()):
                return {"error": "subtotal, vat_amount and total must be decimal numbers"}
            expected = round_monetary(
                amounts["subtotal"] + amounts["vat_amount"], rules.rounding_policy
            )
            difference = round_monetary(
                amounts["total"] - expected, rules.rounding_policy
            )
            return {
                "expected_total": str(expected),
                "actual_total": str(amounts["total"]),
                "difference": str(difference),
                "tolerance": str(rules.monetary_tolerance),
                "within_tolerance": abs(difference) <= rules.monetary_tolerance,
            }

        @tool("flag_human_review", args_schema=ReviewReasonArgs)
        def flag_human_review(reason: str) -> dict[str, Any]:
            """Record a reason why a human must review this audit."""

            cleaned = truncate(
                mask_identifiers(" ".join(str(reason).split())),
                MAX_TOOL_TEXT_CHARACTERS,
            )
            if not cleaned:
                return {"error": "reason must not be empty"}
            if cleaned in self._review_reasons:
                return {"recorded": False, "reason": "already recorded"}
            if len(self._review_reasons) >= MAX_REVIEW_REASONS:
                return {"recorded": False, "reason": "the review-reason limit was reached"}
            self._review_reasons.append(cleaned)
            return {"recorded": True, "review_reasons": list(self._review_reasons)}

        self._tools[compute_expected_vat.name] = compute_expected_vat
        self._tools[check_invoice_totals.name] = check_invoice_totals
        self._tools[flag_human_review.name] = flag_human_review