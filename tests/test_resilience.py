"""Офлайн-тесты core.resilience: таймаут + ретрай транзиентных ошибок, без сети/живого SDK.

Проверяем КЛАССИФИКАЦИЮ (что ретраим, что нет) — это денежно-критично:
- Google Ads: транзиентный код (RATE_EXCEEDED…) ретраится; auth/неизвестный — НЕТ; таймаут — НЕТ.
- OpenRouter: RateLimitError ретраится; AuthenticationError — НЕТ.
Фейки ошибок собираем через __new__ (без сети/настоящих ответов).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.resilience as R  # noqa: E402


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    """Сводим backoff к нулю — тесты не должны реально спать."""
    monkeypatch.setattr(R, "ADS_WAIT_MULTIPLIER", 0.0)
    monkeypatch.setattr(R, "ADS_WAIT_MAX", 0.0)
    monkeypatch.setattr(R, "LLM_WAIT_MULTIPLIER", 0.0)
    monkeypatch.setattr(R, "LLM_WAIT_MAX", 0.0)


def _mk_ads(name: str):
    """GoogleAdsException с одним error_code.name (дакт-фейк через __new__)."""
    from google.ads.googleads.errors import GoogleAdsException

    ex = GoogleAdsException.__new__(GoogleAdsException)
    ex.failure = SimpleNamespace(errors=[SimpleNamespace(error_code=SimpleNamespace(name=name))])
    ex.args = (name,)
    return ex


def _mk_openai(cls_name: str):
    import openai

    cls = getattr(openai, cls_name)
    e = cls.__new__(cls)
    e.args = (cls_name,)
    return e


# ── Google Ads: транзиентное ретраится ───────────────────────────────────────────
async def test_run_ads_call_retries_transient_then_succeeds():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _mk_ads("RATE_EXCEEDED")
        return {"applied": True}

    res = await R.run_ads_call(fn)
    assert res == {"applied": True}
    assert calls["n"] == 3  # 2 транзиентных отказа + успех


async def test_run_ads_call_does_not_retry_auth_error():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _mk_ads("AUTHENTICATION_ERROR")

    with pytest.raises(Exception):  # noqa: B017 — GoogleAdsException пере-выброшен как есть
        await R.run_ads_call(fn)
    assert calls["n"] == 1  # auth НЕ ретраится


async def test_run_ads_call_does_not_retry_unknown_code():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _mk_ads("SOME_WEIRD_UNCLASSIFIED_CODE")

    with pytest.raises(Exception):  # noqa: B017
        await R.run_ads_call(fn)
    assert calls["n"] == 1  # неизвестное на денежном пути — не повторяем


async def test_run_ads_call_timeout_is_not_retried(monkeypatch):
    monkeypatch.setattr(R, "ADS_TIMEOUT_S", 0.05)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        time.sleep(0.4)  # дольше таймаута → asyncio.timeout прервёт await
        return {"applied": True}

    with pytest.raises(TimeoutError):
        await R.run_ads_call(fn)
    assert calls["n"] == 1  # запрос мог дойти → не повторяем (golden rule: не идемпотентно)


# ── OpenRouter (LLM): rate-limit ретраится, auth — нет ───────────────────────────
async def test_call_llm_retries_rate_limit_then_succeeds():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if calls["n"] < 2:
            raise _mk_openai("RateLimitError")
        return "ok"

    res = await R.call_llm(factory)
    assert res == "ok"
    assert calls["n"] == 2


async def test_call_llm_does_not_retry_auth_error():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        raise _mk_openai("AuthenticationError")

    with pytest.raises(Exception):  # noqa: B017 — AuthenticationError пере-выброшен
        await R.call_llm(factory)
    assert calls["n"] == 1
