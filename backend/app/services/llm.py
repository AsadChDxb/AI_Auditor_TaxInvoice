"""Provider-agnostic LLM layer for structured invoice extraction (Phase 3, Revision 2).

The layer speaks the OpenAI-compatible chat-completions protocol through
LangChain, so the provider is identified only by configuration
(``LLM_BASE_URL`` / ``LLM_API_KEY`` / ``LLM_MODEL``). There is deliberately no
vendor-specific client class: DeepSeek, OpenAI, Groq, Together, OpenRouter,
Ollama, vLLM and LM Studio all go through the same code path.

Security and safety notes
-------------------------
* The API key is read from configuration, handed to the chat model and never
  logged, never stored on a record and never echoed in an error message.
* Invoice text is treated as untrusted data (see :mod:`app.services.llm_prompts`).
* Provider failures are reduced to a short, sanitised message by
  :mod:`app.services.llm_errors`.
* When the configured model cannot produce native structured output, the prompt
  already requests JSON and the response is parsed on our side instead.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

from app.config import LLMSettings, get_llm_settings
from app.models.invoice import RawInvoiceFields
from app.services.llm_errors import (
    LLMInvalidResponseError,
    LLMProviderError,
    configuration_error,
    map_exception,
    summarise_validation_error,
)
from app.services.llm_prompts import SYSTEM_PROMPT, build_user_prompt

DEFAULT_PROVIDER_LABEL = "openai-compatible"
"""Label recorded on extracted invoice data when none is configured."""

_UNSET = object()


@dataclass
class RawExtraction:
    """Raw, schema-validated model output plus client-side warnings."""

    fields: RawInvoiceFields
    model: str | None = None
    client_warnings: list[str] = field(default_factory=list)


def build_chat_model(settings: LLMSettings) -> BaseChatModel:
    """Build the chat model for the configured OpenAI-compatible endpoint.

    Raises:
        LLMConfigurationError: required settings are missing (the configuration
            error is built by :func:`extract_invoice_fields` before this is called).
    """

    return ChatOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.api_key,
        model=settings.llm_model,
        temperature=0,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )


def strip_code_fences(content: str) -> str:
    """Remove an accidental markdown code fence around the JSON payload."""

    text = content.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def content_text(message: BaseMessage) -> str:
    """Return the text of a chat message, tolerating list-style content blocks."""

    content: Any = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
        return "".join(chunks)
    return str(content)


def parse_invoice_fields(content: str) -> RawInvoiceFields:
    """Parse and validate model content into :class:`RawInvoiceFields`."""

    text = strip_code_fences(content)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMInvalidResponseError(
            "The LLM returned content that is not valid JSON "
            f"(line {exc.lineno}, column {exc.colno}: {exc.msg})."
        ) from exc

    if not isinstance(payload, dict):
        raise LLMInvalidResponseError("The LLM returned JSON that is not an object.")

    try:
        return RawInvoiceFields.model_validate(payload)
    except ValidationError as exc:
        raise LLMInvalidResponseError(
            "The LLM output did not match the expected invoice schema: "
            f"{summarise_validation_error(exc)}"
        ) from exc


def coerce_fields(result: Any) -> RawInvoiceFields | None:
    """Return `result` as validated fields, or None when it is not usable."""

    if isinstance(result, RawInvoiceFields):
        return result
    if isinstance(result, dict):
        try:
            return RawInvoiceFields.model_validate(result)
        except ValidationError:
            return None
    return None


class LLMExtractor:
    """Turn extracted page text into schema-validated invoice JSON using the configured LLM."""

    def __init__(
        self,
        chat_model: BaseChatModel | None = None,
        settings: LLMSettings | None = None,
        *,
        provider_label: str | None = None,
    ) -> None:
        """Create the extractor.

        Args:
            chat_model: Pre-built chat model; built from settings on first use when
                omitted. Tests inject fake models so no network call is made.
            settings: Provider configuration; defaults to the cached `.env`/env
                settings. Passing settings explicitly keeps tests deterministic.
            provider_label: Label recorded on results; defaults to the configured
                ``LLM_PROVIDER_LABEL``.
        """

        self._chat_model = chat_model
        self._settings = settings
        self._provider_label = provider_label
        self._structured_model: Any = _UNSET

    @property
    def settings(self) -> LLMSettings:
        """The provider configuration in use (the key is never exposed as text)."""

        return self._resolve_settings()

    @property
    def provider_label(self) -> str:
        """Label recorded on extracted invoice data."""

        if self._provider_label:
            return self._provider_label
        return self._resolve_settings().llm_provider_label or DEFAULT_PROVIDER_LABEL

    def _resolve_settings(self) -> LLMSettings:
        if self._settings is None:
            self._settings = get_llm_settings()
        return self._settings

    def _resolve_chat_model(self, settings: LLMSettings) -> BaseChatModel:
        if self._chat_model is None:
            self._chat_model = build_chat_model(settings)
        return self._chat_model

    def _resolve_structured_model(
        self, chat_model: BaseChatModel, settings: LLMSettings
    ) -> Any:
        """Return a structured-output runnable, or None when it is not available.

        Providers without native structured output raise here; the caller then
        uses the prompt-based JSON path, because the prompt already instructs the
        model to answer with JSON only.
        """

        if settings.llm_structured_method == "plain":
            return None
        if self._structured_model is _UNSET:
            try:
                self._structured_model = chat_model.with_structured_output(
                    RawInvoiceFields, method=settings.llm_structured_method
                )
            except Exception:  # noqa: BLE001 - any failure means "not supported"
                self._structured_model = None
        return self._structured_model

    def extract_invoice_fields(
        self, pages: Sequence[tuple[int, str]]
    ) -> RawExtraction:
        """Request the structured invoice fields for `pages`.

        Args:
            pages: ``(page_number, text)`` pairs taken from the stored document.

        Raises:
            LLMConfigurationError: the API key, model or base URL is missing.
            LLMTimeoutError: the request exceeded the configured timeout.
            LLMProviderError: the provider failed or could not be reached.
            LLMInvalidResponseError: the response was not usable invoice JSON.
        """

        settings = self._resolve_settings()
        missing = settings.missing_settings()
        if missing:
            raise configuration_error(missing)

        api_key = settings.api_key
        prompt, warnings = build_user_prompt(pages, settings.llm_max_input_characters)
        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ]
        chat_model = self._resolve_chat_model(settings)

        structured = self._resolve_structured_model(chat_model, settings)
        if structured is not None:
            try:
                result = structured.invoke(messages)
            except Exception as exc:  # noqa: BLE001 - mapped to a safe LLM error
                mapped = map_exception(exc, (api_key,))
                status = getattr(exc, "status_code", None)
                if not isinstance(mapped, LLMProviderError) or (
                    isinstance(status, int) and 400 <= status < 500
                ):
                    # Timeouts and client errors (bad key, rejected payload) are
                    # final; a server-side or parsing problem may still succeed
                    # through the prompt-based path below.
                    raise mapped from exc
            else:
                fields = coerce_fields(result)
                if fields is not None:
                    return RawExtraction(
                        fields=fields,
                        model=settings.llm_model,
                        client_warnings=warnings,
                    )

        message = self._invoke_plain(chat_model, messages, api_key)
        fields = parse_invoice_fields(content_text(message))
        return RawExtraction(
            fields=fields, model=settings.llm_model, client_warnings=warnings
        )

    def _invoke_plain(
        self, chat_model: BaseChatModel, messages: list[BaseMessage], api_key: str | None
    ) -> BaseMessage:
        """Invoke the model without a structured-output wrapper."""

        try:
            return chat_model.invoke(messages)
        except Exception as exc:  # noqa: BLE001 - mapped to a safe LLM error
            raise map_exception(exc, (api_key,)) from exc

