"""Tests for ``GET /audit/{document_no}`` (Phase 8).

The Next.js UI reads the report it already produced (when "View report" is pressed, or
after a refresh) through this endpoint, so these tests pin that read path: the latest
stored report is returned unchanged, reading it does not re-run the audit, and a missing
document or a document that has never been audited is refused with 404 plus a
next-step hint.

Everything runs offline through the same FastAPI dependency override the Phase 6 tests
use: the auditor is backed by a scripted chat model, so no provider is called and no API
key is needed.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.audits import NO_REPORT_DETAIL, get_ai_auditor
from app.audit.ai_auditor import AiAuditor, report_store
from app.main import app
from app.models.audit import RulesConfig
from app.storage.memory import document_registry
from tests.audit_stubs import default_rules, finding_payload, make_audit_record
from tests.llm_stubs import make_settings, scripted_model

_UNSET = object()


@pytest.fixture(autouse=True)
def clean_report_store(empty_document_registry: None) -> Iterator[None]:
    """Keep the report store and dependency overrides isolated between tests."""

    report_store.clear()
    app.dependency_overrides.pop(get_ai_auditor, None)
    yield
    app.dependency_overrides.pop(get_ai_auditor, None)
    report_store.clear()


def register(*, invoice: Any = _UNSET, status: str = "extracted") -> Any:
    """Store a document record, with the default extracted invoice unless overridden."""

    if invoice is _UNSET:
        record = make_audit_record(status=status)
    else:
        record = make_audit_record(invoice=invoice, status=status)
    return document_registry.add(record)


def compliant_model(rules: RulesConfig) -> Any:
    """A scripted model that reports every applicable rule as passing."""

    findings = []
    for rule in rules.applicable_rules("standard_rated"):
        status = (
            "not_applicable"
            if rule.check == "trn_presence" and not rule.trn_required
            else "pass"
        )
        findings.append(finding_payload(rule.rule_id, status=status))
    return scripted_model(
        content=json.dumps(
            {
                "findings": findings,
                "overall_assessment": "Consistent with the configured rules.",
            }
        )
    )


def serve(model: Any, rules: RulesConfig) -> None:
    """Serve the audit routes with an auditor backed by a scripted model."""

    auditor = AiAuditor(
        chat_model=model,
        settings=make_settings(),
        rules=rules,
        max_iterations=2,
    )
    app.dependency_overrides[get_ai_auditor] = lambda: auditor


# --- 404 paths --------------------------------------------------------------------


def test_unknown_document_number_is_404(client: TestClient) -> None:
    response = client.get("/audit/DOC-NOT-A-REAL-DOCUMENT")

    assert response.status_code == 404
    assert "was not found" in response.json()["detail"]


def test_document_that_was_never_audited_is_404_with_the_next_step(
    client: TestClient,
) -> None:
    record = register()

    response = client.get(f"/audit/{record.document_no}")

    assert response.status_code == 404
    assert response.json()["detail"] == NO_REPORT_DETAIL.format(
        document_no=record.document_no
    )


# --- read path --------------------------------------------------------------------


def test_get_returns_the_report_produced_by_the_audit_run(client: TestClient) -> None:
    rules = default_rules()
    serve(compliant_model(rules), rules)
    record = register()

    audited = client.post(f"/audit/{record.document_no}")
    stored = client.get(f"/audit/{record.document_no}")

    assert audited.status_code == 200
    assert stored.status_code == 200
    assert stored.json() == audited.json()
    assert stored.json()["audit_status"] == "completed"
    assert stored.json()["document_no"] == record.document_no


def test_reading_the_report_does_not_run_the_audit_again(client: TestClient) -> None:
    rules = default_rules()
    model = compliant_model(rules)
    serve(model, rules)
    record = register()

    client.post(f"/audit/{record.document_no}")
    calls_after_audit = model.calls

    first = client.get(f"/audit/{record.document_no}")
    second = client.get(f"/audit/{record.document_no}")

    assert first.status_code == 200
    assert second.status_code == 200
    assert model.calls == calls_after_audit, "GET must not call the model"


def test_report_lookup_is_case_insensitive_like_the_audit_call(
    client: TestClient,
) -> None:
    rules = default_rules()
    serve(compliant_model(rules), rules)
    record = register()
    client.post(f"/audit/{record.document_no}")

    response = client.get(f"/audit/{record.document_no.lower()}")

    assert response.status_code == 200
    assert response.json()["document_no"] == record.document_no


def test_report_read_is_refused_before_the_invoice_is_extracted(
    client: TestClient,
) -> None:
    rules = default_rules()
    serve(compliant_model(rules), rules)
    record = register(invoice=None)

    assert client.post(f"/audit/{record.document_no}").status_code == 409
    assert client.get(f"/audit/{record.document_no}").status_code == 404


# --- schema -----------------------------------------------------------------------


def test_openapi_exposes_both_audit_methods(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()

    operations = schema["paths"]["/audit/{document_no}"]
    assert set(operations) == {"get", "post"}
    assert operations["get"]["tags"] == ["Audit"]
    assert "404" in operations["get"]["responses"]
    ok_schema = operations["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    assert "AuditReport" in json.dumps(ok_schema)

