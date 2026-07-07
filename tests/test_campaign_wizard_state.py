"""§19: персист черновика визарда «Создание кампании» + чистота confirm-гейта на Этапах 0–1.

- CampaignDraftStore: round-trip, возобновление после «рестарта» (новый инстанс стора),
  гард владения (чужой chat_id), abandon, очистка просроченных.
- Хендлеры Этапа 1 (cc_settings_desc → cc_accept): черновик копится в БД, но НИ ОДНОГО proposal
  не минтуется (мутация только на финальном «Создать черновик», вне Phase 1).
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from ads.read import AccountMedians  # noqa: E402
from agent.campaign_settings import CampaignSettings  # noqa: E402
from bot.callbacks import CcCB  # noqa: E402
from bot.campaign_wizard.store import CampaignDraftStore  # noqa: E402
from db.models import Proposal  # noqa: E402
from db.session import Session, init_db  # noqa: E402
from scheduler.jobs import cleanup_stale_campaign_drafts  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


class FakeMessage:
    def __init__(self, text: str = "", chat_id: int = 100):
        self.text = text
        self.chat = type("C", (), {"id": chat_id})()
        self.answers: list = []
        self.edits: list = []

    async def answer(self, text: str = "", **kw):
        self.answers.append((text, kw))
        return self

    async def edit_text(self, text: str = "", **kw):
        self.edits.append((text, kw))
        return self


class FakeCallbackQuery:
    def __init__(self, message, uid: int = 100):
        self.message = message
        self.from_user = type("U", (), {"id": uid})()
        self.answers: list = []

    async def answer(self, text: str = "", show_alert: bool = False, **kw):
        self.answers.append((text, show_alert))


class FakeFSM:
    def __init__(self, data: dict | None = None):
        self._d = dict(data or {})

    async def get_data(self):
        return dict(self._d)

    async def update_data(self, **kw):
        self._d.update(kw)

    async def set_state(self, *a, **k):
        pass

    async def clear(self):
        self._d = {}


# ── Стор: round-trip, возобновление после рестарта, владение ──────────────────────
@pytest.mark.asyncio
async def test_store_roundtrip_and_restart_resume():
    await init_db()
    chat = 7700001
    st = CampaignDraftStore()
    sid = await st.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    await st.patch(sid, lambda s: s["settings"].__setitem__("campaign_name", "Кения · Авто"))
    await st.set_step(sid, 1)
    # «рестарт»: совершенно новый инстанс стора (in-memory FSM был бы потерян, БД — нет)
    st2 = CampaignDraftStore()
    snap = await st2.get_active(chat)
    assert snap is not None
    assert snap.current_step == 1
    assert snap.wizard_state["settings"]["campaign_name"] == "Кения · Авто"


@pytest.mark.asyncio
async def test_store_ownership_guard():
    await init_db()
    chat = 7700002
    st = CampaignDraftStore()
    sid = await st.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    assert await st.get(sid, expected_chat_id=chat + 1) is None  # чужой чат не видит
    assert await st.patch(sid, lambda s: None, expected_chat_id=chat + 1) is None


@pytest.mark.asyncio
async def test_store_create_supersedes_prior_active():
    await init_db()
    chat = 7700003
    st = CampaignDraftStore()
    sid1 = await st.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    sid2 = await st.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)  # гасит sid1
    active = await st.get_active(chat)
    assert active is not None and active.session_id == sid2
    old = await st.get(sid1)
    assert old is not None and old.status == "abandoned"


@pytest.mark.asyncio
async def test_patch_setstep_refuse_inactive_draft():
    await init_db()
    chat = 7700009
    st = CampaignDraftStore()
    sid = await st.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    await st.abandon(sid)  # черновик больше не active (поздний колбэк не должен его воскрешать)
    assert await st.patch(sid, lambda s: s["settings"].__setitem__("x", 1)) is None
    assert await st.set_step(sid, 3) is None
    snap = await st.get(sid)  # get читает любой статус
    assert snap is not None and snap.status == "abandoned" and snap.current_step == 0


@pytest.mark.asyncio
async def test_cleanup_marks_stale_active_abandoned():
    await init_db()
    chat = 7700004
    st = CampaignDraftStore()
    sid = await st.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    # ttl=0 + now в будущем → cutoff после updated_at → черновик просрочен
    future = datetime.now(timezone.utc) + timedelta(days=2)
    n = await cleanup_stale_campaign_drafts(now=future, ttl_hours=0)
    assert n >= 1
    snap = await st.get(sid)
    assert snap is not None and snap.status == "abandoned"


# ── Хендлеры Этапа 1: накопление черновика БЕЗ минтинга proposal (чистота гейта) ──
async def _count_proposals(chat_id: int) -> int:
    async with Session() as s:
        return int(
            (
                await s.execute(
                    select(func.count()).select_from(Proposal).where(Proposal.chat_id == chat_id)
                )
            ).scalar_one()
        )


@pytest.mark.asyncio
async def test_stage1_accumulates_draft_without_proposal():
    await init_db()
    chat = 7700005
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    await bm.CDRAFTS.set_step(sid, 1)
    fsm = FakeFSM({"cc_session": sid})

    async def _fake_extract(text, *, language="ru"):
        return CampaignSettings(geo_locations=["Кения"], goal="calls", budget_daily_units=40)

    async def _fake_medians(customer_id):
        return AccountMedians(None, None, None)

    msg = FakeMessage("Кампания на Кению, б/у авто, $40, звонки", chat_id=chat)
    with (
        patched(bm, "extract_campaign_settings", _fake_extract),
        patched(bm, "_cc_read_medians", _fake_medians),
    ):
        await bm.cc_settings_desc(msg, fsm)

    snap = await bm.CDRAFTS.get(sid)
    assert snap.wizard_state["settings"]["geo_locations"] == ["Кения"]
    assert snap.wizard_state["settings"]["bidding_strategy"] == "maximize_conversions"
    assert await _count_proposals(chat) == 0  # НИ одного proposal на Этапе 1

    # принятие настроек: курсор продвигается (≥2; в текущей сборке 1→Этап 3), по-прежнему без proposal
    cq = FakeCallbackQuery(FakeMessage(chat_id=chat))
    await bm.cc_accept(cq, CcCB(action="accept", sub="settings"), fsm)
    snap2 = await bm.CDRAFTS.get(sid)
    assert snap2.current_step >= 2
    assert await _count_proposals(chat) == 0


@pytest.mark.asyncio
async def test_stage1_edit_patches_settings():
    await init_db()
    chat = 7700006
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    await bm.CDRAFTS.patch(
        sid,
        lambda s: s.__setitem__(
            "settings",
            {
                "budget_daily_micros": 40_000_000,
                "by_analogy": ["budget_daily_micros"],
                "by_default": ["budget_daily_micros"],
            },
        ),
    )
    fsm = FakeFSM({"cc_session": sid})

    async def _fake_extract(text, *, language="ru"):
        return CampaignSettings(budget_daily_units=60)  # «поставь бюджет 60»

    msg = FakeMessage("поставь бюджет 60", chat_id=chat)
    with patched(bm, "extract_campaign_settings", _fake_extract):
        await bm.cc_settings_edit(msg, fsm)

    snap = await bm.CDRAFTS.get(sid)
    assert snap.wizard_state["settings"]["budget_daily_micros"] == 60_000_000
    # поле теперь задано пользователем → ушло из ОБОИХ тегов источника
    assert "budget_daily_micros" not in snap.wizard_state["settings"]["by_analogy"]
    assert "budget_daily_micros" not in snap.wizard_state["settings"]["by_default"]
    assert await _count_proposals(chat) == 0


def test_draft_select_compiles_for_update_on_postgres():
    """1F8: мутирующие пути стора берут SELECT … FOR UPDATE (row-lock на Postgres против
    lost update при параллельных патчах); без флага FOR UPDATE отсутствует. На SQLite
    FOR UPDATE молча игнорируется — поэтому проверяем КОМПИЛЯЦИЮ на postgresql-диалекте."""
    from sqlalchemy.dialects import postgresql

    from bot.campaign_wizard.store import _draft_select

    with_lock = str(
        _draft_select("sid", 1, active_only=True, for_update=True).compile(
            dialect=postgresql.dialect()
        )
    )
    without_lock = str(
        _draft_select("sid", 1, active_only=True).compile(dialect=postgresql.dialect())
    )
    assert "FOR UPDATE" in with_lock
    assert "FOR UPDATE" not in without_lock


@pytest.mark.asyncio
async def test_stage1_edit_unrecognized_answers_not_understood_and_keeps_settings():
    """W2 (живой тест 2026-07-06): нераспознанная правка НЕ ре-рендерит молча ту же карточку —
    настройки байт-в-байт, явный ответ «не понял», сводка не перерисовывается."""
    await init_db()
    chat = 7700016
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    settings0 = {
        "campaign_name": "Уганда · авто · Search",
        "budget_daily_micros": 1_900_000_000,
        "cpc_bid_micros": 500_000,
        "currency": "JPY",
        "by_analogy": [],
        "by_default": ["cpc_bid_micros"],
    }
    await bm.CDRAFTS.patch(sid, lambda s: s.__setitem__("settings", dict(settings0)))
    fsm = FakeFSM({"cc_session": sid})

    async def _empty_extract(text, *, language="ru"):
        return CampaignSettings()  # LLM ничего не извлёк

    msg = FakeMessage("Максимальная цена за клик 75 йен", chat_id=chat)
    with patched(bm, "extract_campaign_settings", _empty_extract):
        await bm.cc_settings_edit(msg, fsm)

    snap = await bm.CDRAFTS.get(sid)
    assert snap.wizard_state["settings"] == settings0  # ничего не перетёрто
    not_understood = bm.i18n.t("cc_settings_edit_not_understood")
    assert any(not_understood == t for t, _ in msg.answers)  # честный отказ
    # сводка-карточка НЕ перерисована (иначе правка «выглядит применённой»)
    assert not any("Кампания (черновик)" in t for t, _ in msg.answers)


@pytest.mark.asyncio
async def test_stage1_edit_max_cpc_updates_draft():
    """W1: «максимальная цена за клик 75» через хендлер Этапа 1 реально меняет черновик."""
    await init_db()
    chat = 7700017
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    await bm.CDRAFTS.patch(
        sid,
        lambda s: s.__setitem__(
            "settings",
            {"cpc_bid_micros": 500_000, "by_analogy": [], "by_default": ["cpc_bid_micros"]},
        ),
    )
    fsm = FakeFSM({"cc_session": sid})

    async def _cpc_extract(text, *, language="ru"):
        return CampaignSettings(max_cpc_units=75)

    msg = FakeMessage("максимальная цена за клик 75", chat_id=chat)
    with patched(bm, "extract_campaign_settings", _cpc_extract):
        await bm.cc_settings_edit(msg, fsm)

    snap = await bm.CDRAFTS.get(sid)
    assert snap.wizard_state["settings"]["cpc_bid_micros"] == 75_000_000
    assert "cpc_bid_micros" not in snap.wizard_state["settings"]["by_default"]
    assert await _count_proposals(chat) == 0  # правка черновика — НЕ мутация
