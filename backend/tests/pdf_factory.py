"""Test helpers that build small PDFs in memory (no files are written to disk).

PyMuPDF is used here (and only here) to *author* the fixtures: the application itself
never imports it, extraction goes through pdfplumber.
"""

import fitz  # PyMuPDF


def build_text_pdf(pages: list[str]) -> bytes:
    """Return a PDF with the given text on one page per list entry."""

    document = fitz.open()
    try:
        for text in pages:
            page = document.new_page()
            page.insert_text((72, 96), text, fontsize=11)
        return document.tobytes()
    finally:
        document.close()


def build_blank_pdf(page_count: int = 1) -> bytes:
    """Return an image-only style PDF: pages with drawn shapes and no text.

    This mimics a scanned invoice, where text extraction yields nothing.
    """

    document = fitz.open()
    try:
        for _ in range(page_count):
            page = document.new_page()
            page.draw_rect(
                fitz.Rect(72, 72, 420, 260), color=(0, 0, 0), fill=(0.85, 0.9, 1.0)
            )
        return document.tobytes()
    finally:
        document.close()


def build_table_pdf(
    rows: list[list[str]],
    *,
    origin: tuple[float, float] = (72.0, 96.0),
    cell_width: float = 150.0,
    cell_height: float = 22.0,
) -> bytes:
    """Return a PDF holding one ruled table, so pdfplumber can rebuild its cells.

    Every cell is drawn as a stroked rectangle (which is what pdfplumber's table
    detector looks for) with the cell text inside it.
    """

    document = fitz.open()
    try:
        page = document.new_page()
        left, top = origin
        for row_index, row in enumerate(rows):
            for column_index, cell in enumerate(row):
                x0 = left + column_index * cell_width
                y0 = top + row_index * cell_height
                page.draw_rect(
                    fitz.Rect(x0, y0, x0 + cell_width, y0 + cell_height),
                    color=(0, 0, 0),
                    width=0.7,
                )
                if cell:
                    page.insert_text(
                        (x0 + 4, y0 + cell_height - 7), str(cell), fontsize=9
                    )
        return document.tobytes()
    finally:
        document.close()


def build_scanned_pdf(text: str = "Scanned by Office Scanner") -> bytes:
    """Return a PDF whose single page is one full-page image, like a real scan.

    The page carries a short text layer (the scanner stamp) on purpose: that is exactly
    the case the image-coverage rule in the extractor has to recognise as a scan.
    """

    document = fitz.open()
    try:
        page = document.new_page()
        pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 240, 320))
        pixmap.set_rect(pixmap.irect, (235, 240, 245))
        page.insert_image(page.rect, pixmap=pixmap)
        if text:
            page.insert_text((72, 60), text, fontsize=9)
        return document.tobytes()
    finally:
        document.close()


def build_encrypted_pdf(password: str = "secret") -> bytes:
    """Return a password-protected PDF, which uploads must reject."""

    document = fitz.open()
    try:
        page = document.new_page()
        page.insert_text((72, 96), "Invoice No: INV-ENCRYPTED", fontsize=11)
        return document.tobytes(
            encryption=fitz.PDF_ENCRYPT_AES_256,
            owner_pw=password,
            user_pw=password,
        )
    finally:
        document.close()
