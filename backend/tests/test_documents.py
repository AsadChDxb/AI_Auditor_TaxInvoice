"""Tests for document upload and metadata retrieval (Phases 2-9)."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.api.documents import get_document_ocr_engine
from app.config import settings
from app.main import app
from app.services.pdf_extractor import NEEDS_OCR_ERROR, REQUIRED_INVOICE_FIELDS
from tests.ocr_stubs import StubOcrEngine
from tests.pdf_factory import (
    build_blank_pdf,
    build_encrypted_pdf,
    build_scanned_pdf,
    build_table_pdf,
    build_text_pdf,
)

INVOICE_LINE = "Invoice No: INV-2024-0001"
INVOICE_TEXT = "\n".join(
    [
        INVOICE_LINE,
        "Supplier: Al Noor Trading LLC",
        "Supplier TRN: 100123456700003",
        "Taxable Amount: 1000.00",
        "VAT Amount: 50.00",
        "Total: 1050.00",
    ]
)
COMPLETE_INVOICE_TEXT = INVOICE_TEXT + "\nDate: 2024-05-03"
"""The same invoice with a date, so the quality check finds every required field."""


def install_ocr_engine(engine: StubOcrEngine) -> StubOcrEngine:
    """Install a stub OCR engine for the upload route and return it."""

    app.dependency_overrides[get_document_ocr_engine] = lambda: engine
    return engine


def upload(
    client: TestClient,
    content: bytes,
    filename: str = "invoice.pdf",
    content_type: str = "application/pdf",
):
    """Post `content` to the upload endpoint as multipart/form-data."""

    return client.post(
        "/documents/upload",
        files={"file": (filename, content, content_type)},
    )


def test_upload_text_pdf_returns_document_no_and_extraction_summary(
    client: TestClient,
) -> None:
    pdf = build_text_pdf([INVOICE_TEXT, "Page two: payment terms apply."])

    response = upload(client, pdf)

    assert response.status_code == 201
    body = response.json()
    assert body["document_no"].startswith("DOC-")
    assert len(body["document_no"]) == len("DOC-") + 32
    assert body["original_filename"] == "invoice.pdf"
    assert body["content_type"] == "application/pdf"
    assert body["size_bytes"] == len(pdf)
    assert body["page_count"] == 2
    assert body["extraction_status"] == "extracted"
    assert body["extraction_error"] is None
    assert body["pages_with_text"] == 2
    assert body["total_characters"] > 0
    assert datetime.fromisoformat(body["uploaded_at"]).tzinfo is not None


def test_upload_generates_unique_document_numbers(client: TestClient) -> None:
    first = upload(client, build_text_pdf([INVOICE_TEXT])).json()["document_no"]
    second = upload(client, build_text_pdf([INVOICE_TEXT])).json()["document_no"]

    assert first != second


def test_upload_validates_by_content_not_filename_or_content_type(
    client: TestClient,
) -> None:
    response = upload(
        client,
        build_text_pdf([INVOICE_TEXT]),
        filename="invoice.txt",
        content_type="text/plain",
    )

    assert response.status_code == 201
    body = response.json()
    assert body["original_filename"] == "invoice.txt"
    assert body["content_type"] == "text/plain"
    assert body["extraction_status"] == "extracted"


def test_get_document_returns_metadata_without_full_text(
    client: TestClient,
) -> None:
    document_no = upload(
        client, build_text_pdf([INVOICE_TEXT, "Page two: payment terms apply."])
    ).json()["document_no"]

    response = client.get(f"/documents/{document_no}")

    assert response.status_code == 200
    body = response.json()
    assert body["document_no"] == document_no
    assert body["page_count"] == 2
    assert body["extraction_status"] == "extracted"
    assert body["raw_text_included"] is False

    first_page = body["pages"][0]
    assert first_page["page_number"] == 1
    assert first_page["has_text"] is True
    assert first_page["text"] is None
    assert first_page["preview"].startswith(INVOICE_LINE)
    assert len(first_page["preview"]) <= 83

    # Content beyond the preview window must not be exposed by default.
    assert "Taxable Amount" not in response.text
    assert any("memory only" in note for note in body["notes"])


def test_get_document_can_include_full_text(client: TestClient) -> None:
    document_no = upload(client, build_text_pdf([INVOICE_TEXT])).json()["document_no"]

    response = client.get(
        f"/documents/{document_no}", params={"include_text": "true"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["raw_text_included"] is True
    page_text = body["pages"][0]["text"]
    assert INVOICE_LINE in page_text
    assert "Taxable Amount" in page_text


def test_document_lookup_is_case_insensitive(client: TestClient) -> None:
    document_no = upload(client, build_text_pdf([INVOICE_TEXT])).json()["document_no"]

    response = client.get(f"/documents/{document_no.lower()}")

    assert response.status_code == 200
    assert response.json()["document_no"] == document_no


def test_upload_rejects_non_pdf_content(client: TestClient) -> None:
    response = upload(
        client,
        b"This is a plain text file and definitely not a PDF.",
        filename="notes.txt",
        content_type="text/plain",
    )

    assert response.status_code == 415
    assert "not a PDF" in response.json()["detail"]


def test_upload_rejects_non_pdf_bytes_named_as_pdf(client: TestClient) -> None:
    # A .pdf filename and an application/pdf content type are not trusted.
    response = upload(
        client, b"%JUNK" * 100, filename="invoice.pdf", content_type="application/pdf"
    )

    assert response.status_code == 415
    assert "not a PDF" in response.json()["detail"]


def test_upload_rejects_corrupted_pdf(client: TestClient) -> None:
    corrupted = b"%PDF-1.4\n" + bytes([0x00, 0xFF, 0x7F]) * 200

    response = upload(client, corrupted, filename="corrupted.pdf")

    assert response.status_code == 400
    assert "corrupt" in response.json()["detail"].lower()
    assert client.get("/documents/DOC-UNKNOWN").status_code == 404


def test_upload_rejects_truncated_pdf(client: TestClient) -> None:
    valid = build_text_pdf([INVOICE_TEXT])
    truncated = valid[:200]

    response = upload(client, truncated, filename="truncated.pdf")

    assert response.status_code == 400
    assert "corrupt" in response.json()["detail"].lower()


def test_upload_rejects_empty_file(client: TestClient) -> None:
    response = upload(client, b"", filename="empty.pdf")

    assert response.status_code == 400
    assert "empty" in response.json()["detail"].lower()


def test_upload_requires_the_file_field(client: TestClient) -> None:
    response = client.post("/documents/upload")

    assert response.status_code == 422


def test_image_only_pdf_is_marked_needs_ocr(
    client: TestClient, without_paddleocr: None
) -> None:
    response = upload(client, build_blank_pdf(page_count=1), filename="scan.pdf")

    assert response.status_code == 201
    body = response.json()
    assert body["extraction_status"] == "needs_ocr"
    assert body["page_count"] == 1
    assert body["pages_with_text"] == 0
    assert body["total_characters"] == 0
    assert body["extraction_error"] == NEEDS_OCR_ERROR
    assert "OCR" in body["message"]
    # The quality check names the page the fallback could not read.
    assert body["used_ocr"] is False
    assert body["ocr_pages"] == []
    assert body["quality"]["pages_needing_ocr"] == [1]
    assert body["quality"]["ocr_attempted"] is False


def test_needs_ocr_document_discloses_the_limitation(
    client: TestClient, without_paddleocr: None
) -> None:
    document_no = upload(client, build_blank_pdf(), filename="scan.pdf").json()[
        "document_no"
    ]

    response = client.get(f"/documents/{document_no}")

    assert response.status_code == 200
    body = response.json()
    assert body["extraction_status"] == "needs_ocr"
    assert body["pages"][0]["has_text"] is False
    assert body["pages"][0]["preview"] == ""
    assert body["pages"][0]["text"] is None
    assert body["pages"][0]["source"] == "pdfplumber"
    assert any("OCR" in note for note in body["notes"])
    assert any("PaddleOCR" in note for note in body["notes"])
    assert body["quality"]["passed"] is False


# --- Phase 9: pdfplumber tables, the quality check and the OCR fallback -----------


def test_upload_reports_the_extraction_quality_check(client: TestClient) -> None:
    body = upload(client, build_text_pdf([COMPLETE_INVOICE_TEXT])).json()

    quality = body["quality"]
    assert quality["passed"] is True
    assert set(quality["required_fields_found"]) == set(REQUIRED_INVOICE_FIELDS)
    assert quality["missing_required_fields"] == []
    assert quality["pages_needing_ocr"] == []
    assert quality["ocr_attempted"] is False
    assert body["used_ocr"] is False
    assert body["ocr_pages"] == []
    assert body["table_count"] == 0
    assert "quality=passed" in quality["summary"]


def test_upload_flags_required_fields_missing_from_the_text_layer(
    client: TestClient,
) -> None:
    body = upload(client, build_text_pdf([INVOICE_TEXT])).json()

    # The invoice has no date, so the check reports it instead of guessing one.
    assert body["quality"]["missing_required_fields"] == ["invoice_date"]
    assert body["quality"]["passed"] is False
    assert any(
        "Required invoice fields were not found" in issue
        for issue in body["quality"]["issues"]
    )
    # The text layer is still usable, so the document is readable and auditable.
    assert body["extraction_status"] == "extracted"


def test_upload_rebuilds_ruled_tables_with_pdfplumber(client: TestClient) -> None:
    rows = [
        ["Description", "Amount"],
        ["Consulting services", "1,000.00"],
        ["Total", "1,050.00"],
    ]

    body = upload(client, build_table_pdf(rows)).json()

    assert body["table_count"] == 1
    assert body["quality"]["table_pages"] == [1]

    detail = client.get(f"/documents/{body['document_no']}").json()
    assert detail["pages"][0]["table_count"] == 1
    assert detail["pages"][0]["tables"] is None  # withheld unless include_text=true

    with_text = client.get(
        f"/documents/{body['document_no']}", params={"include_text": "true"}
    ).json()
    assert with_text["pages"][0]["tables"] == [rows]


def test_upload_uses_the_ocr_fallback_for_scanned_pages(client: TestClient) -> None:
    engine = install_ocr_engine(StubOcrEngine())
    try:
        response = upload(client, build_scanned_pdf(), filename="scan.pdf")
    finally:
        app.dependency_overrides.pop(get_document_ocr_engine, None)

    assert response.status_code == 201
    body = response.json()
    assert body["extraction_status"] == "extracted"
    assert body["used_ocr"] is True
    assert body["ocr_pages"] == [1]
    assert body["table_count"] == 1
    assert body["quality"]["ocr_attempted"] is True
    assert len(engine.images) == 1
    assert "PaddleOCR fallback read them" in body["message"]

    detail = client.get(f"/documents/{body['document_no']}").json()
    assert detail["pages"][0]["source"] == "paddleocr"
    assert detail["pages"][0]["character_count"] > 0
    assert any(
        "PaddleOCR fallback read them" in note for note in detail["notes"]
    )


def test_upload_without_the_ocr_engine_keeps_the_fallback_visible(
    client: TestClient, without_paddleocr: None
) -> None:
    # A scanned page that carries only the scanner stamp: unreadable without OCR.
    body = upload(client, build_scanned_pdf(), filename="scan.pdf").json()

    assert body["used_ocr"] is False
    assert body["ocr_pages"] == []
    assert any(
        "PaddleOCR fallback is not installed" in issue
        for issue in body["quality"]["issues"]
    )


def test_upload_rejects_an_encrypted_pdf(client: TestClient) -> None:
    response = upload(client, build_encrypted_pdf(), filename="protected.pdf")

    assert response.status_code == 400
    assert "password protected" in response.json()["detail"]


def test_upload_reports_table_style_pages_in_the_detail_view(
    client: TestClient,
) -> None:
    rows = [["Description", "Amount"], ["Consulting services", "1,000.00"]]
    document_no = upload(client, build_table_pdf(rows)).json()["document_no"]

    body = client.get(
        f"/documents/{document_no}", params={"include_text": "true"}
    ).json()

    assert body["raw_text_included"] is True
    assert body["pages"][0]["has_text"] is True
    assert body["table_count"] == 1
    assert body["quality"]["table_pages"] == [1]


def test_unknown_document_number_returns_404(client: TestClient) -> None:
    response = client.get("/documents/DOC-00000000000000000000000000000000")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_upload_rejects_file_over_the_configured_limit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    oversized = build_text_pdf([INVOICE_TEXT * 20])
    # Derive the limit from the real payload, since PyMuPDF compresses output.
    monkeypatch.setattr(settings, "max_upload_size_bytes", len(oversized) - 1)

    response = upload(client, oversized)

    assert response.status_code == 413
    assert "maximum allowed size" in response.json()["detail"]


def test_upload_accepts_file_exactly_at_the_configured_limit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = build_text_pdf([INVOICE_TEXT])
    monkeypatch.setattr(settings, "max_upload_size_bytes", len(pdf))

    response = upload(client, pdf)

    assert response.status_code == 201
    assert response.json()["size_bytes"] == len(pdf)


def test_openapi_schema_exposes_document_endpoints(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()

    assert set(schema["paths"]["/documents/upload"]) == {"post"}
    assert set(schema["paths"]["/documents/{document_no}"]) == {"get"}
    assert schema["info"]["version"] == "0.2.0"


def test_health_endpoint_still_works(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


