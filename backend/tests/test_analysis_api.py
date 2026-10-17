"""Tests for the stateless one-shot analysis endpoint (``POST /analysis``).

The endpoint exists because the three-step workflow (upload -> extract -> audit) keeps
the document in process memory: on Vercel/serverless each request can hit a different
instance, so the second request answers 404. ``POST /analysis`` takes the PDF again and
returns everything in one response.

Every test runs offline: the LLM extractor is a stub and the audit chat model is
scripted, exactly as in ``test_audit_api.py``.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.analysis import (
    ANALYSIS_MESSAGE,
    CLIENT_INVOICE_NOTE,
    EXTRACTION_ONLY_MESSAGE,
    STATELESS_NOTE,
)
from app.api.audits import get_ai_auditor
from app.api.documents import get_invoice_extraction_service
from app.audit.ai_auditor import AiAuditor, report_store
from app.main import app
from app.models.audit import RulesConfig
from app.services.invoice_extractor import InvoiceExtractionService
from tests.audit_stubs import default_rules, finding_payload
from tests.llm_stubs import StubExtractor, make_settings, scripted_model
from tests.pdf_factory import build_blank_pdf, build_text_pdf

ANALYSIS_PATH = "/analysis"
INVOICE_TEXT = (
    "Invoice No: INV-2024-0001\n"
    "Supplier: Al Noor Trading LLC\n"
    "Supplier TRN: 100123456700003\n"
    "Taxable Amount: 1000.00\n"
    "VAT Amount: 50.00\n"
    "Total: 1050.00"
)


def compliant_review(rules: RulesConfig) -> dict[str, Any]:
    """A model review that passes every applicable standard-rated rule."""

    findings = []
    for rule in rules.applicable_rules("standard_rated"):
        status = (
            "not_applicable"
            if rule.check == "trn_presence" and not rule.trn_required
            else "pass"
        )
        findings.append(finding_payload(rule.rule_id, status=status))
    return {
        "findings": findings,
        "overall_assessment": "The invoice is consistent with the configured rules.",
    }


@contextmanager
def use_pipeline(
    *,
    model: Any,
    extractor: Any = None,
    rules: RulesConfig | None = None,
) -> Iterator[Any]:
    """Serve /analysis with a stub extractor and a scripted auditing model."""

    extraction = InvoiceExtractionService(extractor=extractor or StubExtractor())
    auditor = AiAuditor(
        chat_model=model,
        settings=make_settings(),
        rules=rules or default_rules(),
        extractor=extractor or StubExtractor(),
        max_iterations=2,
    )
    app.dependency_overrides[get_invoice_extraction_service] = lambda: extraction
    app.dependency_overrides[get_ai_auditor] = lambda: auditor
    try:
        yield auditor
    finally:
        app.dependency_overrides.pop(get_invoice_extraction_service, None)
        app.dependency_overrides.pop(get_ai_auditor, None)


@pytest.fixture(autouse=True)
def clean_report_store() -> Iterator[None]:
    """Keep the process-wide report store isolated between tests."""

    report_store.clear()
    yield
    report_store.clear()


def upload_pdf(client: TestClient, pdf: bytes, filename: str = "invoice.pdf"):
    """POST /analysis with one PDF and no optional form fields."""

    return client.post(
        ANALYSIS_PATH,
        files={"file": (filename, pdf, "application/pdf")},
    )


def test_analysis_returns_the_document_invoice_and_report(
    client: TestClient,
) -> None:
    rules = default_rules()
    model = scripted_model(content=json.dumps(compliant_review(rules)))
    extractor = StubExtractor()

    with use_pipeline(model=model, extractor=extractor, rules=rules):
        response = upload_pdf(client, build_text_pdf([INVOICE_TEXT]))

    assert response.status_code == 200, response.text
    body = response.json()

    # The response is self-contained: the page text, the invoice and the report all
    # arrive together, so the client never needs to resolve a document number.
    assert body["document"]["extraction_status"] == "extracted"
    assert body["document"]["raw_text_included"] is True
    assert body["document"]["pages"][0]["text"]
    assert body["document"]["invoice"]["invoice_number"] == "INV-2024-0001"
    assert body["report"]["document_no"] == body["document"]["document_no"]
    assert body["report"]["findings"]
    assert body["row"]["document_no"] == body["document"]["document_no"]
    assert body["row"]["invoice_extracted"] is True
    assert body["row"]["audited"] is True
    assert body["row"]["can_audit"] is True
    assert body["message"] == ANALYSIS_MESSAGE
    assert STATELESS_NOTE in body["notes"]
    # The document notes must not claim the text is only in server memory: the whole
    # text is in this response and the browser keeps it.
    assert STATELESS_NOTE in body["document"]["notes"]
    assert not any("in memory" in note for note in body["document"]["notes"])
    assert len(extractor.calls) == 1
    assert model.calls


def test_run_audit_false_stops_after_extraction(client: TestClient) -> None:
    rules = default_rules()
    model = scripted_model(content=json.dumps(compliant_review(rules)))
    extractor = StubExtractor()

    with use_pipeline(model=model, extractor=extractor, rules=rules):
        response = client.post(
            ANALYSIS_PATH,
            params={"run_audit": "false"},
            files={"file": ("invoice.pdf", build_text_pdf([INVOICE_TEXT]), "application/pdf")},
        )

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["report"] is None
    assert body["row"]["invoice_extracted"] is True
    assert body["row"]["audited"] is False
    assert body["row"]["can_audit"] is True
    assert body["message"] == EXTRACTION_ONLY_MESSAGE
    assert len(extractor.calls) == 1
    assert model.calls == []


def test_reaudit_reuses_the_document_number_and_supplied_invoice(
    client: TestClient,
) -> None:
    rules = default_rules()
    model = scripted_model(content=json.dumps(compliant_review(rules)))
    extractor = StubExtractor()

    with use_pipeline(model=model, extractor=extractor, rules=rules):
        first = client.post(
            ANALYSIS_PATH,
            params={"run_audit": "false"},
            files={"file": ("invoice.pdf", build_text_pdf([INVOICE_TEXT]), "application/pdf")},
        )
        assert first.status_code == 200, first.text
        extracted = first.json()
        document_no = extracted["document"]["document_no"]

        second = client.post(
            ANALYSIS_PATH,
            files={"file": ("invoice.pdf", build_text_pdf([INVOICE_TEXT]), "application/pdf")},
            data={
                "document_no": document_no,
                "invoice": json.dumps(extracted["document"]["invoice"]),
            },
        )

    assert second.status_code == 200, second.text
    body = second.json()

    # The row keeps its identity, the supplied invoice is audited as-is and the LLM
    # extraction is not paid for a second time.
    assert body["document"]["document_no"] == document_no
    assert body["row"]["document_no"] == document_no
    assert body["row"]["audited"] is True
    assert body["report"]["document_no"] == document_no
    assert CLIENT_INVOICE_NOTE in body["notes"]
    assert len(extractor.calls) == 1


def test_invalid_invoice_json_is_rejected_before_any_model_call(
    client: TestClient,
) -> None:
    rules = default_rules()
    model = scripted_model(content=json.dumps(compliant_review(rules)))
    extractor = StubExtractor()

    with use_pipeline(model=model, extractor=extractor, rules=rules):
        response = client.post(
            ANALYSIS_PATH,
            files={"file": ("invoice.pdf", build_text_pdf([INVOICE_TEXT]), "application/pdf")},
            data={"invoice": json.dumps({"subtotal_excluding_vat": "not a number"})},
        )

    assert response.status_code == 422
    assert "not a valid InvoiceData" in response.json()["detail"]
    assert extractor.calls == []
    assert model.calls == []


def test_an_invalid_document_number_is_rejected(client: TestClient) -> None:
    rules = default_rules()
    model = scripted_model(content=json.dumps(compliant_review(rules)))

    with use_pipeline(model=model):
        response = client.post(
            ANALYSIS_PATH,
            files={"file": ("invoice.pdf", build_text_pdf([INVOICE_TEXT]), "application/pdf")},
            data={"document_no": "NOT-A-DOCUMENT"},
        )

    assert response.status_code == 422
    assert "DOC-" in response.json()["detail"]
    assert model.calls == []


def test_a_document_number_from_the_client_is_normalised(
    client: TestClient,
) -> None:
    rules = default_rules()
    model = scripted_model(content=json.dumps(compliant_review(rules)))

    with use_pipeline(model=model, rules=rules):
        response = client.post(
            ANALYSIS_PATH,
            params={"run_audit": "false"},
            files={"file": ("invoice.pdf", build_text_pdf([INVOICE_TEXT]), "application/pdf")},
            data={"document_no": "  doc-abc123  "},
        )

    assert response.status_code == 200, response.text
    assert response.json()["document"]["document_no"] == "DOC-ABC123"


def test_a_document_that_needs_ocr_is_returned_without_invoice_or_report(
    client: TestClient, without_paddleocr: None
) -> None:
    rules = default_rules()
    model = scripted_model(content=json.dumps(compliant_review(rules)))
    extractor = StubExtractor()

    with use_pipeline(model=model, extractor=extractor, rules=rules):
        response = upload_pdf(client, build_blank_pdf(), filename="scan.pdf")

    assert response.status_code == 200, response.text
    body = response.json()

    # Nothing failed: the PDF simply has no readable text, so the client still gets
    # the document (and can show the needs-OCR badge) instead of a bare 409.
    assert body["document"]["extraction_status"] == "needs_ocr"
    assert body["document"]["extraction_error"]
    assert body["document"]["invoice"] is None
    assert body["report"] is None
    assert body["row"]["can_extract"] is False
    assert body["row"]["can_audit"] is False
    assert extractor.calls == []
    assert model.calls == []


def test_a_non_pdf_upload_is_rejected(client: TestClient) -> None:
    rules = default_rules()
    model = scripted_model(content=json.dumps(compliant_review(rules)))

    with use_pipeline(model=model):
        response = upload_pdf(client, b"this is plain text", filename="invoice.pdf")

    assert response.status_code == 415
    assert model.calls == []

