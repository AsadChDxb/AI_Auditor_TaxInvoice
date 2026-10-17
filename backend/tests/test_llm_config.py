"""Unit tests for provider-agnostic LLM configuration (Phase 3, Revision 2)."""

import pytest
from pydantic import ValidationError

from app.config import (
    DEFAULT_LLM_BASE_URL,
    DEFAULT_LLM_MAX_INPUT_CHARACTERS,
    DEFAULT_LLM_MAX_RETRIES,
    DEFAULT_LLM_PROVIDER_LABEL,
    DEFAULT_LLM_TIMEOUT_SECONDS,
    LLMSettings,
    get_llm_settings,
)

ENV_VARIABLES = (
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_TIMEOUT_SECONDS",
    "LLM_MAX_INPUT_CHARACTERS",
    "LLM_MAX_RETRIES",
    "LLM_STRUCTURED_METHOD",
    "LLM_PROVIDER_LABEL",
)


@pytest.fixture()
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove any LLM variables so defaults can be asserted."""

    for name in ENV_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    get_llm_settings.cache_clear()
    yield
    get_llm_settings.cache_clear()


def test_defaults_are_provider_neutral(clean_environment: None) -> None:
    settings = LLMSettings(_env_file=None)

    assert settings.llm_base_url == "https://api.openai.com/v1"
    assert settings.llm_model == ""
    assert settings.llm_timeout_seconds == 60.0
    assert settings.llm_max_input_characters == 60_000
    assert settings.llm_max_retries == 2
    assert settings.llm_structured_method == "json_mode"
    assert settings.llm_provider_label == "openai-compatible"

    assert DEFAULT_LLM_BASE_URL == "https://api.openai.com/v1"
    assert DEFAULT_LLM_TIMEOUT_SECONDS == 60.0
    assert DEFAULT_LLM_MAX_INPUT_CHARACTERS == 60_000
    assert DEFAULT_LLM_MAX_RETRIES == 2
    assert DEFAULT_LLM_PROVIDER_LABEL == "openai-compatible"


def test_unconfigured_by_default_and_reports_missing_variables(
    clean_environment: None,
) -> None:
    settings = LLMSettings(_env_file=None)

    assert settings.api_key is None
    assert settings.llm_api_key is None
    assert settings.is_configured is False
    assert settings.missing_settings() == ["LLM_API_KEY", "LLM_MODEL"]
    assert "api_key=missing" in settings.describe()
    assert "<unset>" in settings.describe()


def test_environment_variables_control_provider_key_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_API_KEY", "sk-from-environment")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("LLM_MAX_INPUT_CHARACTERS", "1000")
    monkeypatch.setenv("LLM_MAX_RETRIES", "4")
    monkeypatch.setenv("LLM_PROVIDER_LABEL", "deepseek")

    settings = LLMSettings(_env_file=None)

    assert settings.api_key == "sk-from-environment"
    assert settings.is_configured is True
    assert settings.missing_settings() == []
    assert settings.llm_base_url == "https://api.deepseek.com/v1"
    assert settings.llm_model == "deepseek-chat"
    assert settings.llm_timeout_seconds == 12.5
    assert settings.llm_max_input_characters == 1000
    assert settings.llm_max_retries == 4
    assert settings.llm_provider_label == "deepseek"
    assert "api_key=set" in settings.describe()


@pytest.mark.parametrize(
    ("base_url", "model", "label"),
    [
        ("https://api.openai.com/v1", "gpt-4o-mini", "openai"),
        ("https://api.deepseek.com/v1", "deepseek-chat", "deepseek"),
        ("https://api.groq.com/openai/v1", "llama-3.1-70b", "groq"),
        ("http://localhost:11434/v1", "llama3.1", "ollama-local"),
        ("https://openrouter.ai/api/v1", "deepseek/deepseek-chat", "openrouter"),
    ],
)
def test_switching_provider_needs_only_environment_values(
    base_url: str, model: str, label: str
) -> None:
    """Any OpenAI-compatible provider is reachable by configuration alone."""

    settings = LLMSettings(
        _env_file=None,
        llm_base_url=base_url,
        llm_model=model,
        llm_api_key="k",
        llm_provider_label=label,
    )

    assert settings.llm_base_url == base_url
    assert settings.llm_model == model
    assert settings.llm_provider_label == label
    assert settings.is_configured is True


def test_dotenv_file_supplies_values_when_present(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_API_KEY=sk-from-dotenv\n"
        "LLM_MODEL=model-from-file\n"
        "LLM_BASE_URL=http://localhost:11434/v1\n",
        encoding="utf-8",
    )

    settings = LLMSettings(_env_file=env_file)

    assert settings.api_key == "sk-from-dotenv"
    assert settings.llm_model == "model-from-file"
    assert settings.llm_base_url == "http://localhost:11434/v1"


def test_blank_values_count_as_missing(
    clean_environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_API_KEY", "   ")
    monkeypatch.setenv("LLM_MODEL", "  ")

    settings = LLMSettings(_env_file=None)

    assert settings.api_key is None
    assert settings.is_configured is False
    assert settings.missing_settings() == ["LLM_API_KEY", "LLM_MODEL"]


def test_secret_value_is_never_in_repr_or_description() -> None:
    settings = LLMSettings(_env_file=None, llm_api_key="sk-super-secret-value")

    assert "sk-super-secret-value" not in repr(settings)
    assert "sk-super-secret-value" not in str(settings)
    assert "sk-super-secret-value" not in settings.describe()
    assert settings.api_key == "sk-super-secret-value"


def test_structured_method_is_validated_and_normalised(clean_environment: None) -> None:
    normalised = LLMSettings(_env_file=None, llm_structured_method="PLAIN")
    defaulted = LLMSettings(_env_file=None, llm_structured_method="")
    explicit = LLMSettings(_env_file=None, llm_structured_method="json_schema")

    assert normalised.llm_structured_method == "plain"
    assert defaulted.llm_structured_method == "json_mode"
    assert explicit.llm_structured_method == "json_schema"

    with pytest.raises(ValidationError):
        LLMSettings(_env_file=None, llm_structured_method="magic")


def test_invalid_numeric_settings_are_rejected(clean_environment: None) -> None:
    with pytest.raises(ValidationError):
        LLMSettings(_env_file=None, llm_timeout_seconds=0)
    with pytest.raises(ValidationError):
        LLMSettings(_env_file=None, llm_max_input_characters=-1)
    with pytest.raises(ValidationError):
        LLMSettings(_env_file=None, llm_max_retries=99)


def test_no_vendor_specific_llm_module_or_class_exists() -> None:
    """Revision 2: the LLM layer must not be tied to one vendor."""

    import importlib.util
    from pathlib import Path

    import app.services.llm as llm_module

    assert importlib.util.find_spec("app.services.deepseek_client") is None
    assert importlib.util.find_spec("app.services.llm") is not None

    source = Path(llm_module.__file__).read_text(encoding="utf-8")
    assert "DeepSeekClient" not in source
    assert "deepseek_client" not in source

    filenames = " ".join(path.name for path in Path(llm_module.__file__).parent.iterdir())
    assert "deepseek" not in filenames.lower()


def test_get_llm_settings_is_cached_and_reloadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_MODEL", "model-first")
    get_llm_settings.cache_clear()

    first = get_llm_settings()
    assert first.llm_model == "model-first"

    monkeypatch.setenv("LLM_MODEL", "model-second")
    assert get_llm_settings() is first

    get_llm_settings.cache_clear()
    assert get_llm_settings().llm_model == "model-second"
    get_llm_settings.cache_clear()

