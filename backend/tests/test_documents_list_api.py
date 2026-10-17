"""Tests for ``GET /documents`` (Phase 8).

The list endpoint is what the Next.js UI renders as one table row per document, so
these tests pin the row contents (text extraction, invoice extraction and the latest
audit result) and the ``can_extract``/``can_audit`` action flags.

Everything runs offline: records are written straight into the in-memory registry and
reports are placed in the in-memory report store, so no provider is called and no API
key is needed.
"""

from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.audit.ai_auditor import report_store
from app.models.audit import AuditFinding, AuditReport, summarise_findings
from app.models.invoice import InvoiceData
from app.services.pdf_extractor import ExtractedPage
from app.storage.memory import DocumentRecord, document_registry
from tests.audit_stubs import make_invoice

LIST_PATH = "/documents"
PAGE_TEXT = "TAX INVOICE INV-2024-0001 Subtotal 1,000.00 VAT 50.00 Total 1,050.00"


@pytest.fixture(autouse=True)
def clean_report_store(empty_document_registry: None) -> Iterator[None]:
    """Keep the in-memory report store isolated between tests."""

    report_store.clear()
    yield
    report_store.clear()


def register(
    document_no: str,
    *,
    invoice: InvoiceData | None,
    status: str = "extracted",
    page_texts: tuple[str, ...] = (PAGE_TEXT,),
    uploaded_at: datetime | None = None,
) -> DocumentRecord:
    """Store one document and return it."""

    record = DocumentRecord(
        document_no=document_no,
        original_filename=f"{document_no.lower()}.pdf",
        content_type="application/pdf",
        size_bytes=1234,
        uploaded_at=uploaded_at or datetime(2024, 5, 3, 12, 0, tzinfo=timezone.utc),
        page_count=len(page_texts),
        pages=[
            ExtractedPage(page_number=index + 1, text=text)
            for index, text in enumerate(page_texts)
        ],
        extraction_status=status,
        extraction_error=None if status == "extracted" else "No extractable text found.",
        invoice=invoice,
    )
    return document_registry.add(record)


def make_report(
    document_no: str,
    *,
    status: str = "completed",
    failed: int = 0,
) -> AuditReport:
    """Build a stored audit report without running the engine."""

    findings = [
        AuditFinding(
            rule_id=f"RULE_{index}",
            status="fail" if index < failed else "pass",
            severity="high",
            explanation="Deterministic check result.",
            confidence_percent=95.0 if index >= failed else 40.0,
            human_review_required=index < failed,
        )
        for index in range(4)
    ]
    return AuditReport(
        document_no=document_no,
        invoice_number="INV-2024-0001",
        audit_status=status,
        overall_confidence_percent=94.1,
        findings=findings,
        summary=summarise_findings(findings),
        ruleset_version="uae-vat-rules-1.0.0",
        human_review_required=failed > 0,
        audited_at=datetime(2024, 5, 3, 13, 0, tzinfo=timezone.utc),
    )


def rows_by_number(client: TestClient) -> dict[str, dict]:
    """Return the list response's rows keyed by document number."""

    body = client.get(LIST_PATH).json()
    return {row["document_no"]: row for row in body["documents"]}


# --- shape and enumeration --------------------------------------------------------


def test_empty_registry_returns_an_empty_documented_list(client: TestClient) -> None:
    response = client.get(LIST_PATH)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 0
    assert body["documents"] == []
    assert body["notes"], "the list must state its in-memory limitations"


def test_rows_are_listed_newest_upload_first(client: TestClient) -> None:
    register(
        "DOC-OLDEST",
        invoice=make_invoice(),
        uploaded_at=datetime(2024, 5, 1, 9, 0, tzinfo=timezone.utc),
    )
    register(
        "DOC-NEWEST",
        invoice=make_invoice(),
        uploaded_at=datetime(2024, 5, 3, 9, 0, tzinfo=timezone.utc),
    )
    register(
        "DOC-MIDDLE",
        invoice=make_invoice(),
        uploaded_at=datetime(2024, 5, 2, 9, 0, tzinfo=timezone.utc),
    )

    body = client.get(LIST_PATH).json()

    assert body["total"] == 3
    assert [row["document_no"] for row in body["documents"]] == [
        "DOC-NEWEST",
        "DOC-MIDDLE",
        "DOC-OLDEST",
    ]


def test_row_describes_text_and_invoice_extraction(client: TestClient) -> None:
    register("DOC-READY", invoice=make_invoice())

    row = rows_by_number(client)["DOC-READY"]

    assert row["original_filename"] == "doc-ready.pdf"
    assert row["content_type"] == "application/pdf"
    assert row["size_bytes"] == 1234
    assert row["page_count"] == 1
    assert row["pages_with_text"] == 1
    assert row["total_characters"] == len(PAGE_TEXT)
    assert row["extraction_status"] == "extracted"
    assert row["extraction_error"] is None
    assert row["invoice_extracted"] is True
    assert row["invoice_number"] == "INV-2024-0001"
    assert row["invoice_total_including_vat"] == "1050.00"
    assert row["vat_classification"] == "standard_rated"


def test_action_flags_follow_the_workflow(client: TestClient) -> None:
    register("DOC-READY", invoice=make_invoice())
    register("DOC-NOT-EXTRACTED", invoice=None)
    register("DOC-NEEDS-OCR", invoice=None, status="needs_ocr", page_texts=("",))

    rows = rows_by_number(client)

    assert (rows["DOC-READY"]["can_extract"], rows["DOC-READY"]["can_audit"]) == (
        True,
        True,
    )
    assert (
        rows["DOC-NOT-EXTRACTED"]["can_extract"],
        rows["DOC-NOT-EXTRACTED"]["can_audit"],
    ) == (True, False)
    assert (
        rows["DOC-NEEDS-OCR"]["can_extract"],
        rows["DOC-NEEDS-OCR"]["can_audit"],
    ) == (False, False)
    assert rows["DOC-NEEDS-OCR"]["extraction_status"] == "needs_ocr"
    assert rows["DOC-NEEDS-OCR"]["extraction_error"]


def test_blank_page_text_is_not_extractable(client: TestClient) -> None:
    register("DOC-BLANK", invoice=None, page_texts=("   ",))

    row = rows_by_number(client)["DOC-BLANK"]

    assert row["extraction_status"] == "extracted"
    assert row["pages_with_text"] == 0
    assert row["can_extract"] is False


# --- audit columns ----------------------------------------------------------------


def test_audit_columns_are_null_before_an_audit(client: TestClient) -> None:
    register("DOC-UNAUDITED", invoice=make_invoice())

    row = rows_by_number(client)["DOC-UNAUDITED"]

    assert row["audited"] is False
    assert row["audit_status"] is None
    assert row["audit_overall_confidence_percent"] is None
    assert row["audit_human_review_required"] is None
    assert row["audit_summary"] is None
    assert row["audit_ruleset_version"] is None
    assert row["audited_at"] is None


def test_audit_columns_reflect_the_latest_report(client: TestClient) -> None:
    register("DOC-AUDITED", invoice=make_invoice())
    report_store.set(make_report("DOC-AUDITED", status="requires_review", failed=1))

    row = rows_by_number(client)["DOC-AUDITED"]

    assert row["audited"] is True
    assert row["audit_status"] == "requires_review"
    assert row["audit_overall_confidence_percent"] == 94.1
    assert row["audit_human_review_required"] is True
    assert row["audit_ruleset_version"] == "uae-vat-rules-1.0.0"
    assert row["audited_at"].startswith("2024-05-03T13:00:00")
    summary = row["audit_summary"]
    assert summary["total"] == 4
    assert summary["passed"] == 3
    assert summary["failed"] == 1


def test_only_the_latest_report_is_reported(client: TestClient) -> None:
    register("DOC-REAUDITED", invoice=make_invoice())
    report_store.set(make_report("DOC-REAUDITED", status="requires_review", failed=2))
    report_store.set(make_report("DOC-REAUDITED", status="completed", failed=0))

    row = rows_by_number(client)["DOC-REAUDITED"]

    assert row["audit_status"] == "completed"
    assert row["audit_human_review_required"] is False
    assert row["audit_summary"]["failed"] == 0


def test_audit_columns_are_scoped_to_their_own_document(client: TestClient) -> None:
    register("DOC-A", invoice=make_invoice())
    register("DOC-B", invoice=make_invoice())
    report_store.set(make_report("DOC-A", status="completed"))

    rows = rows_by_number(client)

    assert rows["DOC-A"]["audited"] is True
    assert rows["DOC-B"]["audited"] is False


# --- routing and schema -----------------------------------------------------------


def test_list_path_is_not_shadowed_by_the_detail_route(client: TestClient) -> None:
    """``GET /documents`` must resolve to the list, not to ``/{document_no}``."""

    register("DOC-SHADOW", invoice=make_invoice())

    response = client.get(LIST_PATH)

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["documents"][0]["document_no"] == "DOC-SHADOW"


def test_openapi_exposes_the_list_route(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()

    assert set(schema["paths"]["/documents"]) == {"get"}
    assert set(schema["paths"]["/documents/{document_no}"]) == {"get"}
    assert set(schema["paths"]["/documents/upload"]) == {"post"}

    item = schema["components"]["schemas"]["DocumentListItem"]["properties"]
    for field in (
        "document_no",
        "original_filename",
        "extraction_status",
        "invoice_extracted",
        "audited",
        "audit_status",
        "audit_summary",
        "can_extract",
        "can_audit",
    ):
        assert field in item, f"missing list field: {field}"

    listing = schema["components"]["schemas"]["DocumentListResponse"]["properties"]
    assert {"total", "documents", "notes"} <= set(listing)


