"""The configured UAE VAT ruleset (Phase 4, Revision 2).

The ruleset is the auditor's **authoritative configuration**: deterministic checks and
the agent read it, and neither may invent, rename or extend a rule. The default rules
below are a small, documented starting point.

Configuration comes from the environment through
:class:`app.config.AuditSettings`:

* ``VAT_AUDIT_MONETARY_TOLERANCE`` (default ``0.01``)
* ``VAT_AUDIT_HUMAN_REVIEW_CONFIDENCE_THRESHOLD`` (default ``80``)
* ``VAT_AUDIT_RULESET_VERSION`` (default ``uae-vat-rules-1.0.0``)
* ``VAT_AUDIT_ROUNDING_POLICY`` (default ``ROUND_HALF_UP``)
* ``VAT_AUDIT_REQUIRES_SUPPLIER_TRN`` (default ``true``)
* ``VAT_AUDIT_REQUIRES_CUSTOMER_TRN`` (default ``false``)
* ``VAT_AUDIT_DISABLED_RULES`` (comma-separated rule IDs)
"""

from collections.abc import Sequence
from decimal import Decimal
from functools import lru_cache

from app.config import (
    DEFAULT_HUMAN_REVIEW_CONFIDENCE_THRESHOLD,
    DEFAULT_MONETARY_TOLERANCE,
    AuditSettings,
    get_audit_settings,
)
from app.models.audit import (
    RATE_CHECK_ALLOWED_CLASSIFICATIONS,
    ZERO_TREATMENTS,
    AuditRule,
    DisabledRuleError,
    RuleConfigError,
    RulesConfig,
    UnknownRuleError,
)
from app.models.invoice import VatClassification

__all__ = [
    "DEFAULT_RULE_IDS",
    "DEFAULT_TOLERANCE",
    "HUMAN_REVIEW_CONFIDENCE_THRESHOLD",
    "LEGISLATION_REVIEW_NOTE",
    "STANDARD_VAT_RATE_PERCENT",
    "TRN_FORMAT_NOTE",
    "TRN_FORMAT_PATTERN",
    "AuditRule",
    "DisabledRuleError",
    "RuleConfigError",
    "RulesConfig",
    "UnknownRuleError",
    "build_rules_config",
    "default_rules",
    "get_rules_config",
    "resolve_expected_vat_rate",
    "validate_rule_id",
]
"""Public surface of the rules-engine configuration."""

HUMAN_REVIEW_CONFIDENCE_THRESHOLD = DEFAULT_HUMAN_REVIEW_CONFIDENCE_THRESHOLD
"""Default confidence (0-100) below which a finding needs human review (80%)."""

DEFAULT_TOLERANCE = DEFAULT_MONETARY_TOLERANCE
"""Default AED tolerance for monetary comparisons."""

STANDARD_VAT_RATE_PERCENT = Decimal("5")
"""UAE standard VAT rate used as the initial configured expectation."""

TRN_FORMAT_PATTERN = r"^\d{15}$"
"""UAE TRNs are 15 digits; this is a *format* check only."""

TRN_FORMAT_NOTE = (
    "TRN validation is a format check only (15 digits). It does not establish that the "
    "number is registered with the Federal Tax Authority or that it belongs to the "
    "supplier or customer named on the invoice."
)

LEGISLATION_REVIEW_NOTE = (
    "These rules are configuration, not law: review them against current UAE FTA "
    "legislation and guidance before production use."
)


def _apply_disabled(rule: AuditRule, disabled: set[str]) -> AuditRule:
    """Switch a rule off when its ID is listed as disabled."""

    if rule.rule_id in disabled:
        rule.enabled = False
    return rule


def _summary_rules(
    *,
    requires_supplier_trn: bool,
    requires_customer_trn: bool,
    disabled: set[str],
) -> list[AuditRule]:
    """The rate and TRN rules of the default ruleset."""

    rules = [
        AuditRule(
            rule_id="VAT_RATE_CHECK",
            title="Expected VAT rate",
            description=(
                "Compare the VAT rate reported on the invoice with the expected rate for "
                "the reported VAT treatment."
            ),
            check="expected_vat_rate",
            severity="high",
            applies_to=list(RATE_CHECK_ALLOWED_CLASSIFICATIONS),
            expected_vat_rate_percent=STANDARD_VAT_RATE_PERCENT,
            guidance=(
                "The expected rate is only applied when the treatment is known and "
                "rate-bearing (5% standard-rated). A 5% rate is never applied to "
                "zero-rated or exempt supplies; under an unknown treatment no rate is "
                "assumed and the finding is review_required."
            ),
        ),
        AuditRule(
            rule_id="SUPPLIER_TRN_CHECK",
            title="Supplier TRN presence and format",
            description=(
                "Flag a missing supplier TRN, or one that is not 15 digits, when the "
                "configured scenario requires a supplier TRN."
            ),
            check="trn_presence",
            severity="medium",
            trn_role="supplier",
            trn_required=requires_supplier_trn,
            guidance=(
                f"{TRN_FORMAT_NOTE} A missing supplier TRN is only flagged when the "
                "configured scenario requires one."
            ),
        ),
        AuditRule(
            rule_id="CUSTOMER_TRN_CHECK",
            title="Customer TRN presence and format",
            description=(
                "Flag a missing customer TRN, or one that is not 15 digits, only when the "
                "configured scenario requires a customer TRN."
            ),
            check="trn_presence",
            severity="low",
            trn_role="customer",
            trn_required=requires_customer_trn,
            guidance=(
                f"{TRN_FORMAT_NOTE} Off by default: when the configured scenario does not "
                "require a customer TRN the finding is not_applicable, and uncertainty is "
                "reported as review_required rather than as a confirmed violation."
            ),
        ),
    ]
    return [_apply_disabled(rule, disabled) for rule in rules]


def _monetary_rules(
    *, requires_supplier_trn: bool, requires_customer_trn: bool, disabled: set[str]
) -> list[AuditRule]:
    """The monetary and classification rules of the default ruleset."""

    rules = [
        AuditRule(
            rule_id="TAXABLE_AMOUNT_VAT_MISMATCH",
            title="Taxable amount and VAT reconciliation",
            description=(
                "Compare the reported VAT with the taxable amount multiplied by the "
                "applicable configured rate."
            ),
            check="taxable_amount_vs_vat",
            severity="high",
            applies_to=["standard_rated", "zero_rated", "exempt"],
            expected_vat_rate_percent=STANDARD_VAT_RATE_PERCENT,
            guidance=(
                "Expected VAT is the taxable amount multiplied by the rate resolved from "
                "the treatment (5% standard-rated, 0% zero-rated or exempt), compared as "
                "Decimal with the configured rounding policy and AED tolerance. When the "
                "taxable amount or the VAT amount is missing, the finding is "
                "review_required, never a silent pass."
            ),
        ),
        AuditRule(
            rule_id="INVOICE_TOTAL_CHECK",
            title="Invoice total reconciliation",
            description=(
                "Compare the invoice total with subtotal plus VAT, plus any explicitly "
                "supported adjustments."
            ),
            check="invoice_total",
            severity="high",
            guidance=(
                "Discounts, deposits or charges that extraction did not capture are "
                "reported as a warning instead of being silently ignored, and a total that "
                "cannot be reconciled is review_required."
            ),
        ),
        AuditRule(
            rule_id="VAT_CLASSIFICATION_REVIEW",
            title="VAT treatment review",
            description=(
                "Flag an unknown or ambiguous VAT treatment, and review zero-rated or "
                "exempt treatment when the supporting evidence is insufficient."
            ),
            check="classification_review",
            severity="medium",
            applies_to=["unknown", "zero_rated", "exempt"],
            guidance=(
                "Unknown or ambiguous treatment always needs human review, and zero-rated "
                "or exempt treatment is reviewed when the invoice does not carry enough "
                "supporting evidence. Reported as review_required, never as a confirmed "
                "legal violation."
            ),
        ),
    ]
    return [_apply_disabled(rule, disabled) for rule in rules]


def default_rules(
    *,
    requires_supplier_trn: bool = True,
    requires_customer_trn: bool = False,
    disabled_rule_ids: Sequence[str] = (),
) -> list[AuditRule]:
    """Build the documented default ruleset.

    Args:
        requires_supplier_trn: Whether the configured scenario requires a supplier
            TRN; the TRN rule records this and only flags a missing TRN when it is
            required.
        requires_customer_trn: Whether a customer TRN is required. Off by default,
            because a missing customer TRN is not automatically a violation.
        disabled_rule_ids: Rule IDs switched off for this deployment.
    """

    disabled = {str(item).strip().upper() for item in disabled_rule_ids}
    return _summary_rules(
        requires_supplier_trn=requires_supplier_trn,
        requires_customer_trn=requires_customer_trn,
        disabled=disabled,
    ) + _monetary_rules(
        requires_supplier_trn=requires_supplier_trn,
        requires_customer_trn=requires_customer_trn,
        disabled=disabled,
    )


DEFAULT_RULE_IDS: tuple[str, ...] = tuple(rule.rule_id for rule in default_rules())
"""Rule IDs provided by the default ruleset, in configuration order."""


def build_rules_config(
    settings: AuditSettings | None = None, *, rules: Sequence[AuditRule] | None = None
) -> RulesConfig:
    """Build the ruleset for this deployment.

    Args:
        settings: Audit settings to use; defaults to the process-wide, env-driven
            settings.
        rules: Explicit rule list (tests or a future rule pack). When supplied, the
            disabled IDs from the settings are not applied again, because the caller
            owns the ``enabled`` flags.

    Raises:
        pydantic.ValidationError: the resulting configuration is invalid.
    """

    resolved = settings or get_audit_settings()
    configured = (
        list(rules)
        if rules is not None
        else default_rules(
            requires_supplier_trn=resolved.requires_supplier_trn,
            requires_customer_trn=resolved.requires_customer_trn,
            disabled_rule_ids=resolved.disabled_rule_ids,
        )
    )
    return RulesConfig(
        version=resolved.ruleset_version,
        monetary_tolerance=resolved.monetary_tolerance,
        human_review_confidence_threshold=resolved.human_review_confidence_threshold,
        rounding_policy=resolved.rounding_policy,
        rules=configured,
    )


@lru_cache(maxsize=1)
def get_rules_config() -> RulesConfig:
    """The process-wide ruleset, built once from the environment.

    Tests can call ``get_rules_config.cache_clear()`` after changing the environment.
    """

    return build_rules_config()


def validate_rule_id(rule_id: str, config: RulesConfig | None = None) -> str:
    """Return the canonical ID of a configured, enabled rule.

    This is the gate the Phase 5 guardrails use: any rule ID a model returns that is
    not part of the configured ruleset, or is disabled in this deployment, is rejected
    and recorded as a warning instead of becoming a finding.

    Raises:
        UnknownRuleError: the ID is not configured.
        DisabledRuleError: the rule exists in the ruleset but is switched off.
    """

    resolved = config or get_rules_config()
    return resolved.require(rule_id).rule_id


def resolve_expected_vat_rate(
    rule: AuditRule, classification: VatClassification
) -> Decimal | None:
    """The expected VAT rate for a rule and treatment, or None when none may be assumed.

    * ``standard_rated`` -> the rule's configured rate (5% by default).
    * ``zero_rated`` or ``exempt`` -> 0%, so a positive rate is never applied to a
      zero-rated or exempt supply.
    * ``unknown`` -> None: the treatment must be reviewed instead of guessed.
    """

    if classification in ZERO_TREATMENTS:
        return Decimal("0")
    if classification == "standard_rated":
        return rule.expected_vat_rate_percent
    return None