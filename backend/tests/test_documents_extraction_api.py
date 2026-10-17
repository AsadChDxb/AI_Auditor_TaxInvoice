"""API-level tests for Phase 3 invoice extraction.

The LLM is scripted in every test (a fake chat model or a stub extractor), so the
suite needs no API key and makes no network calls.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.api.documents import get_invoice_extraction_service
from app.main import app
from app.services.invoice_extractor import InvoiceExtractionService
from app.services.llm import LLMExtractor
from tests.llm_stubs import (
    INVOICE_JSON,
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

INVOICE_TEXT = "\n".join(
    [
        "Invoice No: INV-2024-0001",
        "Supplier: Al Noor Trading LLC",
        "Supplier TRN: 100123456700003",
        "Taxable Amount: 1000.00",
        "VAT Amount: 50.00",
        "Total: 1050.00",
    ]
)
CONTENT = json.dumps(INVOICE_JSON)


@pytest.fixture()
def install_service():
    """Replace the extraction service for one test, then restore the route.

    Pass an extractor (stub, fake-model backed, or real) and it is wrapped in a
    service for the route; the object passed in is returned so tests can inspect
    the calls it recorded.
    """

    def _install(extractor_or_service):
        service = (
            extractor_or_service
            if isinstance(extractor_or_service, InvoiceExtractionService)
            else InvoiceExtractionService(extractor=extractor_or_service)
        )
        app.dependency_overrides[get_invoice_extraction_service] = lambda: service
        return extractor_or_service

    yield _install
    app.dependency_overrides.pop(get_invoice_extraction_service, None)


def upload_invoice(
    client: TestClient, page_text: str = INVOICE_TEXT, filename: str = "invoice.pdf"
):
    """Upload a one-page text PDF and return the response."""

    return client.post(
        "/documents/upload",
        files={"file": (filename, build_text_pdf([page_text]), "application/pdf")},
    )


def test_extract_returns_and_stores_validated_invoice_data(
    client: TestClient, install_service
) -> None:
    stub = install_service(StubExtractor())
    document_no = upload_invoice(client).json()["document_no"]

    response = client.post(f"/documents/{document_no}/extract")

    assert response.status_code == 200
    body = response.json()
    assert body["document_no"] == document_no
    assert body["reused_existing"] is False

    invoice = body["invoice"]
    assert invoice["invoice_number"] == "INV-2024-0001"
    assert invoice["invoice_date"] == "2024-05-03"
    assert invoice["supplier_name"] == "Al Noor Trading LLC"
    assert invoice["supplier_trn"] == "100123456700003"
    assert invoice["customer_trn"] == "100987654300003"
    assert invoice["currency"] == "AED"
    # Decimals are serialised as strings so JSON cannot lose precision.
    assert invoice["subtotal_excluding_vat"] == "1000.00"
    assert invoice["vat_amount"] == "50.00"
    assert invoice["total_including_vat"] == "1050.00"
    assert invoice["vat_rate_percent"] == "5.00"
    assert invoice["vat_classification"] == "standard_rated"
    assert invoice["missing_fields"] == []
    assert invoice["provider"] == TEST_PROVIDER_LABEL
    assert invoice["model"] == TEST_MODEL
    assert invoice["field_evidence"]["supplier_trn"]["confidence_percent"] == 95.0
    assert any(w.startswith("model: ") for w in invoice["warnings"])

    pages_sent = stub.calls[0]
    assert pages_sent[0][0] == 1
    assert "INV-2024-0001" in pages_sent[0][1]


def test_get_document_exposes_invoice_data_and_warnings(
    client: TestClient, install_service
) -> None:
    install_service(StubExtractor())
    document_no = upload_invoice(client).json()["document_no"]

    before = client.get(f"/documents/{document_no}").json()
    assert before["invoice"] is None
    assert any("have not been extracted yet" in note for note in before["notes"])

    client.post(f"/documents/{document_no}/extract")
    after = client.get(f"/documents/{document_no}").json()

    assert after["invoice"]["invoice_number"] == "INV-2024-0001"
    assert after["invoice"]["warnings"]
    assert after["invoice"]["vat_classification"] == "standard_rated"
    assert any("verified against the source invoice" in note for note in after["notes"])
    # Phase 2 behaviour is unchanged: page text is still withheld by default.
    assert after["raw_text_included"] is False
    assert after["pages"][0]["text"] is None
    assert "Taxable Amount" not in json.dumps(after["pages"])


def test_extraction_is_reused_unless_force_is_requested(
    client: TestClient, install_service
) -> None:
    stub = install_service(StubExtractor())
    document_no = upload_invoice(client).json()["document_no"]

    first = client.post(f"/documents/{document_no}/extract").json()
    second = client.post(f"/documents/{document_no}/extract").json()

    assert len(stub.calls) == 1
    assert second["reused_existing"] is True
    assert second["invoice"] == first["invoice"]

    third = client.post(
        f"/documents/{document_no}/extract", params={"force": "true"}
    ).json()

    assert len(stub.calls) == 2
    assert third["reused_existing"] is False


def test_extract_unknown_document_returns_404(client: TestClient) -> None:
    response = client.post("/documents/DOC-UNKNOWN/extract")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_extract_needs_ocr_document_returns_409_without_calling_the_provider(
    client: TestClient, install_service, without_paddleocr: None
) -> None:
    stub = install_service(StubExtractor())
    document_no = client.post(
        "/documents/upload",
        files={"file": ("scan.pdf", build_blank_pdf(), "application/pdf")},
    ).json()["document_no"]

    response = client.post(f"/documents/{document_no}/extract")

    assert response.status_code == 409
    assert "OCR" in response.json()["detail"]
    assert stub.calls == []


def test_extract_without_api_key_returns_503_configuration_error(
    client: TestClient, install_service
) -> None:
    install_service(LLMExtractor(settings=make_settings(llm_api_key=None, llm_model="")))
    document_no = upload_invoice(client).json()["document_no"]

    response = client.post(f"/documents/{document_no}/extract")

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "LLM_API_KEY" in detail and "LLM_MODEL" in detail
    assert ".env.example" in detail
    assert "sk-" not in detail


def test_provider_failure_returns_502_with_a_sanitised_message(
    client: TestClient, install_service
) -> None:
    model = scripted_model(error=FakeProviderError(500, f"invalid key {TEST_API_KEY}"))
    install_service(LLMExtractor(model, make_settings()))
    document_no = upload_invoice(client).json()["document_no"]

    response = client.post(f"/documents/{document_no}/extract")

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "HTTP 500" in detail
    assert TEST_API_KEY not in detail


def test_provider_timeout_returns_504(client: TestClient, install_service) -> None:
    model = scripted_model(error=FakeTimeoutError("slow"))
    install_service(LLMExtractor(model, make_settings()))
    document_no = upload_invoice(client).json()["document_no"]

    response = client.post(f"/documents/{document_no}/extract")

    assert response.status_code == 504
    assert "did not respond within" in response.json()["detail"]


def test_invalid_provider_json_returns_502_without_storing_an_invoice(
    client: TestClient, install_service
) -> None:
    install_service(LLMExtractor(scripted_model("this is not JSON at all"), make_settings()))
    document_no = upload_invoice(client).json()["document_no"]

    response = client.post(f"/documents/{document_no}/extract")

    assert response.status_code == 502
    assert "not valid JSON" in response.json()["detail"]
    assert client.get(f"/documents/{document_no}").json()["invoice"] is None


def test_api_key_never_appears_in_any_response(
    client: TestClient, install_service
) -> None:
    install_service(LLMExtractor(scripted_model(CONTENT), make_settings()))
    document_no = upload_invoice(client).json()["document_no"]

    extract_response = client.post(f"/documents/{document_no}/extract")
    detail_response = client.get(f"/documents/{document_no}")

    assert extract_response.status_code == 200
    for body in (extract_response.text, detail_response.text):
        assert TEST_API_KEY not in body
        assert "api_key" not in body.lower()
        assert "authorization" not in body.lower()


def test_provider_and_model_recorded_come_from_configuration_alone(
    client: TestClient, install_service
) -> None:
    """Switching provider is a configuration change only (Revision 2)."""

    install_service(
        LLMExtractor(
            scripted_model(CONTENT),
            make_settings(
                llm_provider_label="ollama-local",
                llm_model="llama3.1",
                llm_base_url="http://localhost:11434/v1",
            ),
        )
    )
    document_no = upload_invoice(client).json()["document_no"]

    invoice = client.post(f"/documents/{document_no}/extract").json()["invoice"]

    assert invoice["provider"] == "ollama-local"
    assert invoice["model"] == "llama3.1"


def test_openapi_exposes_the_extraction_route_and_invoice_schema(
    client: TestClient,
) -> None:
    schema = client.get("/openapi.json").json()

    assert set(schema["paths"]["/documents/{document_no}/extract"]) == {"post"}
    assert set(schema["paths"]["/documents/{document_no}"]) == {"get"}
    assert schema["info"]["title"] == "UAE VAT Audit API"

    invoice_schema = schema["components"]["schemas"]["InvoiceData"]
    assert invoice_schema["properties"]["vat_classification"]["enum"] == [
        "standard_rated",
        "zero_rated",
        "exempt",
        "unknown",
    ]
    assert "invoice" in schema["components"]["schemas"]["DocumentDetail"]["properties"]
