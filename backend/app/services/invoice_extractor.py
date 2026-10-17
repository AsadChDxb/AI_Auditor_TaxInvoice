"""Structured invoice field extraction (Phase 3).

Turns the raw, schema-validated model output from the provider-agnostic
``app.services.llm`` layer into the application-facing
:class:`~app.models.invoice.InvoiceData`:

* monetary amounts are parsed into ``Decimal`` **after** the provider response has
  already been validated by Pydantic,
* missing or unreadable values stay ``None`` instead of being guessed, and
* every interpretation that needed a judgement call is recorded as a warning.

VAT audit decisions are not made here; that is Phase 4.
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import cast

from app.models.invoice import (
    INVOICE_FIELD_NAMES,
    FieldEvidence,
    InvoiceData,
    RawFieldEvidence,
    VatClassification,
)
from app.services.llm import DEFAULT_PROVIDER_LABEL, LLMExtractor, RawExtraction
from app.storage.memory import DocumentRecord

MONEY_QUANTUM = Decimal("0.01")
RATE_QUANTUM = Decimal("0.01")
MAX_EVIDENCE_CHARACTERS = 300
MAX_WARNING_CHARACTERS = 300
MAX_WARNINGS = 25

MISSING_MARKERS = frozenset(
    {
        "",
        "-",
        "--",
        "---",
        "n/a",
        "na",
        "none",
        "null",
        "nil",
        "unknown",
        "not available",
        "not applicable",
        "not specified",
        "not stated",
        "missing",
        "unreadable",
        "illegible",
    }
)
"""Values that mean "not present" rather than a real value."""

CURRENCY_TOKENS = re.compile(
    r"(?i)(aed|usd|eur|gbp|sar|qar|omr|kwd|bhd|inr|pkr|dhs|dirhams?|riyals?|"
    r"dollars?|euros?|[$€£¥])"
)
TAX_WORDS = re.compile(
    r"(?i)\b(only|vat|v\.a\.t\.?|tax|incl\.?|excl\.?|inclusive|exclusive)\b"
)
ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

CLASSIFICATION_ALIASES: dict[str, str] = {
    "standard_rated": "standard_rated",
    "standard": "standard_rated",
    "standardrated": "standard_rated",
    "standard_rate": "standard_rated",
    "zero_rated": "zero_rated",
    "zero": "zero_rated",
    "zerorated": "zero_rated",
    "zero_rate": "zero_rated",
    "exempt": "exempt",
    "exempted": "exempt",
    "vat_exempt": "exempt",
    "unknown": "unknown",
    "unspecified": "unknown",
    "unclear": "unknown",
}
"""Accepted spellings for a reported VAT treatment; anything else becomes ``unknown``."""

OCR_BLOCKING_MESSAGE = (
    "Invoice fields cannot be extracted because the document has no extractable "
    "text (it needs OCR)."
)


class InvoiceExtractionError(RuntimeError):
    """Invoice field extraction failed."""


class DocumentNotExtractableError(InvoiceExtractionError):
    """The stored document cannot be used for field extraction (needs OCR)."""


def truncate(text: str, limit: int) -> str:
    """Return `text` capped at `limit` characters, with an ellipsis marker."""

    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def clean_text(value: str | None) -> str | None:
    """Collapse whitespace and treat placeholder markers as missing values."""

    if value is None:
        return None
    text = " ".join(str(value).split())
    if text.lower() in MISSING_MARKERS:
        return None
    return text or None


def quantize(amount: Decimal, quantum: Decimal) -> Decimal:
    """Round `amount` to `quantum`; fall back to the original on exotic input."""

    try:
        return amount.quantize(quantum, rounding=ROUND_HALF_UP)
    except InvalidOperation:  # pragma: no cover - extremes only
        return amount


def parse_decimal(
    value: str | int | float | Decimal | None, field_name: str
) -> tuple[Decimal | None, list[str]]:
    """Parse a provider value into a ``Decimal`` amount plus any warnings.

    Returns ``(None, [])`` for genuinely absent values (``""``, ``"N/A"``, ...) and
    ``(None, [warning])`` when a value was present but could not be interpreted.
    Formatting variants such as ``"AED 1,000.00"``, ``"1.234,56"`` or ``"(50.00)"``
    are handled; the reading is described in a warning when it involved a choice.
    """

    if value is None:
        return None, []
    if isinstance(value, bool):
        return None, [
            f"{field_name}: the boolean value '{value}' is not a monetary amount."
        ]
    if isinstance(value, int):
        return quantize(Decimal(value), MONEY_QUANTUM), []
    if isinstance(value, Decimal):
        return quantize(value, MONEY_QUANTUM), []
    if isinstance(value, float):
        return quantize(Decimal(str(value)), MONEY_QUANTUM), []

    text = str(value).strip()
    if text.lower() in MISSING_MARKERS:
        return None, []

    warnings: list[str] = []
    negative = text.startswith("(") and text.endswith(")")
    cleaned = text[1:-1] if negative else text
    cleaned = CURRENCY_TOKENS.sub("", cleaned)
    cleaned = TAX_WORDS.sub("", cleaned)
    cleaned = (
        cleaned.replace("\u00a0", "")
        .replace("%", "")
        .replace(" ", "")
        .replace("_", "")
    )
    if cleaned.startswith("-"):
        negative = True
        cleaned = cleaned[1:]
    elif cleaned.startswith("+"):
        cleaned = cleaned[1:]

    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
            warnings.append(
                f"{field_name}: '{text}' used comma-decimal formatting and was read "
                f"as {cleaned}."
            )
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        parts = cleaned.split(",")
        if len(parts) == 2 and len(parts[1]) != 3:
            cleaned = ".".join(parts)
            warnings.append(
                f"{field_name}: '{text}' used comma-decimal formatting and was read "
                f"as {cleaned}."
            )
        else:
            cleaned = "".join(parts)
            warnings.append(
                f"{field_name}: '{text}' was read as a thousands-separated amount "
                f"({cleaned}); confirm it against the source invoice."
            )

    if cleaned.count(".") > 1:
        groups = cleaned.split(".")
        if all(len(group) == 3 for group in groups[1:]):
            cleaned = "".join(groups)
        else:
            return None, warnings + [
                f"{field_name}: '{text}' has an ambiguous decimal separator and was "
                "left null."
            ]

    if not cleaned or cleaned == ".":
        return None, warnings + [
            f"{field_name}: '{text}' could not be interpreted as a number."
        ]

    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None, warnings + [
            f"{field_name}: '{text}' could not be interpreted as a number."
        ]

    if not amount.is_finite():
        return None, warnings + [f"{field_name}: '{text}' is not a finite number."]
    if negative:
        amount = -amount
    return quantize(amount, MONEY_QUANTUM), warnings


def normalise_date(value: str | None, field_name: str) -> tuple[str | None, list[str]]:
    """Keep an ISO 8601 date as-is; keep anything else verbatim plus a warning.

    Day/month order cannot be inferred reliably, so an ambiguous date is never
    silently rewritten.
    """

    text = clean_text(value)
    if text is None:
        return None, []

    match = ISO_DATE.match(text)
    if match:
        year, month, day = (int(part) for part in match.groups())
        try:
            date(year, month, day)
        except ValueError:
            return text, [
                f"{field_name}: '{text}' is not a real calendar date; the reported "
                "value is kept and needs human confirmation."
            ]
        return text, []

    return text, [
        f"{field_name}: '{text}' is not ISO 8601 (YYYY-MM-DD); the reported value is "
        "kept as-is because the day/month order is ambiguous."
    ]


def normalise_classification(value: str | None) -> tuple[VatClassification, list[str]]:
    """Map a reported treatment onto the four allowed values, warning otherwise.

    Unlike other text fields, the literal ``"unknown"`` is meaningful here (it is
    one of the allowed classifications), so only ``None``/blank counts as missing.
    Every path that ends in ``unknown`` records a warning that human confirmation
    is required.
    """

    text = None if value is None else " ".join(str(value).split())
    if not text:
        return "unknown", [
            "vat_classification: no VAT treatment was reported; recorded as 'unknown' "
            "and needs human confirmation."
        ]

    key = re.sub(
        r"_+", "_", text.lower().replace("-", "_").replace(" ", "_")
    ).strip("_")
    classification = CLASSIFICATION_ALIASES.get(key)
    if classification is None:
        return "unknown", [
            f"vat_classification: '{text}' is not a recognised UAE VAT treatment; "
            "recorded as 'unknown' and needs human confirmation."
        ]
    return cast(VatClassification, classification), []


def normalise_confidence(
    value: str | int | float | Decimal | None, field_name: str
) -> tuple[float | None, list[str]]:
    """Parse a model confidence into a 0-100 percentage, or drop it."""

    if value is None:
        return None, []
    if isinstance(value, bool):
        return None, [
            f"{field_name}: confidence '{value}' is not a number and was dropped."
        ]

    warnings: list[str] = []
    if isinstance(value, (int, float, Decimal)):
        number = float(value)
    else:
        text = str(value).strip().rstrip("%").strip()
        if not text or text.lower() in MISSING_MARKERS:
            return None, []
        try:
            number = float(text)
        except ValueError:
            return None, [
                f"{field_name}: confidence '{value}' is not a number and was dropped."
            ]

    if 0 < number < 1:
        warnings.append(
            f"{field_name}: confidence {number:g} was interpreted as a fraction and "
            f"converted to {number * 100:g} percent."
        )
        number *= 100

    if not 0 <= number <= 100:
        return None, warnings + [
            f"{field_name}: confidence {number:g} is outside the 0-100 range and was dropped."
        ]
    return round(number, 2), warnings


def build_field_evidence(
    raw_evidence: dict[str, RawFieldEvidence],
    page_count: int | None = None,
) -> tuple[dict[str, FieldEvidence], list[str]]:
    """Validate and normalise the model's per-field provenance block."""

    evidence: dict[str, FieldEvidence] = {}
    warnings: list[str] = []

    for name, raw in raw_evidence.items():
        if name not in INVOICE_FIELD_NAMES:
            warnings.append(
                f"field_evidence: ignored unknown field '{truncate(str(name), 60)}'."
            )
            continue

        confidence, confidence_warnings = normalise_confidence(
            raw.confidence_percent, f"field_evidence.{name}.confidence_percent"
        )
        warnings.extend(confidence_warnings)

        page_number = raw.page_number if (raw.page_number or 0) >= 1 else None
        if raw.page_number is not None and page_number is None:
            warnings.append(
                f"field_evidence.{name}: page number {raw.page_number} is not a valid "
                "1-based page and was dropped."
            )
        elif page_number is not None and page_count is not None and page_number > page_count:
            warnings.append(
                f"field_evidence.{name}: page number {page_number} is beyond the "
                f"document length ({page_count} page(s)) and may be unreliable."
            )

        snippet = clean_text(raw.evidence)
        evidence[name] = FieldEvidence(
            value=None,
            confidence_percent=confidence,
            page_number=page_number,
            evidence=truncate(snippet, MAX_EVIDENCE_CHARACTERS) if snippet else None,
        )

    return evidence, warnings


def build_invoice_data(
    raw_extraction: RawExtraction,
    *,
    provider: str = DEFAULT_PROVIDER_LABEL,
    page_count: int | None = None,
) -> InvoiceData:
    """Convert raw provider output into validated, warning-annotated invoice data."""

    fields = raw_extraction.fields
    warnings: list[str] = []

    for item in fields.extraction_warnings:
        text = " ".join(str(item).split())
        if text:
            warnings.append(f"model: {text}")
    warnings.extend(raw_extraction.client_warnings)

    invoice_date, date_warnings = normalise_date(fields.invoice_date, "invoice_date")
    warnings.extend(date_warnings)

    classification, classification_warnings = normalise_classification(
        fields.vat_classification
    )
    warnings.extend(classification_warnings)
    if classification == "unknown" and not classification_warnings:
        warnings.append(
            "vat_classification: the reported treatment is 'unknown' and needs human "
            "confirmation."
        )

    subtotal, subtotal_warnings = parse_decimal(
        fields.subtotal_excluding_vat, "subtotal_excluding_vat"
    )
    vat_amount, vat_warnings = parse_decimal(fields.vat_amount, "vat_amount")
    total, total_warnings = parse_decimal(
        fields.total_including_vat, "total_including_vat"
    )
    rate, rate_warnings = parse_decimal(fields.vat_rate_percent, "vat_rate_percent")
    warnings.extend(subtotal_warnings + vat_warnings + total_warnings + rate_warnings)
    if rate is not None:
        rate = quantize(rate, RATE_QUANTUM)

    evidence, evidence_warnings = build_field_evidence(
        fields.field_evidence, page_count=page_count
    )
    warnings.extend(evidence_warnings)

    values: dict[str, object | None] = {
        "invoice_number": clean_text(fields.invoice_number),
        "invoice_date": invoice_date,
        "supplier_name": clean_text(fields.supplier_name),
        "supplier_trn": clean_text(fields.supplier_trn),
        "customer_name": clean_text(fields.customer_name),
        "customer_trn": clean_text(fields.customer_trn),
        "currency": clean_text(fields.currency),
        "subtotal_excluding_vat": subtotal,
        "vat_amount": vat_amount,
        "total_including_vat": total,
        "vat_rate_percent": rate,
        "vat_classification": classification,
    }

    for name, entry in evidence.items():
        value = values.get(name)
        entry.value = None if value is None else str(value)

    missing_fields = [name for name in INVOICE_FIELD_NAMES if values.get(name) is None]
    if missing_fields:
        warnings.append(
            "Missing or unreadable fields were left null, not guessed: "
            + ", ".join(missing_fields)
            + "."
        )

    cleaned_warnings = [
        truncate(" ".join(str(item).split()), MAX_WARNING_CHARACTERS) for item in warnings
    ]
    cleaned_warnings = [item for item in cleaned_warnings if item]
    if len(cleaned_warnings) > MAX_WARNINGS:
        dropped = len(cleaned_warnings) - MAX_WARNINGS
        cleaned_warnings = cleaned_warnings[:MAX_WARNINGS] + [
            f"{dropped} further extraction warning(s) were dropped to keep the response small."
        ]

    return InvoiceData(
        invoice_number=values["invoice_number"],
        invoice_date=values["invoice_date"],
        supplier_name=values["supplier_name"],
        supplier_trn=values["supplier_trn"],
        customer_name=values["customer_name"],
        customer_trn=values["customer_trn"],
        currency=values["currency"],
        subtotal_excluding_vat=subtotal,
        vat_amount=vat_amount,
        total_including_vat=total,
        vat_rate_percent=rate,
        vat_classification=classification,
        field_evidence=evidence,
        missing_fields=missing_fields,
        warnings=cleaned_warnings,
        provider=provider,
        model=raw_extraction.model,
    )


class InvoiceExtractionService:
    """Coordinate the stored document text, the LLM extractor and normalisation."""

    def __init__(
        self,
        extractor: LLMExtractor | None = None,
        *,
        provider: str | None = None,
    ) -> None:
        """Create the service.

        Args:
            extractor: Provider-agnostic LLM extractor; created on first use when
                omitted, and injected by tests so no network access is required.
            provider: Provider label recorded on the produced invoice data.
                Defaults to the configured ``LLM_PROVIDER_LABEL``.
        """

        self._extractor = extractor
        self._provider = provider

    def _resolve_extractor(self) -> LLMExtractor:
        if self._extractor is None:
            self._extractor = LLMExtractor()
        return self._extractor

    def extract(self, record: DocumentRecord) -> InvoiceData:
        """Extract structured invoice fields for a stored document.

        Raises:
            DocumentNotExtractableError: the document has no extractable text.
            LLMError: the provider failed, timed out or returned unusable data.
        """

        pages = [
            (page.page_number, page.text) for page in record.pages if page.text.strip()
        ]
        if record.extraction_status != "extracted" or not pages:
            raise DocumentNotExtractableError(OCR_BLOCKING_MESSAGE)

        extractor = self._resolve_extractor()
        raw_extraction = extractor.extract_invoice_fields(pages)
        return build_invoice_data(
            raw_extraction,
            provider=self._provider or extractor.provider_label,
            page_count=record.page_count,
        )


