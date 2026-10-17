"""Vendor-neutral error types and message sanitisation for the LLM layer (Phase 3, Revision 2).

Nothing in this module knows which provider is configured: the same errors are
raised for DeepSeek, OpenAI, Groq, Together, OpenRouter, Ollama, vLLM, LM Studio or
any other OpenAI-compatible endpoint. Messages never contain the API key and never
include a full provider payload.
"""

from collections.abc import Iterable

import httpx
from pydantic import ValidationError

MAX_MESSAGE_CHARACTERS = 200
REDACTION = "***"
ERROR_LOCATION_LIMIT = 3

CONFIGURATION_ERROR_MESSAGE = (
    "The LLM is not configured: {variables} must be set. Copy .env.example to .env in "
    "the backend directory, fill in the provider base URL, API key and model name, then "
    "restart the service."
)

STATUS_HINTS = {
    400: "The provider rejected the request payload.",
    401: "The configured API key was rejected.",
    402: "The provider account has insufficient balance.",
    403: "Access to the configured model was denied.",
    404: "The configured base URL or model was not found.",
    422: "The provider could not process the request.",
    429: "The provider rate limit was reached; retry later.",
    500: "The provider reported an internal error.",
    502: "The provider gateway returned a bad response.",
    503: "The provider is temporarily unavailable.",
}


class LLMError(RuntimeError):
    """Base class for LLM failures. Messages never contain the API key."""


class LLMConfigurationError(LLMError):
    """The LLM layer is not configured (missing key, model or base URL)."""


class LLMTimeoutError(LLMError):
    """The provider did not answer within the configured timeout."""


class LLMProviderError(LLMError):
    """The provider returned an HTTP error or could not be reached."""


class LLMInvalidResponseError(LLMError):
    """The provider answered, but the payload was not usable invoice JSON."""


def collapse(text: str, limit: int = MAX_MESSAGE_CHARACTERS) -> str:
    """Collapse whitespace and cap the length of provider-supplied text."""

    collapsed = " ".join(str(text).split())
    if len(collapsed) > limit:
        collapsed = collapsed[:limit].rstrip() + "..."
    return collapsed


def redact(text: str, secrets: Iterable[str | None] = ()) -> str:
    """Replace any configured secret that leaked into `text`."""

    result = text
    for secret in secrets:
        if secret:
            result = result.replace(secret, REDACTION)
    return result


def summarise_validation_error(exc: ValidationError, limit: int = ERROR_LOCATION_LIMIT) -> str:
    """Return a short, payload-free summary of Pydantic validation errors."""

    details = []
    for error in exc.errors()[:limit]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "<root>"
        details.append(f"{location}: {error.get('msg', 'invalid value')}")
    return "; ".join(details)


def configuration_error(missing: Iterable[str]) -> LLMConfigurationError:
    """Build the clear configuration error listing every missing variable."""

    return LLMConfigurationError(
        CONFIGURATION_ERROR_MESSAGE.format(variables=", ".join(missing))
    )


def provider_message_of(exc: BaseException) -> str:
    """Extract the most useful short provider message from an arbitrary exception."""

    candidates: list[str] = []

    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            candidates.append(str(error["message"]))
        elif isinstance(error, str):
            candidates.append(error)
        if not candidates and isinstance(body.get("message"), str):
            candidates.append(body["message"])

    message = getattr(exc, "message", None)
    if not candidates and isinstance(message, str) and message.strip():
        candidates.append(message)

    if not candidates:
        candidates.append(str(exc) or type(exc).__name__)

    return candidates[0]


def map_exception(exc: BaseException, secrets: Iterable[str | None] = ()) -> LLMError:
    """Map any provider/library exception onto a safe :class:`LLMError`.

    The API key is redacted, provider text is collapsed and truncated, and no
    response body, header or stack trace is ever forwarded.
    """

    if isinstance(exc, LLMError):
        return exc

    if isinstance(exc, (TimeoutError, httpx.TimeoutException)) or "timeout" in (
        type(exc).__name__.lower()
    ):
        return LLMTimeoutError(
            "The LLM provider did not respond within the configured timeout."
        )

    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)

    detail = collapse(redact(provider_message_of(exc), secrets))

    if isinstance(status, int):
        message = f"The LLM provider returned HTTP {status}."
        hint = STATUS_HINTS.get(status)
        if hint:
            message = f"{message} {hint}"
        if detail:
            message = f"{message} Provider message: {detail}"
        return LLMProviderError(message)

    if isinstance(exc, ValidationError):
        return LLMInvalidResponseError(
            "The LLM response did not match the expected invoice schema: "
            f"{summarise_validation_error(exc)}"
        )

    return LLMProviderError(
        f"The LLM provider request failed ({type(exc).__name__}): {detail}"
    )
