"""Tests for the audit endpoint (Phase 6).

The endpoint is exercised through FastAPI's test client with the AI auditor dependency
replaced by a real :class:`AiAuditor` wired to a scripted chat model, so the whole
stack (route -> service -> graph -> guardrails -> report store) runs offline with no
network call and no API key.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.audits import (
    AUDIT_MESSAGE,
    REPEATED_AUDIT_MESSAGE,
    get_ai_auditor,
)
from app.audit.ai_auditor import AiAuditor, report_store
from app.main import app
from app.models.audit import RulesConfig
from app.storage.memory import document_registry
from tests.audit_stubs import (
    EXTRACTION_JSON,
    PAGE_TEXT,
    SUPPLIER_TRN,
    default_rules,
    finding_payload,
    make_audit_record,
    make_invoice,
)
from tests.llm_stubs import (
    TEST_API_KEY,
    FakeProviderError,
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
AUDIT_PATH = "/audit/{document_no}"
REPORT_FIELDS = (
    "document_no",
    "invoice_number",
    "audit_status",
    "provider",
    "model",
    "ruleset_version",
    "audited_at",
    "overall_confidence_percent",
    "findings",
    "summary",
    "human_review_required",
)


@pytest.fixture(autouse=True)
def clean_audit_store(empty_document_registry: None) -> Iterator[None]:
    """Keep the report store and dependency overrides isolated between tests."""

    report_store.clear()
    app.dependency_overrides.pop(get_ai_auditor, None)
    yield
    app.dependency_overrides.pop(get_ai_auditor, None)
    report_store.clear()


def compliant_payload(rules: RulesConfig, **overrides: Any) -> dict[str, Any]:
    """A model review that satisfies every applicable rule."""

    findings = []
    for rule in rules.applicable_rules("standard_rated"):
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


@contextmanager
def use_auditor(
    model: Any,
    *,
    rules: RulesConfig | None = None,
    extractor: Any = None,
    max_iterations: int = 2,
) -> Iterator[AiAuditor]:
    """Serve the audit endpoint with an auditor backed by a scripted model."""

    auditor = AiAuditor(
        chat_model=model,
        settings=make_settings(),
        rules=rules or default_rules(),
        extractor=extractor,
        max_iterations=max_iterations,
    )
    app.dependency_overrides[get_ai_auditor] = lambda: auditor
    try:
        yield auditor
    finally:
        app.dependency_overrides.pop(get_ai_auditor, None)


def register(invoice: Any = ..., *, status: str = "extracted", **kwargs: Any) -> Any:
    """Store a document record and return it.

    The invoice defaults to a consistent standard-rated one; pass ``None`` explicitly
    to store a record whose structured fields have not been extracted.
    """

    if invoice is ...:
        record = make_audit_record(status=status, **kwargs)
    else:
        record = make_audit_record(invoice=invoice, status=status, **kwargs)
    return document_registry.add(record)


# --- successful audit ------------------------------------------------------------


def test_successful_audit_returns_the_documented_report(client: TestClient) -> None:
    rules = default_rules()
    record = register()
    model = scripted_model(content=json.dumps(compliant_payload(rules)))

    with use_auditor(model, rules=rules):
        response = client.post(f"/audit/{record.document_no}")

    assert response.status_code == 200
    body = response.json()
    for field in REPORT_FIELDS:
        assert field in body, f"missing report field: {field}"

    assert body["document_no"] == record.document_no
    assert body["invoice_number"] == "INV-2024-0001"
    assert body["audit_status"] == "completed"
    assert body["human_review_required"] is False
    assert body["provider"] == "test-provider"
    assert body["model"] == "test-model"
    assert body["ruleset_version"] == rules.version
    assert body["overall_confidence_percent"] >= 80
    assert "severity-weighted mean" in body["confidence_explanation"]
    assert body["message"] == AUDIT_MESSAGE
    assert body["review_reasons"] == []
    assert body["warnings"] == []
    assert datetime.fromisoformat(body["audited_at"].replace("Z", "+00:00"))

    # Findings are ordered by severity, then by ruleset order (Phase 5 behaviour).
    assert [f["rule_id"] for f in body["findings"]] == [
        "VAT_RATE_CHECK",
        "TAXABLE_AMOUNT_VAT_MISMATCH",
        "INVOICE_TOTAL_CHECK",
        "SUPPLIER_TRN_CHECK",
        "CUSTOMER_TRN_CHECK",
    ]
    assert {f["rule_id"] for f in body["findings"]} == set(STANDARD_RATED_RULES)
    for finding in body["findings"]:
        assert finding["status"] in ("pass", "not_applicable")
        assert finding["severity"] in ("critical", "high", "medium", "low")
        assert finding["explanation"]
        assert 0 <= finding["confidence_percent"] <= 100
        assert finding["source"] in ("deterministic", "model", "guardrail")

    summary = body["summary"]
    assert summary["total"] == len(STANDARD_RATED_RULES)
    assert summary["passed"] == len(STANDARD_RATED_RULES) - 1
    assert summary["not_applicable"] == 1
    assert summary["failed"] == 0
    assert summary["ruleset_version"] == rules.version
    assert summary["enabled_rule_count"] == 6

    trace = body["trace"]
    assert trace["nodes"] == [
        "load_context",
        "select_rules",
        "deterministic_checks",
        "agent_review",
        "guardrail_validate",
        "aggregate_report",
    ]
    assert trace["model_calls"] == 1
    assert trace["iterations"] == 0
    assert trace["tool_calls"] == []
    assert "does not assert fraud" in body["overall_assessment"]

    stored = report_store.get(record.document_no)
    assert stored is not None
    assert stored.document_no == body["document_no"]
    assert len(report_store) == 1


def test_completed_with_warnings_when_the_model_omits_a_rule(
    client: TestClient,
) -> None:
    rules = default_rules()
    record = register()
    payload = {
        "findings": [finding_payload("VAT_RATE_CHECK")],
        "overall_assessment": "Partial review.",
    }

    with use_auditor(scripted_model(content=json.dumps(payload)), rules=rules):
        body = client.post(f"/audit/{record.document_no}").json()

    assert body["audit_status"] == "completed_with_warnings"
    assert body["human_review_required"] is False
    assert any("returned no result" in warning for warning in body["warnings"])
    assert body["summary"]["total"] == len(STANDARD_RATED_RULES)


def test_repeated_calls_replace_the_stored_report(client: TestClient) -> None:
    rules = default_rules()
    record = register()

    with use_auditor(
        scripted_model(content=json.dumps(compliant_payload(rules))), rules=rules
    ):
        first = client.post(f"/audit/{record.document_no}").json()
        second = client.post(f"/audit/{record.document_no}").json()

    assert first["message"] == AUDIT_MESSAGE
    assert second["message"] == REPEATED_AUDIT_MESSAGE
    assert "only the latest report is stored" in second["message"]
    assert first["document_no"] == second["document_no"] == record.document_no
    assert len(report_store) == 1
    assert report_store.get(record.document_no) is not None


# --- refusals --------------------------------------------------------------------


def test_unknown_document_returns_404(client: TestClient) -> None:
    with use_auditor(scripted_model(content="{}")):
        response = client.post("/audit/DOC-NOT-A-REAL-DOCUMENT")

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert "DOC-NOT-A-REAL-DOCUMENT" in detail
    assert "memory only" in detail


def test_document_that_needs_ocr_is_refused_with_409(client: TestClient) -> None:
    record = register(invoice=None, status="needs_ocr", page_texts=("",))
    model = scripted_model(content="{}")

    with use_auditor(model):
        response = client.post(f"/audit/{record.document_no}")

    assert response.status_code == 409
    assert "OCR" in response.json()["detail"]
    assert model.calls == []
    assert len(report_store) == 0


def test_document_without_extracted_fields_is_refused_with_409(
    client: TestClient,
) -> None:
    record = register(invoice=None)
    model = scripted_model(content="{}")

    with use_auditor(model):
        response = client.post(f"/audit/{record.document_no}")

    assert response.status_code == 409
    assert "extracted" in response.json()["detail"]
    assert model.calls == []
    assert len(report_store) == 0


def test_cannot_audit_when_no_rule_applies(client: TestClient) -> None:
    rules = default_rules(
        disabled_rule_ids=[
            "VAT_RATE_CHECK",
            "SUPPLIER_TRN_CHECK",
            "CUSTOMER_TRN_CHECK",
            "TAXABLE_AMOUNT_VAT_MISMATCH",
            "INVOICE_TOTAL_CHECK",
        ]
    )
    record = register()

    with use_auditor(scripted_model(content="{}"), rules=rules):
        body = client.post(f"/audit/{record.document_no}").json()

    assert body["audit_status"] == "cannot_audit"
    assert body["overall_confidence_percent"] == 0
    assert body["human_review_required"] is True
    assert body["findings"] == []
    assert "no audit could be performed" in body["overall_assessment"].lower()
    assert any("no enabled rule" in w.lower() for w in body["warnings"])


# --- review states ---------------------------------------------------------------


def test_low_confidence_findings_require_human_review(client: TestClient) -> None:
    rules = default_rules()
    record = register()
    payload = compliant_payload(rules, confidence_percent=35)

    with use_auditor(scripted_model(content=json.dumps(payload)), rules=rules):
        body = client.post(f"/audit/{record.document_no}").json()

    assert body["audit_status"] == "requires_review"
    assert body["human_review_required"] is True
    assert body["overall_confidence_percent"] <= 79
    assert any("below the review threshold" in r for r in body["review_reasons"])
    flagged = [f for f in body["findings"] if f["status"] != "not_applicable"]
    assert flagged and all(f["human_review_required"] for f in flagged)
    assert all(f["human_review_reason"] for f in flagged)


def test_ambiguous_classification_requires_human_review(client: TestClient) -> None:
    rules = default_rules()
    record = register(
        make_invoice(
            vat_classification="unknown", vat_rate_percent=None, vat_amount=None
        )
    )
    payload = {
        "findings": [
            finding_payload(rule_id)
            for rule_id in rules.applicable_rule_ids("unknown")
        ],
        "overall_assessment": "The treatment could not be established.",
    }

    with use_auditor(scripted_model(content=json.dumps(payload)), rules=rules):
        body = client.post(f"/audit/{record.document_no}").json()

    assert body["audit_status"] == "requires_review"
    assert body["human_review_required"] is True
    review = next(
        f for f in body["findings"] if f["rule_id"] == "VAT_CLASSIFICATION_REVIEW"
    )
    assert review["status"] == "review_required"
    assert "unknown" in (review["human_review_reason"] or "").lower() or "ambiguous" in (
        review["human_review_reason"] or ""
    ).lower()
    assert "VAT_RATE_CHECK" in body["trace"]["rules_evaluated"]


def test_missing_fields_end_in_review_not_invented_values(client: TestClient) -> None:
    rules = default_rules()
    record = register(make_invoice(vat_amount=None, total_including_vat=None))
    payload = compliant_payload(rules)

    with use_auditor(
        scripted_model(content=json.dumps(payload)),
        rules=rules,
        extractor=StubExtractor(fields={}),
    ):
        body = client.post(f"/audit/{record.document_no}").json()

    assert body["audit_status"] == "requires_review"
    assert body["human_review_required"] is True
    vat = next(
        f for f in body["findings"] if f["rule_id"] == "TAXABLE_AMOUNT_VAT_MISMATCH"
    )
    assert vat["status"] == "review_required"
    assert vat["expected_value"] is None
    assert vat["recommended_action"]
    assert any("missing" in r.lower() for r in body["review_reasons"])


def test_deterministic_arithmetic_mismatch_is_reported_and_reviewed(
    client: TestClient,
) -> None:
    rules = default_rules()
    record = register(
        make_invoice(vat_amount=Decimal("10.00"), total_including_vat=Decimal("1010.00"))
    )
    payload = compliant_payload(rules)

    with use_auditor(scripted_model(content=json.dumps(payload)), rules=rules):
        body = client.post(f"/audit/{record.document_no}").json()

    assert body["audit_status"] == "requires_review"
    vat = next(
        f for f in body["findings"] if f["rule_id"] == "TAXABLE_AMOUNT_VAT_MISMATCH"
    )
    assert vat["status"] == "review_required"
    assert vat["expected_value"] == "50.00"
    assert vat["actual_value"] == "10.00"
    assert "The model reported 'pass'" in (vat["human_review_reason"] or "")
    assert any(
        "disagreed with the deterministic calculation" in reason
        for reason in body["review_reasons"]
    )
    assert any("deterministic result was kept" in w for w in body["warnings"])


def test_provider_error_is_reported_as_review_not_success(client: TestClient) -> None:
    rules = default_rules()
    record = register()
    model = scripted_model(error=FakeProviderError(429, f"rate limited {TEST_API_KEY}"))

    with use_auditor(model, rules=rules):
        response = client.post(f"/audit/{record.document_no}")

    assert response.status_code == 200
    body = response.json()
    assert body["audit_status"] == "requires_review"
    assert body["human_review_required"] is True
    assert body["overall_confidence_percent"] <= 40
    assert any("HTTP 429" in w for w in body["warnings"])
    assert TEST_API_KEY not in json.dumps(body)
    assert body["findings"]
    assert all(f["source"] == "deterministic" for f in body["findings"])


# --- data handling and documentation ---------------------------------------------


def test_response_excludes_secrets_trns_and_full_page_text(client: TestClient) -> None:
    rules = default_rules()
    long_page = f"{PAGE_TEXT} {'X' * 4000}"
    record = register(page_texts=(long_page,))

    with use_auditor(
        scripted_model(content=json.dumps(compliant_payload(rules))), rules=rules
    ):
        response = client.post(f"/audit/{record.document_no}")

    text = response.text
    assert TEST_API_KEY not in text
    assert "sk-" not in text
    assert SUPPLIER_TRN not in text
    assert "100****0003" in text
    assert "X" * 100 not in text
    assert long_page not in text


def test_extract_then_audit_through_the_api(client: TestClient) -> None:
    from app.api.documents import get_invoice_extraction_service
    from app.services.invoice_extractor import InvoiceExtractionService

    rules = default_rules()
    record = register(invoice=None)
    app.dependency_overrides[get_invoice_extraction_service] = (
        lambda: InvoiceExtractionService(
            extractor=StubExtractor(fields=EXTRACTION_JSON)
        )
    )
    try:
        extraction = client.post(f"/documents/{record.document_no}/extract")
        assert extraction.status_code == 200
        with use_auditor(
            scripted_model(content=json.dumps(compliant_payload(rules))), rules=rules
        ):
            audit = client.post(f"/audit/{record.document_no}")
    finally:
        app.dependency_overrides.pop(get_invoice_extraction_service, None)

    assert audit.status_code == 200
    body = audit.json()
    assert body["audit_status"] == "completed"
    assert body["invoice_number"] == "INV-2024-0001"
    assert body["human_review_required"] is False


def test_openapi_documents_the_audit_endpoint() -> None:
    schema = app.openapi()

    operation = schema["paths"]["/audit/{document_no}"]["post"]
    assert operation["tags"] == ["Audit"]
    assert set(operation["responses"]) >= {"200", "404", "409", "500"}
    ok_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert "AuditReport" in json.dumps(ok_schema)

    properties = schema["components"]["schemas"]["AuditReport"]["properties"]
    for field in REPORT_FIELDS:
        assert field in properties

    finding_properties = schema["components"]["schemas"]["AuditFinding"]["properties"]
    for field in (
        "rule_id",
        "status",
        "severity",
        "explanation",
        "expected_value",
        "actual_value",
        "page_number",
        "evidence",
        "confidence_percent",
        "human_review_reason",
        "recommended_action",
    ):
        assert field in finding_properties


def test_swagger_describes_the_full_workflow() -> None:
    schema = app.openapi()

    upload = schema["paths"]["/documents/upload"]["post"]
    detail = schema["paths"]["/documents/{document_no}"]["get"]

    assert "Full workflow" in upload["description"]
    assert "/audit/{document_no}" in upload["description"]
    assert "/audit/{document_no}" in detail["description"]
    assert schema["info"]["version"] == "0.2.0"
    assert "audit" in schema["info"]["description"].lower()


def test_document_detail_points_at_the_audit_endpoint(client: TestClient) -> None:
    record = register()

    body = client.get(f"/documents/{record.document_no}").json()

    assert any("POST /audit/{document_no}" in note for note in body["notes"])
    assert body["invoice"]["invoice_number"] == "INV-2024-0001"