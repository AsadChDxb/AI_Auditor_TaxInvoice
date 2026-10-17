"""Runtime configuration for the UAE VAT Audit MVP.

Two groups of settings:

* upload/extraction limits (Phase 2), parsed with the standard library, and
* LLM provider settings (Phase 3, Revision 2), loaded from the environment and a
  local ``.env`` file through ``pydantic-settings``. The LLM layer is
  provider-agnostic: only the base URL, API key and model name identify a provider.

Secrets are read from the environment only: nothing is hardcoded, and the API key
is held in a ``SecretStr`` so it cannot be printed accidentally.
"""

import os
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "VAT_AUDIT_"
DEFAULT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB, per the Phase 2 requirements
DEFAULT_READ_CHUNK_BYTES = 1024 * 1024
DEFAULT_SIGNATURE_SCAN_BYTES = 1024

# --- OCR fallback (Phase 9) -------------------------------------------------------
# The PaddleOCR fallback runs only on pages whose pdfplumber text layer is too thin to
# use, and it degrades gracefully: without the optional PaddleOCR dependency the page
# simply stays ``needs_ocr``.
DEFAULT_OCR_ENABLED = True
DEFAULT_OCR_LANGUAGE = "en"
DEFAULT_OCR_DEVICE = "cpu"
"""Inference device for PaddleOCR; ``cpu`` so a GPU is never required."""
DEFAULT_OCR_ENABLE_MKLDNN = False
"""PaddleOCR's oneDNN (MKL-DNN) acceleration, off by default.

PaddlePaddle 3.x fails inside oneDNN on some CPU builds - "NotImplementedError:
ConvertPirAttribute2RuntimeAttribute not support" while running text detection - which
breaks OCR completely, so the portable setting is the default. Set
``VAT_AUDIT_OCR_ENABLE_MKLDNN=true`` on a machine whose PaddlePaddle build supports it.
"""
DEFAULT_OCR_DPI = 150
DEFAULT_OCR_CPU_THREADS = 4
"""Threads for PaddleOCR's CPU kernels.

PaddleOCR defaults to 10, which oversubscribes a typical laptop CPU: measured on a
4-core laptop the same page took 22s with 4 threads and 34s with the default 10.
"""
DEFAULT_OCR_DET_MODEL = "PP-OCRv5_mobile_det"
DEFAULT_OCR_REC_MODEL = "en_PP-OCRv5_mobile_rec"
"""CPU-sized OCR models (mobile detector + English mobile recogniser).

PaddleOCR 3.7 defaults to the PP-OCRv6 *medium* detector and recogniser, which took
~150s per scanned page on a CPU-only laptop; the mobile pair reads the same invoice in
~26s. Set both to an empty string to fall back to PaddleOCR's own choice (more accurate,
much slower on a CPU). The recogniser is language-specific, so change it together with
``VAT_AUDIT_OCR_LANGUAGE`` or clear both to let PaddleOCR resolve them.
"""
DEFAULT_OCR_MIN_PAGE_CHARACTERS = 12
DEFAULT_OCR_MAX_PAGES = 25

ENV_FILE = ".env"
DEFAULT_LLM_BASE_URL = "https://api.openai.com/v1"
DEFAULT_LLM_TIMEOUT_SECONDS = 60.0
DEFAULT_LLM_MAX_INPUT_CHARACTERS = 60_000
DEFAULT_LLM_MAX_RETRIES = 2
DEFAULT_LLM_PROVIDER_LABEL = "openai-compatible"
LLM_STRUCTURED_METHODS = ("json_mode", "json_schema", "function_calling", "plain")
"""Structured-output strategies, in order of preference for portability."""



def _positive_int_from_env(name: str, default: int) -> int:
    """Read a positive integer from the environment, falling back to `default`."""

    raw = os.environ.get(ENV_PREFIX + name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value > 0 else default


TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def _bool_from_env(name: str, default: bool) -> bool:
    """Read a boolean flag from the environment, falling back to `default`."""

    raw = os.environ.get(ENV_PREFIX + name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    return default


def _text_from_env(name: str, default: str) -> str:
    """Read a non-empty text value from the environment."""

    raw = os.environ.get(ENV_PREFIX + name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


def _optional_text_from_env(name: str, default: str) -> str:
    """Read a text value that an empty assignment is allowed to clear."""

    raw = os.environ.get(ENV_PREFIX + name)
    if raw is None:
        return default
    return raw.strip()


class Settings(BaseModel):
    """Configurable limits used by the API and the PDF extraction service (Phases 2-9).

    Attributes:
        max_upload_size_bytes: Maximum accepted upload size, 10 MB by default.
            Set ``VAT_AUDIT_MAX_UPLOAD_BYTES`` to change it.
        read_chunk_size_bytes: Chunk size used when streaming an upload into
            memory, so oversized uploads are rejected without buffering
            everything first.
        pdf_signature_scan_bytes: How many leading bytes are scanned for the
            ``%PDF-`` signature before pdfplumber is asked to open the file.
        ocr_enabled: Whether the PaddleOCR fallback may run on pages whose
            pdfplumber text layer is unusable (``VAT_AUDIT_OCR_ENABLED``).
        ocr_language: PaddleOCR language code (``VAT_AUDIT_OCR_LANGUAGE``).
        ocr_device: Device PaddleOCR runs on (``VAT_AUDIT_OCR_DEVICE``); ``cpu`` by
            default so no GPU is required (use ``gpu:0`` on a machine that has one).
        ocr_enable_mkldnn: Whether PaddleOCR may use oneDNN acceleration
            (``VAT_AUDIT_OCR_ENABLE_MKLDNN``); off by default because PaddlePaddle 3.x
            crashes inside oneDNN on some CPU builds.
        ocr_cpu_threads: Threads for PaddleOCR's CPU kernels
            (``VAT_AUDIT_OCR_CPU_THREADS``); 4 by default, because 10 oversubscribes a
            laptop CPU.
        ocr_det_model: PaddleOCR text-detection model name
            (``VAT_AUDIT_OCR_DET_MODEL``); the mobile model by default, because the medium
            default is roughly four times slower per scanned page on a CPU.
        ocr_rec_model: PaddleOCR text-recognition model name
            (``VAT_AUDIT_OCR_REC_MODEL``); language-specific, empty means "let PaddleOCR
            choose" together with ``ocr_det_model``.
        ocr_dpi: Resolution used to render a page for OCR
            (``VAT_AUDIT_OCR_DPI``); 150 by default, because it reads a printed invoice as
            well as 200 while taking roughly half the time on a CPU. Higher is more
            accurate and slower.
        ocr_min_page_characters: A page whose collapsed text is shorter than this is
            treated as having no usable text layer and is an OCR candidate
            (``VAT_AUDIT_OCR_MIN_PAGE_CHARACTERS``).
        ocr_max_pages: Upper bound on how many pages one upload may OCR, so a huge
            scanned document cannot stall the request (``VAT_AUDIT_OCR_MAX_PAGES``).
    """

    max_upload_size_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    read_chunk_size_bytes: int = DEFAULT_READ_CHUNK_BYTES
    pdf_signature_scan_bytes: int = DEFAULT_SIGNATURE_SCAN_BYTES
    ocr_enabled: bool = DEFAULT_OCR_ENABLED
    ocr_language: str = DEFAULT_OCR_LANGUAGE
    ocr_device: str = DEFAULT_OCR_DEVICE
    ocr_enable_mkldnn: bool = DEFAULT_OCR_ENABLE_MKLDNN
    ocr_cpu_threads: int = DEFAULT_OCR_CPU_THREADS
    ocr_det_model: str = DEFAULT_OCR_DET_MODEL
    ocr_rec_model: str = DEFAULT_OCR_REC_MODEL
    ocr_dpi: int = DEFAULT_OCR_DPI
    ocr_min_page_characters: int = DEFAULT_OCR_MIN_PAGE_CHARACTERS
    ocr_max_pages: int = DEFAULT_OCR_MAX_PAGES

    @property
    def max_upload_size_mb(self) -> float:
        """The upload limit in megabytes, for user-facing messages."""

        return round(self.max_upload_size_bytes / (1024 * 1024), 2)


settings = Settings(
    max_upload_size_bytes=_positive_int_from_env(
        "MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES
    ),
    read_chunk_size_bytes=_positive_int_from_env(
        "READ_CHUNK_SIZE_BYTES", DEFAULT_READ_CHUNK_BYTES
    ),
    pdf_signature_scan_bytes=_positive_int_from_env(
        "PDF_SIGNATURE_SCAN_BYTES", DEFAULT_SIGNATURE_SCAN_BYTES
    ),
    ocr_enabled=_bool_from_env("OCR_ENABLED", DEFAULT_OCR_ENABLED),
    ocr_language=_text_from_env("OCR_LANGUAGE", DEFAULT_OCR_LANGUAGE),
    ocr_device=_text_from_env("OCR_DEVICE", DEFAULT_OCR_DEVICE),
    ocr_enable_mkldnn=_bool_from_env(
        "OCR_ENABLE_MKLDNN", DEFAULT_OCR_ENABLE_MKLDNN
    ),
    ocr_cpu_threads=_positive_int_from_env(
        "OCR_CPU_THREADS", DEFAULT_OCR_CPU_THREADS
    ),
    ocr_det_model=_optional_text_from_env("OCR_DET_MODEL", DEFAULT_OCR_DET_MODEL),
    ocr_rec_model=_optional_text_from_env("OCR_REC_MODEL", DEFAULT_OCR_REC_MODEL),
    ocr_dpi=_positive_int_from_env("OCR_DPI", DEFAULT_OCR_DPI),
    ocr_min_page_characters=_positive_int_from_env(
        "OCR_MIN_PAGE_CHARACTERS", DEFAULT_OCR_MIN_PAGE_CHARACTERS
    ),
    ocr_max_pages=_positive_int_from_env("OCR_MAX_PAGES", DEFAULT_OCR_MAX_PAGES),
)


class LLMSettings(BaseSettings):
    """Provider-agnostic LLM configuration (Phase 3, Revision 2).

    Values come from the process environment first and a local ``.env`` file
    second, so a developer can keep credentials out of source control. Copy
    ``.env.example`` to ``.env``, then set the base URL, API key and model name for
    whichever OpenAI-compatible provider is used (DeepSeek, OpenAI, Groq, Together,
    OpenRouter, Ollama, vLLM, LM Studio, ...). No code change is required to switch
    provider.

    Attributes:
        llm_base_url: Provider base URL. The OpenAI endpoint is the default because
            it is the canonical OpenAI-compatible surface.
        llm_api_key: Provider credential. Never logged, never returned by the API
            and never written to a record.
        llm_model: Chat model identifier, for example ``deepseek-chat``,
            ``gpt-4o-mini`` or ``llama3.1``. Required before extraction runs.
        llm_timeout_seconds: Per-request timeout in seconds.
        llm_max_input_characters: Safety cap on how many characters of invoice text
            are sent in one request; longer documents are truncated with a warning.
        llm_max_retries: Transport-level retries handed to the chat model.
        llm_structured_method: ``json_mode`` (default), ``json_schema``,
            ``function_calling`` or ``plain``. Unsupported strategies fall back to
            prompt-based JSON parsing automatically.
        llm_provider_label: Label recorded in reports, for example ``deepseek`` or
            ``ollama-local``.
    """

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    llm_base_url: str = DEFAULT_LLM_BASE_URL
    llm_api_key: SecretStr | None = None
    llm_model: str = ""
    llm_timeout_seconds: float = Field(default=DEFAULT_LLM_TIMEOUT_SECONDS, gt=0)
    llm_max_input_characters: int = Field(
        default=DEFAULT_LLM_MAX_INPUT_CHARACTERS, gt=0
    )
    llm_max_retries: int = Field(default=DEFAULT_LLM_MAX_RETRIES, ge=0, le=10)
    llm_structured_method: str = "json_mode"
    llm_provider_label: str = DEFAULT_LLM_PROVIDER_LABEL

    @field_validator("llm_base_url", "llm_model", "llm_provider_label", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> Any:
        """Trim surrounding whitespace so ``" "`` counts as missing, not as a value."""

        return value.strip() if isinstance(value, str) else value

    @field_validator("llm_structured_method", mode="before")
    @classmethod
    def _validate_method(cls, value: Any) -> Any:
        """Normalise the structured-output strategy and reject unknown values."""

        method = str(value).strip().lower() if value is not None else ""
        if not method:
            return "json_mode"
        if method not in LLM_STRUCTURED_METHODS:
            allowed = ", ".join(LLM_STRUCTURED_METHODS)
            raise ValueError(f"llm_structured_method must be one of: {allowed}")
        return method

    @property
    def api_key(self) -> str | None:
        """The API key as plain text, or None when it is missing or blank."""

        if self.llm_api_key is None:
            return None
        value = self.llm_api_key.get_secret_value().strip()
        return value or None

    @property
    def is_configured(self) -> bool:
        """True when nothing required for a request is missing."""

        return not self.missing_settings()

    def missing_settings(self) -> list[str]:
        """Names of the environment variables that still have to be provided."""

        missing = []
        if self.api_key is None:
            missing.append("LLM_API_KEY")
        if not self.llm_model:
            missing.append("LLM_MODEL")
        if not self.llm_base_url:
            missing.append("LLM_BASE_URL")
        return missing

    def describe(self) -> str:
        """A safe one-line summary for diagnostics; never contains the key."""

        key_state = "set" if self.api_key else "missing"
        return (
            f"base_url={self.llm_base_url} model={self.llm_model or '<unset>'} "
            f"timeout={self.llm_timeout_seconds:g}s method={self.llm_structured_method} "
            f"label={self.llm_provider_label} api_key={key_state}"
        )


@lru_cache(maxsize=1)
def get_llm_settings() -> LLMSettings:
    """Return the process-wide LLM settings.

    The result is cached so ``.env`` is read once per process. Tests can call
    ``get_llm_settings.cache_clear()`` after changing the environment.
    """

    return LLMSettings()


# --- Phase 4: VAT rules-engine settings ------------------------------------------

MONETARY_QUANTUM = Decimal("0.01")
"""Rounding quantum for AED amounts; every comparison uses this scale."""


def _decimal_from_env(
    name: str,
    default: Decimal,
    *,
    low: Decimal | None = None,
    high: Decimal | None = None,
) -> Decimal:
    """Read a decimal from the environment, falling back to `default`."""

    raw = os.environ.get(ENV_PREFIX + name)
    if raw is None or not raw.strip():
        return default
    try:
        value = Decimal(raw.strip())
    except (InvalidOperation, ValueError):
        return default
    if low is not None and value < low:
        return default
    if high is not None and value > high:
        return default
    return value


def _list_from_env(name: str) -> list[str]:
    """Read a comma-separated list from the environment."""

    raw = os.environ.get(ENV_PREFIX + name)
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


# --- Phase 8: browser (Next.js UI) access ----------------------------------------

DEFAULT_CORS_ALLOW_ORIGINS: tuple[str, ...] = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://ai-auditor-tax-invoice-ua5q.vercel.app",
)
"""Origins the bundle UI runs on during development (Next.js dev/start server)."""


def get_cors_allow_origins() -> list[str]:
    """Return the origins allowed to call the API from a browser.

    Defaults to the local Next.js UI, because the UI is served from a different
    origin (port 3000) than the API (port 8000) and the browser therefore sends a
    preflight request. Set ``VAT_AUDIT_CORS_ALLOW_ORIGINS`` to a comma-separated
    list to override it, or to ``*`` to allow any origin (development only).

    Credentials are never enabled: this API has no cookies or sessions.
    """

    configured = _list_from_env("CORS_ALLOW_ORIGINS")
    if not configured:
        return list(DEFAULT_CORS_ALLOW_ORIGINS)
    if "*" in configured:
        return ["*"]
    return configured


DEFAULT_MONETARY_TOLERANCE = Decimal("0.01")
"""Default AED tolerance for monetary comparisons (rounded per the rounding policy)."""

DEFAULT_HUMAN_REVIEW_CONFIDENCE_THRESHOLD = Decimal("80")
"""Default confidence below which a finding must go to human review (80%)."""

DEFAULT_RULESET_VERSION = "uae-vat-rules-1.0.0"
"""Version identifier recorded with every audit run."""

DEFAULT_ROUNDING_POLICY = "ROUND_HALF_UP"
ROUNDING_POLICIES = ("ROUND_HALF_UP", "ROUND_HALF_EVEN", "ROUND_DOWN", "ROUND_UP")
"""Explicit rounding policies supported for monetary reconciliation."""

DEFAULT_MAX_AGENT_ITERATIONS = 2
"""Default bound on model/re-extraction passes per audit."""

MAX_AGENT_ITERATIONS_LIMIT = 5
"""Hard ceiling for the bounded agent loop."""


def _rounding_policy_from_env(name: str, default: str) -> str:
    """Read a rounding policy from the environment, ignoring unsupported values."""

    policy = _text_from_env(name, default).upper()
    return policy if policy in ROUNDING_POLICIES else default


class AuditSettings(BaseModel):
    """Configurable VAT rules-engine settings (Phase 4).

    These values make the ruleset authoritative, auditable configuration: they are
    supplied to the auditor, never invented by a model, and the tolerance, review
    threshold and ruleset version are recorded with every audit run.

    Attributes:
        monetary_tolerance: Maximum accepted difference in AED before a monetary
            check fails; comparisons round with :data:`MONETARY_QUANTUM` first.
        human_review_confidence_threshold: Findings below this confidence (0-100)
            must be routed to human review. ``0.80`` as a fraction equals ``80``.
        ruleset_version: Version identifier for the configured rules.
        rounding_policy: One of :data:`ROUNDING_POLICIES`; applied to monetary
            comparisons so results are reproducible.
        requires_supplier_trn: Whether the configured scenario requires a supplier
            TRN on the invoice.
        requires_customer_trn: Whether the configured scenario requires a customer
            TRN. Off by default: a missing customer TRN is not automatically a
            violation.
        disabled_rule_ids: Rule IDs switched off for this deployment.
    """

    monetary_tolerance: Decimal = Field(default=DEFAULT_MONETARY_TOLERANCE, ge=0)
    human_review_confidence_threshold: Decimal = Field(
        default=DEFAULT_HUMAN_REVIEW_CONFIDENCE_THRESHOLD, ge=0, le=100
    )
    ruleset_version: str = DEFAULT_RULESET_VERSION
    rounding_policy: str = DEFAULT_ROUNDING_POLICY
    requires_supplier_trn: bool = True
    requires_customer_trn: bool = False
    disabled_rule_ids: list[str] = Field(default_factory=list)
    agent_max_iterations: int = Field(
        default=DEFAULT_MAX_AGENT_ITERATIONS,
        ge=1,
        le=MAX_AGENT_ITERATIONS_LIMIT,
        description=(
            "Maximum model/re-extraction passes per audit, so the agent loop is bounded."
        ),
    )

    @field_validator("rounding_policy", mode="before")
    @classmethod
    def _validate_rounding_policy(cls, value: Any) -> Any:
        """Normalise the rounding policy and reject unsupported values."""

        policy = str(value).strip().upper() if value is not None else ""
        if not policy:
            return DEFAULT_ROUNDING_POLICY
        if policy not in ROUNDING_POLICIES:
            allowed = ", ".join(ROUNDING_POLICIES)
            raise ValueError(f"rounding_policy must be one of: {allowed}")
        return policy

    @field_validator("ruleset_version", mode="before")
    @classmethod
    def _default_version(cls, value: Any) -> Any:
        return value if isinstance(value, str) and value.strip() else DEFAULT_RULESET_VERSION

    @field_validator("disabled_rule_ids", mode="before")
    @classmethod
    def _normalise_rule_ids(cls, value: Any) -> Any:
        """Accept a list or a comma-separated string and normalise to upper case."""

        if value is None:
            return []
        items = value.split(",") if isinstance(value, str) else list(value)
        normalised: list[str] = []
        for item in items:
            rule_id = str(item).strip().upper()
            if rule_id and rule_id not in normalised:
                normalised.append(rule_id)
        return normalised

    @property
    def rounding_quantum(self) -> Decimal:
        """Scale at which monetary values are rounded before comparison."""

        return MONETARY_QUANTUM

    def describe(self) -> str:
        """A safe one-line summary for diagnostics."""

        disabled = ",".join(self.disabled_rule_ids) or "none"
        return (
            f"ruleset={self.ruleset_version} tolerance={self.monetary_tolerance} "
            f"review_threshold={self.human_review_confidence_threshold} "
            f"rounding={self.rounding_policy} supplier_trn={self.requires_supplier_trn} "
            f"customer_trn={self.requires_customer_trn} disabled={disabled}"
        )


@lru_cache(maxsize=1)
def get_audit_settings() -> AuditSettings:
    """Return the process-wide VAT rules-engine settings.

    Values are read from the environment on first use; tests can call
    ``get_audit_settings.cache_clear()`` after changing the environment.
    """

    return AuditSettings(
        monetary_tolerance=_decimal_from_env(
            "MONETARY_TOLERANCE", DEFAULT_MONETARY_TOLERANCE, low=Decimal("0")
        ),
        human_review_confidence_threshold=_decimal_from_env(
            "HUMAN_REVIEW_CONFIDENCE_THRESHOLD",
            DEFAULT_HUMAN_REVIEW_CONFIDENCE_THRESHOLD,
            low=Decimal("0"),
            high=Decimal("100"),
        ),
        ruleset_version=_text_from_env("RULESET_VERSION", DEFAULT_RULESET_VERSION),
        rounding_policy=_rounding_policy_from_env(
            "ROUNDING_POLICY", DEFAULT_ROUNDING_POLICY
        ),
        requires_supplier_trn=_bool_from_env("REQUIRES_SUPPLIER_TRN", True),
        requires_customer_trn=_bool_from_env("REQUIRES_CUSTOMER_TRN", False),
        disabled_rule_ids=_list_from_env("DISABLED_RULES"),
        agent_max_iterations=min(
            MAX_AGENT_ITERATIONS_LIMIT,
            _positive_int_from_env("MAX_AGENT_ITERATIONS", DEFAULT_MAX_AGENT_ITERATIONS),
        ),
    )
