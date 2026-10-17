"""Tests for the configurable VAT rules engine (Phase 4, Revision 2).

No LLM and no network are involved: these tests cover the authoritative ruleset that
the deterministic checks and the agent read.
"""

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.audit import rules_config
from app.audit.rules_config import (
    DEFAULT_RULE_IDS,
    HUMAN_REVIEW_CONFIDENCE_THRESHOLD,
    LEGISLATION_REVIEW_NOTE,
    STANDARD_VAT_RATE_PERCENT,
    TRN_FORMAT_PATTERN,
    build_rules_config,
    default_rules,
    get_rules_config,
    resolve_expected_vat_rate,
    validate_rule_id,
)
from app.config import (
    DEFAULT_HUMAN_REVIEW_CONFIDENCE_THRESHOLD,
    DEFAULT_MONETARY_TOLERANCE,
    DEFAULT_ROUNDING_POLICY,
    DEFAULT_RULESET_VERSION,
    AuditSettings,
    get_audit_settings,
)
from app.models.audit import (
    AuditRule,
    DisabledRuleError,
    RulesConfig,
    UnknownRuleError,
)

DOCUMENTED_RULE_IDS = (
    "VAT_RATE_CHECK",
    "SUPPLIER_TRN_CHECK",
    "CUSTOMER_TRN_CHECK",
    "TAXABLE_AMOUNT_VAT_MISMATCH",
    "INVOICE_TOTAL_CHECK",
    "VAT_CLASSIFICATION_REVIEW",
)

AUDIT_ENV_VARIABLES = (
    "VAT_AUDIT_MONETARY_TOLERANCE",
    "VAT_AUDIT_HUMAN_REVIEW_CONFIDENCE_THRESHOLD",
    "VAT_AUDIT_RULESET_VERSION",
    "VAT_AUDIT_ROUNDING_POLICY",
    "VAT_AUDIT_REQUIRES_SUPPLIER_TRN",
    "VAT_AUDIT_REQUIRES_CUSTOMER_TRN",
    "VAT_AUDIT_DISABLED_RULES",
)


@pytest.fixture()
def clean_audit_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove audit variables and caches so documented defaults can be asserted."""

    for name in AUDIT_ENV_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    get_audit_settings.cache_clear()
    get_rules_config.cache_clear()
    yield
    get_audit_settings.cache_clear()
    get_rules_config.cache_clear()


def test_default_ruleset_contains_the_documented_initial_rules(
    clean_audit_environment: None,
) -> None:
    config = get_rules_config()

    assert tuple(config.rule_ids()) == DOCUMENTED_RULE_IDS
    assert DEFAULT_RULE_IDS == DOCUMENTED_RULE_IDS
    assert len(config.rules) == 6
    assert config.enabled_rule_ids() == list(DOCUMENTED_RULE_IDS)
    assert config.disabled_rules() == []


def test_documented_defaults_for_thresholds_and_version(
    clean_audit_environment: None,
) -> None:
    config = get_rules_config()

    assert HUMAN_REVIEW_CONFIDENCE_THRESHOLD == Decimal("80")
    assert DEFAULT_HUMAN_REVIEW_CONFIDENCE_THRESHOLD == Decimal("80")
    assert config.human_review_confidence_threshold == Decimal("80")
    assert config.monetary_tolerance == Decimal("0.01")
    assert DEFAULT_MONETARY_TOLERANCE == Decimal("0.01")
    assert config.rounding_policy == "ROUND_HALF_UP"
    assert DEFAULT_ROUNDING_POLICY == "ROUND_HALF_UP"
    assert config.version == "uae-vat-rules-1.0.0"
    assert DEFAULT_RULESET_VERSION == "uae-vat-rules-1.0.0"
    assert STANDARD_VAT_RATE_PERCENT == Decimal("5")
    assert TRN_FORMAT_PATTERN == r"^\d{15}$"


def test_every_default_rule_is_documented_and_expected(
    clean_audit_environment: None,
) -> None:
    config = get_rules_config()
    severities = {rule.rule_id: rule.severity for rule in config.rules}

    for rule in config.rules:
        assert rule.title and rule.description and rule.guidance
        assert rule.enabled is True
        assert rule.applies_to

    assert severities["VAT_RATE_CHECK"] == "high"
    assert severities["SUPPLIER_TRN_CHECK"] == "medium"
    assert severities["CUSTOMER_TRN_CHECK"] == "low"
    assert severities["TAXABLE_AMOUNT_VAT_MISMATCH"] == "high"
    assert severities["INVOICE_TOTAL_CHECK"] == "high"
    assert severities["VAT_CLASSIFICATION_REVIEW"] == "medium"

    assert LEGISLATION_REVIEW_NOTE in rules_config.LEGISLATION_REVIEW_NOTE
    assert "FTA" in LEGISLATION_REVIEW_NOTE


def test_trn_requirements_follow_the_configured_scenario(
    clean_audit_environment: None,
) -> None:
    config = get_rules_config()

    supplier = config.require("SUPPLIER_TRN_CHECK")
    customer = config.require("CUSTOMER_TRN_CHECK")

    assert supplier.trn_role == "supplier"
    assert supplier.trn_required is True
    assert customer.trn_role == "customer"
    assert customer.trn_required is False
    assert "not_applicable" in (customer.guidance or "")
    assert "15 digits" in (supplier.guidance or "")
    assert "Federal Tax Authority" in (supplier.guidance or "")
    assert "format check only" in (customer.guidance or "")


def test_environment_configures_tolerance_threshold_trn_and_disabled_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VAT_AUDIT_MONETARY_TOLERANCE", "0.05")
    monkeypatch.setenv("VAT_AUDIT_HUMAN_REVIEW_CONFIDENCE_THRESHOLD", "90")
    monkeypatch.setenv("VAT_AUDIT_RULESET_VERSION", "uae-vat-rules-2026.1")
    monkeypatch.setenv("VAT_AUDIT_ROUNDING_POLICY", "round_half_even")
    monkeypatch.setenv("VAT_AUDIT_REQUIRES_SUPPLIER_TRN", "no")
    monkeypatch.setenv("VAT_AUDIT_REQUIRES_CUSTOMER_TRN", "yes")
    monkeypatch.setenv("VAT_AUDIT_DISABLED_RULES", "vat_rate_check, CUSTOMER_TRN_CHECK")
    get_audit_settings.cache_clear()
    get_rules_config.cache_clear()

    settings = get_audit_settings()
    config = get_rules_config()

    assert settings.monetary_tolerance == Decimal("0.05")
    assert settings.human_review_confidence_threshold == Decimal("90")
    assert settings.rounding_policy == "ROUND_HALF_EVEN"
    assert settings.disabled_rule_ids == ["VAT_RATE_CHECK", "CUSTOMER_TRN_CHECK"]

    assert config.version == "uae-vat-rules-2026.1"
    assert config.monetary_tolerance == Decimal("0.05")
    assert config.human_review_confidence_threshold == Decimal("90")
    assert config.rounding_policy == "ROUND_HALF_EVEN"
    assert config.enabled_rule_ids() == [
        rule_id
        for rule_id in DOCUMENTED_RULE_IDS
        if rule_id not in {"VAT_RATE_CHECK", "CUSTOMER_TRN_CHECK"}
    ]
    assert config.require("SUPPLIER_TRN_CHECK").trn_required is False
    assert config.find("CUSTOMER_TRN_CHECK").trn_required is True
    assert config.require("INVOICE_TOTAL_CHECK").enabled is True

    context = config.audit_context_payload()
    assert context["disabled_rule_ids"] == ["VAT_RATE_CHECK", "CUSTOMER_TRN_CHECK"]
    assert context["supplier_trn_required"] is False
    assert context["customer_trn_required"] is True
    assert context["ruleset_version"] == "uae-vat-rules-2026.1"

    get_audit_settings.cache_clear()
    get_rules_config.cache_clear()


def test_invalid_environment_values_fall_back_to_the_documented_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VAT_AUDIT_MONETARY_TOLERANCE", "-1")
    monkeypatch.setenv("VAT_AUDIT_HUMAN_REVIEW_CONFIDENCE_THRESHOLD", "150")
    monkeypatch.setenv("VAT_AUDIT_ROUNDING_POLICY", "MAGIC")
    monkeypatch.setenv("VAT_AUDIT_REQUIRES_SUPPLIER_TRN", "maybe")
    monkeypatch.setenv("VAT_AUDIT_RULESET_VERSION", "   ")
    monkeypatch.setenv("VAT_AUDIT_DISABLED_RULES", " , ")
    get_audit_settings.cache_clear()

    settings = get_audit_settings()

    assert settings.monetary_tolerance == Decimal("0.01")
    assert settings.human_review_confidence_threshold == Decimal("80")
    assert settings.rounding_policy == "ROUND_HALF_UP"
    assert settings.requires_supplier_trn is True
    assert settings.ruleset_version == DEFAULT_RULESET_VERSION
    assert settings.disabled_rule_ids == []

    get_audit_settings.cache_clear()


def test_settings_reject_unsupported_values_when_built_directly() -> None:
    with pytest.raises(ValidationError):
        AuditSettings(rounding_policy="MAGIC")

    with pytest.raises(ValidationError):
        AuditSettings(monetary_tolerance=Decimal("-0.01"))

    with pytest.raises(ValidationError):
        AuditSettings(human_review_confidence_threshold=Decimal("101"))

    normalised = AuditSettings(rounding_policy="round_down", disabled_rule_ids="a , b,a")
    assert normalised.rounding_policy == "ROUND_DOWN"
    assert normalised.disabled_rule_ids == ["A", "B"]
    assert normalised.rounding_quantum == Decimal("0.01")
    assert "ruleset=" in normalised.describe()


def test_applicable_rules_follow_the_vat_classification(
    clean_audit_environment: None,
) -> None:
    config = get_rules_config()

    standard = config.applicable_rule_ids("standard_rated")
    assert "VAT_RATE_CHECK" in standard
    assert "TAXABLE_AMOUNT_VAT_MISMATCH" in standard
    assert "INVOICE_TOTAL_CHECK" in standard
    assert "VAT_CLASSIFICATION_REVIEW" not in standard

    unknown = config.applicable_rule_ids("unknown")
    assert "VAT_CLASSIFICATION_REVIEW" in unknown
    assert "VAT_RATE_CHECK" in unknown
    assert "TAXABLE_AMOUNT_VAT_MISMATCH" not in unknown

    zero = set(config.applicable_rule_ids("zero_rated"))
    assert "VAT_RATE_CHECK" not in zero
    assert {"TAXABLE_AMOUNT_VAT_MISMATCH", "VAT_CLASSIFICATION_REVIEW"} <= zero
    assert zero == set(config.applicable_rule_ids("exempt"))


def test_unknown_classification_never_assumes_a_vat_rate(
    clean_audit_environment: None,
) -> None:
    config = get_rules_config()
    rule = config.require("VAT_RATE_CHECK")

    assert resolve_expected_vat_rate(rule, "standard_rated") == Decimal("5")
    assert resolve_expected_vat_rate(rule, "zero_rated") == Decimal("0")
    assert resolve_expected_vat_rate(rule, "exempt") == Decimal("0")
    assert resolve_expected_vat_rate(rule, "unknown") is None

    for configured in config.rules:
        for treatment in ("zero_rated", "exempt"):
            assert resolve_expected_vat_rate(configured, treatment) != Decimal("5")


def test_rate_rules_cannot_target_zero_rated_or_exempt_supplies() -> None:
    def build(**overrides):
        values = {
            "rule_id": "BAD_RATE_RULE",
            "title": "Bad rate rule",
            "description": "Should never be accepted",
            "check": "expected_vat_rate",
            "expected_vat_rate_percent": Decimal("5"),
        }
        values.update(overrides)
        return AuditRule(**values)

    with pytest.raises(ValidationError):
        build(applies_to=["zero_rated"])
    with pytest.raises(ValidationError):
        build(applies_to=["standard_rated", "exempt"])
    with pytest.raises(ValidationError):
        build(expected_vat_rate_percent=None)

    assert build(applies_to=["standard_rated"]).expected_vat_rate_percent == Decimal("5")


def test_decimal_tolerance_is_used_for_monetary_comparisons() -> None:
    config = build_rules_config(settings=AuditSettings())
    tolerance = config.monetary_tolerance

    assert isinstance(tolerance, Decimal)
    assert tolerance == Decimal("0.01")

    rate = resolve_expected_vat_rate(
        config.require("TAXABLE_AMOUNT_VAT_MISMATCH"), "standard_rated"
    )
    expected = (Decimal("1000.00") * rate / Decimal("100")).quantize(Decimal("0.01"))
    assert expected == Decimal("50.00")
    assert abs(Decimal("50.01") - expected) <= tolerance
    assert abs(Decimal("50.02") - expected) > tolerance

    per_rule = AuditRule(
        rule_id="STRICT_TOLERANCE_CHECK",
        title="Stricter tolerance",
        description="A rule may tighten the ruleset tolerance.",
        check="invoice_total",
        tolerance=Decimal("0.001"),
    )
    assert per_rule.effective_tolerance(tolerance) == Decimal("0.001")
    assert config.require("INVOICE_TOTAL_CHECK").effective_tolerance(
        tolerance
    ) == Decimal("0.01")

    override = AuditRule(
        rule_id="RULE_REVIEW_THRESHOLD",
        title="Rule specific threshold",
        description="A rule may override the review threshold.",
        check="invoice_total",
        review_confidence_threshold=Decimal("95"),
    )
    assert override.review_threshold(config.human_review_confidence_threshold) == Decimal(
        "95"
    )
    assert config.require("INVOICE_TOTAL_CHECK").review_threshold(
        config.human_review_confidence_threshold
    ) == Decimal("80")


def test_disabled_rules_are_off_excluded_and_rejected(
    clean_audit_environment: None,
) -> None:
    config = build_rules_config(
        settings=AuditSettings(
            disabled_rule_ids=["customer_trn_check", "INVOICE_TOTAL_CHECK"]
        )
    )

    assert config.find("CUSTOMER_TRN_CHECK").enabled is False
    assert "CUSTOMER_TRN_CHECK" not in config.enabled_rule_ids()
    assert {rule.rule_id for rule in config.disabled_rules()} == {
        "CUSTOMER_TRN_CHECK",
        "INVOICE_TOTAL_CHECK",
    }
    assert "CUSTOMER_TRN_CHECK" not in config.applicable_rule_ids("standard_rated")
    assert "CUSTOMER_TRN_CHECK" not in {
        payload["rule_id"] for payload in config.as_tool_payload()
    }

    with pytest.raises(DisabledRuleError) as excinfo:
        config.require("CUSTOMER_TRN_CHECK")
    assert "disabled" in str(excinfo.value)

    with pytest.raises(DisabledRuleError):
        validate_rule_id("CUSTOMER_TRN_CHECK", config)

    assert validate_rule_id("SUPPLIER_TRN_CHECK", config) == "SUPPLIER_TRN_CHECK"


def test_unknown_rule_ids_are_rejected(clean_audit_environment: None) -> None:
    config = get_rules_config()

    with pytest.raises(UnknownRuleError) as excinfo:
        validate_rule_id("FRAUD_CHECK", config)

    message = str(excinfo.value)
    assert "FRAUD_CHECK" in message
    assert "VAT_RATE_CHECK" in message
    assert isinstance(excinfo.value, rules_config.RuleConfigError)

    with pytest.raises(UnknownRuleError):
        validate_rule_id("", config)
    with pytest.raises(UnknownRuleError):
        config.require("not_a_rule")
    assert config.find("nope") is None


def test_rule_ids_are_matched_case_insensitively(clean_audit_environment: None) -> None:
    config = get_rules_config()

    assert validate_rule_id(" vat_rate_check ", config) == "VAT_RATE_CHECK"
    assert config.require("invoice_total_check").rule_id == "INVOICE_TOTAL_CHECK"
    assert config.find(" Supplier_Trn_Check ").rule_id == "SUPPLIER_TRN_CHECK"


def test_tool_payload_is_compact_json_safe_and_leaks_nothing(
    clean_audit_environment: None,
) -> None:
    config = get_rules_config()
    payload = config.as_tool_payload()

    assert len(payload) == len(DOCUMENTED_RULE_IDS)
    assert json.loads(json.dumps(payload)) == payload
    assert set(payload[0]) == {
        "rule_id",
        "title",
        "description",
        "check",
        "severity",
        "enabled",
        "applies_to",
        "expected_vat_rate_percent",
        "trn_role",
        "trn_required",
        "tolerance",
        "review_confidence_threshold",
        "guidance",
    }

    for rule_payload in payload:
        encoded = json.dumps(rule_payload)
        assert "api_key" not in encoded.lower()
        assert "sk-" not in encoded
        assert not any(isinstance(value, float) for value in rule_payload.values())
        assert rule_payload["tolerance"] == "0.01"
        assert rule_payload["review_confidence_threshold"] == "80"

    rate_payload = next(p for p in payload if p["rule_id"] == "VAT_RATE_CHECK")
    assert rate_payload["expected_vat_rate_percent"] == "5"
    assert rate_payload["applies_to"] == ["standard_rated", "unknown"]

    supplier_payload = next(p for p in payload if p["rule_id"] == "SUPPLIER_TRN_CHECK")
    assert supplier_payload["trn_role"] == "supplier"
    assert supplier_payload["trn_required"] is True

    context = config.audit_context_payload()
    assert context["enabled_rule_ids"] == list(DOCUMENTED_RULE_IDS)
    assert context["monetary_tolerance"] == "0.01"
    assert context["human_review_confidence_threshold"] == "80"
    assert context["rounding_policy"] == "ROUND_HALF_UP"
    assert context["supplier_trn_required"] is True
    assert context["customer_trn_required"] is False
    assert "api_key" not in json.dumps(context).lower()
    assert config.describe().startswith("ruleset=uae-vat-rules-1.0.0")


def test_missing_configuration_still_produces_the_documented_ruleset(
    clean_audit_environment: None,
) -> None:
    config = build_rules_config(settings=AuditSettings())

    assert len(config.enabled_rules()) == 6
    assert config.version == DEFAULT_RULESET_VERSION
    assert "api_key=set" not in get_audit_settings().describe()


def test_empty_ruleset_is_valid_but_rejects_every_rule_id() -> None:
    config = build_rules_config(settings=AuditSettings(), rules=[])

    assert config.enabled_rule_ids() == []
    assert config.as_tool_payload() == []
    assert config.applicable_rule_ids("standard_rated") == []
    assert config.audit_context_payload()["supplier_trn_required"] is None

    with pytest.raises(UnknownRuleError) as excinfo:
        validate_rule_id("VAT_RATE_CHECK", config)
    assert "configured rules: none" in str(excinfo.value)


def test_invalid_rule_definitions_and_duplicate_ids_are_rejected() -> None:
    duplicate = AuditRule(
        rule_id="DUP_CHECK", title="Duplicate", description="d", check="invoice_total"
    )

    with pytest.raises(ValidationError):
        RulesConfig(version="test", rules=[duplicate, duplicate])
    with pytest.raises(ValidationError):
        AuditRule(
            rule_id="TRN_WITHOUT_ROLE",
            title="t",
            description="d",
            check="trn_presence",
        )
    with pytest.raises(ValidationError):
        AuditRule(
            rule_id="ROLE_ON_WRONG_CHECK",
            title="t",
            description="d",
            check="invoice_total",
            trn_role="supplier",
        )
    with pytest.raises(ValidationError):
        AuditRule(
            rule_id="MISSING_RATE",
            title="t",
            description="d",
            check="taxable_amount_vs_vat",
        )
    with pytest.raises(ValidationError):
        AuditRule(rule_id="1BAD", title="t", description="d", check="invoice_total")
    with pytest.raises(ValidationError):
        AuditRule(
            rule_id="BAD_TREATMENT",
            title="t",
            description="d",
            check="invoice_total",
            applies_to=["not_a_treatment"],
        )
    with pytest.raises(ValidationError):
        AuditRule(
            rule_id="NO_APPLIES_TO",
            title="t",
            description="d",
            check="invoice_total",
            applies_to=[],
        )

    normalised = AuditRule(
        rule_id="lower_case_id",
        title="t",
        description="d",
        check="invoice_total",
        applies_to="standard_rated",
    )
    assert normalised.rule_id == "LOWER_CASE_ID"
    assert normalised.applies_to == ["standard_rated"]
    assert normalised.applies_to_classification("standard_rated") is True
    assert normalised.applies_to_classification("exempt") is False


def test_get_rules_config_is_cached_and_reloadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VAT_AUDIT_RULESET_VERSION", "version-one")
    get_audit_settings.cache_clear()
    get_rules_config.cache_clear()

    first = get_rules_config()
    assert first.version == "version-one"

    monkeypatch.setenv("VAT_AUDIT_RULESET_VERSION", "version-two")
    assert get_rules_config() is first

    get_audit_settings.cache_clear()
    get_rules_config.cache_clear()
    assert get_rules_config().version == "version-two"

    get_audit_settings.cache_clear()
    get_rules_config.cache_clear()


def test_default_rules_function_is_fresh_on_every_call() -> None:
    first = default_rules()
    second = default_rules()

    assert [rule.rule_id for rule in first] == list(DOCUMENTED_RULE_IDS)
    assert first[0] is not second[0]

    disabled = default_rules(disabled_rule_ids=["INVOICE_TOTAL_CHECK"])
    by_id = {rule.rule_id: rule for rule in disabled}
    assert by_id["INVOICE_TOTAL_CHECK"].enabled is False
    assert all(
        rule.enabled
        for rule_id, rule in by_id.items()
        if rule_id != "INVOICE_TOTAL_CHECK"
    )

