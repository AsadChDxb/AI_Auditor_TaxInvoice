"""Phase 7 end-to-end tests: the documented workflow through the HTTP API, offline.

Every scenario follows the same path a user takes: upload a real (in-memory) text PDF,
extract the structured invoice fields, retrieve the document, then audit it with
``POST /audit/{document_no}`` and check the rule-level results, the confidence
percentages and the human-review flags.

Nothing here touches the network and no API key is needed: the chat model is a scripted
fake, the extractor is a stub, and settings/rules are built from explicit values so a
developer's ``.env`` cannot influence a test run.
"""

import ast
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.audits import get_ai_auditor
from app.api.documents import get_document_ocr_engine, get_invoice_extraction_service
from app.audit.ai_auditor import AiAuditor, report_store
from app.audit.rules_config import build_rules_config, get_rules_config
from app.config import LLMSettings, get_audit_settings, get_llm_settings
from app.main import app
from app.services.invoice_extractor import InvoiceExtractionService
from app.services.llm import LLMExtractor
from app.storage.memory import document_registry
from tests.audit_stubs import (
    EXTRACTION_JSON,
    default_rules,
    finding_payload,
    tool_calling_model,
)
from tests.llm_stubs import (
    TEST_API_KEY,
    TEST_MODEL,
    TEST_PROVIDER_LABEL,
    FakeProviderError,
    FakeTimeoutError,
    StubExtractor,
    make_settings,
    scripted_model,
)
from tests.pdf_factory import build_blank_pdf, build_text_pdf

BACKEND_DIR = Path(__file__).resolve().parents[1]
INVOICE_PDF_TEXT = (
    "TAX INVOICE INV-2024-0001\n"
    "Supplier: Al Noor Trading LLC TRN 100123456700003\n"
    "Customer: Gulf Retail FZE TRN 100987654300003\n"
    "Subtotal AED 1,000.00 | VAT 5% AED 50.00 | Total AED 1,050.00\n"
)
STANDARD_RATED_RULES = (
    "VAT_RATE_CHECK",
    "SUPPLIER_TRN_CHECK",
    "CUSTOMER_TRN_CHECK",
    "TAXABLE_AMOUNT_VAT_MISMATCH",
    "INVOICE_TOTAL_CHECK",
)
BASE_EXTRACTION: dict[str, Any] = {**EXTRACTION_JSON, "extraction_warnings": []}
"""A realistic extraction payload; scenarios override single fields."""


@pytest.fixture(autouse=True)
def clean_state(empty_document_registry: None) -> Iterator[None]:
    """Isolate the registry, the report store, settings caches and overrides."""

    report_store.clear()
    app.dependency_overrides.pop(get_ai_auditor, None)
    app.dependency_overrides.pop(get_invoice_extraction_service, None)
    app.dependency_overrides.pop(get_document_ocr_engine, None)
    get_audit_settings.cache_clear()
    get_rules_config.cache_clear()
    get_llm_settings.cache_clear()
    yield
    app.dependency_overrides.pop(get_ai_auditor, None)
    app.dependency_overrides.pop(get_invoice_extraction_service, None)
    app.dependency_overrides.pop(get_document_ocr_engine, None)
    get_audit_settings.cache_clear()
    get_rules_config.cache_clear()
    get_llm_settings.cache_clear()
    report_store.clear()


def model_review(
    rules: Any,
    classification: str = "standard_rated",
    *,
    statuses: dict[str, str] | None = None,
    **finding_overrides: Any,
) -> str:
    """A model review claiming a result for every applicable rule.

    Rules that do not apply in this configuration (an optional TRN) default to
    ``not_applicable``, so the audit is not flagged on their account.
    """

    overrides = dict(statuses or {})
    findings = []
    for rule in rules.applicable_rules(classification):
        default = (
            "not_applicable"
            if rule.check == "trn_presence" and not rule.trn_required
            else "pass"
        )
        findings.append(
            finding_payload(
                rule.rule_id,
                status=overrides.get(rule.rule_id, default),
                severity=None,
                **finding_overrides,
            )
        )
    return json.dumps(
        {
            "findings": findings,
            "overall_assessment": "Consistent on the face of the invoice.",
        }
    )


@dataclass
class Scenario:
    """A document that has been uploaded and extracted, ready to audit."""

    client: TestClient
    document_no: str
    rules: Any

    def audit(self, **kwargs: Any) -> Any:
        """Call the audit endpoint for this document."""

        return self.client.post(f"/audit/{self.document_no}", **kwargs)

    def report(self, **kwargs: Any) -> dict[str, Any]:
        """Audit the document, require 200, and return the JSON report."""

        response = self.audit(**kwargs)
        assert response.status_code == 200, response.text
        return response.json()

    def finding(self, report: dict[str, Any], rule_id: str) -> dict[str, Any]:
        """The finding for `rule_id`, failing the test when it is absent."""

        for finding in report["findings"]:
            if finding["rule_id"] == rule_id:
                return finding
        raise AssertionError(f"{rule_id} is missing from {report['findings']}")

    def detail(self, **kwargs: Any) -> dict[str, Any]:
        """``GET /documents/{document_no}`` for this scenario."""

        response = self.client.get(f"/documents/{self.document_no}", params=kwargs)
        assert response.status_code == 200, response.text
        return response.json()


@contextmanager
def workflow(
    model: Any,
    *,
    fields: dict[str, Any] | None = None,
    pdf_bytes: bytes | None = None,
    page_texts: tuple[str, ...] = (INVOICE_PDF_TEXT,),
    rules: Any = None,
    audit_settings: dict[str, Any] | None = None,
    llm_settings: dict[str, Any] | None = None,
    settings: LLMSettings | None = None,
    extract_error: Exception | None = None,
    max_iterations: int | None = None,
    reextract_fields: dict[str, Any] | None = None,
) -> Iterator[Scenario]:
    """Upload and extract a PDF through the API, yielding an auditable document.

    The auditor's bounded re-extraction pass uses an extractor stub as well, so no test
    depends on a real ``.env`` when a scenario leaves a field missing.
    """

    pdf = pdf_bytes if pdf_bytes is not None else build_text_pdf(list(page_texts))
    ruleset = rules if rules is not None else default_rules(**(audit_settings or {}))
    payload = BASE_EXTRACTION if fields is None else fields
    extractor = StubExtractor(fields=payload, error=extract_error)
    app.dependency_overrides[get_invoice_extraction_service] = (
        lambda: InvoiceExtractionService(extractor=extractor)
    )
    app.dependency_overrides[get_ai_auditor] = lambda: AiAuditor(
        chat_model=model,
        settings=settings if settings is not None else make_settings(**(llm_settings or {})),
        rules=ruleset,
        extractor=StubExtractor(
            fields=payload if reextract_fields is None else reextract_fields
        ),
        max_iterations=max_iterations,
    )
    try:
        client = TestClient(app)
        upload = client.post(
            "/documents/upload",
            files={"file": ("invoice.pdf", pdf, "application/pdf")},
        )
        assert upload.status_code == 201, upload.text
        document_no = upload.json()["document_no"]
        extraction = client.post(f"/documents/{document_no}/extract")
        assert extraction.status_code == 200, extraction.text
        yield Scenario(client=client, document_no=document_no, rules=ruleset)
    finally:
        app.dependency_overrides.pop(get_invoice_extraction_service, None)
        app.dependency_overrides.pop(get_ai_auditor, None)


def extraction(**overrides: Any) -> dict[str, Any]:
    """The base extraction payload with `overrides` applied."""

    return {**BASE_EXTRACTION, **overrides}


# --- 1. correct standard-rated invoice -------------------------------------------


def test_correct_standard_rated_invoice_completes_without_review() -> None:
    rules = default_rules()
    with workflow(scripted_model(content=model_review(rules)), rules=rules) as scenario:
        document = scenario.detail()
        report = scenario.report()

    assert document["extraction_status"] == "extracted"
    assert document["invoice"]["invoice_number"] == "INV-2024-0001"
    assert document["invoice"]["subtotal_excluding_vat"] == "1000.00"

    assert report["audit_status"] == "completed"
    assert report["human_review_required"] is False
    assert report["review_reasons"] == []
    assert report["warnings"] == []
    assert report["overall_confidence_percent"] >= 80
    assert report["provider"] == TEST_PROVIDER_LABEL
    assert report["model"] == TEST_MODEL
    assert report["ruleset_version"] == rules.version

    statuses = {finding["rule_id"]: finding["status"] for finding in report["findings"]}
    assert statuses == {
        "VAT_RATE_CHECK": "pass",
        "TAXABLE_AMOUNT_VAT_MISMATCH": "pass",
        "INVOICE_TOTAL_CHECK": "pass",
        "SUPPLIER_TRN_CHECK": "pass",
        "CUSTOMER_TRN_CHECK": "not_applicable",
    }
    vat = scenario.finding(report, "TAXABLE_AMOUNT_VAT_MISMATCH")
    assert vat["expected_value"] == "50.00"
    assert vat["actual_value"] == "50.00"
    assert vat["confidence_percent"] >= 80
    assert vat["explanation"]
    assert report["summary"]["passed"] == 4
    assert report["summary"]["failed"] == 0
    assert report["trace"]["iterations"] == 0


# --- 2-3. incorrect VAT amount and rate ------------------------------------------


def test_incorrect_vat_amount_is_flagged_and_needs_review() -> None:
    rules = default_rules()
    fields = extraction(vat_amount="40.00", total_including_vat="1040.00")
    with workflow(scripted_model(content=model_review(rules)), rules=rules, fields=fields) as s:
        report = s.report()

    assert report["audit_status"] == "requires_review"
    assert report["human_review_required"] is True
    assert report["summary"]["review_required"] >= 1

    vat = s.finding(report, "TAXABLE_AMOUNT_VAT_MISMATCH")
    assert vat["status"] == "review_required"
    assert vat["expected_value"] == "50.00"
    assert vat["actual_value"] == "40.00"
    assert vat["human_review_required"] is True
    assert any(
        "deterministic result was kept" in warning for warning in report["warnings"]
    )
    assert any(
        "TAXABLE_AMOUNT_VAT_MISMATCH" in reason for reason in report["review_reasons"]
    )


def test_incorrect_vat_rate_is_flagged() -> None:
    rules = default_rules()
    # The amounts stay consistent with the ruleset's expected 5%, so only the reported
    # rate is wrong: this isolates VAT_RATE_CHECK from the mismatch rule.
    fields = extraction(vat_rate_percent="15%")
    with workflow(scripted_model(content=model_review(rules)), rules=rules, fields=fields) as s:
        report = s.report()

    assert report["audit_status"] == "requires_review"
    rate = s.finding(report, "VAT_RATE_CHECK")
    assert rate["status"] == "review_required"
    assert "5" in (rate["expected_value"] or "")
    assert "15" in (rate["actual_value"] or "")
    assert rate["recommended_action"]
    assert s.finding(report, "TAXABLE_AMOUNT_VAT_MISMATCH")["status"] == "pass"


def test_reported_rate_drives_the_vat_recomputation() -> None:
    """A 15% rate with 15% VAT still fails: VAT is recomputed at the expected rate."""

    rules = default_rules()
    fields = extraction(vat_rate_percent="15%", vat_amount="150.00", total_including_vat="1150.00")
    with workflow(scripted_model(content=model_review(rules)), rules=rules, fields=fields) as s:
        report = s.report()

    mismatch = s.finding(report, "TAXABLE_AMOUNT_VAT_MISMATCH")
    assert mismatch["status"] == "review_required"
    assert mismatch["expected_value"] == "50.00"
    assert mismatch["actual_value"] == "150.00"


# --- 4. incorrect invoice total --------------------------------------------------


def test_incorrect_invoice_total_is_flagged() -> None:
    rules = default_rules()
    fields = extraction(total_including_vat="1035.00")
    with workflow(scripted_model(content=model_review(rules)), rules=rules, fields=fields) as s:
        report = s.report()

    assert report["audit_status"] == "requires_review"
    total = s.finding(report, "INVOICE_TOTAL_CHECK")
    assert total["status"] == "review_required"
    assert total["expected_value"] == "1050.00"
    assert total["actual_value"] == "1035.00"
    # Subtotal x rate still matches the reported VAT, so that rule stays clean.
    assert s.finding(report, "TAXABLE_AMOUNT_VAT_MISMATCH")["status"] == "pass"


# --- 5-6. TRN presence -----------------------------------------------------------


def test_missing_supplier_trn_is_flagged_when_required() -> None:
    rules = default_rules()
    with workflow(
        scripted_model(content=model_review(rules)),
        rules=rules,
        fields=extraction(supplier_trn=None),
    ) as scenario:
        report = scenario.report()

    assert report["audit_status"] == "requires_review"
    trn = scenario.finding(report, "SUPPLIER_TRN_CHECK")
    # The model claimed this check passed; the deterministic verdict wins the conflict.
    assert trn["status"] == "review_required"
    assert trn["expected_value"] == "15-digit TRN"
    assert trn["actual_value"] == "missing"
    assert trn["human_review_required"] is True
    assert trn["source"] == "deterministic"


def test_missing_customer_trn_raises_no_finding_when_not_required() -> None:
    rules = default_rules()
    with workflow(
        scripted_model(content=model_review(rules)),
        rules=rules,
        fields=extraction(customer_trn=None),
    ) as scenario:
        report = scenario.report()

    # No rule violation is raised for the optional TRN...
    assert scenario.finding(report, "CUSTOMER_TRN_CHECK")["status"] == "not_applicable"
    assert report["summary"]["failed"] == 0
    assert not any(
        "CUSTOMER_TRN_CHECK" in reason for reason in report["review_reasons"]
    )
    # ...but a missing field is still missing evidence, so the audit asks for a human.
    assert report["audit_status"] == "requires_review"
    assert report["human_review_required"] is True
    assert any("customer_trn" in reason for reason in report["review_reasons"])
    assert report["overall_confidence_percent"] <= 70


def test_missing_customer_trn_is_a_finding_when_the_scenario_requires_it() -> None:
    rules = default_rules(requires_customer_trn=True)
    payload = model_review(rules, statuses={"CUSTOMER_TRN_CHECK": "review_required"})
    with workflow(
        scripted_model(content=payload),
        rules=rules,
        fields=extraction(customer_trn=None),
    ) as scenario:
        report = scenario.report()

    assert report["audit_status"] == "requires_review"
    trn = scenario.finding(report, "CUSTOMER_TRN_CHECK")
    # The deterministic verdict is kept even when the model asks for a milder status.
    assert trn["status"] == "fail"
    assert trn["expected_value"] == "15-digit TRN"
    assert trn["actual_value"] == "missing"
    assert trn["human_review_required"] is True


# --- 7-9. classification and missing amounts -------------------------------------


def test_zero_rated_invoice_needs_review() -> None:
    rules = default_rules()
    fields = extraction(
        vat_classification="Zero Rated",
        vat_rate_percent="0%",
        vat_amount="0.00",
        total_including_vat="1000.00",
    )
    payload = model_review(
        rules, "zero_rated", statuses={"VAT_CLASSIFICATION_REVIEW": "review_required"}
    )
    with workflow(scripted_model(content=payload), rules=rules, fields=fields) as scenario:
        report = scenario.report()

    assert report["audit_status"] == "requires_review"
    assert report["human_review_required"] is True
    review = scenario.finding(report, "VAT_CLASSIFICATION_REVIEW")
    assert review["status"] == "review_required"
    assert "zero" in (review["explanation"] or "").lower()
    assert review["human_review_reason"]
    assert review["recommended_action"]
    # Zero-rated has no VAT to recompute, so the arithmetic rules stay clean.
    assert scenario.finding(report, "TAXABLE_AMOUNT_VAT_MISMATCH")["status"] == "pass"


def test_exempt_invoice_needs_review() -> None:
    rules = default_rules()
    fields = extraction(
        vat_classification="Exempt",
        vat_rate_percent="0%",
        vat_amount="0.00",
        total_including_vat="1000.00",
    )
    payload = model_review(
        rules, "exempt", statuses={"VAT_CLASSIFICATION_REVIEW": "review_required"}
    )
    with workflow(scripted_model(content=payload), rules=rules, fields=fields) as scenario:
        report = scenario.report()

    assert report["audit_status"] == "requires_review"
    review = scenario.finding(report, "VAT_CLASSIFICATION_REVIEW")
    assert review["status"] == "review_required"
    assert "exempt" in (review["explanation"] or "").lower()
    # A report with a review point must not read as a confident pass.
    assert report["overall_confidence_percent"] < 80


def test_unknown_classification_needs_review_without_guessing() -> None:
    rules = default_rules()
    fields = extraction(
        vat_classification="Unknown",
        vat_rate_percent=None,
        vat_amount=None,
        total_including_vat="1000.00",
    )
    payload = model_review(
        rules, "unknown", statuses={"VAT_CLASSIFICATION_REVIEW": "review_required"}
    )
    with workflow(scripted_model(content=payload), rules=rules, fields=fields) as scenario:
        report = scenario.report()

    assert report["audit_status"] == "requires_review"
    assert report["human_review_required"] is True
    assert scenario.finding(report, "VAT_CLASSIFICATION_REVIEW")["status"] == "review_required"
    # No VAT rate is assumed when the treatment is unknown.
    rate = scenario.finding(report, "VAT_RATE_CHECK")
    assert rate["status"] in ("review_required", "fail")
    assert rate["human_review_required"] is True


def test_missing_amounts_are_never_invented() -> None:
    rules = default_rules()
    fields = extraction(
        subtotal_excluding_vat=None, vat_amount=None, total_including_vat=None
    )
    with workflow(scripted_model(content=model_review(rules)), rules=rules, fields=fields) as s:
        report = s.report()

    assert report["audit_status"] == "requires_review"
    mismatch = s.finding(report, "TAXABLE_AMOUNT_VAT_MISMATCH")
    assert mismatch["status"] == "review_required"
    assert mismatch["expected_value"] is None
    assert "not extracted" in (mismatch["explanation"] or "")
    assert "no value is assumed" in (mismatch["explanation"] or "")
    assert mismatch["recommended_action"]
    assert any("missing" in reason.lower() for reason in report["review_reasons"])
    assert report["overall_confidence_percent"] < 80
    assert s.detail()["invoice"]["vat_amount"] is None


# --- 10. low confidence and model/deterministic conflicts ------------------------


def test_low_confidence_finding_caps_the_report_and_needs_review() -> None:
    rules = default_rules()
    payload = model_review(rules, confidence_percent=35)
    with workflow(scripted_model(content=payload), rules=rules) as scenario:
        report = scenario.report()

    assert report["audit_status"] == "requires_review"
    assert report["human_review_required"] is True
    # A report with a review point must not read as a confident pass: the overall value
    # is capped just below the configured 80% review threshold.
    assert report["overall_confidence_percent"] < 80
    assert "below the review threshold" in report["confidence_explanation"]
    assert any(
        "below the review threshold" in reason for reason in report["review_reasons"]
    )
    vat = scenario.finding(report, "TAXABLE_AMOUNT_VAT_MISMATCH")
    # The arithmetic keeps its own (high) confidence; the model's 35% does not lower the
    # number, it flags the finding and the reason names the model's confidence.
    assert vat["confidence_percent"] >= 90
    assert vat["human_review_required"] is True
    assert "35" in (vat["human_review_reason"] or "")
    assert "80" in (vat["human_review_reason"] or "")
    considered = [f for f in report["findings"] if f["status"] != "not_applicable"]
    assert considered and all(f["human_review_required"] for f in considered)


def test_model_contradicting_arithmetic_keeps_the_deterministic_result() -> None:
    rules = default_rules()
    fields = extraction(vat_amount="40.00", total_including_vat="1040.00")
    # The model claims the invoice adds up; the Decimal check says it does not.
    payload = model_review(
        rules, statuses={"TAXABLE_AMOUNT_VAT_MISMATCH": "pass"}, confidence_percent=99
    )
    with workflow(scripted_model(content=payload), rules=rules, fields=fields) as s:
        report = s.report()

    mismatch = s.finding(report, "TAXABLE_AMOUNT_VAT_MISMATCH")
    assert mismatch["expected_value"] == "50.00"
    assert mismatch["actual_value"] == "40.00"
    # The deterministic verdict replaces the model's "pass" and demands a human.
    assert mismatch["status"] == "review_required"
    assert mismatch["source"] == "deterministic"
    assert mismatch["confidence_percent"] >= 90
    assert any(
        "deterministic result was kept" in warning for warning in report["warnings"]
    )
    # The conflict is counted and caps the overall confidence at 70%, even though the
    # model claimed 99%.
    assert "conflict" in report["confidence_explanation"]
    assert report["overall_confidence_percent"] <= 70
    assert report["audit_status"] == "requires_review"


# --- 11. scanned PDFs that need OCR ----------------------------------------------


def test_scanned_pdf_needs_ocr_and_cannot_be_audited(
    without_paddleocr: None,
) -> None:
    model = scripted_model(content="{}")
    app.dependency_overrides[get_ai_auditor] = lambda: AiAuditor(
        chat_model=model, settings=make_settings(), rules=default_rules()
    )
    # The fixture above is exactly the state of a deployment without PaddleOCR installed.
    client = TestClient(app)

    upload = client.post(
        "/documents/upload",
        files={"file": ("scan.pdf", build_blank_pdf(), "application/pdf")},
    )
    assert upload.status_code == 201
    assert upload.json()["extraction_status"] == "needs_ocr"
    assert "OCR" in upload.json()["message"]
    # The extraction quality check and the fallback outcome are reported.
    assert upload.json()["used_ocr"] is False
    assert upload.json()["quality"]["pages_needing_ocr"] == [1]
    assert upload.json()["quality"]["ocr_attempted"] is False
    document_no = upload.json()["document_no"]

    detail = client.get(f"/documents/{document_no}")
    assert detail.status_code == 200
    assert detail.json()["extraction_status"] == "needs_ocr"
    assert detail.json()["total_characters"] == 0
    assert any("OCR" in note for note in detail.json()["notes"])

    extraction = client.post(f"/documents/{document_no}/extract")
    assert extraction.status_code == 409
    assert "OCR" in extraction.json()["detail"]

    audit = client.post(f"/audit/{document_no}")
    assert audit.status_code == 409
    assert "OCR" in audit.json()["detail"]
    assert "audit_status" not in audit.json()
    # Nothing was claimed and nothing was sent to a model.
    assert model.calls == []
    assert len(report_store) == 0


# --- 12. corrupted input and unknown document numbers ----------------------------


def test_corrupted_and_non_pdf_uploads_are_rejected(client: TestClient) -> None:
    non_pdf = client.post(
        "/documents/upload",
        files={"file": ("invoice.pdf", b"this is plain text", "application/pdf")},
    )
    corrupted = client.post(
        "/documents/upload",
        files={"file": ("invoice.pdf", b"%PDF-1.7\n not really a pdf", "application/pdf")},
    )
    empty = client.post(
        "/documents/upload", files={"file": ("invoice.pdf", b"", "application/pdf")}
    )

    assert non_pdf.status_code == 415
    assert corrupted.status_code == 400
    assert empty.status_code == 400
    assert document_registry.list_document_numbers() == []


def test_unknown_document_number_is_404_on_every_route(client: TestClient) -> None:
    model = scripted_model(content="{}")
    app.dependency_overrides[get_ai_auditor] = lambda: AiAuditor(
        chat_model=model, settings=make_settings(), rules=default_rules()
    )

    detail = client.get("/documents/DOC-DOES-NOT-EXIST")
    extraction = client.post("/documents/DOC-DOES-NOT-EXIST/extract")
    audit = client.post("/audit/DOC-DOES-NOT-EXIST")

    assert detail.status_code == 404
    assert extraction.status_code == 404
    assert audit.status_code == 404
    assert "memory only" in audit.json()["detail"]
    assert model.calls == []
    assert len(report_store) == 0


# --- 13. provider failures, configuration and malformed output -------------------


def no_key_settings() -> LLMSettings:
    """Settings for a deployment where the provider was never configured.

    Every value is passed explicitly, so a developer's OS environment cannot supply a
    real key, and the base URL points at an unreachable local port: a stray provider
    call cannot leave the machine or reach a network.
    """

    return LLMSettings(
        _env_file=None,
        llm_api_key=None,
        llm_model="",
        llm_base_url="http://127.0.0.1:1/v1",
    )


def upload(client: TestClient) -> str:
    """Upload the sample invoice PDF and return its document number."""

    response = client.post(
        "/documents/upload",
        files={"file": ("invoice.pdf", build_text_pdf([INVOICE_PDF_TEXT]), "application/pdf")},
    )
    assert response.status_code == 201, response.text
    return response.json()["document_no"]


@pytest.mark.parametrize(
    ("error", "label"),
    [
        (FakeTimeoutError("the provider did not answer"), "timeout"),
        (FakeProviderError(429, "rate limited"), "rate limit"),
        (FakeProviderError(500, "internal server error"), "provider error"),
        (FakeProviderError(401, "bad credentials"), "auth error"),
    ],
)
def test_provider_failures_never_produce_a_passing_audit(
    error: Exception, label: str
) -> None:
    rules = default_rules()
    model = scripted_model(error=error)

    with workflow(model, rules=rules) as scenario:
        report = scenario.report()

    assert report["audit_status"] == "requires_review", label
    assert report["human_review_required"] is True
    assert report["overall_confidence_percent"] <= 40
    assert "the model could not be used" in report["confidence_explanation"]
    assert all(finding["source"] == "deterministic" for finding in report["findings"])
    assert any("model" in warning.lower() for warning in report["warnings"])
    assert TEST_API_KEY not in json.dumps(report)
    # The retry loop is bounded: the initial pass plus two configured retries, no more.
    assert report["trace"]["iterations"] == 2
    assert report["trace"]["model_calls"] == 3
    assert len(model.calls) == 3


def test_malformed_model_output_never_produces_a_passing_audit() -> None:
    rules = default_rules()
    model = scripted_model(content="I could not read this invoice, sorry.")

    with workflow(model, rules=rules) as scenario:
        report = scenario.report()

    assert report["audit_status"] == "requires_review"
    assert report["human_review_required"] is True
    assert report["overall_confidence_percent"] <= 40
    assert all(finding["source"] == "deterministic" for finding in report["findings"])
    assert any("model" in warning.lower() for warning in report["warnings"])


def test_model_cannot_introduce_rules_outside_the_ruleset() -> None:
    rules = default_rules()
    payload = json.dumps(
        {
            "findings": [finding_payload("FAKE_LOW_RATE_CHECK", status="fail")],
            "overall_assessment": "Invented a rule.",
        }
    )

    with workflow(scripted_model(content=payload), rules=rules) as scenario:
        report = scenario.report()

    rule_ids = {finding["rule_id"] for finding in report["findings"]}
    assert "FAKE_LOW_RATE_CHECK" not in rule_ids
    assert rule_ids == set(STANDARD_RATED_RULES)
    assert any("FAKE_LOW_RATE_CHECK" in warning for warning in report["warnings"])
    assert report["audit_status"] == "completed_with_warnings"
    assert report["human_review_required"] is False


def test_extraction_provider_failures_are_mapped_and_store_nothing(
    client: TestClient,
) -> None:
    cases = [
        (FakeTimeoutError("slow"), 504),
        (FakeProviderError(429, "rate limited"), 502),
        (FakeProviderError(500, "server error"), 502),
    ]
    for error, expected_status in cases:
        # A real LLMExtractor over a scripted chat model: the provider-agnostic layer
        # maps the failure, exactly as it would for a live provider.
        app.dependency_overrides[get_invoice_extraction_service] = (
            lambda error=error: InvoiceExtractionService(
                extractor=LLMExtractor(
                    chat_model=scripted_model(error=error), settings=make_settings()
                )
            )
        )
        document_no = upload(client)
        response = client.post(f"/documents/{document_no}/extract")

        assert response.status_code == expected_status, response.text
        assert TEST_API_KEY not in response.text
        assert client.get(f"/documents/{document_no}").json()["invoice"] is None


def test_missing_api_key_is_a_configuration_error_and_never_a_pass(
    client: TestClient,
) -> None:
    assert no_key_settings().missing_settings() == ["LLM_API_KEY", "LLM_MODEL"]
    app.dependency_overrides[get_invoice_extraction_service] = (
        lambda: InvoiceExtractionService(
            extractor=LLMExtractor(settings=no_key_settings())
        )
    )
    document_no = upload(client)

    extraction = client.post(f"/documents/{document_no}/extract")

    assert extraction.status_code == 503
    detail = extraction.json()["detail"]
    assert "LLM_API_KEY" in detail
    assert "LLM_MODEL" in detail
    assert ".env.example" in detail
    assert client.get(f"/documents/{document_no}").json()["invoice"] is None

    # The audit route refuses as well instead of inventing a result.
    audit = client.post(f"/audit/{document_no}")
    assert audit.status_code == 409
    assert "cannot be audited" in audit.json()["detail"]


def test_audit_without_a_configured_model_asks_for_review() -> None:
    rules = default_rules()
    with workflow(scripted_model(content=model_review(rules)), rules=rules) as scenario:
        app.dependency_overrides[get_ai_auditor] = lambda: AiAuditor(
            chat_model=None, settings=no_key_settings(), rules=rules
        )
        report = scenario.report()

    assert report["audit_status"] == "requires_review"
    assert report["human_review_required"] is True
    assert report["overall_confidence_percent"] <= 40
    assert report["model"] is None
    assert all(finding["source"] == "deterministic" for finding in report["findings"])
    assert any(
        "LLM_API_KEY" in warning or "not configured" in warning.lower()
        for warning in report["warnings"]
    )


# --- 14. configuration from .env only -------------------------------------------


def test_env_file_alone_switches_provider_and_model(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_BASE_URL=http://localhost:11434/v1\n"
        "LLM_API_KEY=local-placeholder\n"
        "LLM_MODEL=llama3.1\n"
        "LLM_PROVIDER_LABEL=ollama-local\n",
        encoding="utf-8",
    )
    settings = LLMSettings(_env_file=env_file)

    assert settings.llm_base_url == "http://localhost:11434/v1"
    assert settings.llm_model == "llama3.1"
    assert settings.missing_settings() == []

    # No code change: the same auditor reports the provider and model from the file.
    rules = default_rules()
    with workflow(
        scripted_model(content=model_review(rules)), rules=rules, settings=settings
    ) as scenario:
        report = scenario.report()

    assert report["provider"] == "ollama-local"
    assert report["model"] == "llama3.1"


def test_env_file_alone_changes_the_review_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VAT_AUDIT_HUMAN_REVIEW_CONFIDENCE_THRESHOLD", "30")
    get_audit_settings.cache_clear()
    settings = get_audit_settings()

    assert settings.human_review_confidence_threshold == 30

    rules = build_rules_config(settings=settings)
    payload = model_review(rules, confidence_percent=35)
    with workflow(scripted_model(content=payload), rules=rules) as scenario:
        report = scenario.report()

    # 35% is below the default 80% threshold but above the configured 30% one, so the
    # same invoice and the same model output now pass without a review flag.
    assert report["audit_status"] == "completed"
    assert report["human_review_required"] is False
    assert rules.human_review_confidence_threshold == 30


def test_env_file_controls_the_base_url_and_timeout(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_BASE_URL=https://api.groq.com/openai/v1\n"
        "LLM_TIMEOUT_SECONDS=12.5\n"
        "LLM_API_KEY=placeholder\n"
        "LLM_MODEL=llama-3.1-70b-versatile\n",
        encoding="utf-8",
    )
    settings = LLMSettings(_env_file=env_file)

    assert settings.llm_base_url == "https://api.groq.com/openai/v1"
    assert settings.llm_timeout_seconds == 12.5


def test_ruleset_version_and_tolerance_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VAT_AUDIT_RULESET_VERSION", "uae-vat-rules-2026.1")
    monkeypatch.setenv("VAT_AUDIT_MONETARY_TOLERANCE", "0.05")
    get_audit_settings.cache_clear()
    get_rules_config.cache_clear()

    settings = get_audit_settings()
    rules = get_rules_config()

    assert settings.ruleset_version == "uae-vat-rules-2026.1"
    assert rules.version == "uae-vat-rules-2026.1"
    assert str(rules.monetary_tolerance) == "0.05"
    assert build_rules_config(settings=settings).version == "uae-vat-rules-2026.1"


# --- 15. repository hygiene and documented scope ---------------------------------


def env_example_values() -> dict[str, str]:
    """The assignments in ``.env.example`` (comments and blanks removed)."""

    values: dict[str, str] = {}
    text = (BACKEND_DIR / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        values[name.strip()] = value.strip()
    return values


def test_env_example_documents_every_setting() -> None:
    values = env_example_values()

    for name in (
        "LLM_API_KEY",
        "LLM_MODEL",
        "LLM_BASE_URL",
        "LLM_TIMEOUT_SECONDS",
        "LLM_PROVIDER_LABEL",
        "VAT_AUDIT_HUMAN_REVIEW_CONFIDENCE_THRESHOLD",
        "VAT_AUDIT_RULESET_VERSION",
        "VAT_AUDIT_MAX_AGENT_ITERATIONS",
        "VAT_AUDIT_OCR_ENABLED",
        "VAT_AUDIT_OCR_LANGUAGE",
        "VAT_AUDIT_OCR_DEVICE",
        "VAT_AUDIT_OCR_ENABLE_MKLDNN",
        "VAT_AUDIT_OCR_CPU_THREADS",
        "VAT_AUDIT_OCR_DET_MODEL",
        "VAT_AUDIT_OCR_REC_MODEL",
        "VAT_AUDIT_OCR_DPI",
        "VAT_AUDIT_OCR_MIN_PAGE_CHARACTERS",
        "VAT_AUDIT_OCR_MAX_PAGES",
    ):
        assert name in values, name


def test_env_example_contains_placeholders_only() -> None:
    text = (BACKEND_DIR / ".env.example").read_text(encoding="utf-8")
    values = env_example_values()

    # No value may look like a live provider key: a real key is shared through the
    # git-ignored .env file, never through the committed example.
    assert not re.search(r"sk-[A-Za-z0-9]{16,}", text)
    assert "placeholders only" in text.lower()
    assert "your" in values["LLM_API_KEY"].lower()
    assert "your" in values["LLM_MODEL"].lower()
    assert all(len(value) < 100 for value in values.values())


def test_env_is_git_ignored_and_the_example_is_not() -> None:
    ignore = (BACKEND_DIR / ".gitignore").read_text(encoding="utf-8")

    assert "\n.env\n" in ignore
    assert "!.env.example" in ignore
    assert ".env.*" in ignore


def test_no_secret_looking_value_is_committed() -> None:
    allowed = {"sk-test-key-not-real-1234567890"}
    offenders: list[str] = []
    for path in BACKEND_DIR.rglob("*"):
        if not path.is_file() or path.name == ".env":
            continue
        if "__pycache__" in path.parts or ".pytest_cache" in str(path):
            continue
        if path.suffix not in {".py", ".md", ".txt", ".example", ".json", ""}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for candidate in allowed:
            text = text.replace(candidate, "")
        if re.search(r"sk-[A-Za-z0-9]{16,}", text):
            offenders.append(str(path))

    assert offenders == []


def test_no_database_frontend_auth_or_unapproved_extraction_was_added() -> None:
    requirements = (BACKEND_DIR / "requirements.txt").read_text(encoding="utf-8").lower()
    forbidden_packages = (
        "sqlalchemy",
        "psycopg",
        "asyncpg",
        "alembic",
        "django",
        "flask",
        "streamlit",
        "gradio",
        "pytesseract",
        "easyocr",
        "pdf2image",
        "celery",
        "redis",
        "stripe",
    )
    for package in forbidden_packages:
        assert package not in requirements, package

    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in (BACKEND_DIR / "app").rglob("*.py")
    ).lower()
    for module in ("sqlalchemy", "pytesseract", "easyocr", "pdf2image", "fastapi_users"):
        assert f"import {module}" not in sources
        assert f"from {module}" not in sources


def test_extraction_uses_pdfplumber_and_only_paddleocr_as_the_ocr_engine() -> None:
    """Phase 9: pdfplumber plus an optional PaddleOCR fallback, nothing else."""

    extractor = (BACKEND_DIR / "app/services/pdf_extractor.py").read_text(encoding="utf-8")
    ocr = (BACKEND_DIR / "app/services/ocr_fallback.py").read_text(encoding="utf-8")
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in (BACKEND_DIR / "app").rglob("*.py")
    ).lower()

    # pdfplumber reads the document; PyMuPDF is a test-only fixture author.
    assert "import pdfplumber" in extractor
    assert "import fitz" not in sources
    assert "pymupdf" not in sources

    # PaddleOCR is the only OCR engine, and it is imported lazily on purpose: no
    # module-level import of it exists anywhere in app/, so importing the application
    # never imports PaddleOCR (and never fails when it is missing).
    assert "from paddleocr import" in ocr
    for path in (BACKEND_DIR / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Import):
                assert all(alias.name != "paddleocr" for alias in node.names), path
            elif isinstance(node, ast.ImportFrom):
                assert node.module != "paddleocr", path
    assert "easyocr" not in sources and "pytesseract" not in sources

    # The optional stack is declared outside requirements.txt, so the API and the whole
    # test suite keep working without it.
    optional = (BACKEND_DIR / "requirements-ocr.txt").read_text(encoding="utf-8").lower()
    assert "paddleocr" in optional

    required_lines = (
        (BACKEND_DIR / "requirements.txt").read_text(encoding="utf-8").lower().splitlines()
    )
    assert not any(line.startswith("paddleocr") for line in required_lines)


def test_in_memory_storage_limitation_is_documented() -> None:
    readme = (BACKEND_DIR / "README.md").read_text(encoding="utf-8")
    collapsed = " ".join(readme.split())

    assert "memory only" in collapsed
    assert "PaddleOCR fallback" in collapsed
    assert "not proof of tax non-compliance" in collapsed
    assert "lost on restart" in collapsed


def test_records_disappear_when_the_process_restarts(client: TestClient) -> None:
    rules = default_rules()
    with workflow(scripted_model(content=model_review(rules)), rules=rules) as scenario:
        document_no = scenario.document_no
        assert scenario.report()["audit_status"] == "completed"

        # A restart means empty in-memory stores: the document and its report are gone.
        document_registry.clear()
        report_store.clear()

        assert client.get(f"/documents/{document_no}").status_code == 404
        assert report_store.get(document_no) is None
        assert client.post(f"/audit/{document_no}").status_code == 404


# --- 16. bounded agent loop and trace -------------------------------------------


@pytest.mark.parametrize(("max_iterations", "expected_calls"), [(1, 2), (2, 3)])
def test_agent_loop_is_bounded_by_max_iterations(
    max_iterations: int, expected_calls: int
) -> None:
    rules = default_rules()
    model = scripted_model(error=FakeTimeoutError("slow"))

    with workflow(model, rules=rules, max_iterations=max_iterations) as scenario:
        report = scenario.report()

    assert report["audit_status"] == "requires_review"
    assert report["trace"]["iterations"] == max_iterations
    assert report["trace"]["model_calls"] == expected_calls
    assert len(model.calls) == expected_calls
    # The graph never routes back into the loop after the bound is reached.
    assert report["trace"]["nodes"].count("select_rules") <= max_iterations + 1


def test_tool_calls_are_traced_by_name_only() -> None:
    rules = default_rules()
    model = tool_calling_model(
        [
            {"tool_calls": [{"name": "list_rules", "args": {}}]},
            {"tool_calls": [{"name": "check_invoice_totals", "args": {}}]},
            {"content": model_review(rules)},
        ]
    )

    with workflow(model, rules=rules) as scenario:
        report = scenario.report()

    assert report["audit_status"] == "completed"
    assert report["trace"]["tool_calls"] == ["list_rules", "check_invoice_totals"]
    assert report["trace"]["model_calls"] == 1
    assert model.bound_tool_names == [
        "list_rules",
        "get_rule",
        "get_invoice_field",
        "get_page_evidence",
        "compute_expected_vat",
        "check_invoice_totals",
        "flag_human_review",
    ]
    # Tool arguments are never echoed into the report.
    assert "args" not in json.dumps(report["trace"])
