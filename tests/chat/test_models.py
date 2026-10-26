from types import SimpleNamespace

import pytest

from app.chat import models


class FakeChatOpenAI:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


class FakeChatGoogleGenerativeAI:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


class FakeChatAnthropic:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


class FakeFallbackCapableModel:
    def __init__(self) -> None:
        self.fallbacks: list[object] | None = None

    def with_fallbacks(self, fallbacks: list[object]):
        self.fallbacks = fallbacks
        return self


def model_settings(**overrides: object) -> SimpleNamespace:
    values = {
        "openai_model_name": "gpt-5-mini",
        "openai_api_key": "test-openai-key",
        "openai_base_url": "https://gateway.example.com/v1",
        "openai_timeout_seconds": 18,
        "anthropic_model_name": "claude-haiku-4",
        "anthropic_api_key": "test-anthropic-key",
        "anthropic_base_url": "https://gateway.example.com",
        "anthropic_timeout_seconds": 40,
        "gemini_model_name": "gemini-2.5-flash",
        "gemini_api_key": "test-gemini-key",
        "gemini_timeout_seconds": 20,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_openai_model_uses_configured_timeout_without_sdk_retry(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        models,
        "settings",
        model_settings(),
    )
    monkeypatch.setattr(models, "ChatOpenAI", FakeChatOpenAI)

    model = models.build_openai_model()

    assert model.kwargs == {
        "model": "gpt-5-mini",
        "base_url": "https://gateway.example.com/v1",
        "api_key": "test-openai-key",
        "timeout": 18,
        "max_retries": 0,
    }


def test_gemini_model_is_built_only_when_explicitly_selected(monkeypatch) -> None:
    monkeypatch.setattr(models, "settings", model_settings())
    monkeypatch.setattr(models, "ChatGoogleGenerativeAI", FakeChatGoogleGenerativeAI)

    model = models.build_model("gemini")

    assert model.kwargs == {
        "model": "gemini-2.5-flash",
        "temperature": 0,
        "api_key": "test-gemini-key",
        "request_timeout": 20,
        "retries": 0,
    }


def test_gemini_model_requires_api_key(monkeypatch) -> None:
    monkeypatch.setattr(models, "settings", model_settings(gemini_api_key=""))

    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        models.build_model("gemini")


def test_anthropic_model_requires_max_tokens_and_gateway_url(monkeypatch) -> None:
    monkeypatch.setattr(models, "settings", model_settings())
    monkeypatch.setattr(models, "ChatAnthropic", FakeChatAnthropic)

    model = models.build_model("anthropic")

    assert model.kwargs == {
        "model": "claude-haiku-4",
        "base_url": "https://gateway.example.com",
        "api_key": "test-anthropic-key",
        "max_tokens": 2048,
        "timeout": 40,
        "max_retries": 0,
    }


def test_anthropic_model_requires_api_key(monkeypatch) -> None:
    monkeypatch.setattr(models, "settings", model_settings(anthropic_api_key=""))

    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        models.build_model("anthropic")


def test_openai_model_falls_back_to_anthropic_then_gemini(monkeypatch) -> None:
    anthropic_model = object()
    gemini_model = object()
    primary_model = FakeFallbackCapableModel()
    monkeypatch.setattr(models, "settings", model_settings())
    monkeypatch.setattr(models, "build_openai_model", lambda: primary_model)
    monkeypatch.setattr(models, "build_anthropic_model", lambda: anthropic_model)
    monkeypatch.setattr(models, "build_gemini_model", lambda: gemini_model)

    model = models.build_openai_fallback_model()

    assert model is primary_model
    assert primary_model.fallbacks == [anthropic_model, gemini_model]


def test_openai_model_skips_providers_without_keys(monkeypatch) -> None:
    anthropic_model = object()
    primary_model = FakeFallbackCapableModel()
    monkeypatch.setattr(models, "settings", model_settings(gemini_api_key=""))
    monkeypatch.setattr(models, "build_openai_model", lambda: primary_model)
    monkeypatch.setattr(models, "build_anthropic_model", lambda: anthropic_model)

    models.build_openai_fallback_model()

    assert primary_model.fallbacks == [anthropic_model]


def test_openai_model_without_fallback_keys_has_no_fallback(monkeypatch) -> None:
    primary_model = object()
    monkeypatch.setattr(
        models,
        "settings",
        model_settings(gemini_api_key="", anthropic_api_key=""),
    )
    monkeypatch.setattr(models, "build_openai_model", lambda: primary_model)

    assert models.build_openai_fallback_model() is primary_model
