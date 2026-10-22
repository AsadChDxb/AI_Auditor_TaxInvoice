"""Bundled sample invoices, listed and streamed read-only (testing aid).

Why this exists: an upload screen is only useful if there is something to upload, and a
reviewer rarely has a UAE tax invoice at hand. ``scripts/generate_sample_invoices.py``
writes five documents into ``backend/sample_inv``; this router lists them
(``GET /samples``) and streams one (``GET /samples/{file_name}``), so the UI can offer a
sample that then travels through exactly the same upload path as a picked file.

Everything here is read-only and self-contained:

* only ``*.pdf`` files whose name matches :data:`SAMPLE_FILE_PATTERN` are listed;
* the resolved path must stay inside the configured folder, so a traversal attempt is a
  404 instead of a file read;
* the listing carries file metadata only - never page text, TRNs or invoice fields;
* serving a sample does not add it to the document registry: it still has to be
  uploaded, so extraction and audit behave exactly as for a user-picked file.

``VAT_AUDIT_SAMPLES_DIR`` points the router at another folder; the default is the folder
the generator writes to (``backend/sample_inv``).
"""

import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import pdfplumber
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

from app.config import settings
from app.models.sample import (
    SampleExpectedOutcome,
    SampleInvoice,
    SampleInvoiceListResponse,
)

router = APIRouter(tags=["Samples"])

SAMPLE_FILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.pdf$")
"""File names this router is willing to serve: a plain PDF name, no separators.

Only ASCII letters, digits, dot, underscore and hyphen are accepted, which excludes path
separators, ``..`` traversal, hidden files (they start with a dot) and non-PDF files.
"""

PDF_MEDIA_TYPE = "application/pdf"
SAMPLE_ROUTE_PREFIX = "/samples"
VALID_TOKEN = "_valid"
INVALID_TOKEN = "_invalid"
MAX_SAMPLES = 50
"""Upper bound on the listing, so a folder cannot be used to produce a huge response."""

NOT_FOUND_DETAIL = "No sample invoice with that file name is available."

SAMPLE_NOTE = (
    "Sample invoices are generated test documents: the companies, TRNs and bank "
    "accounts are fictitious. They are served read-only and only become a document once "
    "they are uploaded, so extraction and audit behave exactly as for a picked file."
)

MISSING_DIRECTORY_NOTE = (
    "The sample folder '{path}' does not exist on this deployment, so no sample "
    "invoices are available. Run 'python scripts/generate_sample_invoices.py' in "
    "backend/ or point VAT_AUDIT_SAMPLES_DIR at a folder of PDFs."
)

LIMIT_NOTE = (
    "Only the first {limit} of {total} sample invoices are listed "
    "(see MAX_SAMPLES in app/api/samples.py)."
)


def _clean_metadata(value: object) -> str | None:
    """Return a collapsed metadata string, or None when it is not usable text."""

    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text or None


@lru_cache(maxsize=128)
def _pdf_metadata(
    path_text: str, size_bytes: int, modified_ns: int
) -> tuple[str | None, str | None]:
    """Read a sample's Title and Author, or ``(None, None)`` when it has none.

    ``size_bytes`` and ``modified_ns`` are part of the cache key on purpose: editing or
    regenerating a sample invalidates its entry instead of serving a stale title. A PDF
    that cannot be opened never fails the listing - it is described by its file name.
    """

    try:
        with pdfplumber.open(path_text) as document:
            metadata = document.metadata or {}
    except Exception:  # noqa: BLE001 - a broken sample must not fail the whole listing
        return (None, None)
    return (
        _clean_metadata(metadata.get("Title")),
        _clean_metadata(metadata.get("Author")),
    )


def _humanise(stem: str) -> str:
    """Turn ``01_valid_goods_gulf_star`` into ``Valid goods gulf star``."""

    words = stem.replace("_", " ").replace("-", " ").split()
    if words and words[0].isdigit():
        words = words[1:]
    text = " ".join(words) or stem
    return text[:1].upper() + text[1:]


def expected_outcome(file_name: str) -> SampleExpectedOutcome:
    """``pass``/``fail`` for the generator's naming convention, else ``unknown``."""

    lowered = file_name.lower()
    if INVALID_TOKEN in lowered:
        return "fail"
    if VALID_TOKEN in lowered:
        return "pass"
    return "unknown"


def _describe(path: Path) -> SampleInvoice:
    """Build the response entry for one sample file."""

    stat = path.stat()
    title, author = _pdf_metadata(str(path), stat.st_size, stat.st_mtime_ns)
    return SampleInvoice(
        file_name=path.name,
        title=title or _humanise(path.stem),
        supplier_name=author,
        size_bytes=stat.st_size,
        expected_outcome=expected_outcome(path.name),
        url=f"{SAMPLE_ROUTE_PREFIX}/{quote(path.name)}",
    )


def _list_sample_files(directory: Path) -> list[Path]:
    """Return the servable PDFs in `directory`, sorted by file name."""

    try:
        entries = list(directory.iterdir())
    except OSError:  # pragma: no cover - the is_dir() check runs first
        return []
    return sorted(
        (
            entry
            for entry in entries
            if entry.is_file() and SAMPLE_FILE_PATTERN.fullmatch(entry.name)
        ),
        key=lambda entry: entry.name,
    )


def _resolve_sample(file_name: str) -> Path:
    """Resolve one requested file, rejecting anything outside the samples folder."""

    if not SAMPLE_FILE_PATTERN.fullmatch(file_name):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)

    directory = Path(settings.samples_dir)
    try:
        root = directory.resolve(strict=True)
        resolved = (directory / file_name).resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL
        ) from None

    if resolved.parent != root or not resolved.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    return resolved


@router.get(
    "/samples",
    response_model=SampleInvoiceListResponse,
    summary="List the bundled sample invoices",
    description=(
        "Returns the sample invoice PDFs available on this deployment, sorted by file "
        "name, with the metadata the upload screen needs to offer them. Samples are "
        "generated test documents; serving one does not upload or audit it."
    ),
)
def list_sample_invoices() -> SampleInvoiceListResponse:
    """List the sample PDFs in the configured folder."""

    directory = Path(settings.samples_dir)
    if not directory.is_dir():
        return SampleInvoiceListResponse(
            total=0,
            samples=[],
            notes=[MISSING_DIRECTORY_NOTE.format(path=directory)],
        )

    files = _list_sample_files(directory)
    samples = [_describe(path) for path in files[:MAX_SAMPLES]]
    notes = [SAMPLE_NOTE]
    if len(files) > len(samples):
        notes.append(LIMIT_NOTE.format(limit=len(samples), total=len(files)))
    return SampleInvoiceListResponse(total=len(samples), samples=samples, notes=notes)


@router.get(
    f"{SAMPLE_ROUTE_PREFIX}/{{file_name}}",
    response_class=FileResponse,
    summary="Stream one bundled sample invoice",
    description=(
        "Returns the PDF bytes with ``Content-Disposition: inline`` so a browser can "
        "open the sample in a tab. Only PDF file names inside the samples folder are "
        "served; anything else is a 404."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "No sample invoice with that file name is available."
        }
    },
)
def read_sample_invoice(file_name: str) -> FileResponse:
    """Stream one sample PDF so the UI can open it or upload it like a picked file."""

    path = _resolve_sample(file_name)
    return FileResponse(
        path,
        media_type=PDF_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'inline; filename="{path.name}"',
            "Cache-Control": "public, max-age=300",
        },
    )
