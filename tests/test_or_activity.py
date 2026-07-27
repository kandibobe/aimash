"""core.or_activity: ридер трат OpenRouter (#10). Мокаем HTTP через httpx.MockTransport (seam
client=), без сети. Проверяем парсинг /key и /activity, fail-soft на 403, opt-in без ключей."""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from core import or_activity
from core.config import settings

_KEY_JSON = {
    "data": {
        "usage": 12.5,
        "usage_daily": 3.25,
        "usage_weekly": 8.0,
        "usage_monthly": 12.5,
        "limit": 50.0,
        "limit_remaining": 37.5,
        "limit_reset": "daily",
        "is_free_tier": False,
    }
}

_ACTIVITY_JSON = {
    "data": [
        {
            "byok_usage_inference": 0.012,
            "completion_tokens": 125,
            "date": "2025-08-24",
            "endpoint_id": "550e8400-e29b-41d4-a716-446655440000",
            "model": "openai/gpt-4.1",
            "model_permaslug": "openai/gpt-4.1-2025-04-14",
            "prompt_tokens": 50,
            "provider_name": "OpenAI",
            "reasoning_tokens": 25,
            "requests": 5,
            "usage": 0.015,
        },
        {"date": "2025-08-24", "model": None},  # мусор без модели — отбрасывается
    ]
}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://x")


async def test_fetch_key_usage_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "openrouter_api_key", SecretStr("inf-key"))

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/key")
        assert req.headers["Authorization"] == "Bearer inf-key"
        return httpx.Response(200, json=_KEY_JSON)

    async with _client(handler) as c:
        usage = await or_activity.fetch_key_usage(client=c)
        cost = await or_activity.fetch_daily_cost_usd(client=c)

    assert usage is not None
    assert usage.usage_daily == 3.25
    assert usage.limit_reset == "daily"
    assert usage.limit_remaining == 37.5
    assert cost == 3.25  # живой дневной cap читает именно usage_daily


async def test_fetch_key_usage_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "openrouter_api_key", SecretStr(""))
    # Без ключа — ни одного HTTP-вызова, None (fail-soft).
    assert await or_activity.fetch_key_usage() is None
    assert await or_activity.fetch_daily_cost_usd() is None


async def test_fetch_activity_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "openrouter_provisioning_key", SecretStr("mgmt-key"))
    monkeypatch.setattr(settings, "openrouter_key_hash", "")

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/activity")
        assert req.headers["Authorization"] == "Bearer mgmt-key"
        return httpx.Response(200, json=_ACTIVITY_JSON)

    async with _client(handler) as c:
        rows = await or_activity.fetch_activity(date="2025-08-24", client=c)

    assert len(rows) == 1  # мусорная строка без модели отброшена
    r = rows[0]
    assert r.model == "openai/gpt-4.1"
    assert r.usage == 0.015
    assert r.prompt_tokens == 50
    assert r.completion_tokens == 125
    assert r.provider_name == "OpenAI"


async def test_fetch_activity_opt_in_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "openrouter_provisioning_key", SecretStr(""))

    def handler(req: httpx.Request) -> httpx.Response:  # не должен вызваться
        raise AssertionError("HTTP не должен вызываться без provisioning-ключа")

    async with _client(handler) as c:
        assert await or_activity.fetch_activity(client=c) == []


async def test_fetch_activity_403_fail_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "openrouter_provisioning_key", SecretStr("inference-not-mgmt"))
    monkeypatch.setattr(settings, "openrouter_key_hash", "")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "management key required"}})

    async with _client(handler) as c:
        # 403 (обычный ключ на /activity) — fail-soft: пустой список, не исключение.
        assert await or_activity.fetch_activity(client=c) == []
