"""A4/A5: деградация router.chat на резервную модель + запрет параллельных tool-calls.

A4: транзиентный отказ ОСНОВНОЙ модели (rate-limit/timeout/5xx после ретраев) → ОДНА попытка на
    ROLE_MODELS['fallback'], если она отлична. Нетранзиентная ошибка (BadRequest/схема) — НЕ
    фолбэчим (та же ошибка). Раньше роль fallback была объявлена, но не вызывалась нигде.
A5: для роли parsing с tools передаётся parallel_tool_calls=False (одно действие за вызов).

call_llm подменяется passthrough (без tenacity-backoff), _client — фейком, чей create по модели
решает: бросить или вернуть ответ. Сеть/OpenRouter не трогаются.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent.router as R  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


async def _passthrough(factory, label=None, circuit=None):
    """Замена core.resilience.call_llm: без ретраев/бэкоффа — просто выполнить фабрику один раз.

    Сигнатура повторяет настоящую (включая `circuit` — имя цепи размыкателя) намеренно: дубль,
    принимающий `**kwargs`, молча проглотил бы рассинхрон с оригиналом."""
    return await factory()


def _resp(text: str):
    """Фейк ответа OpenAI SDK: .choices[0].message + .usage (для core.usage.record)."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=None))],
        usage=None,
    )


class _FakeCompletions:
    def __init__(self, behavior, captured):
        self._behavior = behavior  # model_slug -> callable() -> resp | raises
        self._captured = captured

    async def create(self, **kwargs):
        self._captured.append(kwargs)
        model = kwargs["model"]
        return self._behavior(model)


class _FakeClient:
    def __init__(self, behavior, captured):
        self.chat = SimpleNamespace(completions=_FakeCompletions(behavior, captured))


@contextmanager
def _router_env(behavior, captured, *, chosen="primary/model", fallback="fallback/model"):
    """Подменить _client (фейк по поведению), call_llm (passthrough) и роли (primary/fallback)."""
    with (
        patched(R, "call_llm", _passthrough),
        patched(R, "_client", lambda: _FakeClient(behavior, captured)),
        patched(R, "_active_model", None),
        patched(R, "ROLE_MODELS", {**R.ROLE_MODELS, "parsing": chosen, "fallback": fallback}),
    ):
        yield


async def test_transient_primary_failure_falls_back():
    captured: list[dict] = []

    def behavior(model):
        if model == "primary/model":
            raise TimeoutError("primary недоступна")  # _is_retryable_llm ловит TimeoutError
        return _resp("ok from fallback")

    with _router_env(behavior, captured):
        msg = await R.chat([{"role": "user", "content": "hi"}], role="parsing")
    assert msg.content == "ok from fallback"
    used = [k["model"] for k in captured]
    assert used == ["primary/model", "fallback/model"]  # сначала primary, потом fallback


async def test_nontransient_failure_does_not_fall_back():
    captured: list[dict] = []

    def behavior(model):
        raise ValueError("bad request / invalid schema")  # НЕ транзиентная

    with _router_env(behavior, captured):
        with pytest.raises(ValueError):
            await R.chat([{"role": "user", "content": "hi"}], role="parsing")
    assert [k["model"] for k in captured] == ["primary/model"]  # fallback НЕ пробовали


async def test_no_fallback_when_same_model():
    """Если fallback == основной модели — второй попытки нет (иначе бессмысленный повтор)."""
    captured: list[dict] = []

    def behavior(model):
        raise TimeoutError("boom")

    with _router_env(behavior, captured, chosen="same/model", fallback="same/model"):
        with pytest.raises(TimeoutError):
            await R.chat([{"role": "user", "content": "hi"}], role="parsing")
    assert [k["model"] for k in captured] == ["same/model"]  # ровно одна попытка


async def test_parsing_with_tools_disables_parallel_tool_calls():
    captured: list[dict] = []

    def behavior(model):
        return _resp("ok")

    tools = [{"type": "function", "function": {"name": "noop", "parameters": {}}}]
    with _router_env(behavior, captured):
        await R.chat([{"role": "user", "content": "hi"}], role="parsing", tools=tools)
    assert captured[0].get("parallel_tool_calls") is False


async def test_copy_role_does_not_set_parallel_tool_calls():
    """parallel_tool_calls гасим только на parsing (денежный путь). copy — не трогаем."""
    captured: list[dict] = []

    def behavior(model):
        return _resp("ok")

    tools = [{"type": "function", "function": {"name": "noop", "parameters": {}}}]
    with _router_env(behavior, captured):
        await R.chat([{"role": "user", "content": "hi"}], role="copy", tools=tools)
    assert "parallel_tool_calls" not in captured[0]


# ── Гард sampling-параметров: Opus 4.7+/Sonnet 5/Fable 5 отклоняют temperature (400) ──
def test_omits_sampling_by_slug():
    """«Строгие» семейства (точка ИЛИ дефис в версии) — да; deepseek/sonnet-4.6 — нет."""
    assert R._omits_sampling("anthropic/claude-opus-4.8")
    assert R._omits_sampling("anthropic/claude-opus-4-8")
    assert R._omits_sampling("anthropic/claude-sonnet-5")
    assert R._omits_sampling("anthropic/claude-fable-5")
    assert not R._omits_sampling("deepseek/deepseek-chat")
    assert not R._omits_sampling("anthropic/claude-sonnet-4.6")
    assert not R._omits_sampling("")


async def test_temperature_stripped_for_strict_model():
    """Opus 4.8 отклоняет temperature на нативном API → chat НЕ кладёт его в payload (по слагу)."""
    captured: list[dict] = []
    with _router_env(
        lambda m: _resp("ok"), captured, chosen="anthropic/claude-opus-4.8", fallback="fb/model"
    ):
        await R.chat([{"role": "user", "content": "hi"}], role="parsing", temperature=0.4)
    assert "temperature" not in captured[0]


async def test_temperature_sent_for_lenient_model():
    """Дешёвая модель (deepseek) temperature ПРИНИМАЕТ → передаём как есть."""
    captured: list[dict] = []
    with _router_env(
        lambda m: _resp("ok"), captured, chosen="deepseek/deepseek-chat", fallback="fb/model"
    ):
        await R.chat([{"role": "user", "content": "hi"}], role="parsing", temperature=0.4)
    assert captured[0].get("temperature") == 0.4


async def test_temperature_guard_is_per_model_on_fallback():
    """Ключевое: гард решает ПО ФАКТИЧЕСКОМУ слагу — primary(lenient) получил temperature, а
    fallback(strict opus) после сбоя — уже без него (иначе fallback на opus ронял бы 400)."""
    captured: list[dict] = []

    def behavior(model):
        if model == "deepseek/deepseek-chat":
            raise TimeoutError("boom")  # транзиентно → уходим на fallback
        return _resp("ok")

    with _router_env(
        behavior,
        captured,
        chosen="deepseek/deepseek-chat",
        fallback="anthropic/claude-opus-4.8",
    ):
        await R.chat([{"role": "user", "content": "hi"}], role="parsing", temperature=0.3)
    assert captured[0].get("temperature") == 0.3  # primary (lenient) — с temperature
    assert "temperature" not in captured[1]  # fallback (strict opus) — без temperature
