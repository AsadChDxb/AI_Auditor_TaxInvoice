"""Pydantic schemas for the configurable VAT rules engine (Phase 4, Revision 2).

Two design rules drive this module:

* The ruleset is **authoritative configuration** supplied to the auditor. The model
  never invents, renames or extends a rule: an unknown or disabled rule ID returned
  by a model is rejected and recorded as a warning (see ``validate_rule_id`` in
  :mod:`app.audit.rules_config`).
* Uncertain outcomes are surfaced as ``review_required`` rather than as confirmed
  legal violations, and the rules themselves must be reviewed against current UAE
  FTA legislation before production use.

Nothing here calls the LLM and nothing here decides VAT treatment on its own: this
module describes the configuration that the deterministic checks and the agent tools
consume.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.models.invoice import VatClassification

RuleSeverity = Literal["critical", "high", "medium", "low"]
"""Severity of a rule's findings, used for ordering and confidence weighting."""

AuditCheck = Literal[
    "expected_vat_rate",
    "taxable_amount_vs_vat",
    "invoice_total",
    "trn_presence",
    "classification_review",
]
"""Deterministic check a rule maps onto.

``expected_vat_rate``
    Reported rate versus the configured expected rate.
``taxable_amount_vs_vat``
    Reported VAT versus taxable amount multiplied by the applicable rate.
``invoice_total``
    Total versus subtotal plus VAT and any supported adjustments.
``trn_presence``
    TRN presence and format for the configured role.
``classification_review``
    Unknown/ambiguous treatment, or a treatment that needs supporting evidence.
"""

FindingStatus = Literal["pass", "fail", "warning", "not_applicable", "review_required"]
"""Outcome of one rule.

``review_required`` marks uncertain cases (ambiguous treatment, thin evidence);
it is never used to assert that a legal violation happened.
"""

TrnRole = Literal["supplier", "customer"]

RULE_ID_PATTERN = r"^[A-Z][A-Z0-9_]{2,63}$"
"""Rule identifiers are stable, upper-case configuration keys."""

ALL_CLASSIFICATIONS: tuple[VatClassification, ...] = (
    "standard_rated",
    "zero_rated",
    "exempt",
    "unknown",
)
"""Every VAT treatment an invoice can carry."""

ZERO_TREATMENTS: tuple[VatClassification, ...] = ("zero_rated", "exempt")
"""Treatments whose expected VAT is zero; a positive rate must never be applied."""

RATE_CHECK_ALLOWED_CLASSIFICATIONS: tuple[VatClassification, ...] = (
    "standard_rated",
    "unknown",
)
"""A rate rule may only target a rate-bearing or undetermined treatment."""


class RuleConfigError(ValueError):
    """Base class for ruleset configuration problems."""


class UnknownRuleError(RuleConfigError):
    """A rule ID was supplied that is not part of the configured ruleset."""


class DisabledRuleError(RuleConfigError):
    """A rule ID was supplied for a rule that is disabled in this deployment."""


class AuditRule(BaseModel):
    """One configured VAT rule.

    A rule pairs a stable identifier with the deterministic check that implements it,
    the severity of its findings, the treatments it applies to, and the thresholds
    used when comparing values. Rules are configuration, not model output.
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(
        pattern=RULE_ID_PATTERN,
        description="Stable identifier from the configured ruleset.",
    )
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1)
    check: AuditCheck
    severity: RuleSeverity = "medium"
    enabled: bool = True
    applies_to: list[VatClassification] = Field(
        default_factory=lambda: list(ALL_CLASSIFICATIONS),
        description="VAT treatments this rule is evaluated for.",
    )
    expected_vat_rate_percent: Decimal | None = Field(
        default=None,
        ge=0,
        le=100,
        description="Expected rate for rate-based checks (5 for UAE standard-rated).",
    )
    trn_role: TrnRole | None = Field(
        default=None, description="Which party's TRN a trn_presence rule inspects."
    )
    trn_required: bool = Field(
        default=False,
        description="Whether the configured scenario requires that TRN.",
    )
    tolerance: Decimal | None = Field(
        default=None,
        ge=0,
        description="AED tolerance for this rule; None uses the ruleset tolerance.",
    )
    review_confidence_threshold: Decimal | None = Field(
        default=None,
        ge=0,
        le=100,
        description="Review threshold for this rule; None uses the ruleset threshold.",
    )
    guidance: str | None = Field(
        default=None,
        description="Why the rule exists, and what it deliberately does not claim.",
    )

    @field_validator("rule_id", mode="before")
    @classmethod
    def _normalise_rule_id(cls, value: Any) -> Any:
        """Accept lower-case rule IDs from configuration and normalise them."""

        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("applies_to", mode="before")
    @classmethod
    def _normalise_applicability(cls, value: Any) -> Any:
        """Accept a single classification, a list, or a comma-separated string."""

        if value is None:
            return list(ALL_CLASSIFICATIONS)
        items = [value] if isinstance(value, str) else list(value)
        normalised: list[VatClassification] = []
        for item in items:
            classification = str(item).strip().lower()
            if classification not in ALL_CLASSIFICATIONS:
                allowed = ", ".join(ALL_CLASSIFICATIONS)
                raise ValueError(f"applies_to must contain: {allowed}")
            if classification not in normalised:
                normalised.append(classification)
        if not normalised:
            raise ValueError("applies_to must list at least one VAT treatment")
        return normalised

    @model_validator(mode="after")
    def _validate_check_specifics(self) -> "AuditRule":
        """Enforce the invariants each check kind depends on."""

        if self.check == "expected_vat_rate":
            if self.expected_vat_rate_percent is None:
                raise ValueError(
                    "an expected_vat_rate rule needs expected_vat_rate_percent"
                )
            unsupported = [c for c in self.applies_to if c in ZERO_TREATMENTS]
            if unsupported:
                raise ValueError(
                    "an expected_vat_rate rule must not apply to zero-rated or exempt "
                    "supplies; those treatments carry no positive rate"
                )
        if (
            self.check == "taxable_amount_vs_vat"
            and self.expected_vat_rate_percent is None
        ):
            raise ValueError(
                "a taxable_amount_vs_vat rule needs expected_vat_rate_percent for "
                "standard-rated supplies"
            )
        if self.check == "trn_presence" and self.trn_role is None:
            raise ValueError("a trn_presence rule needs trn_role")
        if self.check != "trn_presence" and self.trn_role is not None:
            raise ValueError("trn_role only applies to trn_presence rules")
        return self

    def applies_to_classification(self, classification: VatClassification) -> bool:
        """True when this rule is evaluated for `classification`."""

        return classification in self.applies_to

    def review_threshold(self, default: Decimal) -> Decimal:
        """The review threshold for this rule, falling back to the ruleset value."""

        if self.review_confidence_threshold is None:
            return default
        return self.review_confidence_threshold

    def effective_tolerance(self, default: Decimal) -> Decimal:
        """The AED tolerance for this rule, falling back to the ruleset value."""

        return default if self.tolerance is None else self.tolerance

    def as_tool_payload(
        self, *, tolerance: Decimal, review_threshold: Decimal
    ) -> dict[str, Any]:
        """Compact, JSON-safe payload handed to the agent for this rule.

        Only what an agent needs to reason about the rule is exposed: the ID,
        description, severity, applicability, expected rate and the thresholds. The
        payload never contains invoice data, secrets or executable content.
        """

        return {
            "rule_id": self.rule_id,
            "title": self.title,
            "description": self.description,
            "check": self.check,
            "severity": self.severity,
            "enabled": self.enabled,
            "applies_to": list(self.applies_to),
            "expected_vat_rate_percent": (
                None
                if self.expected_vat_rate_percent is None
                else str(self.expected_vat_rate_percent)
            ),
            "trn_role": self.trn_role,
            "trn_required": self.trn_required if self.trn_role else None,
            "tolerance": str(self.effective_tolerance(tolerance)),
            "review_confidence_threshold": str(self.review_threshold(review_threshold)),
            "guidance": self.guidance,
        }


class RulesConfig(BaseModel):
    """The authoritative ruleset supplied to deterministic checks and the auditor.

    The configuration is validated once (unique rule IDs, valid thresholds) and then
    consumed read-only. Every lookup returns a *configured* rule, so a rule ID a model
    invents can never become part of an audit.
    """

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, description="Ruleset version recorded per run.")
    monetary_tolerance: Decimal = Field(default=Decimal("0.01"), ge=0)
    human_review_confidence_threshold: Decimal = Field(
        default=Decimal("80"), ge=0, le=100
    )
    rounding_policy: str = Field(default="ROUND_HALF_UP")
    rules: list[AuditRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_unique_rule_ids(self) -> "RulesConfig":
        """Reject duplicate rule IDs: a rule ID must identify exactly one rule."""

        seen: set[str] = set()
        duplicates: list[str] = []
        for rule in self.rules:
            if rule.rule_id in seen:
                duplicates.append(rule.rule_id)
            seen.add(rule.rule_id)
        if duplicates:
            raise ValueError(
                "duplicate rule IDs in the ruleset: " + ", ".join(sorted(set(duplicates)))
            )
        return self

    def rules_by_id(self) -> dict[str, AuditRule]:
        """Every configured rule keyed by its rule ID."""

        return {rule.rule_id: rule for rule in self.rules}

    def rule_ids(self) -> list[str]:
        """Configured rule IDs, in configuration order."""

        return [rule.rule_id for rule in self.rules]

    def enabled_rules(self) -> list[AuditRule]:
        """Rules that are switched on for this deployment."""

        return [rule for rule in self.rules if rule.enabled]

    def enabled_rule_ids(self) -> list[str]:
        """Rule IDs that are switched on for this deployment."""

        return [rule.rule_id for rule in self.enabled_rules()]

    def disabled_rules(self) -> list[AuditRule]:
        """Rules that are switched off for this deployment."""

        return [rule for rule in self.rules if not rule.enabled]

    def find(self, rule_id: str) -> AuditRule | None:
        """The configured rule with this ID, or None when it is not configured."""

        return self.rules_by_id().get(str(rule_id).strip().upper())

    def require(self, rule_id: str) -> AuditRule:
        """The configured, enabled rule with this ID.

        Raises:
            UnknownRuleError: the ID is not in the ruleset (for example because a
                model invented it).
            DisabledRuleError: the ID belongs to a rule that is switched off.
        """

        canonical = str(rule_id).strip().upper()
        rule = self.find(canonical)
        if rule is None:
            configured = ", ".join(self.rule_ids()) or "none"
            raise UnknownRuleError(
                f"unknown rule ID '{rule_id}': it is not part of ruleset "
                f"{self.version} (configured rules: {configured})"
            )
        if not rule.enabled:
            raise DisabledRuleError(
                f"rule '{canonical}' is disabled in ruleset {self.version}"
            )
        return rule

    def applicable_rules(self, classification: VatClassification) -> list[AuditRule]:
        """Enabled rules that apply to `classification`, in configuration order."""

        return [
            rule
            for rule in self.enabled_rules()
            if rule.applies_to_classification(classification)
        ]

    def applicable_rule_ids(self, classification: VatClassification) -> list[str]:
        """Rule IDs of the enabled rules that apply to `classification`."""

        return [rule.rule_id for rule in self.applicable_rules(classification)]

    def as_tool_payload(self) -> list[dict[str, Any]]:
        """Compact payloads for every enabled rule, ready to hand to the agent."""

        return [
            rule.as_tool_payload(
                tolerance=self.monetary_tolerance,
                review_threshold=self.human_review_confidence_threshold,
            )
            for rule in self.enabled_rules()
        ]

    def audit_context_payload(self) -> dict[str, Any]:
        """Ruleset-level context recorded with a run and given to the agent.

        Contains no invoice data and no secret: version, thresholds, rounding policy,
        the enabled rule IDs and the configured TRN requirements.
        """

        supplier = next(
            (
                rule.trn_required
                for rule in self.rules
                if rule.trn_role == "supplier"
            ),
            None,
        )
        customer = next(
            (
                rule.trn_required
                for rule in self.rules
                if rule.trn_role == "customer"
            ),
            None,
        )
        return {
            "ruleset_version": self.version,
            "monetary_tolerance": str(self.monetary_tolerance),
            "human_review_confidence_threshold": str(
                self.human_review_confidence_threshold
            ),
            "rounding_policy": self.rounding_policy,
            "enabled_rule_ids": self.enabled_rule_ids(),
            "disabled_rule_ids": [
                rule.rule_id for rule in self.disabled_rules()
            ],
            "supplier_trn_required": supplier,
            "customer_trn_required": customer,
        }

    def describe(self) -> str:
        """A safe one-line summary for diagnostics and logs."""

        return (
            f"ruleset={self.version} rules={len(self.rules)} "
            f"enabled={len(self.enabled_rules())} "
            f"tolerance={self.monetary_tolerance} "
            f"review_threshold={self.human_review_confidence_threshold} "
            f"rounding={self.rounding_policy}"
        )


# --- Phase 5: audit findings, traces and reports --------------------------------

AuditStatus = Literal[
    "completed", "completed_with_warnings", "requires_review", "cannot_audit"
]
"""Status of an audit run.

``completed``
    Every applicable rule was evaluated with no warnings and no review flags.
``completed_with_warnings``
    Evaluated, but something a human should read was recorded (for example a
    dropped model finding).
``requires_review``
    At least one finding needs human review: low confidence, uncertain treatment,
    missing evidence, a deterministic/model conflict, or a model failure.
``cannot_audit``
    The document could not be audited at all (no extracted invoice, or it needs OCR).
"""

FindingSource = Literal["deterministic", "model", "guardrail"]
"""Who produced a finding: our Decimal arithmetic, the model, or the guardrail."""

SEVERITY_WEIGHTS: dict[str, float] = {
    "critical": 4.0,
    "high": 3.0,
    "medium": 2.0,
    "low": 1.0,
}
"""Weights used when a severe finding has to dominate the overall confidence."""

SEVERITY_ORDER: tuple[str, ...] = ("critical", "high", "medium", "low")


def severity_rank(severity: str) -> int:
    """Sort key for severities: ``critical`` first, unknown values last."""

    try:
        return SEVERITY_ORDER.index(severity)
    except ValueError:
        return len(SEVERITY_ORDER)


class ModelFinding(BaseModel):
    """One finding as returned by the model.

    Deliberately *shape-only* leniency: a formatting quirk must not discard an
    otherwise usable audit. Semantic validation (rule ID, status, confidence range)
    happens in the guardrail node, which drops or coerces the offending finding and
    records a warning instead of trusting it.
    """

    model_config = ConfigDict(extra="ignore")

    rule_id: str = ""
    status: str | None = None
    severity: str | None = None
    explanation: str | None = None
    expected_value: str | int | float | None = None
    actual_value: str | int | float | None = None
    page_number: int | None = None
    evidence: str | None = None
    confidence_percent: str | int | float | None = None
    human_review_required: bool = False
    human_review_reason: str | None = None
    recommended_action: str | None = None

    @field_validator("expected_value", "actual_value", "evidence", "explanation",
                     "human_review_reason", "recommended_action", mode="before")
    @classmethod
    def _as_text(cls, value: Any) -> Any:
        """Keep numeric amounts intact by rendering them as text."""

        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, (int, float)):
            return str(value)
        return value


class ModelAuditReview(BaseModel):
    """Structured audit review requested from the model."""

    model_config = ConfigDict(extra="ignore")

    findings: list[ModelFinding] = Field(default_factory=list)
    overall_assessment: str | None = None


class AuditFinding(BaseModel):
    """A validated finding, produced by a deterministic check, the model or the guardrail."""

    rule_id: str
    rule_title: str | None = None
    status: FindingStatus
    severity: RuleSeverity
    explanation: str
    expected_value: str | None = None
    actual_value: str | None = None
    page_number: int | None = Field(None, ge=1)
    evidence: str | None = None
    confidence_percent: float = Field(0, ge=0, le=100)
    human_review_required: bool = False
    human_review_reason: str | None = None
    recommended_action: str | None = None
    source: FindingSource = "model"

    @property
    def needs_attention(self) -> bool:
        """True when the finding is not a clean pass."""

        return self.status in ("fail", "warning", "review_required")


class AuditTrace(BaseModel):
    """Compact, secret-free record of how the audit was produced."""

    nodes: list[str] = Field(default_factory=list)
    iterations: int = 0
    model_calls: int = 0
    tool_calls: list[str] = Field(
        default_factory=list,
        description="Names of the tools the model asked for (never their arguments).",
    )
    re_extraction_performed: bool = False
    rules_evaluated: list[str] = Field(default_factory=list)


class AuditSummary(BaseModel):
    """Counts used by the report header."""

    total: int = 0
    passed: int = 0
    failed: int = 0
    warnings: int = 0
    not_applicable: int = 0
    review_required: int = 0
    severity_counts: dict[str, int] = Field(default_factory=dict)
    ruleset_version: str = ""
    enabled_rule_count: int = 0


class AuditReport(BaseModel):
    """The audit result returned by ``POST /audit/{document_no}`` (Phase 6).

    Documented behaviour:

    * the report is a *review aid*: it never asserts fraud or definitive
      non-compliance, and it names the ruleset version and provider/model that
      produced it,
    * ``audit_status`` follows the rules documented on :data:`AuditStatus`, and
    * the in-memory registry keeps only the **latest** report per document number, so
      a repeated call re-runs the audit and replaces the previous report.
    """

    document_no: str
    invoice_number: str | None = None
    audit_status: AuditStatus
    overall_assessment: str = ""
    overall_confidence_percent: float = Field(0, ge=0, le=100)
    confidence_explanation: str = ""
    provider: str = ""
    model: str | None = None
    ruleset_version: str = ""
    audited_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    findings: list[AuditFinding] = Field(default_factory=list)
    summary: AuditSummary = Field(default_factory=AuditSummary)
    warnings: list[str] = Field(default_factory=list)
    review_reasons: list[str] = Field(default_factory=list)
    human_review_required: bool = False
    trace: AuditTrace = Field(default_factory=AuditTrace)
    message: str | None = Field(
        None,
        description=(
            "Next-step guidance, including what a repeated call for the same document "
            "does."
        ),
    )

    @property
    def is_completed(self) -> bool:
        """True when the run produced a usable assessment (with or without warnings)."""

        return self.audit_status in ("completed", "completed_with_warnings")

    def finding_for(self, rule_id: str) -> AuditFinding | None:
        """The finding for `rule_id`, or None when the rule produced none."""

        for finding in self.findings:
            if finding.rule_id == rule_id:
                return finding
        return None


def summarise_findings(findings: list[AuditFinding]) -> AuditSummary:
    """Count findings per status and severity."""

    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return AuditSummary(
        total=len(findings),
        passed=sum(1 for f in findings if f.status == "pass"),
        failed=sum(1 for f in findings if f.status == "fail"),
        warnings=sum(1 for f in findings if f.status == "warning"),
        not_applicable=sum(1 for f in findings if f.status == "not_applicable"),
        review_required=sum(1 for f in findings if f.status == "review_required"),
        severity_counts=counts,
    )
