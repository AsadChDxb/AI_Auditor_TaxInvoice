"""Author five realistic UAE tax-invoice PDFs for manual testing.

Why this script exists
----------------------
``tests/pdf_factory.py`` builds tiny PDFs for unit tests, and the two files at
``backend/sample_invoice.pdf``/``scaned_inv.pdf`` are single documents. Demoing the UI,
checking LLM extraction quality and reviewing the VAT rules need a *set* of documents
that look like the real thing: a letterhead, a billed party, a ruled line-item grid and
a VAT summary. This script authors five such invoices.

Two of them are fully compliant and three carry a deliberate, documented defect, so the
audit outcome is known before the file is uploaded (see ``sample_inv/README.md``):

1. valid goods invoice                        -> every VAT rule passes
2. valid services invoice with line discounts  -> every VAT rule passes
3. VAT amount computed at the wrong rate      -> TAXABLE_AMOUNT_VAT_MISMATCH fails
4. supplier TRN missing                       -> SUPPLIER_TRN_CHECK fails
5. supplier TRN malformed + 10% VAT printed   -> VAT_RATE_CHECK and the VAT check fail

The PDFs are *text* PDFs (fonts, not scans), so ``pdfplumber`` reads the text and the
ruled tables without the OCR fallback. PyMuPDF is used for authoring only, exactly like
``tests/pdf_factory.py``; the application never imports it.

Run from ``backend``::

    .\\.venv\\Scripts\\python.exe scripts\\generate_sample_invoices.py
    .\\.venv\\Scripts\\python.exe scripts\\generate_sample_invoices.py --output-dir D:\\tmp\\invoices
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import fitz  # PyMuPDF: authoring only

OUTPUT_DIR = Path(__file__).resolve().parents[1] / "sample_inv"
"""Default destination: ``backend/sample_inv`` (created when missing)."""

PAGE = fitz.paper_rect("a4")
"""A4 portrait in PDF points (595.28 x 841.89)."""

MARGIN = 40.0
CONTENT_WIDTH = PAGE.width - 2 * MARGIN
"""Usable width between the left and right margins (515.28 pt)."""

CENT = Decimal("0.01")

# Palette (RGB floats in 0-1). The brand tone is per supplier, the rest is shared so the
# five documents look like one design system with five letterheads.
INK = (0.12, 0.14, 0.18)
MUTED = (0.42, 0.46, 0.52)
RULE = (0.76, 0.79, 0.83)
WHITE = (1.0, 1.0, 1.0)
ROW_ALT = (0.965, 0.975, 0.985)
TINT = (0.905, 0.940, 0.955)

FONT_REGULAR = "helv"
FONT_BOLD = "hebo"

TABLE_COLUMNS: tuple[tuple[str, str, float, str], ...] = (
    ("number", "#", 24.0, "center"),
    ("description", "DESCRIPTION", 170.0, "left"),
    ("quantity", "QTY", 32.0, "center"),
    ("unit_price", "UNIT PRICE", 58.0, "right"),
    ("discount", "DISC.", 40.0, "right"),
    ("taxable", "TAXABLE", 62.0, "right"),
    ("vat_rate", "VAT %", 34.0, "center"),
    ("vat", "VAT", 44.0, "right"),
    ("line_total", "TOTAL", 51.0, "right"),
)
"""Line-item grid: key, header label, width in points, alignment.

The widths add up to ``CONTENT_WIDTH``; every cell is drawn as a stroked rectangle,
which is what ``pdfplumber`` needs to rebuild the table (the same trick
``tests/pdf_factory.py`` uses for its fixture).
"""

ROW_FONT_SIZE = 8.2
HEADER_FONT_SIZE = 7.4
MIN_ROW_HEIGHT = 22.0


def round_money(value: Decimal) -> Decimal:
    """Round an amount to two decimals with the invoice rounding policy."""

    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def money(value: Decimal) -> str:
    """Format an amount the way an invoice prints it (``12,850.00``)."""

    return f"{value:,.2f}"


def percent(value: Decimal) -> str:
    """Format a rate without trailing zeros (``5%``, ``7.5%``)."""

    return f"{value.normalize():f}%"


def quantity(value: Decimal) -> str:
    """Format a quantity without trailing zeros (``6``, ``2.5``)."""

    return f"{value.normalize():f}"


ONES = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
TENS = (
    "",
    "",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
)
SCALES = ((1_000_000_000, "billion"), (1_000_000, "million"), (1_000, "thousand"), (1, ""))


def _below_hundred(value: int) -> str:
    """Spell a number below 100."""

    if value < 20:
        return ONES[value]
    tens, ones = divmod(value, 10)
    return TENS[tens] + (f"-{ONES[ones]}" if ones else "")


def _below_thousand(value: int) -> str:
    """Spell a number below 1000."""

    hundreds, rest = divmod(value, 100)
    parts = [f"{ONES[hundreds]} hundred"] if hundreds else []
    if rest:
        parts.append(_below_hundred(rest))
    return " ".join(parts)


def _integer_words(value: int) -> str:
    """Spell a non-negative integer (enough for invoice amounts)."""

    if value == 0:
        return "zero"
    parts: list[str] = []
    remainder = value
    for scale_value, scale_name in SCALES:
        chunk, remainder = divmod(remainder, scale_value)
        if chunk:
            parts.append(f"{_below_thousand(chunk)} {scale_name}".strip())
    return " ".join(parts)


def _titled(text: str) -> str:
    """Title-case the number words, keeping the conjunction lowercase."""

    return " ".join(
        part if part == "and" else part[:1].upper() + part[1:] for part in text.split()
    )


def amount_in_words(
    value: Decimal, *, currency: str = "UAE Dirhams", subunit: str = "Fils"
) -> str:
    """Render an amount as words, the way a tax invoice prints it.

    ``13492.50`` -> ``UAE Dirhams Thirteen Thousand Four Hundred Ninety-Two and Fils
    Fifty Only``.
    """

    whole = int(value)
    fraction = int(round_money(value - whole) * 100)
    words = f"{currency} {_titled(_integer_words(whole))}"
    if fraction:
        words += f" and {subunit} {_titled(_integer_words(fraction))}"
    return f"{words} Only"


def text_width(value: str, size: float, bold: bool = False) -> float:
    """Width of a string in points for the font it will be drawn with."""

    return fitz.get_text_length(
        value, fontname=FONT_BOLD if bold else FONT_REGULAR, fontsize=size
    )


def wrap(value: str, width: float, size: float, bold: bool = False) -> list[str]:
    """Wrap a string into lines no wider than `width` points."""

    lines: list[str] = []
    current = ""
    for word in value.split():
        candidate = f"{current} {word}".strip()
        if current and text_width(candidate, size, bold) > width:
            lines.append(current)
            current = word
            continue
        current = candidate
    if current:
        lines.append(current)
    return lines


def draw_text(
    page: fitz.Page,
    x: float,
    y: float,
    value: str,
    *,
    size: float = 9.0,
    bold: bool = False,
    color: tuple[float, float, float] = INK,
    align: str = "left",
) -> None:
    """Draw one line of text; ``align='right'`` treats ``x`` as the right edge."""

    width = text_width(value, size, bold)
    if align == "right":
        x -= width
    elif align == "center":
        x -= width / 2
    page.insert_text(
        (x, y),
        value,
        fontname=FONT_BOLD if bold else FONT_REGULAR,
        fontsize=size,
        color=color,
    )


def draw_box(
    page: fitz.Page,
    rect: fitz.Rect,
    *,
    fill: tuple[float, float, float] | None = None,
    color: tuple[float, float, float] | None = RULE,
    width: float = 0.6,
) -> None:
    """Draw a rectangle that is either filled, stroked, or both."""

    page.draw_rect(rect, color=color, fill=fill, width=width)


HUNDRED = Decimal("100")


@dataclass(frozen=True)
class Party:
    """A supplier or customer block: name, address, TRN and contact details."""

    name: str
    address_lines: tuple[str, ...]
    trn: str | None
    tagline: str | None = None
    contact: str | None = None
    phone: str | None = None
    email: str | None = None
    website: str | None = None

    @property
    def address(self) -> str:
        """The address as one printable line."""

        return ", ".join(self.address_lines)

    @property
    def trn_display(self) -> str:
        """The TRN as printed, with an explicit fallback for an unregistered party."""

        return self.trn if self.trn else "Not Registered"

    @property
    def contact_line(self) -> str:
        """``Attn: ... | +971 ...`` for the customer card, or an empty string."""

        parts = [part for part in (self.contact, self.phone) if part]
        return " | ".join(parts)


@dataclass(frozen=True)
class LineItem:
    """One invoice line: quantity x unit price, with an optional line discount."""

    description: str
    quantity: Decimal
    unit_price: Decimal
    discount_percent: Decimal = Decimal("0")

    @property
    def gross(self) -> Decimal:
        """Line amount before the line discount."""

        return round_money(self.quantity * self.unit_price)

    @property
    def discount(self) -> Decimal:
        """Line discount amount."""

        return round_money(self.gross * self.discount_percent / HUNDRED)

    @property
    def taxable(self) -> Decimal:
        """Line amount that VAT is charged on."""

        return round_money(self.gross - self.discount)

    def vat(self, rate_percent: Decimal) -> Decimal:
        """VAT for this line at the printed rate."""

        return round_money(self.taxable * rate_percent / HUNDRED)

    def total(self, rate_percent: Decimal) -> Decimal:
        """Line total including VAT."""

        return round_money(self.taxable + self.vat(rate_percent))


@dataclass(frozen=True)
class InvoiceSpec:
    """Everything needed to draw one invoice, plus the audit outcome it is meant to have.

    The printed VAT and total are optional overrides: leaving them out prints the
    arithmetically correct values, while setting them reproduces the defect a sample is
    supposed to carry (a wrong VAT amount, or a total that does not reconcile).
    """

    slug: str
    file_name: str
    number: str
    issue_date: str
    due_date: str
    po_number: str
    payment_terms: str
    currency: str
    brand: tuple[float, float, float]
    accent: tuple[float, float, float]
    supplier: Party
    customer: Party
    items: tuple[LineItem, ...]
    vat_rate_percent: Decimal
    bank: tuple[tuple[str, str], ...]
    notes: tuple[str, ...]
    expected_audit: str
    defects: tuple[str, ...] = ()
    vat_amount: Decimal | None = None
    total_amount: Decimal | None = None
    service_note: str | None = None

    @property
    def gross_subtotal(self) -> Decimal:
        """Sum of the line amounts before discounts."""

        return round_money(sum((item.gross for item in self.items), Decimal("0")))

    @property
    def discount_total(self) -> Decimal:
        """Sum of the line discounts."""

        return round_money(sum((item.discount for item in self.items), Decimal("0")))

    @property
    def taxable_subtotal(self) -> Decimal:
        """Sum of the taxable line amounts, i.e. the invoice subtotal excluding VAT."""

        return round_money(sum((item.taxable for item in self.items), Decimal("0")))

    @property
    def has_discounts(self) -> bool:
        """True when at least one line carries a discount."""

        return self.discount_total > 0

    @property
    def printed_vat(self) -> Decimal:
        """The VAT amount printed in the summary (correct unless overridden)."""

        if self.vat_amount is not None:
            return self.vat_amount
        return round_money(self.taxable_subtotal * self.vat_rate_percent / HUNDRED)

    @property
    def printed_total(self) -> Decimal:
        """The grand total printed in the summary (correct unless overridden)."""

        if self.total_amount is not None:
            return self.total_amount
        return round_money(self.taxable_subtotal + self.printed_vat)

    @property
    def reference_line(self) -> str:
        """Second footer line: document identity without any customer data."""

        return f"{self.number} | {self.issue_date} | {self.currency}"


def draw_letterhead(page: fitz.Page, spec: InvoiceSpec) -> float:
    """Draw the brand band with the supplier letterhead; returns the body's start y."""

    band_height = 104.0
    draw_box(
        page,
        fitz.Rect(0, 0, PAGE.width, band_height),
        fill=spec.brand,
        color=spec.brand,
        width=0,
    )
    draw_box(
        page,
        fitz.Rect(0, band_height, PAGE.width, band_height + 3),
        fill=spec.accent,
        color=spec.accent,
        width=0,
    )

    draw_text(page, MARGIN, 44, spec.supplier.name, size=18, bold=True, color=WHITE)
    if spec.supplier.tagline:
        draw_text(page, MARGIN, 60, spec.supplier.tagline, size=8.4, color=(0.82, 0.88, 0.91))
    address_lines = wrap(spec.supplier.address, 320, 8.2)
    for offset, line in enumerate(address_lines):
        draw_text(page, MARGIN, 73 + offset * 10.2, line, size=8.2, color=(0.88, 0.92, 0.94))
    contact = " | ".join(
        part
        for part in (
            f"Tel {spec.supplier.phone}" if spec.supplier.phone else None,
            spec.supplier.email,
            spec.supplier.website,
        )
        if part
    )
    if contact:
        draw_text(page, MARGIN, 96, contact, size=8.2, color=(0.88, 0.92, 0.94))

    draw_text(
        page, PAGE.width - MARGIN, 46, "TAX INVOICE", size=19, bold=True, color=WHITE, align="right"
    )
    draw_text(
        page,
        PAGE.width - MARGIN,
        62,
        "VAT Invoice - Original",
        size=8.4,
        color=(0.88, 0.92, 0.94),
        align="right",
    )
    draw_text(
        page,
        PAGE.width - MARGIN,
        78,
        f"Currency: {spec.currency}",
        size=8.4,
        color=(0.88, 0.92, 0.94),
        align="right",
    )
    return band_height + 3


def draw_supplier_and_meta(page: fitz.Page, spec: InvoiceSpec, top: float) -> float:
    """Supplier address/TRN on the left and the invoice meta card on the right."""

    card_width = 232.0
    card_height = 96.0
    card_x = PAGE.width - MARGIN - card_width

    draw_text(page, MARGIN, top + 9, "SUPPLIER", size=7.2, bold=True, color=MUTED)
    draw_text(page, MARGIN, top + 24, spec.supplier.name, size=10.2, bold=True)
    y = top + 36
    for line in wrap(spec.supplier.address, 250, 8.3):
        draw_text(page, MARGIN, y, line, size=8.3, color=MUTED)
        y += 10.2
    draw_text(page, MARGIN, y + 4, f"TRN: {spec.supplier.trn_display}", size=8.8, bold=True)
    supplier_contact = " | ".join(
        part for part in (spec.supplier.phone, spec.supplier.email) if part
    )
    if supplier_contact:
        draw_text(page, MARGIN, y + 16, supplier_contact, size=8.2, color=MUTED)

    draw_box(
        page, fitz.Rect(card_x, top, card_x + card_width, top + card_height), fill=TINT, color=RULE
    )
    draw_box(
        page,
        fitz.Rect(card_x, top, card_x + card_width, top + 3),
        fill=spec.brand,
        color=spec.brand,
        width=0,
    )
    rows = (
        ("Invoice No.", spec.number),
        ("Invoice Date", spec.issue_date),
        ("Due Date", spec.due_date),
        ("PO Number", spec.po_number),
        ("Payment Terms", spec.payment_terms),
        ("Currency", spec.currency),
    )
    row_y = top + 15
    for label, value in rows:
        draw_text(page, card_x + 10, row_y, label, size=7.6, color=MUTED)
        draw_text(page, card_x + card_width - 10, row_y, value, size=8.4, bold=True, align="right")
        row_y += 13.0
    return top + card_height


def draw_bill_to(page: fitz.Page, spec: InvoiceSpec, top: float) -> float:
    """Draw the customer card; returns the y below it."""

    height = 78.0
    draw_box(
        page,
        fitz.Rect(MARGIN, top, MARGIN + CONTENT_WIDTH, top + height),
        fill=WHITE,
        color=RULE,
    )
    draw_box(
        page,
        fitz.Rect(MARGIN, top, MARGIN + 4, top + height),
        fill=spec.brand,
        color=spec.brand,
        width=0,
    )
    draw_text(page, MARGIN + 14, top + 14, "BILL TO", size=7.2, bold=True, color=MUTED)
    draw_text(page, MARGIN + 14, top + 29, spec.customer.name, size=10.2, bold=True)
    y = top + 41
    for line in wrap(spec.customer.address, 300, 8.3):
        draw_text(page, MARGIN + 14, y, line, size=8.3, color=MUTED)
        y += 10.2
    draw_text(
        page, MARGIN + 14, y + 4, f"TRN: {spec.customer.trn_display}", size=8.6, bold=True
    )
    if spec.customer.contact_line:
        draw_text(
            page,
            MARGIN + CONTENT_WIDTH - 14,
            top + 29,
            spec.customer.contact_line,
            size=8.2,
            color=MUTED,
            align="right",
        )
    return top + height


def draw_items_table(page: fitz.Page, spec: InvoiceSpec, top: float) -> float:
    """Draw the ruled line-item grid; returns the y below the last row."""

    header_height = 24.0
    x = MARGIN
    for _key, label, width, align in TABLE_COLUMNS:
        draw_box(
            page,
            fitz.Rect(x, top, x + width, top + header_height),
            fill=spec.brand,
            color=spec.brand,
            width=0.5,
        )
        if align == "right":
            draw_text(
                page,
                x + width - 6,
                top + 15.6,
                label,
                size=HEADER_FONT_SIZE,
                bold=True,
                color=WHITE,
                align="right",
            )
        elif align == "center":
            draw_text(
                page,
                x + width / 2,
                top + 15.6,
                label,
                size=HEADER_FONT_SIZE,
                bold=True,
                color=WHITE,
                align="center",
            )
        else:
            draw_text(
                page, x + 6, top + 15.6, label, size=HEADER_FONT_SIZE, bold=True, color=WHITE
            )
        x += width

    y = top + header_height
    rate = spec.vat_rate_percent
    for index, item in enumerate(spec.items, start=1):
        description_lines = wrap(item.description, TABLE_COLUMNS[1][2] - 12, ROW_FONT_SIZE)
        row_height = max(MIN_ROW_HEIGHT, 11.4 * len(description_lines) + 8.0)
        baseline = y + row_height / 2 + 2.9
        cells: dict[str, str | list[str]] = {
            "number": str(index),
            "description": description_lines,
            "quantity": quantity(item.quantity),
            "unit_price": money(item.unit_price),
            "discount": percent(item.discount_percent) if item.discount_percent else "-",
            "taxable": money(item.taxable),
            "vat_rate": percent(rate),
            "vat": money(item.vat(rate)),
            "line_total": money(item.total(rate)),
        }
        x = MARGIN
        for key, _label, width, align in TABLE_COLUMNS:
            draw_box(
                page,
                fitz.Rect(x, y, x + width, y + row_height),
                fill=ROW_ALT if index % 2 == 0 else WHITE,
                color=RULE,
                width=0.5,
            )
            value = cells[key]
            if isinstance(value, list):
                if len(value) == 1:
                    draw_text(page, x + 6, baseline, value[0], size=ROW_FONT_SIZE)
                else:
                    for offset, line in enumerate(value):
                        draw_text(page, x + 6, y + 12 + offset * 10.4, line, size=ROW_FONT_SIZE)
            elif align == "right":
                draw_text(page, x + width - 6, baseline, value, size=ROW_FONT_SIZE, align="right")
            elif align == "center":
                draw_text(page, x + width / 2, baseline, value, size=ROW_FONT_SIZE, align="center")
            else:
                draw_text(page, x + 6, baseline, value, size=ROW_FONT_SIZE)
            x += width
        y += row_height
    return y


def draw_summary(page: fitz.Page, spec: InvoiceSpec, top: float) -> float:
    """Draw the remarks block and the VAT summary; returns the y below the grand total."""

    width = 246.0
    x = MARGIN + CONTENT_WIDTH - width
    if spec.service_note:
        draw_text(page, MARGIN, top + 10, "REMARKS", size=7.2, bold=True, color=MUTED)
        for offset, line in enumerate(wrap(spec.service_note, CONTENT_WIDTH - width - 20, 8.3)):
            draw_text(page, MARGIN, top + 24 + offset * 10.4, line, size=8.3)

    rows: list[tuple[str, str]] = []
    if spec.has_discounts:
        rows.append(("Gross Amount", money(spec.gross_subtotal)))
        rows.append(("Less: Line Discounts", f"-{money(spec.discount_total)}"))
    rows.append(("Taxable Amount (Excl. VAT)", money(spec.taxable_subtotal)))
    rows.append((f"VAT ({percent(spec.vat_rate_percent)})", money(spec.printed_vat)))

    row_height = 17.0
    y = top
    for label, value in rows:
        draw_box(page, fitz.Rect(x, y, x + width, y + row_height), fill=WHITE, color=RULE, width=0.5)
        draw_text(page, x + 8, y + 11.6, label, size=8.4, color=MUTED)
        draw_text(page, x + width - 8, y + 11.6, value, size=8.8, bold=True, align="right")
        y += row_height

    total_height = 26.0
    draw_box(
        page,
        fitz.Rect(x, y, x + width, y + total_height),
        fill=spec.brand,
        color=spec.brand,
        width=0.5,
    )
    draw_text(
        page,
        x + 8,
        y + 17,
        f"Total Amount ({spec.currency})",
        size=9.0,
        bold=True,
        color=WHITE,
    )
    draw_text(
        page,
        x + width - 8,
        y + 17.6,
        money(spec.printed_total),
        size=11.4,
        bold=True,
        color=WHITE,
        align="right",
    )
    return y + total_height


def draw_amount_in_words(page: fitz.Page, spec: InvoiceSpec, top: float) -> float:
    """Draw the amount-in-words strip; returns the y below it."""

    words = amount_in_words(spec.printed_total)
    lines = wrap(words, CONTENT_WIDTH - 130, 8.4)
    height = 22.0 + 10.4 * len(lines)
    draw_box(
        page,
        fitz.Rect(MARGIN, top, MARGIN + CONTENT_WIDTH, top + height),
        fill=TINT,
        color=RULE,
    )
    draw_text(page, MARGIN + 12, top + 13, "AMOUNT IN WORDS", size=7.2, bold=True, color=MUTED)
    for offset, line in enumerate(lines):
        draw_text(page, MARGIN + 12, top + 26 + offset * 10.4, line, size=8.4)
    return top + height


def draw_payment_and_terms(page: fitz.Page, spec: InvoiceSpec, top: float) -> None:
    """Two reference columns: bank details on the left, terms and notes on the right."""

    column_width = 246.0
    right_x = MARGIN + CONTENT_WIDTH - column_width

    draw_text(page, MARGIN, top, "PAYMENT DETAILS", size=7.2, bold=True, color=MUTED)
    page.draw_line(
        fitz.Point(MARGIN, top + 4.5),
        fitz.Point(MARGIN + column_width, top + 4.5),
        color=spec.brand,
        width=0.8,
    )
    y = top + 18
    for label, value in spec.bank:
        draw_text(page, MARGIN, y, label, size=8.0, color=MUTED)
        draw_text(page, MARGIN + 92, y, value, size=8.0, bold=True)
        y += 12.4

    draw_text(page, right_x, top, "TERMS & CONDITIONS", size=7.2, bold=True, color=MUTED)
    page.draw_line(
        fitz.Point(right_x, top + 4.5),
        fitz.Point(right_x + column_width, top + 4.5),
        color=spec.brand,
        width=0.8,
    )
    y = top + 18
    for note in spec.notes:
        for offset, line in enumerate(wrap(note, column_width - 10, 7.8)):
            draw_text(page, right_x, y, f"- {line}" if offset == 0 else f"  {line}", size=7.8)
            y += 10.2
        y += 2


def draw_signature(page: fitz.Page, spec: InvoiceSpec, top: float) -> None:
    """Supplier signature block plus the customer's goods-received acknowledgement."""

    width = 190.0
    x = MARGIN + CONTENT_WIDTH - width
    draw_text(page, x, top, f"For {spec.supplier.name}", size=8.4, bold=True)
    page.draw_line(
        fitz.Point(x, top + 34), fitz.Point(x + width, top + 34), color=RULE, width=0.8
    )
    draw_text(page, x, top + 46, "Authorised Signatory", size=7.6, color=MUTED)

    acknowledgement_width = 230.0
    draw_text(page, MARGIN, top, "Received in good order by (Customer)", size=8.4, bold=True)
    page.draw_line(
        fitz.Point(MARGIN, top + 34),
        fitz.Point(MARGIN + acknowledgement_width, top + 34),
        color=RULE,
        width=0.8,
    )
    draw_text(page, MARGIN, top + 46, "Name, Signature and Date", size=7.6, color=MUTED)


def draw_footer(page: fitz.Page, spec: InvoiceSpec) -> None:
    """Legal footer with the document identity and the page number."""

    top = PAGE.height - 46
    page.draw_line(
        fitz.Point(MARGIN, top), fitz.Point(PAGE.width - MARGIN, top), color=RULE, width=0.6
    )
    draw_text(
        page,
        MARGIN,
        top + 12,
        "This is a computer-generated Tax Invoice issued under UAE VAT Law.",
        size=7.2,
        color=MUTED,
    )
    draw_text(
        page,
        PAGE.width - MARGIN,
        top + 12,
        f"{spec.reference_line} | Page 1 of 1",
        size=7.2,
        color=MUTED,
        align="right",
    )


def build_invoice_pdf(spec: InvoiceSpec) -> bytes:
    """Render one invoice and return the PDF bytes."""

    document = fitz.open()
    try:
        page = document.new_page(width=PAGE.width, height=PAGE.height)
        top = draw_letterhead(page, spec)
        top = draw_supplier_and_meta(page, spec, top + 14)
        top = draw_bill_to(page, spec, top + 12)
        top = draw_items_table(page, spec, top + 12)
        top = draw_summary(page, spec, top + 12)
        top = draw_amount_in_words(page, spec, top + 10)
        draw_payment_and_terms(page, spec, top + 24)
        draw_signature(page, spec, top + 116)
        draw_footer(page, spec)
        document.set_metadata(
            {
                "title": f"Tax Invoice {spec.number} - {spec.supplier.name}",
                "author": spec.supplier.name,
                "subject": "UAE VAT tax invoice (sample document)",
                "keywords": f"{spec.number}, {spec.customer.name}, VAT invoice, UAE",
                "creator": "SuperAuditor sample invoice generator",
                "producer": "PyMuPDF",
            }
        )
        return document.tobytes(deflate=True, garbage=3)
    finally:
        document.close()


D = Decimal
"""Short alias so the sample data below stays readable."""

GULF_STAR_TRADING = InvoiceSpec(
    slug="valid-goods-standard-rated",
    file_name="01_valid_goods_gulf_star_GST-2026-0142.pdf",
    number="GST-2026-0142",
    issue_date="12/09/2026",
    due_date="12/10/2026",
    po_number="ANL-PO-8842",
    payment_terms="Net 30 days",
    currency="AED",
    brand=(0.055, 0.243, 0.322),
    accent=(0.83, 0.53, 0.13),
    supplier=Party(
        name="Gulf Star Trading LLC",
        tagline="Electronics, Home Appliances and Commercial Fit-Out",
        address_lines=(
            "Office 1204, Bay Square Building 12",
            "Business Bay, Dubai, U.A.E.",
        ),
        trn="100234567800003",
        phone="+971 4 555 0192",
        email="accounts@gulfstartrading.ae",
        website="www.gulfstartrading.ae",
    ),
    customer=Party(
        name="Al Noor Contracting LLC",
        address_lines=("Warehouse 7, Al Quoz Industrial Area 3", "Dubai, U.A.E."),
        trn="100987654300003",
        contact="Attn: Mr. Faisal Rahman",
        phone="+971 50 447 8821",
    ),
    items=(
        LineItem("Samsung 55 inch 4K Smart TV - Model UA55DU7000", D("6"), D("1450.00")),
        LineItem("Wall Mount Bracket - Full Motion, 32 to 65 inch", D("6"), D("125.00")),
        LineItem("Extended Warranty - 24 Months", D("6"), D("150.00")),
        LineItem("Installation and Configuration Service", D("1"), D("2500.00")),
    ),
    vat_rate_percent=D("5"),
    bank=(
        ("Bank", "Emirates NBD"),
        ("Account Name", "Gulf Star Trading LLC"),
        ("Account Number", "1012345678901"),
        ("IBAN", "AE070331234567890123456"),
        ("SWIFT", "EBILAEAD"),
    ),
    notes=(
        "Payment due within 30 days of the invoice date.",
        "Please quote the invoice number on the transfer reference.",
        "Goods remain the property of the supplier until paid in full.",
    ),
    service_note=(
        "Delivery: 14/09/2026 to the Al Quoz warehouse. Warranty: 24 months on "
        "electronics and 12 months on installation work."
    ),
    expected_audit="PASS - rate, VAT amount, total and 15-digit supplier TRN are correct.",
)

SHINY_HOME_CLEANING = InvoiceSpec(
    slug="valid-services-line-discount",
    file_name="02_valid_services_shiny_home_SH02739.pdf",
    number="SH02739",
    issue_date="30/09/2026",
    due_date="30/10/2026",
    po_number="CAR-FM-2026-77",
    payment_terms="Net 30 days",
    currency="AED",
    brand=(0.06, 0.35, 0.28),
    accent=(0.85, 0.63, 0.15),
    supplier=Party(
        name="Shiny Home Cleaning Services",
        tagline="Commercial Cleaning and Facilities Support",
        address_lines=(
            "Office 207, Hilal Salim Bin Tarraf Building",
            "Sheikh Zayed Road, Business Bay, Dubai, U.A.E.",
        ),
        trn="100451361800003",
        phone="+971 4 328 7710",
        email="billing@shinyhome.ae",
        website="www.shinyhome.ae",
    ),
    customer=Party(
        name="The Carlisle Contracting LLC",
        address_lines=("501, Control Tower, Motor City", "Dubai, U.A.E."),
        trn="100492373400003",
        contact="Attn: Ms. Priya Nair",
        phone="+971 55 210 7734",
    ),
    items=(
        LineItem("Office Cleaning Service - Daily (September 2026)", D("1"), D("4200.00"), D("5")),
        LineItem("Deep Cleaning - Common Areas and Washrooms", D("1"), D("950.00")),
        LineItem("Consumables and Cleaning Supplies", D("1"), D("380.00")),
        LineItem("External Window Cleaning - Tower A", D("2"), D("260.00"), D("10")),
    ),
    vat_rate_percent=D("5"),
    bank=(
        ("Bank", "Mashreq Bank"),
        ("Account Name", "Shiny Home Cleaning Services"),
        ("Account Number", "0198765432107"),
        ("IBAN", "AE440330000019876543210"),
        ("SWIFT", "BOMLAEAD"),
    ),
    notes=(
        "Payment due within 30 days of the invoice date.",
        "Services were delivered from 01/09/2026 to 30/09/2026.",
        "Line discounts are already deducted from the taxable amount.",
    ),
    service_note=(
        "Service period: 01/09/2026 to 30/09/2026. Site supervisor: Mr. Ravi Kumar, "
        "+971 55 903 4412."
    ),
    expected_audit=(
        "PASS - the line discounts are already netted, so taxable amount + VAT equals "
        "the total and every rule passes."
    ),
)

AL_MADINA_AUTO_PARTS = InvoiceSpec(
    slug="invalid-vat-amount",
    file_name="03_invalid_vat_amount_al_madina_AMD-2026-5510.pdf",
    number="AMD-2026-5510",
    issue_date="18/08/2026",
    due_date="17/09/2026",
    po_number="DRL-4471",
    payment_terms="Net 30 days",
    currency="AED",
    brand=(0.42, 0.10, 0.13),
    accent=(0.78, 0.45, 0.12),
    supplier=Party(
        name="Al Madina Auto Spare Parts LLC",
        tagline="Genuine Spare Parts and Workshop Supplies",
        address_lines=("Shop 14, Al Sajaa Industrial Area", "Sharjah, U.A.E."),
        trn="100876543200003",
        phone="+971 6 535 4420",
        email="sales@almadina-parts.ae",
        website="www.almadina-parts.ae",
    ),
    customer=Party(
        name="Desert Rose Logistics LLC",
        address_lines=("Plot 22, Dubai Investment Park 2", "Dubai, U.A.E."),
        trn="100112233400003",
        contact="Attn: Mr. Imran Qureshi",
        phone="+971 52 664 1180",
    ),
    items=(
        LineItem("Brake Pad Set - Front (Toyota Hilux 2016-2020)", D("20"), D("185.00")),
        LineItem("Oil Filter - Heavy Duty Diesel", D("40"), D("42.50")),
        LineItem("Engine Oil 5W-30 - 20 Litre Drum", D("10"), D("310.00")),
        LineItem("Labour - Vehicle Servicing (per hour)", D("15"), D("120.00")),
        LineItem("Air Filter - Cabin", D("25"), D("44.00")),
    ),
    vat_rate_percent=D("5"),
    vat_amount=D("1026.00"),
    total_amount=D("12426.00"),
    bank=(
        ("Bank", "Sharjah Islamic Bank"),
        ("Account Name", "Al Madina Auto Spare Parts LLC"),
        ("Account Number", "0021456789012"),
        ("IBAN", "AE320040000214567890123"),
        ("SWIFT", "NBSHAEAS"),
    ),
    notes=(
        "Payment due within 30 days of the invoice date.",
        "Parts are supplied against the vehicle chassis number on the job card.",
        "Warranty claims require the original invoice.",
    ),
    service_note=(
        "Workshop job card: JC-2026-4471. Parts fitted on 18/08/2026 at the customer site."
    ),
    expected_audit=(
        "FAIL - VAT is printed as 1,026.00 (9%) instead of 570.00 (5%). The total is "
        "consistent with the wrong VAT, so INVOICE_TOTAL_CHECK passes while "
        "TAXABLE_AMOUNT_VAT_MISMATCH fails."
    ),
    defects=("VAT amount computed at 9% while the invoice prints a 5% rate",),
)

BRIGHT_FUTURE_TECHNICAL = InvoiceSpec(
    slug="invalid-missing-supplier-trn",
    file_name="04_invalid_missing_trn_bright_future_BFT-2026-0093.pdf",
    number="BFT-2026-0093",
    issue_date="05/09/2026",
    due_date="05/10/2026",
    po_number="MH-FM-3390",
    payment_terms="Net 30 days",
    currency="AED",
    brand=(0.09, 0.22, 0.45),
    accent=(0.25, 0.62, 0.75),
    supplier=Party(
        name="Bright Future Technical Services",
        tagline="Air Conditioning, MEP and General Maintenance",
        address_lines=(
            "Office 6, Al Barsha 1, Behind Mall of the Emirates",
            "Dubai, U.A.E.",
        ),
        trn=None,
        phone="+971 4 341 9087",
        email="info@brightfuture-ts.ae",
        website="www.brightfuture-ts.ae",
    ),
    customer=Party(
        name="Marina Heights Facilities Management LLC",
        address_lines=("Marina Heights Tower, Dubai Marina", "Dubai, U.A.E."),
        trn="100765432100003",
        contact="Attn: Mr. Sanjay Menon",
        phone="+971 56 771 2244",
    ),
    items=(
        LineItem("AC Duct Cleaning - Tower A (per apartment)", D("12"), D("275.00")),
        LineItem("Split AC General Service and Gas Top-Up", D("8"), D("165.00")),
        LineItem("Replacement Filters and Coils", D("1"), D("890.00")),
    ),
    vat_rate_percent=D("5"),
    bank=(
        ("Bank", "Abu Dhabi Commercial Bank"),
        ("Account Name", "Bright Future Technical Services"),
        ("Account Number", "1155667799001"),
        ("IBAN", "AE070030011556677990012"),
        ("SWIFT", "ADCBAEAA"),
    ),
    notes=(
        "Payment due within 30 days of the invoice date.",
        "Service report SR-2026-1188 is attached to the works order.",
        "VAT is charged at the standard rate of 5%.",
    ),
    service_note=(
        "Work completed on 04/09/2026 and signed off by the building supervisor."
    ),
    expected_audit=(
        "FAIL - no supplier TRN appears on the invoice ('Not Registered'), so "
        "SUPPLIER_TRN_CHECK fails even though the arithmetic is correct."
    ),
    defects=("Supplier TRN missing",),
)

HORIZON_DIGITAL = InvoiceSpec(
    slug="invalid-rate-and-trn",
    file_name="05_invalid_rate_and_trn_horizon_digital_HDM-2026-1187.pdf",
    number="HDM-2026-1187",
    issue_date="08/09/2026",
    due_date="08/10/2026",
    po_number="PJH-MKT-2026-14",
    payment_terms="Net 30 days",
    currency="AED",
    brand=(0.24, 0.16, 0.45),
    accent=(0.62, 0.35, 0.75),
    supplier=Party(
        name="Horizon Digital Marketing FZE",
        tagline="Performance Marketing, Content and Media Buying",
        address_lines=("Techno Hub 1, Dubai Silicon Oasis", "Dubai, U.A.E."),
        trn="10045678",
        phone="+971 4 701 2265",
        email="billing@horizondigital.ae",
        website="www.horizondigital.ae",
    ),
    customer=Party(
        name="Palm Jumeirah Hospitality LLC",
        address_lines=("Palm Tower, Palm Jumeirah", "Dubai, U.A.E."),
        trn="100998877600003",
        contact="Attn: Ms. Layla Al Suwaidi",
        phone="+971 4 244 8890",
    ),
    items=(
        LineItem("Social Media Management - October 2026", D("1"), D("4500.00")),
        LineItem("Content Production - Short Form Video (per unit)", D("12"), D("350.00")),
        LineItem("Paid Ads Management Fee - 10% of Media Spend", D("1"), D("1200.00")),
    ),
    vat_rate_percent=D("10"),
    bank=(
        ("Bank", "Dubai Islamic Bank"),
        ("Account Name", "Horizon Digital Marketing FZE"),
        ("Account Number", "0223344556677"),
        ("IBAN", "AE330240000223344556677"),
        ("SWIFT", "DUIBAEAD"),
    ),
    notes=(
        "Payment due within 30 days of the invoice date.",
        "Creative files remain licensed until the invoice is settled.",
        "Media spend is billed separately by the advertising platforms.",
    ),
    service_note=(
        "Campaign period: 01/10/2026 to 31/10/2026. Account manager: Ms. Reem Haddad."
    ),
    expected_audit=(
        "FAIL - 10% VAT is charged (VAT_RATE_CHECK and TAXABLE_AMOUNT_VAT_MISMATCH) "
        "and the supplier TRN has 8 digits instead of 15 (SUPPLIER_TRN_CHECK)."
    ),
    defects=("VAT charged at 10% instead of 5%", "Supplier TRN has 8 digits instead of 15"),
)


SAMPLE_INVOICES: tuple[InvoiceSpec, ...] = (
    GULF_STAR_TRADING,
    SHINY_HOME_CLEANING,
    AL_MADINA_AUTO_PARTS,
    BRIGHT_FUTURE_TECHNICAL,
    HORIZON_DIGITAL,
)
"""The five samples, in the order they are written (valid ones first)."""


def write_samples(output_dir: Path = OUTPUT_DIR) -> list[Path]:
    """Render every sample into `output_dir` and return the written paths."""

    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for spec in SAMPLE_INVOICES:
        path = output_dir / spec.file_name
        path.write_bytes(build_invoice_pdf(spec))
        written.append(path)
    return written


def summarise(spec: InvoiceSpec) -> str:
    """One-line description used by the CLI output."""

    defects = f" | defects: {'; '.join(spec.defects)}" if spec.defects else ""
    return (
        f"{spec.file_name}  [{spec.slug}]\n"
        f"  {spec.number} | {spec.supplier.name} -> {spec.customer.name} | "
        f"taxable {money(spec.taxable_subtotal)} {spec.currency} | "
        f"VAT {money(spec.printed_vat)} | total {money(spec.printed_total)}\n"
        f"  expected: {spec.expected_audit}{defects}"
    )


def main(argv: list[str] | None = None) -> int:
    """Write the sample invoices and print what each one is meant to prove."""

    parser = argparse.ArgumentParser(
        description="Author five sample UAE tax-invoice PDFs (2 valid, 3 invalid)."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=OUTPUT_DIR,
        help=f"Destination folder (default: {OUTPUT_DIR}).",
    )
    args = parser.parse_args(argv)

    written = write_samples(args.output_dir)
    for spec, path in zip(SAMPLE_INVOICES, written):
        print(summarise(spec))
        print(f"  wrote: {path} ({path.stat().st_size / 1024:.1f} KB)")
    print(f"\nWrote {len(written)} sample invoice(s) to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
