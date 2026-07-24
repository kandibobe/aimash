"""Рантайм-переключатель модели ИИ (/model): выбор активной модели в agent.router.

Чистые юнит-тесты — без сети: проверяем только логику выбора модели (override > роль-дефолт).
"""

from __future__ import annotations

import pytest

from agent import router


@pytest.fixture(autouse=True)
def _reset_active_model():
    """Изолируем глобальный override между тестами (модуль-стейт процесса)."""
    saved = router.get_active_model()
    router.set_active_model(None)
    yield
    router.set_active_model(saved)


def test_default_effective_model_is_role_default():
    assert router.get_active_model() is None
    assert router.effective_model("parsing") == router.ROLE_MODELS["parsing"]
    assert router.effective_model("copy") == router.ROLE_MODELS["copy"]


def test_override_applies_to_all_roles():
    router.set_active_model("openai/gpt-4o-mini")
    assert router.get_active_model() == "openai/gpt-4o-mini"
    assert router.effective_model("parsing") == "openai/gpt-4o-mini"
    assert router.effective_model("copy") == "openai/gpt-4o-mini"
    assert router.effective_model("fallback") == "openai/gpt-4o-mini"


def test_reset_restores_role_defaults():
    router.set_active_model("openai/gpt-4o")
    router.set_active_model(None)
    assert router.get_active_model() is None
    assert router.effective_model("parsing") == router.ROLE_MODELS["parsing"]


def test_blank_override_is_treated_as_none():
    router.set_active_model("   ")
    assert router.get_active_model() is None
    assert router.effective_model("parsing") == router.ROLE_MODELS["parsing"]


def test_model_choices_nonempty_with_vendor_prefix():
    assert router.MODEL_CHOICES
    assert all("/" in m for m in router.MODEL_CHOICES)


def test_valid_model_slug_accepts_and_rejects():
    from bot.main import _valid_model_slug

    assert _valid_model_slug("anthropic/claude-sonnet-4.6") == "anthropic/claude-sonnet-4.6"
    assert _valid_model_slug("  openai/gpt-4o  ") == "openai/gpt-4o"
    assert _valid_model_slug("noslash") is None
    assert _valid_model_slug("has space/model") is None
    assert _valid_model_slug("") is None
    assert _valid_model_slug("x/" + "y" * 200) is None
