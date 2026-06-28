"""Офлайн-тесты наблюдаемости затрат LLM: core.usage (накопитель токенов/стоимости),
texts.fmt_balance (рендер бюджета) и agent.openrouter_account.AccountStatus.balance.

Сети нет: usage-объект эмулируем (SDK-pydantic кладёт cost/cached_tokens в .model_extra),
fetch_account не вызываем. Проверяем главное: cost/кэш достаются из доп-полей, агрегация по
ролям и итогу верна, формат не падает на частичных данных."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.usage as U  # noqa: E402
from agent.openrouter_account import AccountStatus  # noqa: E402
from bot import texts  # noqa: E402


def _usage(prompt: int, completion: int, *, cost: float = 0.0, cached: int = 0):
    """Эмуляция response.usage: prompt/completion типизированы у SDK; cost/cached_tokens —
    доп-поля OpenRouter, у pydantic v2 лежат в .model_extra (extract их оттуда и берёт)."""
    details = SimpleNamespace(cached_tokens=cached) if cached else None
    obj = SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        prompt_tokens_details=details,
    )
    obj.model_extra = {"cost": cost}  # cost — доп-поле OpenRouter
    return obj


# ── extract: тянет cost/cached из доп-полей ───────────────────────────────────────
def test_extract_reads_cost_and_cached_from_extra():
    v = U.extract(_usage(100, 20, cost=0.0015, cached=64))
    assert v == {"prompt": 100, "completion": 20, "cached": 64, "cost": 0.0015}


def test_extract_handles_dict_usage():
    v = U.extract({"prompt_tokens": 5, "completion_tokens": 3, "cost": 0.0001})
    assert v["prompt"] == 5 and v["completion"] == 3 and v["cost"] == 0.0001
    assert v["cached"] == 0  # нет prompt_tokens_details → 0


def test_extract_tolerates_none():
    assert U.extract(None) == {"prompt": 0, "completion": 0, "cached": 0, "cost": 0.0}


# ── record + snapshot: агрегация по ролям и итогу ─────────────────────────────────
def test_record_accumulates_by_role_and_total():
    U.reset()
    U.record("parsing", _usage(100, 10, cost=0.001, cached=64))
    U.record("parsing", _usage(200, 20, cost=0.002))
    U.record("copy", _usage(50, 300, cost=0.005))
    snap = U.snapshot()
    assert snap["roles"]["parsing"].calls == 2
    assert snap["roles"]["parsing"].prompt_tokens == 300
    assert snap["roles"]["parsing"].cached_tokens == 64
    total = snap["total"]
    assert total.calls == 3
    assert total.prompt_tokens == 350
    assert total.completion_tokens == 330
    assert abs(total.cost - 0.008) < 1e-9
    U.reset()


def test_record_never_raises_on_garbage():
    U.reset()
    U.record("parsing", object())  # не usage-объект — молча игнор, без исключения
    snap = U.snapshot()
    # object() даёт prompt=0/cost=0 → запись есть, но нулевая (наблюдаемость не роняет путь)
    assert snap["total"].cost == 0.0
    U.reset()


# ── AccountStatus.balance ─────────────────────────────────────────────────────────
def test_balance_computed_from_credits():
    st = AccountStatus(total_credits=10.0, total_usage=3.25)
    assert st.balance == 6.75


def test_balance_none_when_credits_unavailable():
    st = AccountStatus(key_usage=0.5)  # только /key ответил
    assert st.balance is None


# ── fmt_balance: не падает на частичных/пустых данных, показывает что есть ─────────
def test_fmt_balance_full():
    U.reset()
    U.record("parsing", _usage(1000, 100, cost=0.0021, cached=640))
    st = AccountStatus(total_credits=10.0, total_usage=4.0, limit_remaining=6.0)
    out = texts.fmt_balance(st, U.snapshot())
    assert "Остаток" in out and "$6.00" in out
    assert "Потрачено" in out
    assert "Из кэша" in out  # cached_tokens > 0 → строка про кэш-экономию
    assert "парсинг" in out
    U.reset()


def test_fmt_balance_only_key_endpoint():
    # /credits недоступен (нужен management-ключ) → показываем траты по ключу, не падаем
    st = AccountStatus(key_usage=0.1234, is_free_tier=True)
    out = texts.fmt_balance(st, {"total": None, "roles": {}})
    assert "Потрачено ключом" in out
    assert "free" in out
    assert "вызовов модели ещё не было" in out


def test_fmt_balance_empty_account():
    st = AccountStatus()  # ничего не пришло
    out = texts.fmt_balance(st, {"total": None, "roles": {}})
    assert "недоступны" in out  # честная пометка, не пустой экран
