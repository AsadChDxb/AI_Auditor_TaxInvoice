"""Pydantic schemas for structured invoice data (Phase 3).

Two layers are deliberate:

* :class:`RawInvoiceFields` is the *model-facing* schema. It only validates shape
  and basic types, and is deliberately permissive, because a formatting quirk from
  the provider must not fail an otherwise usable document.
* :class:`InvoiceData` is the *application-facing* schema. Monetary amounts are
  ``Decimal`` values, missing values are ``None`` (never guessed), and every
  interpretation that required a judgement call is recorded in ``warnings``.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

VatClassification = Literal["standard_rated", "zero_rated", "exempt", "unknown"]
"""UAE VAT treatment reported for the invoice.

``unknown`` is a first-class outcome: ambiguous or missing classification must be
surfaced for human review instead of being guessed.
"""

INVOICE_FIELD_NAMES: tuple[str, ...] = (
    "invoice_number",
    "invoice_date",
    "supplier_name",
    "supplier_trn",
    "customer_name",
    "customer_trn",
    "currency",
    "subtotal_excluding_vat",
    "vat_amount",
    "total_including_vat",
    "vat_rate_percent",
    "vat_classification",
)
"""Every extractable invoice field, in report order."""

MONETARY_FIELD_NAMES: tuple[str, ...] = (
    "subtotal_excluding_vat",
    "vat_amount",
    "total_including_vat",
)
"""Fields whose values are monetary amounts and are parsed into ``Decimal``."""

TEXT_FIELD_NAMES: tuple[str, ...] = (
    "invoice_number",
    "invoice_date",
    "supplier_name",
    "supplier_trn",
    "customer_name",
    "customer_trn",
    "currency",
    "vat_classification",
)
"""Fields that are reported as text (``invoice_date`` is normalised separately)."""


def _text_from_number(value: Any) -> Any:
    """Return numbers as clean text so ``"supplier_trn": 100123456700003`` survives.

    Providers occasionally emit identifiers as JSON numbers. Only bools are left
    untouched (they are rejected as text), and whole floats lose their meaningless
    ``.0`` suffix.
    """

    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return value


class RawFieldEvidence(BaseModel):
    """Model-reported provenance for a single field (lenient)."""

    model_config = ConfigDict(extra="ignore")

    confidence_percent: str | int | float | None = None
    page_number: int | None = None
    evidence: str | int | float | None = None

    @field_validator("evidence", mode="before")
    @classmethod
    def _evidence_as_text(cls, value: Any) -> Any:
        return _text_from_number(value)


class RawInvoiceFields(BaseModel):
    """Shape of the JSON object requested from the provider (lenient)."""

    model_config = ConfigDict(extra="ignore")

    invoice_number: str | None = None
    invoice_date: str | None = None
    supplier_name: str | None = None
    supplier_trn: str | None = None
    customer_name: str | None = None
    customer_trn: str | None = None
    currency: str | None = None
    subtotal_excluding_vat: str | int | float | None = None
    vat_amount: str | int | float | None = None
    total_including_vat: str | int | float | None = None
    vat_rate_percent: str | int | float | None = None
    vat_classification: str | None = None
    field_evidence: dict[str, RawFieldEvidence] = Field(default_factory=dict)
    extraction_warnings: list[str] = Field(default_factory=list)

    @field_validator(*TEXT_FIELD_NAMES, mode="before")
    @classmethod
    def _text_fields_as_text(cls, value: Any) -> Any:
        return _text_from_number(value)


class FieldEvidence(BaseModel):
    """Validated provenance for a single field."""

    value: str | None = Field(None, description="Reported value in text form.")
    confidence_percent: float | None = Field(
        None, ge=0, le=100, description="Model confidence for this field, 0-100."
    )
    page_number: int | None = Field(None, ge=1, description="1-based source page.")
    evidence: str | None = Field(None, description="Short quoted excerpt from the page.")


class InvoiceData(BaseModel):
    """Validated structured invoice fields stored against a document."""

    invoice_number: str | None = None
    invoice_date: str | None = None
    supplier_name: str | None = None
    supplier_trn: str | None = None
    customer_name: str | None = None
    customer_trn: str | None = None
    currency: str | None = None
    subtotal_excluding_vat: Decimal | None = None
    vat_amount: Decimal | None = None
    total_including_vat: Decimal | None = None
    vat_rate_percent: Decimal | None = None
    vat_classification: VatClassification = "unknown"
    field_evidence: dict[str, FieldEvidence] = Field(default_factory=dict)
    missing_fields: list[str] = Field(
        default_factory=list,
        description="Fields that could not be established and were left null.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Extraction warnings, including unparsed values and ambiguous readings.",
    )
    provider: str = Field(
        "openai-compatible",
        description="Configured provider label, for example deepseek or ollama-local.",
    )
    model: str | None = Field(None, description="Provider model identifier used.")
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_serializer(*MONETARY_FIELD_NAMES, "vat_rate_percent", when_used="json")
    def _serialise_decimal(self, value: Decimal | None) -> str | None:
        """Serialise amounts as strings so JSON cannot lose decimal precision."""

        return None if value is None else str(value)


class InvoiceExtractionResponse(BaseModel):
    """Response of ``POST /documents/{document_no}/extract``."""

    document_no: str
    reused_existing: bool = Field(
        False,
        description="True when a previous extraction was returned instead of calling the provider again.",
    )
    invoice: InvoiceData
    message: str | None = None

