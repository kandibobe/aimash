"""Офлайн-тесты agent/loop.py (ТЗ §4 AI Agent Core): русская команда → правильный исход.

LLM (router.chat) подменяется заглушкой. Проверяем ветвление handle_command:
clarify / proposal (mutation, НЕ исполняется) / read-intent (rsa/keywords) / текстовый фолбэк /
валидация аргументов В КОДЕ / устойчивость к мусорному tool-call. SDK/сеть не трогаются.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent.loop as L  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


def _tc(name: str, args: dict):
    """Фейк tool_call в форме OpenAI SDK: .function.name / .function.arguments(JSON-строка)."""
    return SimpleNamespace(
        function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False))
    )


def _msg(tool_calls=None, content: str = ""):
    return SimpleNamespace(tool_calls=tool_calls, content=content)


def _chat_returning(*msgs):
    """Заглушка router.chat: на i-й вызов отдаёт msgs[i] (последний повторяется)."""
    calls = {"n": 0}

    async def _chat(messages, **kwargs):
        i = min(calls["n"], len(msgs) - 1)
        calls["n"] += 1
        return msgs[i]

    return _chat, calls


# ── clarify: неоднозначная команда → вопрос, не угадывание ────────────────────────
async def test_ask_clarification():
    fake, _ = _chat_returning(_msg([_tc("ask_clarification", {"question": "Какую кампанию?"})]))
    with patched(L, "chat", fake):
        out = await L.handle_command("повысь бюджет")
    assert out["type"] == "clarify" and out["question"] == "Какую кампанию?"


# ── mutation → proposal (черновик, НЕ исполнен, без user_initiated) ───────────────
async def test_mutation_returns_unexecuted_proposal():
    args = {"campaign": "Brand", "mode": "increase_by_percent", "value": 20}
    fake, _ = _chat_returning(_msg([_tc("update_budget", args)]))
    with patched(L, "chat", fake):
        out = await L.handle_command("повысь бюджет Brand на 20%", chat_id=42)
    assert out["type"] == "proposal" and out["operation"] == "update_budget"
    assert out["params"]["value"] == 20 and out["confirmation_id"]
    # провенанс «прямая команда человека» проставляет бот, НЕ агент про себя (fail-closed)
    assert "user_initiated" not in out["params"]


# ── валидацию диапазонов считает КОД (не доверяем модели) ─────────────────────────
async def test_invalid_args_rejected_in_code():
    # value=0 нарушает Field(gt=0) → ValidationError → текст, а не proposal
    fake, _ = _chat_returning(
        _msg([_tc("update_budget", {"campaign": "X", "mode": "set_to", "value": 0})])
    )
    with patched(L, "chat", fake):
        out = await L.handle_command("установи бюджет X в 0")
    assert out["type"] == "text" and "некорректные аргументы" in out["text"]


# ── read-intent: генерация RSA / подбор ключей → намерение боту (исполняет он) ────
async def test_generate_rsa_intent():
    fake, _ = _chat_returning(_msg([_tc("generate_rsa", {"topic": "доставка цветов"})]))
    with patched(L, "chat", fake):
        out = await L.handle_command("придумай тексты для доставки цветов")
    assert out["type"] == "rsa_intent" and out["brief"]["topic"] == "доставка цветов"


async def test_keyword_research_intent():
    fake, _ = _chat_returning(
        _msg([_tc("keyword_research", {"seeds": ["цветы"], "language": "ru"})])
    )
    with patched(L, "chat", fake):
        out = await L.handle_command("подбери ключи по слову цветы")
    assert out["type"] == "keywords_intent" and out["brief"]["seeds"] == ["цветы"]


# ── нет tool-call и нет текста → одна повторная попытка → фолбэк-текст ─────────────
async def test_empty_response_retries_then_text_fallback():
    fake, calls = _chat_returning(_msg(None, ""), _msg(None, ""))
    with patched(L, "chat", fake):
        out = await L.handle_command("абракадабра")
    assert out["type"] == "text"
    assert calls["n"] == 2  # был ровно один повтор


# ── устойчивость к мусору: битый JSON аргументов и неизвестный инструмент ──────────
async def test_bad_tool_arguments_json():
    bad = SimpleNamespace(function=SimpleNamespace(name="update_budget", arguments="{не json"))
    fake, _ = _chat_returning(_msg([bad]))
    with patched(L, "chat", fake):
        out = await L.handle_command("...")
    assert out["type"] == "text" and "аргументы" in out["text"]


async def test_unknown_tool_name():
    fake, _ = _chat_returning(_msg([_tc("teleport", {})]))
    with patched(L, "chat", fake):
        out = await L.handle_command("...")
    assert out["type"] == "text" and "неизвестный инструмент" in out["text"]


# ── 2D: get_stats резолвит аккаунт (не молчаливый allowed[0]) ─────────────────────
def _fake_stats_env(monkeypatch, seen: dict):
    """Подменить чтение SDK: фиксируем, какой cid реально читается."""
    import ads.read as ar
    from ads.client import DRAFT_ACCOUNT_ID  # noqa: F401
    from types import SimpleNamespace as NS

    def _stats(client, cid, days):
        seen["cid"] = cid
        return NS(impressions=1, clicks=1, cost=1.0, conversions=0.0, conv_value=0.0)

    monkeypatch.setattr(ar, "account_stats", _stats)
    monkeypatch.setattr(ar, "account_currency", lambda client, cid: "USD")

    async def _client(cid=None):
        seen["client_cid"] = cid
        return object()

    import ads.client as ac

    monkeypatch.setattr(ac, "build_client_async", _client)


async def test_get_stats_honors_account_argument(monkeypatch):
    """NL «статистика аккаунта X» читает ИМЕННО X (id нормализован), а не первый разрешённый."""
    from core.config import settings

    extra = "6764040266"
    monkeypatch.setattr(settings, "account_access_mode", "legacy")
    monkeypatch.setattr(settings, "google_ads_allowed_customer_ids", "7753643025")
    monkeypatch.setattr(settings, "google_ads_read_customer_ids", extra)
    seen: dict = {}
    _fake_stats_env(monkeypatch, seen)
    fake, _ = _chat_returning(_msg([_tc("get_stats", {"account": "676-404-0266"})]))
    with patched(L, "chat", fake):
        out = await L.handle_command("покажи статистику аккаунта 676-404-0266", chat_id=42)
    assert out["type"] == "read" and out["account"] == extra
    assert seen["cid"] == extra and seen["client_cid"] == extra  # per-account клиент


async def test_get_stats_denied_account_refuses(monkeypatch):
    """Запрещённый аккаунт → внятный отказ (НЕ подмена другим аккаунтом)."""
    from core.config import settings

    monkeypatch.setattr(settings, "google_ads_allowed_customer_ids", "7753643025")
    monkeypatch.setattr(settings, "google_ads_read_customer_ids", "")
    seen: dict = {}
    _fake_stats_env(monkeypatch, seen)
    fake, _ = _chat_returning(_msg([_tc("get_stats", {"account": "9998887776"})]))
    with patched(L, "chat", fake):
        out = await L.handle_command("статистика 9998887776", chat_id=42)
    assert out["type"] == "text"
    assert "cid" not in seen  # чтение не выполнялось


async def test_get_stats_defaults_to_chat_active_account(monkeypatch):
    """Без аргумента account → активный аккаунт чата (Draft по умолчанию)."""
    from core.config import settings
    from db.session import init_db

    await init_db()  # get_active_account читает user_settings
    monkeypatch.setattr(settings, "google_ads_allowed_customer_ids", "7753643025")
    seen: dict = {}
    _fake_stats_env(monkeypatch, seen)
    fake, _ = _chat_returning(_msg([_tc("get_stats", {})]))
    with patched(L, "chat", fake):
        out = await L.handle_command("покажи статистику", chat_id=43)
    assert out["type"] == "read" and out["account"] == "7753643025"


async def test_get_stats_nonnumeric_period_days_coerces_gracefully(monkeypatch):
    """P0-B3: модель прислала нечисловой period_days → не крэш (мимо try в глобальный обработчик),
    а graceful read с дефолтом 30 дней (клампится)."""
    from core.config import settings
    from db.session import init_db

    await init_db()
    monkeypatch.setattr(settings, "google_ads_allowed_customer_ids", "7753643025")
    seen: dict = {}
    _fake_stats_env(monkeypatch, seen)
    fake, _ = _chat_returning(_msg([_tc("get_stats", {"period_days": "last month"})]))
    with patched(L, "chat", fake):
        out = await L.handle_command("покажи статистику за прошлый месяц", chat_id=44)
    assert out["type"] == "read" and out["days"] == 30  # дефолт, без ValueError


async def test_get_stats_ambiguous_name_asks(monkeypatch):
    """Имя, матчащее несколько дочерних → уточнение (LookupError → text), не угадывание."""
    from ads.client import set_discovered_read_children, set_discovered_read_children_meta
    from ads.read import ChildAccount
    from core.config import settings

    monkeypatch.setattr(settings, "account_access_mode", "legacy")
    monkeypatch.setattr(settings, "google_ads_allowed_customer_ids", "7753643025")
    set_discovered_read_children(["1111111111", "2222222222"])
    set_discovered_read_children_meta(
        [
            ChildAccount(
                id="1111111111",
                name="Kasi A",
                currency="USD",
                manager=False,
                level=1,
                status="ENABLED",
            ),
            ChildAccount(
                id="2222222222",
                name="Kasi B",
                currency="USD",
                manager=False,
                level=1,
                status="ENABLED",
            ),
        ]
    )
    try:
        seen: dict = {}
        _fake_stats_env(monkeypatch, seen)
        fake, _ = _chat_returning(_msg([_tc("get_stats", {"account": "Kasi"})]))
        with patched(L, "chat", fake):
            out = await L.handle_command("статистика Kasi", chat_id=44)
        assert out["type"] == "text" and "accounts" in out["text"].lower() or "Kasi" in out["text"]
        assert "cid" not in seen
    finally:
        set_discovered_read_children([])
        set_discovered_read_children_meta([])


# ── C1-C3 (гибрид): контекст диалога + подстановка местоимения-кампании ──────────────
def test_is_pronoun_campaign_detection():
    # местоимения/пусто → True; реальные имена → False
    assert L._is_pronoun_campaign("")
    assert L._is_pronoun_campaign("эта кампания")
    assert L._is_pronoun_campaign("этой кампании")
    assert L._is_pronoun_campaign("текущую кампанию")
    assert L._is_pronoun_campaign("this campaign")
    assert L._is_pronoun_campaign("эту")
    assert not L._is_pronoun_campaign("Brand Search")
    assert not L._is_pronoun_campaign("Текущая акция")  # реальное имя, нет слова «кампания»
    assert not L._is_pronoun_campaign(
        "Летняя кампания 2026"
    )  # имя со словом «кампания», без демонстратива


async def test_pronoun_campaign_resolved_from_context():
    # «поставь на паузу ЭТУ кампанию» + контекст → реальное имя в черновике (скрин из живого теста)
    fake, _ = _chat_returning(_msg([_tc("pause_campaign", {"campaign": "этой кампании"})]))
    ctx = {"last_campaign": "Deep Lake Immersion", "last_account": "", "history": []}
    with patched(L, "chat", fake):
        out = await L.handle_command("поставь на паузу эту кампанию", chat_id=1, context=ctx)
    assert out["type"] == "proposal"
    assert out["params"]["campaign"] == "Deep Lake Immersion"


async def test_real_campaign_name_not_overwritten_by_context():
    fake, _ = _chat_returning(_msg([_tc("pause_campaign", {"campaign": "Brand Search"})]))
    ctx = {"last_campaign": "Deep Lake", "history": []}
    with patched(L, "chat", fake):
        out = await L.handle_command("пауза Brand Search", chat_id=1, context=ctx)
    assert out["params"]["campaign"] == "Brand Search"  # реальное имя не трогаем


async def test_no_context_leaves_pronoun_as_is():
    # без контекста подставлять нечего → буквальный текст (ниже сработает обычный резолв/ошибка)
    fake, _ = _chat_returning(_msg([_tc("pause_campaign", {"campaign": "этой кампании"})]))
    with patched(L, "chat", fake):
        out = await L.handle_command("пауза этой кампании", chat_id=1)
    assert out["params"]["campaign"] == "этой кампании"


def test_conversation_context_block():
    block = L._conversation_context_block(
        {"last_campaign": "Deep Lake", "history": ["сделай отчёт"]}
    )
    assert block and "Deep Lake" in block and "КОНТЕКСТ ДИАЛОГА" in block
    assert L._conversation_context_block(None) is None
    assert L._conversation_context_block({}) is None


async def test_context_block_passed_to_model():
    captured = {}

    async def _chat(messages, **kwargs):
        captured["messages"] = messages
        return _msg([_tc("ask_clarification", {"question": "?"})])

    ctx = {"last_campaign": "Deep Lake", "history": ["сделай отчёт"]}
    with patched(L, "chat", _chat):
        await L.handle_command("измени гео", chat_id=1, context=ctx)
    joined = " ".join(m["content"] for m in captured["messages"])
    assert "Deep Lake" in joined and "КОНТЕКСТ ДИАЛОГА" in joined
