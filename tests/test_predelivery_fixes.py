"""Предсдаточный аудит: тесты на исправленные баги (B1–B15).

Каждый тест закрывает КЛАСС бага гардом (self-heal/ассерт), а не разовой заплаткой:
- B1  откат composite-создания кампании при сбое шага 3/4 (не оставляем мусорную PAUSED-кампанию);
- B2  кратность денежных величин минимальной биллинг-единице (10 000 micros);
- B5  фильтр ❌-строк и валидация сырых ключей из Google Sheets;
- B6  тип соответствия ключей — подтверждённый на Этапе 1, не хардкод phrase;
- B7  постраничный пикер аккаунтов (100+ дочерних не роняют inline-клавиатуру);
- B12 charset display path (замена не может внести пробел/слэш);
- B14 /cancel сворачивает активный визард;
- B15 дедуп фоновых краулов по customer_id.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ads.mutations as mut  # noqa: E402
import bot.main as bm  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from db.session import init_db  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


# ── B2: кратность денежных величин минимальной биллинг-единице (10 000 micros) ────
def test_round_micros_snaps_to_billing_unit():
    from ads.mutations import _round_micros as rm_mut
    from ads.read import _round_micros as rm_read

    for rm in (rm_mut, rm_read):
        assert rm(123_456) == 120_000  # CPC «по аналогии» из cost/clicks → кратно
        assert rm(126_000) == 130_000
        assert rm(40_000_000) == 40_000_000  # уже кратно — без изменений
        assert rm(5_000) == 10_000  # положительное < единицы → одна единица (не 0)
        assert rm(0) == 0
        assert rm(-1) == -1  # валидируется выше, не трогаем
        assert rm(987_654) % 10_000 == 0


# ── B1: сбой шага 3/4 composite-создания откатывает бюджет+кампанию(+группу) ──────
def _results(rn: str):
    return SimpleNamespace(results=[SimpleNamespace(resource_name=rn)])


class _RBService:
    def __init__(self, name: str, parent: "_RollbackClient"):
        self.name = name
        self.p = parent

    def _rec(self, method: str, operations):
        op = operations[0]
        kind = "remove" if isinstance(getattr(op, "remove", None), str) else "create"
        self.p.calls.append((method, kind))

    def mutate_campaign_budgets(self, *, customer_id, operations):
        self._rec("budget", operations)
        return _results("customers/x/campaignBudgets/1")

    def mutate_campaigns(self, *, customer_id, operations):
        self._rec("campaign", operations)
        return _results("customers/x/campaigns/2")

    def mutate_ad_groups(self, *, customer_id, operations):
        self._rec("adgroup", operations)
        if self.p.fail_step == "adgroup":
            raise RuntimeError("BID_TOO_MANY_FRACTIONAL_DIGITS")
        return _results("customers/x/adGroups/3")

    def mutate_ad_group_ads(self, *, customer_id, operations):
        self._rec("ad", operations)
        if self.p.fail_step == "ad":
            raise RuntimeError("PATH_HAS_SLASH")
        return _results("customers/x/adGroupAds/4")


class _RollbackClient:
    """Мини-фейк google-ads client для _create_search_campaign_via_sdk. enums — MagicMock (любая
    цепочка атрибутов), get_type — MagicMock (create/remove присваиваются)."""

    def __init__(self, fail_step: str):
        self.fail_step = fail_step
        self.calls: list[tuple[str, str]] = []
        self.enums = MagicMock()

    def get_type(self, name):
        return MagicMock()

    def get_service(self, name):
        return _RBService(name, self)


def _run_sdk(client):
    return mut._create_search_campaign_via_sdk(
        client,
        DRAFT_ACCOUNT_ID,
        campaign_name="Test",
        final_url="https://x/",
        headlines=["a", "b", "c"],
        descriptions=["d", "e"],
        budget_micros=40_000_000,
        keywords=[],
        match_type="phrase",
        cpc_bid_micros=123_456,
    )


def test_composite_rollback_on_adgroup_failure():
    client = _RollbackClient(fail_step="adgroup")
    with (
        patched(mut, "_downgrade_bidding_if_no_conversions", lambda c, cid, b: (b, None)),
        patched(mut, "_apply_bidding_on_create", lambda c, camp, b: None),
    ):
        with pytest.raises(RuntimeError):
            _run_sdk(client)
    # шаг 3 упал → откат: бюджет+кампания удалены (группа не создалась → не удаляется)
    assert ("budget", "create") in client.calls
    assert ("campaign", "create") in client.calls
    assert ("campaign", "remove") in client.calls
    assert ("budget", "remove") in client.calls


def test_composite_rollback_on_rsa_failure():
    client = _RollbackClient(fail_step="ad")
    with (
        patched(mut, "_downgrade_bidding_if_no_conversions", lambda c, cid, b: (b, None)),
        patched(mut, "_apply_bidding_on_create", lambda c, camp, b: None),
    ):
        with pytest.raises(RuntimeError):
            _run_sdk(client)
    # шаг 4 (RSA) упал → откат группы+кампании+бюджета
    assert ("adgroup", "remove") in client.calls
    assert ("campaign", "remove") in client.calls
    assert ("budget", "remove") in client.calls


# ── B12: charset display path — замена не может внести пробел/слэш ────────────────
def test_display_path_charset_guard():
    from agent.campaign_edit import _replace_kind_ok, apply_text_replace

    assert _replace_kind_ok("Nairobi", "path") is True
    assert _replace_kind_ok("avto/kenya", "path") is False  # слэш
    assert _replace_kind_ok("used cars", "path") is False  # пробел
    assert _replace_kind_ok("a" * 16, "path") is False  # длина > 15
    # apply_text_replace НЕ трогает path, если результат невалиден (откат)
    state = {"ad": {"path1": "avto", "path2": "", "headlines": [], "descriptions": []}}
    n = apply_text_replace(state, "avto", "avto/kenya")
    assert n == 0 and state["ad"]["path1"] == "avto"


# ── B5: read_keyword_column пропускает ❌-строки и учитывает колонку E ─────────────
class _FakeSheetsSvc:
    def __init__(self, values):
        self._values = values

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, **kw):
        return self

    def execute(self):
        return {"values": self._values}


def test_read_keyword_column_skips_irrelevant():
    from reports.sheets import read_keyword_column

    values = [
        ["Keyword", "Avg. searches", "Competition", "Top-of-page bid", "Релевантность"],
        ["used cars nairobi", "12100", "MEDIUM", "$0.14", "✅ Релевантно"],
        ["car rental nairobi", "9900", "HIGH", "$0.20", "❌ Нерелевантно"],
        ["cheap cars"],  # нет колонки релевантности → берём
    ]
    out = read_keyword_column("id", service=_FakeSheetsSvc(values))
    assert "used cars nairobi" in out
    assert "cheap cars" in out
    assert "car rental nairobi" not in out  # ❌ отфильтровано (safety-net)


# ── B5: _cc_sanitize_keywords отбрасывает невалидные сырые ключи ──────────────────
def test_cc_sanitize_keywords_drops_invalid():
    long_kw = " ".join(["word"] * 20)  # >10 слов → assert_keyword_ok отбрасывает
    clean, dropped = bm._cc_sanitize_keywords(["used cars", long_kw, "buy toyota"])
    assert "used cars" in clean and "buy toyota" in clean
    assert long_kw not in clean
    assert dropped == 1


# ── B6: тип соответствия по умолчанию — из подтверждённых настроек, не хардкод ────
def test_cc_default_match_type_from_settings():
    d_exact = SimpleNamespace(wizard_state={"settings": {"match_type": "exact"}})
    d_empty = SimpleNamespace(wizard_state={"settings": {}})
    assert bm._cc_default_match_type(d_exact) == "exact"
    assert bm._cc_default_match_type(d_empty) == "phrase"  # DEFAULT_MATCH_TYPE


# ── B7: постраничный пикер аккаунтов — 120 детей не роняют клавиатуру ─────────────
def test_cc_accounts_kb_paginates():
    from bot.keyboards import _ACCT_PAGE, cc_accounts_kb

    rows = [SimpleNamespace(id=str(i), name=f"Account {i}") for i in range(120)]
    mk = cc_accounts_kb(rows, page=0)
    buttons = [b for row in mk.inline_keyboard for b in row]
    acct_btns = [b for b in buttons if b.text.startswith("🏢")]
    assert len(acct_btns) == _ACCT_PAGE  # на странице ровно _ACCT_PAGE аккаунтов
    assert any(b.text == "›" for b in buttons)  # есть кнопка «вперёд»
    assert len(buttons) < 20  # с запасом под лимит Telegram (не 120 кнопок)
    # последняя страница: есть «‹», нет «›»
    last = (120 + _ACCT_PAGE - 1) // _ACCT_PAGE - 1
    mk_last = cc_accounts_kb(rows, page=last)
    texts = [b.text for row in mk_last.inline_keyboard for b in row]
    assert "‹" in texts and "›" not in texts


# ── B14: /cancel (через _abandon_active_flow) сворачивает активный визард ──────────
class _FakeState:
    def __init__(self, data: dict, state_val):
        self._d = dict(data)
        self._s = state_val

    async def get_data(self):
        return dict(self._d)

    async def get_state(self):
        return self._s

    async def clear(self):
        self._d = {}
        self._s = None

    async def update_data(self, **kw):
        self._d.update(kw)


@pytest.mark.asyncio
async def test_abandon_active_flow_marks_draft_abandoned():
    await init_db()
    chat = 99120
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    st = _FakeState({"cc_session": sid}, "CreateCampaignWizard:final")
    assert await bm._abandon_active_flow(chat, st) is True
    snap = await bm.CDRAFTS.get(sid)
    assert snap.status == "abandoned"


@pytest.mark.asyncio
async def test_abandon_active_flow_false_when_idle():
    chat = 99121
    st = _FakeState({}, None)
    assert await bm._abandon_active_flow(chat, st) is False  # нет визарда → падаем в reject-ветку


# ── B15: дедуп фоновых краулов по customer_id ────────────────────────────────────
@pytest.mark.asyncio
async def test_spawn_crawl_dedup_by_customer():
    ev = asyncio.Event()

    async def fake_run(bot, chat_id, customer_id, url, *, mode="full"):
        await ev.wait()

    bm._CRAWL_INFLIGHT.clear()
    try:
        with patched(bm, "_run_client_crawl", fake_run):
            first = bm._spawn_crawl(None, 1, "777", "https://x")
            second = bm._spawn_crawl(None, 1, "777", "https://x")  # тот же аккаунт — не плодим
            assert first is True
            assert second is False
            other = bm._spawn_crawl(None, 1, "888", "https://y")  # другой аккаунт — можно
            assert other is True
    finally:
        ev.set()
        for t in list(bm._CRAWL_INFLIGHT.values()):
            t.cancel()
        await asyncio.gather(*bm._CRAWL_INFLIGHT.values(), return_exceptions=True)
        bm._CRAWL_INFLIGHT.clear()
