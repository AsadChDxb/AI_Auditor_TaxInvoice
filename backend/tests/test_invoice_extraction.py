"""Unit tests for invoice field extraction and normalisation (Phase 3).

No text leaves the process: the LLM extractor is replaced by a stub from
``tests.llm_stubs``.
"""

from decimal import Decimal

import pytest

from app.models.invoice import INVOICE_FIELD_NAMES, RawInvoiceFields
from app.services.invoice_extractor import (
    DocumentNotExtractableError,
    InvoiceExtractionService,
    build_invoice_data,
    normalise_classification,
    normalise_confidence,
    normalise_date,
    parse_decimal,
)
from app.services.llm import DEFAULT_PROVIDER_LABEL, RawExtraction
from app.services.llm_errors import LLMProviderError
from tests.llm_stubs import (
    INVOICE_JSON,
    TEST_MODEL,
    TEST_PROVIDER_LABEL,
    StubExtractor,
    make_record,
)


def build(fields: dict, *, page_count: int | None = None, model: str | None = TEST_MODEL):
    """Validate `fields` like the client would, then normalise them."""

    raw = RawExtraction(fields=RawInvoiceFields.model_validate(fields), model=model)
    return build_invoice_data(raw, page_count=page_count)


@pytest.mark.parametrize(
    ("raw", "expected", "expect_warning"),
    [
        (None, None, False),
        ("", None, False),
        ("N/A", None, False),
        ("-", None, False),
        ("1000", Decimal("1000.00"), False),
        (1000, Decimal("1000.00"), False),
        (50.5, Decimal("50.50"), False),
        (Decimal("1000.005"), Decimal("1000.01"), False),
        ("AED 1,000.00", Decimal("1000.00"), False),
        ("1,234,567.89", Decimal("1234567.89"), False),
        ("1.234.567", Decimal("1234567.00"), False),
        ("1.234,56", Decimal("1234.56"), True),
        ("1000,00", Decimal("1000.00"), True),
        ("(50.00)", Decimal("-50.00"), False),
        ("-25.5", Decimal("-25.50"), False),
        ("50.00 VAT", Decimal("50.00"), False),
        ("5%", Decimal("5.00"), False),
        ("1,234", Decimal("1234.00"), True),
        ("one thousand", None, True),
        ("12.34.56", None, True),
        ("nan", None, True),
        (True, None, True),
    ],
)
def test_parse_decimal_handles_realistic_money_formats(
    raw, expected: Decimal | None, expect_warning: bool
) -> None:
    amount, warnings = parse_decimal(raw, "vat_amount")

    assert amount == expected
    assert bool(warnings) is expect_warning
    for warning in warnings:
        assert "vat_amount" in warning


@pytest.mark.parametrize(
    ("raw", "expected", "expect_warning"),
    [
        (None, None, False),
        ("n/a", None, False),
        ("2024-05-03", "2024-05-03", False),
        (" 2024-05-03 ", "2024-05-03", False),
        ("03/05/2024", "03/05/2024", True),
        ("2024-02-31", "2024-02-31", True),
    ],
)
def test_normalise_date_never_rewrites_ambiguous_values(
    raw, expected: str | None, expect_warning: bool
) -> None:
    value, warnings = normalise_date(raw, "invoice_date")

    assert value == expected
    assert bool(warnings) is expect_warning


@pytest.mark.parametrize(
    ("raw", "expected", "expect_warning"),
    [
        ("Standard Rated", "standard_rated", False),
        ("standard_rated", "standard_rated", False),
        ("zero-rated", "zero_rated", False),
        ("Zero Rated", "zero_rated", False),
        ("Exempt", "exempt", False),
        ("exempted", "exempt", False),
        ("unknown", "unknown", False),
        (None, "unknown", True),
        ("reverse charge", "unknown", True),
        ("5% standard", "unknown", True),
    ],
)
def test_normalise_classification_maps_or_requests_review(
    raw, expected: str, expect_warning: bool
) -> None:
    classification, warnings = normalise_classification(raw)

    assert classification == expected
    assert bool(warnings) is expect_warning


@pytest.mark.parametrize(
    ("raw", "expected", "expect_warning"),
    [
        (None, None, False),
        (95, 95.0, False),
        ("88", 88.0, False),
        ("90%", 90.0, False),
        (0.9, 90.0, True),
        (150, None, True),
        ("high", None, True),
        (True, None, True),
    ],
)
def test_normalise_confidence_parses_units_and_rejects_junk(
    raw, expected: float | None, expect_warning: bool
) -> None:
    confidence, warnings = normalise_confidence(raw, "field_evidence.vat_amount")

    assert confidence == expected
    assert bool(warnings) is expect_warning


def test_full_extraction_is_validated_normalised_and_evidence_linked() -> None:
    invoice = build(INVOICE_JSON, page_count=1)

    assert invoice.invoice_number == "INV-2024-0001"
    assert invoice.invoice_date == "2024-05-03"
    assert invoice.supplier_name == "Al Noor Trading LLC"
    assert invoice.supplier_trn == "100123456700003"
    assert invoice.customer_name == "Gulf Retail FZE"
    assert invoice.customer_trn == "100987654300003"
    assert invoice.currency == "AED"

    assert isinstance(invoice.subtotal_excluding_vat, Decimal)
    assert invoice.subtotal_excluding_vat == Decimal("1000.00")
    assert invoice.vat_amount == Decimal("50.00")
    assert invoice.total_including_vat == Decimal("1050.00")
    assert invoice.vat_rate_percent == Decimal("5.00")
    assert invoice.vat_classification == "standard_rated"

    assert invoice.missing_fields == []
    assert invoice.provider == DEFAULT_PROVIDER_LABEL
    assert invoice.model == TEST_MODEL
    assert invoice.extracted_at.tzinfo is not None

    supplier_evidence = invoice.field_evidence["supplier_trn"]
    assert supplier_evidence.value == "100123456700003"
    assert supplier_evidence.confidence_percent == 95.0
    assert supplier_evidence.page_number == 1
    assert supplier_evidence.evidence == "Supplier TRN: 100123456700003"
    assert invoice.field_evidence["vat_amount"].value == "50.00"

    assert "model: The due date was not printed on the invoice." in invoice.warnings
    assert not any("were left null" in warning for warning in invoice.warnings)


def test_missing_values_stay_null_and_are_listed() -> None:
    invoice = build({})

    expected_missing = set(INVOICE_FIELD_NAMES) - {"vat_classification"}
    assert set(invoice.missing_fields) == expected_missing
    assert invoice.invoice_number is None
    assert invoice.subtotal_excluding_vat is None
    assert invoice.vat_classification == "unknown"

    assert any("left null, not guessed" in warning for warning in invoice.warnings)
    assert any("needs human confirmation" in warning for warning in invoice.warnings)


def test_placeholder_text_values_become_null() -> None:
    invoice = build(
        {
            "invoice_number": "  INV 2024 \n 0001 ",
            "supplier_trn": 100123456700003,
            "customer_trn": "N/A",
            "currency": "unknown",
        }
    )

    assert invoice.invoice_number == "INV 2024 0001"
    assert invoice.supplier_trn == "100123456700003"
    assert invoice.customer_trn is None
    assert invoice.currency is None
    assert "customer_trn" in invoice.missing_fields
    assert "currency" in invoice.missing_fields


def test_unreadable_amount_is_null_with_a_warning_and_listed_as_missing() -> None:
    invoice = build({"subtotal_excluding_vat": "approximately one thousand"})

    assert invoice.subtotal_excluding_vat is None
    assert any(
        "could not be interpreted as a number" in warning for warning in invoice.warnings
    )
    assert "subtotal_excluding_vat" in invoice.missing_fields


def test_evidence_for_unknown_fields_and_bad_pages_is_reported() -> None:
    invoice = build(
        {
            "invoice_number": "INV-1",
            "field_evidence": {
                "taxable_amount": {"confidence_percent": 50},
                "invoice_number": {"page_number": 7},
                "vat_amount": {"confidence_percent": "high", "page_number": 0},
            },
        },
        page_count=2,
    )

    assert set(invoice.field_evidence) == {"invoice_number", "vat_amount"}
    assert invoice.field_evidence["invoice_number"].page_number == 7
    assert invoice.field_evidence["vat_amount"].confidence_percent is None
    assert invoice.field_evidence["vat_amount"].page_number is None

    joined = " ".join(invoice.warnings)
    assert "ignored unknown field 'taxable_amount'" in joined
    assert "beyond the document length (2 page(s))" in joined
    assert "is not a number and was dropped" in joined
    assert "not a valid 1-based page" in joined


def test_explicit_unknown_classification_still_requests_confirmation() -> None:
    invoice = build({"vat_classification": "unknown"})

    assert invoice.vat_classification == "unknown"
    assert any("needs human confirmation" in warning for warning in invoice.warnings)


def test_warning_list_is_capped_to_keep_responses_small() -> None:
    invoice = build(
        {"extraction_warnings": [f"warning {index}" for index in range(30)]}
    )

    assert len(invoice.warnings) == 26
    assert "further extraction warning(s) were dropped" in invoice.warnings[-1]


def test_service_sends_stored_pages_to_the_provider() -> None:
    stub = StubExtractor()
    service = InvoiceExtractionService(extractor=stub)
    record = make_record(["Invoice No: INV-2024-0001", "Payment terms"])

    invoice = service.extract(record)

    assert invoice.invoice_number == "INV-2024-0001"
    assert invoice.provider == TEST_PROVIDER_LABEL
    assert stub.calls == [[(1, "Invoice No: INV-2024-0001"), (2, "Payment terms")]]


def test_service_refuses_documents_that_need_ocr_without_calling_the_provider() -> None:
    stub = StubExtractor()
    service = InvoiceExtractionService(extractor=stub)

    with pytest.raises(DocumentNotExtractableError) as excinfo:
        service.extract(make_record([""], status="needs_ocr"))

    assert "no extractable text" in str(excinfo.value)
    assert stub.calls == []


def test_service_refuses_documents_without_any_text() -> None:
    stub = StubExtractor()
    service = InvoiceExtractionService(extractor=stub)

    with pytest.raises(DocumentNotExtractableError):
        service.extract(make_record(["   \n  "]))

    assert stub.calls == []


def test_service_propagates_provider_failures() -> None:
    service = InvoiceExtractionService(
        extractor=StubExtractor(error=LLMProviderError("boom"))
    )

    with pytest.raises(LLMProviderError):
        service.extract(make_record())


def test_service_builds_its_extractor_lazily_when_none_is_injected(monkeypatch) -> None:
    created: list[int] = []

    def fake_extractor() -> StubExtractor:
        created.append(1)
        return StubExtractor()

    monkeypatch.setattr("app.services.invoice_extractor.LLMExtractor", fake_extractor)
    service = InvoiceExtractionService()

    assert created == []

    invoice = service.extract(make_record())

    assert created == [1]
    assert invoice.invoice_number == INVOICE_JSON["invoice_number"]
