"""Deterministic, offline LLM stand-ins for the Phase 3 test suite.

Tests never call a real provider:

* settings are built from explicit values with ``_env_file=None``, so a
  developer's ``.env`` or environment variables cannot leak a real key in, and
* the chat model is a scripted fake that needs no API key and touches no network.
"""

import json
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import ConfigDict, Field

from app.config import LLMSettings
from app.models.invoice import RawInvoiceFields
from app.services.llm import RawExtraction
from app.services.pdf_extractor import ExtractedPage
from app.storage.memory import DocumentRecord

TEST_API_KEY = "sk-test-key-not-real-1234567890"
TEST_BASE_URL = "https://llm.test.invalid/v1"
TEST_MODEL = "test-model"
TEST_PROVIDER_LABEL = "test-provider"
TEST_DOCUMENT_NO = "DOC-TESTDOCUMENT0000000000000000000"

INVOICE_JSON: dict[str, Any] = {
    "invoice_number": "INV-2024-0001",
    "invoice_date": "2024-05-03",
    "supplier_name": "Al Noor Trading LLC",
    "supplier_trn": "100123456700003",
    "customer_name": "Gulf Retail FZE",
    "customer_trn": "100987654300003",
    "currency": "AED",
    "subtotal_excluding_vat": "1,000.00",
    "vat_amount": "50.00",
    "total_including_vat": "1050.00",
    "vat_rate_percent": "5%",
    "vat_classification": "Standard Rated",
    "field_evidence": {
        "supplier_trn": {
            "confidence_percent": 95,
            "page_number": 1,
            "evidence": "Supplier TRN: 100123456700003",
        },
        "vat_amount": {
            "confidence_percent": 88,
            "page_number": 1,
            "evidence": "VAT Amount: 50.00",
        },
    },
    "extraction_warnings": ["The due date was not printed on the invoice."],
}
"""A realistic model payload used across the Phase 3 tests."""


def make_settings(**overrides: Any) -> LLMSettings:
    """Build LLM settings from explicit, deterministic values only."""

    values: dict[str, Any] = {
        "llm_api_key": TEST_API_KEY,
        "llm_base_url": TEST_BASE_URL,
        "llm_model": TEST_MODEL,
        "llm_timeout_seconds": 5.0,
        "llm_max_input_characters": 60_000,
        "llm_max_retries": 0,
        "llm_structured_method": "json_mode",
        "llm_provider_label": TEST_PROVIDER_LABEL,
    }
    values.update(overrides)
    return LLMSettings(_env_file=None, **values)


class FakeTimeoutError(TimeoutError):
    """Stands in for a provider timeout."""


class FakeProviderError(Exception):
    """Stands in for a provider/HTTP error that carries a status code."""

    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(message or f"HTTP {status_code}")
        self.status_code = status_code
        self.message = message or f"HTTP {status_code}"


class ScriptedChatModel(BaseChatModel):
    """Fake chat model that replays scripted content and records its invocations.

    ``supports_structured`` mirrors provider capability: when it is False,
    ``with_structured_output`` behaves like a model without native structured
    output and the extractor must fall back to prompt-based JSON parsing.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    contents: list[str] = Field(default_factory=list)
    structured_results: list[Any] = Field(default_factory=list)
    supports_structured: bool = False
    error: Any = None
    calls: list[str] = Field(default_factory=list)
    sent: list[Any] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted-fake-chat-model"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Record the call and return the next scripted reply (or raise)."""

        self.calls.append("plain")
        self.sent.append(list(messages))
        if self.error is not None:
            raise self.error
        if not self.contents:
            raise AssertionError("ScriptedChatModel has no scripted reply left")
        content = self.contents[0] if len(self.contents) == 1 else self.contents.pop(0)
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=content))]
        )

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:
        """Return a runnable replaying scripted structured results, or refuse."""

        if not self.supports_structured:
            raise NotImplementedError("this fake model has no structured output")

        def _invoke(messages: Any) -> Any:
            self.calls.append("structured")
            self.sent.append(messages)
            if self.error is not None:
                raise self.error
            if not self.structured_results:
                raise AssertionError("no scripted structured result left")
            result = self.structured_results.pop(0)
            if isinstance(result, Exception):
                raise result
            if isinstance(result, dict):
                return schema.model_validate(result)
            return result

        return RunnableLambda(_invoke)


def scripted_model(
    content: str | dict | None = None,
    *,
    error: Exception | None = None,
    structured: Any = None,
    supports_structured: bool = False,
    contents: Sequence[str] | None = None,
) -> ScriptedChatModel:
    """Build a scripted fake chat model for a test.

    Args:
        content: A single reply; dicts are serialised as JSON.
        error: Raised on every invocation instead of replying.
        structured: Result(s) returned by the structured-output path.
        supports_structured: Whether the fake supports structured output.
        contents: Explicit list of replies (overrides `content`).
    """

    if contents is not None:
        replies = list(contents)
    elif content is None:
        replies = []
    elif isinstance(content, dict):
        replies = [json.dumps(content)]
    else:
        replies = [content]

    structured_results: list[Any] = []
    if structured is not None:
        structured_results = (
            list(structured) if isinstance(structured, list) else [structured]
        )

    return ScriptedChatModel(
        contents=replies,
        structured_results=structured_results,
        supports_structured=supports_structured,
        error=error,
    )


class StubExtractor:
    """Stand-in for :class:`LLMExtractor` that records calls, using no network."""

    def __init__(
        self,
        fields: dict[str, Any] | None = None,
        error: Exception | None = None,
        model: str | None = TEST_MODEL,
        client_warnings: Sequence[str] = (),
        provider_label: str = TEST_PROVIDER_LABEL,
    ) -> None:
        self.calls: list[list[tuple[int, str]]] = []
        self._fields = dict(INVOICE_JSON) if fields is None else fields
        self._error = error
        self._model = model
        self._client_warnings = list(client_warnings)
        self.provider_label = provider_label

    def extract_invoice_fields(
        self, pages: Sequence[tuple[int, str]]
    ) -> RawExtraction:
        """Record the pages, then return the canned extraction (or raise)."""

        self.calls.append(list(pages))
        if self._error is not None:
            raise self._error
        return RawExtraction(
            fields=RawInvoiceFields.model_validate(self._fields),
            model=self._model,
            client_warnings=list(self._client_warnings),
        )


def make_record(
    page_texts: Sequence[str] = ("Invoice No: INV-2024-0001",),
    status: str = "extracted",
    document_no: str = TEST_DOCUMENT_NO,
) -> DocumentRecord:
    """Build a stored document record for service-level tests."""

    return DocumentRecord(
        document_no=document_no,
        original_filename="invoice.pdf",
        content_type="application/pdf",
        size_bytes=1024,
        uploaded_at=datetime(2024, 5, 3, 12, 0, 0, tzinfo=timezone.utc),
        page_count=len(page_texts),
        pages=[
            ExtractedPage(page_number=index + 1, text=text)
            for index, text in enumerate(page_texts)
        ],
        extraction_status=status,
        extraction_error=None if status == "extracted" else "needs OCR",
    )

