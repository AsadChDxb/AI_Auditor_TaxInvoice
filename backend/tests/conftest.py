"""Shared fixtures for the UAE VAT Audit MVP test suite."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import ocr_fallback
from app.services.ocr_fallback import clear_ocr_engine_cache
from app.storage.memory import document_registry


def paddleocr_is_missing() -> str:
    """Stand in for the import probe when PaddleOCR must look uninstalled."""

    return "ImportError: No module named 'paddleocr'"


@pytest.fixture()
def client() -> TestClient:
    """A test client bound to the application under test."""

    return TestClient(app)


@pytest.fixture()
def without_paddleocr(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pretend PaddleOCR is not installed, as a deployment without it behaves.

    The upload route, the extraction service and the OCR cache all ask the fallback module
    for an engine, so the missing dependency is simulated where they meet - the import
    probe - instead of at a single call site. The engine cache is dropped around the patch
    because an engine built by an earlier test would otherwise be reused, which also keeps
    the suite from loading the real OCR stack on a machine that has PaddleOCR installed.
    """

    clear_ocr_engine_cache()
    monkeypatch.setattr(ocr_fallback, "ocr_availability", paddleocr_is_missing)
    yield
    # Undo the patch before clearing the caches: an engine (or the "unavailable" answer)
    # cached while the dependency was hidden must not survive into the next test.
    monkeypatch.undo()
    clear_ocr_engine_cache()


@pytest.fixture(autouse=True)
def empty_document_registry() -> Iterator[None]:
    """Keep the in-memory registry isolated between tests."""

    document_registry.clear()
    yield
    document_registry.clear()
