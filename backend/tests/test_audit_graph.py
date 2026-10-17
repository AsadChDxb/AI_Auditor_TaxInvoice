"""Tests for the agentic audit engine (Phase 5).

Everything is offline: the chat model is a scripted fake, the ruleset and settings are
built from explicit values, and the extractor is injected. The suite covers the
required scenarios - high-confidence pass, low-confidence finding, ambiguous
classification, missing evidence, model/rule mismatch, deterministic arithmetic
conflict, invalid JSON, timeout, provider failure - plus the bounded agent loop, tool
calling, TRN masking and the report store.
"""

import json
from decimal import Decimal
from typing import Any

import pytest

from app.audit.ai_auditor import (
    AiAuditor,
    DocumentNotAuditableError,
    UnknownDocumentError,
    report_store,
)
from app.audit.graph import build_audit_graph
from app.audit.nodes import (
    MAX_TOOL_ROUNDS,
    overall_confidence,
    run_deterministic_checks,
)
from app.audit.rules_config import get_rules_config
from app.audit.state import initial_state
from app.models.audit import AuditFinding, AuditReport, RulesConfig
from tests.audit_stubs import (
    EXTRACTION_JSON,
    PAGE_TEXT,
    SUPPLIER_TRN,
    default_rules,
    finding_payload,
    make_audit_record,
    make_invoice,
    tool_calling_model,
)
from tests.llm_stubs import (
    TEST_API_KEY,
    FakeProviderError,
    FakeTimeoutError,
    StubExtractor,
    make_settings,
    scripted_model,
)

STANDARD_RATED_RULES = (
    "VAT_RATE_CHECK",
    "SUPPLIER_TRN_CHECK",
    "CUSTOMER_TRN_CHECK",
    "TAXABLE_AMOUNT_VAT_MISMATCH",
    "INVOICE_TOTAL_CHECK",
)


@pytest.fixture(autouse=True)
def clean_report_store() -> Any:
    """Keep the in-memory report store isolated between tests."""

    report_store.clear()
    yield
    report_store.clear()


def find(findings: Any, rule_id: str) -> AuditFinding:
    """The finding for `rule_id`, failing loudly when it is absent."""

    for finding in findings:
        if finding.rule_id == rule_id:
            return finding
    raise AssertionError(
        f"{rule_id} not found; findings were {[f.rule_id for f in findings]}"
    )


def compliant_payload(
    rules: RulesConfig, classification: str = "standard_rated", **overrides: Any
) -> dict[str, Any]:
    """A model review in which every applicable rule is satisfied cleanly."""

    findings = []
    for rule in rules.applicable_rules(classification):
        status = (
            "not_applicable"
            if rule.check == "trn_presence" and not rule.trn_required
            else "pass"
        )
        findings.append(finding_payload(rule.rule_id, status=status, **overrides))
    return {
        "findings": findings,
        "overall_assessment": "The invoice is consistent with the configured rules.",
    }


def run_audit(
    model: Any,
    *,
    record: Any = None,
    rules: RulesConfig | None = None,
    extractor: Any = None,
    max_iterations: int = 2,
) -> AuditReport:
    """Audit a record with an injected model, ruleset and extractor."""

    auditor = AiAuditor(
        chat_model=model,
        settings=make_settings(),
        rules=rules or default_rules(),
        extractor=extractor,
        max_iterations=max_iterations,
    )
    return auditor.audit_record(record or make_audit_record())


# --- deterministic checks --------------------------------------------------------


def test_deterministic_checks_pass_on_a_consistent_invoice() -> None:
    rules = default_rules()

    findings = run_deterministic_checks(rules, make_invoice())

    assert [f.rule_id for f in findings] == list(STANDARD_RATED_RULES)
    assert find(findings, "CUSTOMER_TRN_CHECK").status == "not_applicable"
    for rule_id in ("VAT_RATE_CHECK", "SUPPLIER_TRN_CHECK", "INVOICE_TOTAL_CHECK"):
        assert find(findings, rule_id).status == "pass"
    vat = find(findings, "TAXABLE_AMOUNT_VAT_MISMATCH")
    assert (vat.status, vat.expected_value, vat.actual_value) == ("pass", "50.00", "50.00")
    assert find(findings, "INVOICE_TOTAL_CHECK").expected_value == "1050.00"
    assert all(finding.source == "deterministic" for finding in findings)
    assert not any(finding.human_review_required for finding in findings)


def test_deterministic_vat_mismatch_fails_with_decimal_values() -> None:
    findings = run_deterministic_checks(
        default_rules(), make_invoice(vat_amount=Decimal("49.00"))
    )

    vat = find(findings, "TAXABLE_AMOUNT_VAT_MISMATCH")
    assert vat.status == "fail"
    assert vat.expected_value == "50.00"
    assert vat.actual_value == "49.00"
    assert vat.human_review_required is True
    assert "exceeds the AED 0.01 tolerance" in vat.explanation
    assert "legal conclusion" in (vat.recommended_action or "")


def test_tolerance_accepts_a_one_cent_difference_only() -> None:
    within = find(
        run_deterministic_checks(
            default_rules(), make_invoice(vat_amount=Decimal("50.01"))
        ),
        "TAXABLE_AMOUNT_VAT_MISMATCH",
    )
    outside = find(
        run_deterministic_checks(
            default_rules(), make_invoice(vat_amount=Decimal("50.02"))
        ),
        "TAXABLE_AMOUNT_VAT_MISMATCH",
    )

    assert within.status == "pass"
    assert outside.status == "fail"


def test_missing_vat_amount_is_review_required_and_not_invented() -> None:
    invoice = make_invoice(vat_amount=None, total_including_vat=None)

    findings = run_deterministic_checks(default_rules(), invoice)

    vat = find(findings, "TAXABLE_AMOUNT_VAT_MISMATCH")
    assert vat.status == "review_required"
    assert vat.expected_value is None
    assert vat.human_review_required is True
    assert find(findings, "INVOICE_TOTAL_CHECK").status == "review_required"


def test_unknown_classification_never_assumes_the_standard_rate() -> None:
    invoice = make_invoice(
        vat_classification="unknown", vat_rate_percent=None, vat_amount=None
    )

    findings = run_deterministic_checks(default_rules(), invoice)

    rate = find(findings, "VAT_RATE_CHECK")
    assert rate.status == "review_required"
    assert rate.expected_value is None
    assert "no expected rate was assumed" in rate.explanation
    review = find(findings, "VAT_CLASSIFICATION_REVIEW")
    assert review.status == "review_required"
    assert review.severity == "medium"


def test_zero_rated_with_vat_charged_fails() -> None:
    invoice = make_invoice(
        vat_classification="zero_rated", vat_rate_percent=Decimal("0")
    )

    rules = default_rules()
    findings = run_deterministic_checks(rules, invoice)

    review = find(findings, "VAT_CLASSIFICATION_REVIEW")
    assert review.status == "fail"
    assert review.expected_value == "0.00"
    assert review.actual_value == "50.00"
    # A rate check must never target zero-rated or exempt supplies.
    assert "VAT_RATE_CHECK" not in rules.applicable_rule_ids("zero_rated")
    assert "VAT_RATE_CHECK" not in [f.rule_id for f in findings]
    assert find(findings, "TAXABLE_AMOUNT_VAT_MISMATCH").expected_value == "0.00"


def test_zero_rated_without_evidence_needs_review() -> None:
    invoice = make_invoice(vat_classification="zero_rated", vat_amount=Decimal("0.00"))

    findings = run_deterministic_checks(default_rules(), invoice)

    review = find(findings, "VAT_CLASSIFICATION_REVIEW")
    assert review.status == "review_required"
    assert "supporting evidence" in review.explanation
    assert find(findings, "TAXABLE_AMOUNT_VAT_MISMATCH").status == "pass"


def test_supplier_trn_missing_when_required_fails() -> None:
    findings = run_deterministic_checks(default_rules(), make_invoice(supplier_trn=None))

    trn = find(findings, "SUPPLIER_TRN_CHECK")
    assert trn.status == "fail"
    assert trn.expected_value == "15-digit TRN"
    assert trn.actual_value == "missing"
    assert trn.human_review_required is True


def test_trn_format_check_is_a_format_check_only() -> None:
    findings = run_deterministic_checks(
        default_rules(), make_invoice(supplier_trn="100123456")
    )

    trn = find(findings, "SUPPLIER_TRN_CHECK")
    assert trn.status == "fail"
    assert trn.expected_value == "15 digits"
    assert "Federal Tax Authority" in trn.explanation
    # Stored values are masked; only the malformed shape is reported.
    assert trn.actual_value == "1****3456"
    assert "100123456" not in (trn.actual_value or "")


def test_customer_trn_is_not_a_violation_by_default() -> None:
    invoice = make_invoice(customer_trn=None)

    default = find(
        run_deterministic_checks(default_rules(), invoice), "CUSTOMER_TRN_CHECK"
    )
    required = find(
        run_deterministic_checks(default_rules(requires_customer_trn=True), invoice),
        "CUSTOMER_TRN_CHECK",
    )

    assert default.status == "not_applicable"
    assert default.human_review_required is False
    assert required.status == "fail"
    assert required.human_review_required is True


def test_invoice_total_mismatch_fails_and_mentions_missing_adjustments() -> None:
    findings = run_deterministic_checks(
        default_rules(), make_invoice(total_including_vat=Decimal("1040.00"))
    )

    total = find(findings, "INVOICE_TOTAL_CHECK")
    assert total.status == "fail"
    assert total.expected_value == "1050.00"
    assert "Discounts" in total.explanation
    assert "not extracted" in total.explanation


def test_disabled_rules_are_not_evaluated() -> None:
    rules = default_rules(
        disabled_rule_ids=["INVOICE_TOTAL_CHECK", "customer_trn_check"]
    )

    findings = run_deterministic_checks(rules, make_invoice())

    assert "INVOICE_TOTAL_CHECK" not in [f.rule_id for f in findings]
    assert "CUSTOMER_TRN_CHECK" not in rules.applicable_rule_ids("standard_rated")
    assert rules.find("INVOICE_TOTAL_CHECK").enabled is False


# --- overall confidence ----------------------------------------------------------


def confidence_finding(
    rule_id: str = "VAT_RATE_CHECK",
    *,
    status: str = "pass",
    severity: str = "high",
    confidence: float = 95.0,
    review: bool = False,
) -> AuditFinding:
    """A minimal finding for confidence calculations."""

    return AuditFinding(
        rule_id=rule_id,
        status=status,
        severity=severity,
        explanation="test finding",
        confidence_percent=confidence,
        human_review_required=review,
    )


def test_overall_confidence_is_severity_weighted_and_explained() -> None:
    findings = [
        confidence_finding("VAT_RATE_CHECK", confidence=100.0),
        confidence_finding("INVOICE_TOTAL_CHECK", confidence=100.0),
        confidence_finding(
            "CUSTOMER_TRN_CHECK", status="not_applicable", severity="low"
        ),
    ]

    value, explanation = overall_confidence(findings, threshold=80)

    assert value == 100.0
    assert "severity-weighted mean of 2 finding(s)" in explanation
    assert "weighted double" in explanation
    assert "No capping was necessary." in explanation


def test_low_confidence_finding_caps_the_overall_confidence() -> None:
    findings = [
        confidence_finding("VAT_RATE_CHECK", confidence=100.0),
        confidence_finding("SUPPLIER_TRN_CHECK", severity="medium", confidence=100.0),
        confidence_finding("INVOICE_TOTAL_CHECK", confidence=100.0),
        confidence_finding("TAXABLE_AMOUNT_VAT_MISMATCH", status="fail", confidence=60.0),
    ]

    value, explanation = overall_confidence(findings, threshold=80)

    assert value == 79.0
    assert "below the 80% review threshold" in explanation
    assert "lowest 60%" in explanation


def test_finding_flagged_for_review_caps_the_overall_confidence() -> None:
    findings = [confidence_finding(confidence=99.0, review=True)]

    value, explanation = overall_confidence(findings, threshold=80)

    assert value == 79.0
    assert "flagged for human review" in explanation


def test_model_failure_missing_evidence_and_conflicts_cap_the_value() -> None:
    findings = [confidence_finding(confidence=100.0)]

    model_down, model_note = overall_confidence(
        findings, threshold=80, model_available=False
    )
    missing, missing_note = overall_confidence(
        findings, threshold=80, missing_evidence=True
    )
    conflict, conflict_note = overall_confidence(findings, threshold=80, conflicts=1)

    assert (model_down, missing, conflict) == (40.0, 70.0, 70.0)
    assert "model could not be used" in model_note
    assert "key evidence is missing" in missing_note
    assert "deterministic/model conflict" in conflict_note


def test_overall_confidence_is_zero_when_nothing_was_evaluated() -> None:
    value, explanation = overall_confidence([], threshold=80)

    assert value == 0.0
    assert "nothing to be confident about" in explanation


# --- audit runs ------------------------------------------------------------------


def test_high_confidence_clean_audit_completes() -> None:
    rules = default_rules()
    model = scripted_model(content=json.dumps(compliant_payload(rules)))

    report = run_audit(model, rules=rules)

    assert report.audit_status == "completed"
    assert report.human_review_required is False
    assert report.overall_confidence_percent >= 80
    assert report.review_reasons == []
    assert report.warnings == []
    assert report.summary.total == len(STANDARD_RATED_RULES)
    assert report.summary.passed == len(STANDARD_RATED_RULES) - 1
    assert report.summary.not_applicable == 1
    assert report.ruleset_version == rules.version
    assert report.provider == "test-provider"
    assert report.model == "test-model"
    assert report.invoice_number == "INV-2024-0001"
    assert report.trace.model_calls == 1
    assert report.trace.iterations == 0
    assert report.trace.rules_evaluated == list(STANDARD_RATED_RULES)
    assert report.trace.nodes[:5] == [
        "load_context",
        "select_rules",
        "deterministic_checks",
        "agent_review",
        "guardrail_validate",
    ]
    assert report.is_completed is True
    assert "does not assert fraud" in report.overall_assessment


def test_low_confidence_model_finding_requires_human_review() -> None:
    rules = default_rules()
    model = scripted_model(
        content=json.dumps(compliant_payload(rules, confidence_percent=40))
    )

    report = run_audit(model, rules=rules)

    assert report.audit_status == "requires_review"
    assert report.human_review_required is True
    assert report.overall_confidence_percent <= 79
    assert any(
        "below the review threshold" in reason for reason in report.review_reasons
    )
    assert "Capping applied" in report.confidence_explanation
    assert all(
        finding.human_review_required
        for finding in report.findings
        if finding.status != "not_applicable"
    )


def test_ambiguous_classification_enters_review_without_inventing_a_rate() -> None:
    rules = default_rules()
    invoice = make_invoice(
        vat_classification="unknown", vat_rate_percent=None, vat_amount=None
    )
    payload = compliant_payload(rules, classification="unknown")
    model = scripted_model(content=json.dumps(payload))

    report = run_audit(model, rules=rules, record=make_audit_record(invoice=invoice))

    assert report.audit_status == "requires_review"
    assert "VAT_CLASSIFICATION_REVIEW" in report.trace.rules_evaluated
    review = report.finding_for("VAT_CLASSIFICATION_REVIEW")
    assert review is not None and review.status == "review_required"
    assert any(
        "ambiguous" in reason for reason in report.review_reasons
    ) or any("unknown" in reason.lower() for reason in report.review_reasons)
    assert report.overall_confidence_percent < 80


# --- model output validation -----------------------------------------------------


def test_unknown_rule_id_from_the_model_is_dropped_and_warned() -> None:
    rules = default_rules()
    payload = compliant_payload(rules)
    payload["findings"].append(finding_payload("FRAUD_CHECK", status="fail"))
    model = scripted_model(content=json.dumps(payload))

    report = run_audit(model, rules=rules)

    assert report.finding_for("FRAUD_CHECK") is None
    assert any(
        "FRAUD_CHECK" in warning and "not part of the configured ruleset" in warning
        for warning in report.warnings
    )
    assert report.summary.total == len(STANDARD_RATED_RULES)
    assert report.audit_status == "completed_with_warnings"


def test_model_finding_for_an_inapplicable_rule_is_dropped() -> None:
    rules = default_rules()
    payload = compliant_payload(rules)
    payload["findings"].append(
        finding_payload("VAT_CLASSIFICATION_REVIEW", status="fail")
    )
    model = scripted_model(content=json.dumps(payload))

    report = run_audit(model, rules=rules)

    assert report.finding_for("VAT_CLASSIFICATION_REVIEW") is None
    assert any(
        "does not apply to the reported VAT treatment" in warning
        for warning in report.warnings
    )


def test_invalid_status_is_coerced_to_review_required() -> None:
    rules = default_rules()
    payload = {
        "findings": [
            finding_payload(rule_id, status="definitely_fine")
            for rule_id in STANDARD_RATED_RULES
        ],
        "overall_assessment": "everything is fine",
    }
    model = scripted_model(content=json.dumps(payload))

    report = run_audit(model, rules=rules)

    assert report.audit_status == "requires_review"
    assert any("invalid status" in warning for warning in report.warnings)
    for finding in report.findings:
        assert finding.status in ("review_required", "not_applicable")


def test_model_confidence_outside_the_range_is_clamped_and_warned() -> None:
    rules = default_rules()
    model = scripted_model(
        content=json.dumps(compliant_payload(rules, confidence_percent=140))
    )

    report = run_audit(model, rules=rules)

    assert any("outside 0-100" in warning for warning in report.warnings)
    assert all(finding.confidence_percent <= 100 for finding in report.findings)
    assert report.audit_status in ("completed_with_warnings", "completed")


def test_model_missing_confidence_is_assumed_low_and_reviewed() -> None:
    rules = default_rules()
    model = scripted_model(
        content=json.dumps(compliant_payload(rules, confidence_percent=None))
    )

    report = run_audit(model, rules=rules)

    assert any("no usable confidence" in warning for warning in report.warnings)
    assert report.audit_status == "requires_review"


def test_model_omitting_a_rule_falls_back_to_the_deterministic_result() -> None:
    rules = default_rules()
    model = scripted_model(
        content=json.dumps(
            {
                "findings": [finding_payload("VAT_RATE_CHECK")],
                "overall_assessment": "partial review",
            }
        )
    )

    report = run_audit(model, rules=rules)

    total = report.finding_for("INVOICE_TOTAL_CHECK")
    assert total is not None and total.source == "deterministic"
    assert any(
        "returned no result for INVOICE_TOTAL_CHECK" in warning
        for warning in report.warnings
    )


def test_deterministic_conflict_wins_and_requires_review() -> None:
    rules = default_rules()
    model = scripted_model(content=json.dumps(compliant_payload(rules)))
    invoice = make_invoice(
        vat_amount=Decimal("10.00"), total_including_vat=Decimal("1010.00")
    )

    report = run_audit(
        model, rules=rules, record=make_audit_record(invoice=invoice)
    )

    vat = report.finding_for("TAXABLE_AMOUNT_VAT_MISMATCH")
    assert vat is not None
    assert vat.status == "review_required"
    assert vat.expected_value == "50.00"
    assert vat.actual_value == "10.00"
    assert vat.human_review_required is True
    assert any(
        "the model disagreed with the deterministic calculation" in reason
        for reason in report.review_reasons
    )
    assert any(
        "deterministic result was kept" in warning for warning in report.warnings
    )
    assert report.audit_status == "requires_review"


# --- failure modes ---------------------------------------------------------------


def test_invalid_json_is_not_a_passing_audit() -> None:
    model = scripted_model(content="{this is not json")

    report = run_audit(model)

    assert report.audit_status == "requires_review"
    assert report.overall_confidence_percent <= 40
    assert any("not valid JSON" in warning for warning in report.warnings)
    assert report.findings
    assert all(finding.source == "deterministic" for finding in report.findings)
    assert any(
        "No model assessment is available" in reason
        for reason in report.review_reasons
    )
    assert report.trace.model_calls == 3
    assert report.trace.iterations == 2


def test_timeout_is_a_review_state_not_a_pass() -> None:
    model = scripted_model(error=FakeTimeoutError("provider timed out"))

    report = run_audit(model)

    assert report.audit_status == "requires_review"
    assert report.overall_confidence_percent <= 40
    assert any(
        "did not respond within the configured timeout" in warning
        for warning in report.warnings
    )
    assert report.trace.model_calls == 3


def test_provider_error_is_sanitised_and_the_key_never_leaks() -> None:
    model = scripted_model(error=FakeProviderError(401, f"bad key {TEST_API_KEY}"))

    report = run_audit(model)

    assert report.audit_status == "requires_review"
    dumped = json.dumps(report.model_dump(mode="json"))
    assert TEST_API_KEY not in dumped
    assert "HTTP 401" in dumped
    assert any("HTTP 401" in warning for warning in report.warnings)


def test_model_retry_after_a_failure_can_succeed() -> None:
    rules = default_rules()
    model = scripted_model(
        contents=["not json at all", json.dumps(compliant_payload(rules))]
    )

    report = run_audit(model, rules=rules)

    assert report.trace.iterations == 1
    assert report.trace.model_calls == 2
    assert report.trace.re_extraction_performed is True
    assert report.audit_status in ("completed", "completed_with_warnings")
    assert report.finding_for("INVOICE_TOTAL_CHECK") is not None


# --- tools and re-extraction loops -----------------------------------------------


def test_tool_calling_model_is_followed_and_recorded() -> None:
    rules = default_rules()
    model = tool_calling_model(
        [
            {
                "tool_calls": [
                    {"name": "get_invoice_field", "args": {"field_name": "vat_amount"}}
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "compute_expected_vat",
                        "args": {"taxable_amount": "1000.00", "rate_percent": "5"},
                    }
                ]
            },
            {"content": json.dumps(compliant_payload(rules))},
        ]
    )

    report = run_audit(model, rules=rules)

    assert report.trace.tool_calls == ["get_invoice_field", "compute_expected_vat"]
    assert report.audit_status == "completed"
    assert len(model.bound_tool_names) == 7
    assert "list_rules" in model.bound_tool_names
    tool_messages = [
        message for message in model.sent[-1] if getattr(message, "type", "") == "tool"
    ]
    assert any("expected_vat" in str(getattr(m, "content", "")) for m in tool_messages)


def test_tool_budget_is_bounded_and_the_final_answer_is_requested() -> None:
    rules = default_rules()
    model = tool_calling_model(
        [
            {"tool_calls": [{"name": "list_rules"}]},
            {"tool_calls": [{"name": "list_rules"}]},
            {"tool_calls": [{"name": "list_rules"}]},
            {"tool_calls": [{"name": "list_rules"}]},
            {"content": json.dumps(compliant_payload(rules))},
        ]
    )

    report = run_audit(model, rules=rules)

    assert report.trace.tool_calls == ["list_rules"] * MAX_TOOL_ROUNDS
    assert report.audit_status == "completed"
    assert model.calls == MAX_TOOL_ROUNDS + 2
    assert "Tool budget exhausted" in str(model.sent[-1][-1].content)


def test_re_extraction_fills_missing_fields_once() -> None:
    rules = default_rules()
    model = scripted_model(contents=[json.dumps(compliant_payload(rules))] * 2)
    extractor = StubExtractor(fields=EXTRACTION_JSON)
    record = make_audit_record(invoice=make_invoice(vat_amount=None))

    report = run_audit(model, rules=rules, record=record, extractor=extractor)

    assert report.trace.iterations == 1
    assert report.trace.re_extraction_performed is True
    assert report.trace.model_calls == 3
    assert len(extractor.calls) == 1
    assert any("second extraction pass supplied" in w.lower() for w in report.warnings)
    assert report.finding_for("TAXABLE_AMOUNT_VAT_MISMATCH").status == "pass"
    assert report.audit_status == "completed_with_warnings"


def test_re_extraction_is_bounded_when_the_field_stays_missing() -> None:
    rules = default_rules()
    model = scripted_model(contents=[json.dumps(compliant_payload(rules))] * 3)
    extractor = StubExtractor(fields={})
    record = make_audit_record(invoice=make_invoice(vat_amount=None))

    report = run_audit(model, rules=rules, record=record, extractor=extractor)

    assert report.trace.iterations == 2
    assert report.audit_status == "requires_review"
    assert any(
        "not found by a second extraction pass" in reason
        for reason in report.review_reasons
    )
    assert report.finding_for("TAXABLE_AMOUNT_VAT_MISMATCH").status == "review_required"


def test_re_extraction_failure_keeps_the_first_extraction() -> None:
    rules = default_rules()
    model = scripted_model(contents=[json.dumps(compliant_payload(rules))] * 2)
    extractor = StubExtractor(error=FakeTimeoutError("too slow"))
    record = make_audit_record(invoice=make_invoice(vat_amount=None))

    report = run_audit(model, rules=rules, record=record, extractor=extractor)

    assert any("could not be re-extracted" in warning for warning in report.warnings)
    assert report.audit_status == "requires_review"


# --- cannot-audit paths ----------------------------------------------------------


def test_graph_reports_cannot_audit_when_the_invoice_is_missing() -> None:
    rules = default_rules()
    model = scripted_model(content="{}")
    record = make_audit_record(invoice=None)

    graph = build_audit_graph(
        record, chat_model=model, settings=make_settings(), rules=rules
    )
    final = graph.invoke(
        initial_state(
            document_no=record.document_no, page_count=record.page_count, invoice=None
        ),
        config={"configurable": {"thread_id": "t-missing"}, "recursion_limit": 20},
    )

    assert final["audit_status"] == "cannot_audit"
    assert final["failure_kind"] == "missing_extraction"
    assert final["nodes"] == ["load_context", "cannot_audit"]
    assert model.calls == []


def test_graph_reports_cannot_audit_for_a_document_that_needs_ocr() -> None:
    rules = default_rules()
    model = scripted_model(content="{}")
    record = make_audit_record(status="needs_ocr", page_texts=("",))

    graph = build_audit_graph(
        record, chat_model=model, settings=make_settings(), rules=rules
    )
    final = graph.invoke(
        initial_state(
            document_no=record.document_no, page_count=record.page_count, invoice=None
        ),
        config={"configurable": {"thread_id": "t-ocr"}, "recursion_limit": 20},
    )

    assert final["audit_status"] == "cannot_audit"
    assert final["failure_kind"] == "needs_ocr"
    assert model.calls == []


def test_service_refuses_to_audit_a_document_that_needs_ocr() -> None:
    auditor = AiAuditor(
        chat_model=scripted_model(content="{}"),
        settings=make_settings(),
        rules=default_rules(),
    )

    with pytest.raises(DocumentNotAuditableError):
        auditor.audit_record(make_audit_record(status="needs_ocr", page_texts=("",)))


def test_service_refuses_to_audit_a_document_without_extracted_fields() -> None:
    auditor = AiAuditor(
        chat_model=scripted_model(content="{}"),
        settings=make_settings(),
        rules=default_rules(),
    )

    with pytest.raises(DocumentNotAuditableError):
        auditor.audit_record(make_audit_record(invoice=None))


def test_unknown_document_number_raises() -> None:
    auditor = AiAuditor(
        chat_model=scripted_model(content="{}"),
        settings=make_settings(),
        rules=default_rules(),
    )

    with pytest.raises(UnknownDocumentError):
        auditor.audit("DOC-DOES-NOT-EXIST")


def test_audit_stores_the_latest_report_per_document_number() -> None:
    from app.storage.memory import document_registry

    rules = default_rules()
    record = make_audit_record()
    document_registry.add(record)
    auditor = AiAuditor(
        chat_model=scripted_model(content=json.dumps(compliant_payload(rules))),
        settings=make_settings(),
        rules=rules,
    )

    first = auditor.audit(record.document_no)
    second = auditor.audit(record.document_no)

    assert report_store.get(record.document_no) is second
    assert report_store.get("DOC-UNKNOWN") is None
    assert len(report_store) == 1
    assert first.document_no == second.document_no


# --- data handling ---------------------------------------------------------------


def test_report_contains_no_secrets_full_page_text_or_raw_trn() -> None:
    rules = default_rules()
    long_page = f"{PAGE_TEXT} " + "X" * 4000
    record = make_audit_record(page_texts=(long_page,))
    model = scripted_model(content=json.dumps(compliant_payload(rules)))

    report = run_audit(model, rules=rules, record=record)
    dumped = json.dumps(report.model_dump(mode="json"))

    assert TEST_API_KEY not in dumped
    assert "sk-" not in dumped
    assert SUPPLIER_TRN not in dumped
    assert "100****0003" in dumped
    assert "X" * 100 not in dumped
    assert long_page not in dumped


def test_page_evidence_is_bounded_and_masked() -> None:
    from app.audit.tools import AuditToolbelt

    toolbelt = AuditToolbelt(
        invoice=make_invoice(),
        pages=[(1, f"{PAGE_TEXT} {SUPPLIER_TRN}")],
        rules=default_rules(),
    )

    excerpt = toolbelt.invoke("get_page_evidence", {"page_number": 1})
    missing = toolbelt.invoke("get_page_evidence", {"page_number": 9})
    unknown_tool = toolbelt.invoke("nope")

    assert SUPPLIER_TRN not in excerpt["excerpt"]
    assert excerpt["excerpt_truncated"] is True
    assert missing["error"] == "unknown page"
    assert unknown_tool["error"].startswith("unknown tool")
    assert unknown_tool["available_tools"] == toolbelt.tool_names()


def test_identical_runs_produce_identical_findings() -> None:
    rules = default_rules()
    payload = json.dumps(compliant_payload(rules))

    first = run_audit(scripted_model(content=payload), rules=rules)
    second = run_audit(scripted_model(content=payload), rules=rules)

    assert [f.model_dump() for f in first.findings] == [
        f.model_dump() for f in second.findings
    ]
    assert first.audit_status == second.audit_status
    assert first.overall_confidence_percent == second.overall_confidence_percent
    assert first.summary.model_dump() == second.summary.model_dump()


def test_ruleset_version_and_enabled_rule_count_are_recorded() -> None:
    rules = default_rules(
        ruleset_version="uae-vat-rules-test", human_review_confidence_threshold=70
    )
    model = scripted_model(
        content=json.dumps(compliant_payload(rules, confidence_percent=72))
    )

    report = run_audit(model, rules=rules)

    assert report.ruleset_version == "uae-vat-rules-test"
    assert report.summary.ruleset_version == "uae-vat-rules-test"
    assert report.summary.enabled_rule_count == 6
    assert report.audit_status == "completed"


def test_process_wide_ruleset_is_still_the_configured_default() -> None:
    """The process-wide ruleset stays available for the Phase 6 API layer."""

    config = get_rules_config()

    assert config.rule_ids() == list(get_rules_config().rule_ids())
    assert config.version.startswith("uae-vat-rules-")


# --- graph state contract --------------------------------------------------------


def invoke_graph(record: Any, model: Any, thread: str) -> dict[str, Any]:
    """Run the compiled graph once and return the final state."""

    graph = build_audit_graph(
        record, chat_model=model, settings=make_settings(), rules=default_rules()
    )
    invoice = record.invoice
    return graph.invoke(
        initial_state(
            document_no=record.document_no,
            page_count=record.page_count,
            invoice=invoice.model_dump(mode="json") if invoice else None,
            missing_fields=list(invoice.missing_fields) if invoice else [],
        ),
        config={"configurable": {"thread_id": thread}, "recursion_limit": 20},
    )


def test_graph_state_carries_the_human_review_flag_for_the_report() -> None:
    """The flag the report copies into ``human_review_required`` must survive."""

    clean = invoke_graph(
        make_audit_record(),
        scripted_model(content=json.dumps(compliant_payload(default_rules()))),
        "t-flag-clean",
    )
    failed = invoke_graph(
        make_audit_record(),
        scripted_model(error=FakeTimeoutError("slow")),
        "t-flag-failed",
    )

    assert clean["human_review_required"] is False
    assert clean["audit_status"] == "completed"
    assert failed["human_review_required"] is True
    assert failed["audit_status"] == "requires_review"