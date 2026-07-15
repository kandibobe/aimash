"""Тесты маршрутизации inline-колбэков bot.main: /campaigns пауза/возобновление (только
черновик, без исполнения), RSA-курация, легаси ok:-колбэк. Реальный ConfirmStore/SessionStore
на temp SQLite (conftest); сеть/SDK не трогаем.
"""

from __future__ import annotations

import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from bot.callbacks import AudienceCB, CampCB, RsaCB  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
from db.session import init_db  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


class FakeMessage:
    def __init__(self, chat_id: int = 100, bot=None):
        self.chat = type("C", (), {"id": chat_id})()
        self.bot = bot
        self.answers: list = []
        self.edits: list = []

    async def answer(self, text: str = "", **kw):
        self.answers.append((text, kw))
        return self

    async def edit_text(self, text: str = "", **kw):
        self.edits.append((text, kw))
        return self


class FakeCallbackQuery:
    def __init__(self, message, data: str = "", uid: int = 100):
        self.message = message
        self.data = data
        self.from_user = type("U", (), {"id": uid})()
        self.answers: list = []

    async def answer(self, text: str = "", show_alert: bool = False, **kw):
        self.answers.append((text, show_alert))


# ── /campaigns: кнопка пауза создаёт ТОЛЬКО черновик (исполнение — после ✅) ───────
async def test_camp_pause_creates_pending_proposal_only():
    await init_db()
    chat_id = 201
    bm._CAMP_CACHE[chat_id] = [{"name": "BrandX", "status": "ENABLED"}]
    cq = FakeCallbackQuery(FakeMessage(chat_id=chat_id))
    await bm.camp_pause(cq, CampCB(action="pause", idx=0))

    cid = bm._LAST_PENDING.get(chat_id)
    assert cid is not None
    snap = await ConfirmStore().get_confirmed(cid)
    assert snap.operation == "pause_campaign"
    assert snap.status == "pending"  # НЕ исполнено — ждёт ✅
    assert snap.user_initiated is True


async def test_camp_resume_creates_pending_proposal():
    await init_db()
    chat_id = 202
    bm._CAMP_CACHE[chat_id] = [{"name": "BrandY", "status": "PAUSED"}]
    cq = FakeCallbackQuery(FakeMessage(chat_id=chat_id))
    await bm.camp_resume(cq, CampCB(action="resume", idx=0))
    snap = await ConfirmStore().get_confirmed(bm._LAST_PENDING[chat_id])
    assert snap.operation == "resume_campaign" and snap.status == "pending"


async def test_camp_mutate_stale_cache_alerts_no_proposal():
    await init_db()
    chat_id = 203
    bm._CAMP_CACHE.pop(chat_id, None)
    bm._LAST_PENDING.pop(chat_id, None)
    cq = FakeCallbackQuery(FakeMessage(chat_id=chat_id))
    await bm.camp_pause(cq, CampCB(action="pause", idx=0))
    assert cq.answers and cq.answers[-1][1] is True  # show_alert: список устарел
    assert bm._LAST_PENDING.get(chat_id) is None  # черновик не создан


# ── §3 Аудитории: выбор аудитории создаёт ТОЛЬКО черновик attach_audience ──────────
async def test_audience_pick_creates_pending_attach_proposal():
    await init_db()
    from ads.read import Audience

    chat_id = 208
    bm._CAMP_CACHE[chat_id] = [{"name": "BrandZ", "status": "ENABLED"}]
    bm._AUD_CACHE[chat_id] = [
        Audience(resource_name="customers/1/userLists/55", name="Покупатели", size=1500)
    ]
    cq = FakeCallbackQuery(FakeMessage(chat_id=chat_id))
    await bm.on_audience_pick(cq, AudienceCB(action="pick", camp_idx=0, idx=0))

    cid = bm._LAST_PENDING.get(chat_id)
    assert cid is not None
    snap = await ConfirmStore().get_confirmed(cid)
    assert snap.operation == "attach_audience"
    assert snap.status == "pending"  # НЕ исполнено — ждёт ✅
    assert snap.user_initiated is True
    assert snap.params["campaign"] == "BrandZ"
    assert snap.params["audience_resource_names"] == ["customers/1/userLists/55"]
    assert "Покупатели" in snap.summary  # дружелюбное имя в сводке


async def test_audience_pick_stale_cache_alerts_no_proposal():
    await init_db()
    chat_id = 209
    bm._CAMP_CACHE.pop(chat_id, None)
    bm._AUD_CACHE.pop(chat_id, None)
    bm._LAST_PENDING.pop(chat_id, None)
    cq = FakeCallbackQuery(FakeMessage(chat_id=chat_id))
    await bm.on_audience_pick(cq, AudienceCB(action="pick", camp_idx=0, idx=0))
    assert cq.answers and cq.answers[-1][1] is True  # show_alert: список устарел
    assert bm._LAST_PENDING.get(chat_id) is None  # черновик не создан


# ── §3 /newsearch: бриф → RSA → ТОЛЬКО черновик create_search_campaign ─────────────
class FakeState:
    """Минимальный FSMContext для офлайн-теста визарда (search_brief зовёт лишь clear())."""

    def __init__(self):
        self.cleared = False
        self.state = None

    async def clear(self):
        self.cleared = True
        self.state = None

    async def set_state(self, s):
        self.state = s


async def test_newsearch_brief_creates_pending_search_proposal():
    await init_db()
    from types import SimpleNamespace

    chat_id = 210
    msg = FakeMessage(chat_id=chat_id)
    msg.text = "Цветы | https://flowers.ua | 300 | доставка цветов | роза, букет роз"

    async def fake_gen(brief):  # без LLM — отдаём валидный RSA-набор
        return SimpleNamespace(
            headlines=[f"Заголовок {i}" for i in range(3)],
            descriptions=[f"Описание объявления {i}" for i in range(2)],
        )

    with patched(bm, "_generate_rsa", fake_gen):
        await bm.search_brief(msg, FakeState())

    cid = bm._LAST_PENDING.get(chat_id)
    assert cid is not None
    snap = await ConfirmStore().get_confirmed(cid)
    assert snap.operation == "create_search_campaign"
    assert snap.status == "pending"  # НЕ исполнено — ждёт ✅
    assert snap.user_initiated is True
    assert snap.params["campaign_name"] == "Цветы"
    assert snap.params["final_url"] == "https://flowers.ua"
    assert snap.params["budget_daily_micros"] == 300_000_000  # 300 единиц * 1e6
    assert "роза" in snap.params["keywords"]
    assert "Цветы" in snap.summary


async def test_newsearch_bad_brief_no_proposal():
    await init_db()
    chat_id = 211
    bm._LAST_PENDING.pop(chat_id, None)
    msg = FakeMessage(chat_id=chat_id)
    msg.text = "без разделителей и url"  # неверный формат
    state = FakeState()
    await bm.search_brief(msg, state)
    assert bm._LAST_PENDING.get(chat_id) is None  # черновик не создан
    assert state.cleared is False  # остаёмся в состоянии — ждём корректный бриф


# ── RSA-курация ──────────────────────────────────────────────────────────────────
async def _make_session(chat_id: int):
    return await bm.SESSIONS.create(
        chat_id=chat_id,
        customer_id=DRAFT_ACCOUNT_ID,
        campaign="C",
        ad_group_id="1",
        ad_group_name="AG",
        final_url="https://example.com",
        headlines=["заголовок", "второй", "третий"],
        descriptions=["описание один", "описание два"],
        brief={},
    )


async def test_rsa_approve_sets_state_and_rerenders():
    await init_db()
    sid = await _make_session(204)
    cq = FakeCallbackQuery(FakeMessage(chat_id=204))
    await bm.rsa_approve(cq, RsaCB(action="approve", cid=sid, kind="h", idx=0))
    assert cq.answers and cq.answers[-1][0] == "Одобрено"
    assert cq.message.edits  # шаг курации перерисован в том же сообщении


async def test_rsa_finalize_below_min_alerts():
    await init_db()
    sid = await _make_session(205)  # свежая сессия — 0 одобренных
    cq = FakeCallbackQuery(FakeMessage(chat_id=205))
    await bm.rsa_finalize(cq, RsaCB(action="finalize", cid=sid), FakeState())
    assert cq.answers and cq.answers[-1][1] is True  # show_alert: ниже минимума
    assert bm._LAST_PENDING.get(205) is None  # create_rsa-черновик НЕ создан


async def test_rsa_session_stale_alerts():
    await init_db()
    cq = FakeCallbackQuery(FakeMessage(chat_id=206))
    await bm.rsa_approve(cq, RsaCB(action="approve", cid="no-such-session", kind="h", idx=0))
    assert cq.answers and cq.answers[-1][1] is True  # show_alert: сессия устарела


# ── Легаси ok:-колбэк маршрутизируется в confirm-путь ─────────────────────────────
async def test_legacy_ok_callback_routes_to_confirm():
    await init_db()
    cid = uuid.uuid4().hex
    await bm.STORE.save_proposal(
        confirmation_id=cid,
        operation="resume_campaign",
        customer_id=DRAFT_ACCOUNT_ID,
        params={"campaign": "X"},
        summary="resume X",
        chat_id=207,
        user_initiated=True,
    )

    async def fake_exec(s, c):
        assert await s.claim(c, operation="resume_campaign") is not None
        await s.finalize(c, result={"applied": True})
        return {"applied": True}

    cq = FakeCallbackQuery(FakeMessage(chat_id=207), data=f"ok:{cid}")
    with patched(bm, "execute_confirmed", fake_exec):
        await bm.on_confirm_legacy(cq)
    assert (await ConfirmStore().get_confirmed(cid)).status == "applied"


# ── §UX-память: последний период отчётов + «что дальше» после создания кампании ────
async def test_period_memory_roundtrip_survives_restart():
    """_remember_period персистит пресет в user_settings.ui_prefs; после «рестарта» (чистка
    процессного кэша) _last_period поднимает его из БД. Произвольные даты НЕ запоминаются."""
    await init_db()
    chat = 66_001
    await bm._remember_period(chat, "7")
    assert await bm._last_period(chat) == "7"
    bm._LAST_PERIOD_CODE.clear()  # эмуляция рестарта процесса
    assert await bm._last_period(chat) == "7"  # поднялось из user_settings.ui_prefs
    await bm._remember_period(chat, "2026-01-01 2026-02-01")  # диапазон дат — не пресет
    assert await bm._last_period(chat) == "7"  # не перезаписан мусором
    await bm._remember_period(chat, "mtd")
    assert await bm._last_period(chat) == "MTD"  # нормализация регистра


def test_period_kb_offers_repeat_first():
    from bot.keyboards import period_kb

    kb = period_kb("report", last="7")
    rows = kb.inline_keyboard
    assert len([b for r in rows for b in r]) == 5  # ↻ + 4 пресета
    assert "как в прошлый раз" in rows[0][0].text  # первая строка — повтор
    assert rows[0][0].callback_data.endswith(":7")
    # без last / с мусорным last — обычная клавиатура из 4 кнопок
    assert len([b for r in period_kb("report").inline_keyboard for b in r]) == 4
    assert len([b for r in period_kb("report", last="зюзя").inline_keyboard for b in r]) == 4


async def test_post_create_next_steps_advisory_only():
    """Кнопки «что дальше»: 📋 Кампании — чистое чтение; ➖ Минус-слова — текст-подсказка;
    НИ одна не минтит proposal (advisory, golden rule 1/3)."""
    from bot.callbacks import CcCB
    from bot.keyboards import post_create_kb

    await init_db()
    chat = 66_002
    # клавиатура: 3 кнопки (запуск/кампании/минус-слова)
    kb = post_create_kb("a" * 32)
    assert len([b for r in kb.inline_keyboard for b in r]) == 3

    called = {}

    async def fake_send_campaigns(msg, chat_id):
        called["campaigns"] = chat_id

    cq = FakeCallbackQuery(FakeMessage(chat_id=chat))
    with patched(bm, "_send_campaigns", fake_send_campaigns):
        await bm.cc_view_camps(cq, CcCB(action="view_camps"))
    assert called["campaigns"] == chat

    # минус-слова: подсказка отправлена, proposal НЕ создан
    cq2 = FakeCallbackQuery(FakeMessage(chat_id=chat))
    await bm.cc_hint_neg(cq2, CcCB(action="hint_neg"))
    assert cq2.message.answers and "минус-слова" in cq2.message.answers[-1][0]
    from sqlalchemy import func as _f
    from sqlalchemy import select as _sel

    from db.models import Proposal as _P
    from db.session import Session as _S

    async with _S() as s:
        n = (
            await s.execute(_sel(_f.count()).select_from(_P).where(_P.chat_id == chat))
        ).scalar_one()
    assert int(n) == 0  # ничего не минтили


# ── §UX-память: последний аккаунт + resume визарда из /start + крошка шага ──────────
async def test_account_memory_roundtrip_survives_restart():
    """_remember_account персистит нормализованный id в ui_prefs; после «рестарта» (чистка
    кэша) _last_account поднимает его из БД. Мусор без цифр — не запоминается."""
    await init_db()
    chat = 66_101
    await bm._remember_account(chat, "775-364-3025")
    assert await bm._last_account(chat) == "7753643025"  # нормализован
    bm._LAST_ACCOUNT.clear()  # эмуляция рестарта процесса
    assert await bm._last_account(chat) == "7753643025"  # из user_settings.ui_prefs
    await bm._remember_account(chat, "не-цифры")  # нормализуется в '' → игнор
    assert await bm._last_account(chat) == "7753643025"  # не затёрт мусором


def test_report_accounts_kb_offers_last_account_first():
    from types import SimpleNamespace

    from bot.keyboards import report_accounts_kb

    rows = [
        SimpleNamespace(id="1112223334", name="Acct A", currency="USD"),
        SimpleNamespace(id="7753643025", name="Draft", currency="USD"),
    ]
    kb = report_accounts_kb(rows, "report", last="775-364-3025")  # last в разделённом формате
    flat = [b for r in kb.inline_keyboard for b in r]
    assert "как в прошлый раз" in flat[0].text  # первой строкой — повтор аккаунта
    assert "Draft" in flat[0].text
    # без last — обычный список (2 аккаунта + отмена = 3), без «↻»
    plain = [b for r in report_accounts_kb(rows, "report").inline_keyboard for b in r]
    assert not any("как в прошлый раз" in b.text for b in plain)


async def test_offer_wizard_resume_only_when_active_draft():
    from types import SimpleNamespace

    async def has_draft(chat_id):
        return SimpleNamespace(current_step=3)

    async def no_draft(chat_id):
        return None

    msg = FakeMessage(chat_id=66_102)
    with patched(bm.CDRAFTS, "get_active", has_draft):
        await bm._offer_wizard_resume(msg)
    assert msg.answers and "3/7" in msg.answers[-1][0]  # подсказка с номером этапа

    msg2 = FakeMessage(chat_id=66_103)
    with patched(bm.CDRAFTS, "get_active", no_draft):
        await bm._offer_wizard_resume(msg2)
    assert msg2.answers == []  # нет черновика → молчим


def test_cc_crumb_shows_step_out_of_seven():
    assert "3/7" in bm._cc_crumb(3)
    assert "🆕" in bm._cc_crumb(1)
    assert "7/7" in bm._cc_crumb(99)  # кламп сверху
    assert "1/7" in bm._cc_crumb(0)  # кламп снизу


# ── §UX-память: «↻ повторить прошлый отчёт» (аккаунт+кампания+период) ───────────────
async def test_report_recall_roundtrip_presets_only():
    await init_db()
    chat = 66_201
    await bm._save_report_recall(chat, "775-364-3025", "42", "Search-Brand", "30")
    r = await bm._load_report_recall(chat)
    assert r["account"] == "7753643025"  # нормализован
    assert r["campaign_id"] == "42" and r["campaign_name"] == "Search-Brand" and r["period"] == "30"
    # произвольный диапазон дат — не пресет → не перезаписываем recall
    await bm._save_report_recall(chat, "7753643025", None, None, "2026-01-01 2026-02-01")
    assert (await bm._load_report_recall(chat))["period"] == "30"


def test_report_recall_kb_one_button_with_details():
    from bot.keyboards import report_recall_kb

    kb = report_recall_kb(
        {"account": "7753643025", "campaign_id": "42", "campaign_name": "Brand", "period": "30"}
    )
    flat = [b for r in kb.inline_keyboard for b in r]
    assert len(flat) == 1
    assert flat[0].callback_data == "rpta:report:-2"  # сентинел «повторить»
    assert "3025" in flat[0].text and "Brand" in flat[0].text and "30" in flat[0].text


async def test_report_recall_button_reruns_saved_report():
    from bot.callbacks import ReportAcctCB

    await init_db()
    chat = 66_202
    await bm._save_report_recall(chat, "7753643025", "42", "Brand", "7")
    ran: dict = {}

    async def fake_run(m, period, acct, cid, cname):
        ran.update(acct=acct, cid=cid, cname=cname)

    cq = FakeCallbackQuery(FakeMessage(chat_id=chat))
    with patched(bm, "_run_report", fake_run), patched(bm, "ensure_read_allowed", lambda cid: None):
        # state (#6) aiogram инжектит всегда; здесь ветка recall (idx=-2) его не трогает — фейк-заглушка.
        await bm.on_report_account(cq, ReportAcctCB(target="report", idx=-2), FakeState())
    assert ran == {"acct": "7753643025", "cid": "42", "cname": "Brand"}  # ровно сохранённый отчёт
