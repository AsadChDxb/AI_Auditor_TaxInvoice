"""Tests for the read-only sample invoice routes (the upload screen's testing aid)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import samples as samples_api
from app.config import settings
from tests.pdf_factory import build_text_pdf

VALID_SAMPLE = "01_valid_golden.pdf"
INVALID_SAMPLE = "02_invalid_broken.pdf"


@pytest.fixture()
def samples_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the router at a temporary folder with two PDFs and two non-servable files."""

    directory = tmp_path / "samples"
    directory.mkdir()
    (directory / VALID_SAMPLE).write_bytes(
        build_text_pdf(["Invoice No: INV-2024-0001", "Total: 1050.00"])
    )
    (directory / INVALID_SAMPLE).write_bytes(
        build_text_pdf(["Invoice No: INV-2024-0002", "Total: 999.00"])
    )
    (directory / "notes.txt").write_text("not a PDF", encoding="utf-8")
    (directory / ".hidden.pdf").write_bytes(b"%PDF-1.4 hidden file")
    monkeypatch.setattr(settings, "samples_dir", str(directory))
    return directory


def test_listing_returns_only_servable_pdfs_sorted(
    client: TestClient, samples_folder: Path
) -> None:
    response = client.get("/samples")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert [item["file_name"] for item in body["samples"]] == [
        VALID_SAMPLE,
        INVALID_SAMPLE,
    ]
    assert [item["expected_outcome"] for item in body["samples"]] == ["pass", "fail"]
    assert body["samples"][0]["url"] == f"/samples/{VALID_SAMPLE}"
    assert all(item["size_bytes"] > 0 for item in body["samples"])
    assert body["notes"]


def test_listing_falls_back_to_the_file_name_without_pdf_metadata(
    client: TestClient, samples_folder: Path
) -> None:
    body = client.get("/samples").json()

    assert body["samples"][0]["title"] == "Valid golden"
    assert body["samples"][0]["supplier_name"] is None


def test_a_sample_is_streamed_as_an_inline_pdf(
    client: TestClient, samples_folder: Path
) -> None:
    response = client.get(f"/samples/{INVALID_SAMPLE}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.content == (samples_folder / INVALID_SAMPLE).read_bytes()


@pytest.mark.parametrize(
    "name",
    [
        "missing.pdf",
        "notes.txt",
        "config.py",
        "..%5Capp%5Cconfig.py",
        ".env",
        ".hidden.pdf",
    ],
)
def test_unservable_names_are_not_found(
    client: TestClient, samples_folder: Path, name: str
) -> None:
    response = client.get(f"/samples/{name}")

    assert response.status_code == 404
    assert response.json()["detail"] == samples_api.NOT_FOUND_DETAIL


@pytest.mark.parametrize(
    "path",
    [
        "/samples/..%2Fapp%2Fconfig.py",
        "/samples/%2E%2E%2Fapp%2Fconfig.py",
        "/samples/..%2F..%2F.env",
    ],
)
def test_encoded_traversal_attempts_never_return_a_file(
    client: TestClient, samples_folder: Path, path: str
) -> None:
    """A traversal attempt is a 404, whichever layer normalises the URL first."""

    response = client.get(path)

    assert response.status_code == 404
    assert not response.headers.get("content-type", "").startswith("application/pdf")
    assert b"%PDF-" not in response.content


def test_a_missing_folder_lists_nothing_with_a_note(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "samples_dir", str(tmp_path / "absent"))

    response = client.get("/samples")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 0
    assert body["samples"] == []
    assert any("does not exist" in note for note in body["notes"])


def test_the_bundled_samples_are_listed_streamed_and_uploadable(
    client: TestClient,
) -> None:
    """The generated invoices shipped next to the app are served and can be uploaded."""

    directory = Path(settings.samples_dir)
    if not directory.is_dir():  # pragma: no cover - only when sample_inv/ is absent
        pytest.skip("sample_inv/ is not present in this checkout")

    body = client.get("/samples").json()
    outcomes = [item["expected_outcome"] for item in body["samples"]]
    assert outcomes.count("pass") >= 2
    assert outcomes.count("fail") >= 3

    first = body["samples"][0]
    streamed = client.get(first["url"])
    assert streamed.status_code == 200
    assert streamed.content.startswith(b"%PDF-")

    # Serving a sample is not an upload: it becomes a document only through the normal
    # route, which is what the UI calls after the user picks one.
    upload = client.post(
        "/documents/upload",
        files={"file": (first["file_name"], streamed.content, "application/pdf")},
    )
    assert upload.status_code == 201
    assert upload.json()["extraction_status"] == "extracted"
