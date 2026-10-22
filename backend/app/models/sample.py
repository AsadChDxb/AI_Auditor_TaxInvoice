"""Response schemas for the bundled sample invoices (testing aid).

The samples are the PDFs ``scripts/generate_sample_invoices.py`` writes into
``backend/sample_inv``. ``GET /samples`` describes them (never their page text) and
``GET /samples/{file_name}`` streams one, so the upload screen can offer a ready-made
invoice without keeping a second copy of the files in the frontend.
"""

from typing import Literal

from pydantic import BaseModel, Field

SampleExpectedOutcome = Literal["pass", "fail", "unknown"]
"""What a sample is built to prove, read from its file name.

``scripts/generate_sample_invoices.py`` names compliant files ``NN_valid_...`` and
defective ones ``NN_invalid_...``; anything else is ``unknown``. The value is a UI label
only - the real verdict always comes from the audit.
"""


class SampleInvoice(BaseModel):
    """One bundled sample invoice."""

    file_name: str = Field(
        ...,
        description=(
            "File name inside the samples folder; also the path parameter of "
            "``GET /samples/{file_name}``."
        ),
    )
    title: str = Field(
        ...,
        description="Human-readable title, from the PDF metadata or the file name.",
    )
    supplier_name: str | None = Field(
        None, description="Supplier taken from the PDF metadata, when it has one."
    )
    size_bytes: int = Field(..., ge=0, description="Size of the PDF in bytes.")
    expected_outcome: SampleExpectedOutcome = Field(
        "unknown",
        description=(
            "Whether the sample is built to pass or to fail the configured VAT rules."
        ),
    )
    url: str = Field(
        ...,
        description=(
            "Relative API path that streams the PDF, for example "
            "``/samples/01_valid_goods_gulf_star_GST-2026-0142.pdf``."
        ),
    )


class SampleInvoiceListResponse(BaseModel):
    """Response of ``GET /samples``."""

    total: int = Field(..., ge=0, description="Number of sample invoices available.")
    samples: list[SampleInvoice] = Field(
        default_factory=list, description="The samples, sorted by file name."
    )
    notes: list[str] = Field(
        default_factory=list,
        description=(
            "Explanatory notes, for example when the samples folder is missing on this "
            "deployment."
        ),
    )
