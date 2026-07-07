"""§19 Этапы 2/5/6/7: проводка визарда без минтинга proposal до финала; финал собирает один
composite proposal create_search_campaign из накопленного черновика.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ads.client as ads_client  # noqa: E402
import bot.main as bm  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from ads.read import ReusableAsset  # noqa: E402
from bot.callbacks import CcCB  # noqa: E402
from db.models import Proposal  # noqa: E402
from db.session import Session, init_db  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


class FakeMessage:
    def __init__(self, text="", chat_id=100):
        self.text = text
        self.chat = type("C", (), {"id": chat_id})()
        self.answers: list = []
        self.edits: list = []

    async def answer(self, text="", **kw):
        self.answers.append((text, kw))
        return self

    async def edit_text(self, text="", **kw):
        self.edits.append((text, kw))
        return self


class FakeCallbackQuery:
    def __init__(self, message, uid=100):
        self.message = message
        self.from_user = type("U", (), {"id": uid})()
        self.answers: list = []

    async def answer(self, text="", show_alert=False, **kw):
        self.answers.append((text, show_alert))


class FakeFSM:
    def __init__(self, data=None):
        self._d = dict(data or {})

    async def get_data(self):
        return dict(self._d)

    async def update_data(self, **kw):
        self._d.update(kw)

    async def set_state(self, *a, **k):
        pass

    async def clear(self):
        self._d = {}


async def _muts(chat_id: int) -> int:
    async with Session() as s:
        return int(
            (
                await s.execute(
                    select(func.count())
                    .select_from(Proposal)
                    .where(Proposal.chat_id == chat_id, Proposal.operation != "rsa_curation")
                )
            ).scalar_one()
        )


async def _full_draft(chat: int) -> str:
    """Черновик, дошедший до финала: настройки + объявление + ключи."""
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)

    def _fill(st):
        st["settings"] = {
            "campaign_name": "Кения · Авто · Search",
            "geo_locations": ["Кения"],
            "languages": ["English"],
            "budget_daily_micros": 40_000_000,
            "cpc_bid_micros": 180_000,
            "bidding_strategy": "maximize_conversions",
            "match_type": "phrase",
            "by_analogy": [],
        }
        st["ad"] = {
            "final_url": "https://shop.example/used",
            "path1": "Used-Cars",
            "path2": "Кения",
            "headlines": ["Поддержанные авто", "Проверенные б/у", "Авто с гарантией"],
            "descriptions": ["Большой выбор авто с пробегом.", "Гарантия и проверка."],
        }
        st["keywords"] = {"list": ["used cars nairobi"], "match_type": "phrase"}

    await bm.CDRAFTS.patch(sid, _fill)
    return sid


@pytest.mark.asyncio
async def test_stage2_provide_keywords_reviews_then_advances():
    """§19.4: список принят → ОБЗОР с явным гейтом «✅ Подтвердить ключевые слова» (остаёмся на
    Этапе 2) → по кнопке — Этап 3. Смешанные маркеры сохраняются per-keyword (match_types)."""
    await init_db()
    chat = 7700201
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    await bm.CDRAFTS.set_step(sid, 2)
    fsm = FakeFSM({"cc_session": sid})
    await bm.cc_keywords_text(FakeMessage("[used cars], cheap cars kenya", chat_id=chat), fsm)
    snap = await bm.CDRAFTS.get(sid)
    assert snap.wizard_state["keywords"]["list"] == ["used cars", "cheap cars kenya"]
    # §19.4.1: смешанные типы НЕ схлопнуты в первый — сохранены 1:1 к ключам
    assert snap.wizard_state["keywords"]["match_types"] == ["exact", "phrase"]
    assert snap.current_step == 2  # ждём явного «✅ Подтвердить ключевые слова»
    assert await _muts(chat) == 0
    # Явный гейт: кнопка «✅ Подтвердить ключевые слова» → Этап 3
    cq = FakeCallbackQuery(FakeMessage(chat_id=chat))
    await bm.cc_kw_confirm(cq, CcCB(action="kw_confirm"), fsm)
    snap2 = await bm.CDRAFTS.get(sid)
    assert snap2.current_step == 3  # ушли к объявлению
    assert await _muts(chat) == 0


@pytest.mark.asyncio
async def test_stage2_file_upload_feeds_keywords():
    """§19.4.1 Ввод A (файл): XLSX/CSV-текст кормится в черновик через _cc_keywords_from_document
    (раньше файл падал в общий ingest и сбрасывал визард)."""
    await init_db()
    chat = 7700205
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    await bm.CDRAFTS.set_step(sid, 2)
    fsm = FakeFSM({"cc_session": sid})
    file_text = "used cars nairobi\ncheap cars kenya\n[toyota kenya]"  # как из xlsx/csv-колонки
    await bm._cc_keywords_from_document(FakeMessage(chat_id=chat), fsm, file_text, "kw.xlsx")
    snap = await bm.CDRAFTS.get(sid)
    assert snap.wizard_state["keywords"]["list"] == [
        "used cars nairobi",
        "cheap cars kenya",
        "toyota kenya",
    ]
    assert snap.wizard_state["keywords"]["source"] == "file"
    assert snap.wizard_state["keywords"]["match_types"] == ["phrase", "phrase", "exact"]
    assert snap.current_step == 2  # обзор + явный гейт, как и для текста
    assert await _muts(chat) == 0


@pytest.mark.asyncio
async def test_stage2_verify_rejects_foreign_sheet():
    """§19.4.2 round-trip: менеджер обязан вернуть ТУ ЖЕ таблицу, что создал бот (sheet_id)."""
    await init_db()
    chat = 7700206
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    await bm.CDRAFTS.set_step(sid, 2)
    await bm.CDRAFTS.patch(sid, lambda st: st["keywords"].__setitem__("sheet_id", "SHEET-ORIG-1"))
    fsm = FakeFSM({"cc_session": sid})
    m = FakeMessage("https://docs.google.com/spreadsheets/d/OTHER-SHEET-9/edit", chat_id=chat)
    await bm.cc_kw_verify(m, fsm)
    snap = await bm.CDRAFTS.get(sid)
    assert not (snap.wizard_state["keywords"] or {}).get("verified")  # чужая таблица не принята
    assert any("не та таблица" in (t or "") for t, _ in m.answers)


@pytest.mark.asyncio
async def test_stage5_use_assets_reuses_and_advances():
    await init_db()
    chat = 7700202
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    await bm.CDRAFTS.set_step(sid, 5)
    fsm = FakeFSM({"cc_session": sid})

    async def fake_read(fn, client, cid, **kw):
        return [
            ReusableAsset("customers/x/assets/1", "SITELINK", "Каталог"),
            ReusableAsset("customers/x/assets/2", "CALLOUT", "Гарантия"),
        ]

    cq = FakeCallbackQuery(FakeMessage(chat_id=chat))
    with (
        patched(ads_client, "build_client", lambda *a, **k: object()),
        patched(bm, "run_ads_read_call", fake_read),
    ):
        await bm.cc_use_assets(cq, CcCB(action="use_assets"), fsm)
    snap = await bm.CDRAFTS.get(sid)
    assert len(snap.wizard_state["assets"]["reuse_links"]) == 2
    assert (
        snap.current_step == 5
    )  # вернулись в меню ассетов (можно добавить ещё; «Готово» → этап 6)
    assert await _muts(chat) == 0

    # «Готово» (skip на этапе 5) → этап 6
    cq2 = FakeCallbackQuery(FakeMessage(chat_id=chat))
    await bm.cc_skip(cq2, CcCB(action="skip"), fsm)
    snap2 = await bm.CDRAFTS.get(sid)
    assert snap2.current_step == 6


@pytest.mark.asyncio
async def test_stage6_url_options_saved_and_advances():
    await init_db()
    chat = 7700203
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    await bm.CDRAFTS.set_step(sid, 6)
    fsm = FakeFSM({"cc_session": sid})
    await bm.cc_url_text(
        FakeMessage("{lpurl}?utm_source=google | utm_medium=cpc", chat_id=chat), fsm
    )
    snap = await bm.CDRAFTS.get(sid)
    assert snap.wizard_state["url_options"]["tracking_url_template"] == "{lpurl}?utm_source=google"
    assert snap.wizard_state["url_options"]["final_url_suffix"] == "utm_medium=cpc"
    assert snap.current_step == 7  # ушли к финалу
    assert await _muts(chat) == 0


@pytest.mark.asyncio
async def test_stage7_create_builds_composite_proposal():
    await init_db()
    chat = 7700204
    sid = await _full_draft(chat)
    await bm.CDRAFTS.set_step(sid, 7)
    fsm = FakeFSM({"cc_session": sid})
    captured = {}

    async def fake_present(message, *, chat_id, operation, params, summary, cid, customer_id=None):
        captured.update(operation=operation, params=params, cid=cid, customer_id=customer_id)

    cq = FakeCallbackQuery(FakeMessage(chat_id=chat))
    with patched(bm, "_present_proposal", fake_present):
        await bm.cc_create(cq, CcCB(action="create"), fsm)

    assert captured["operation"] == "create_search_campaign"
    # §8: при активном аккаунте=Draft (тесты: пустой allow-list) визард минтит на Draft (fail-closed)
    assert captured["customer_id"] == bm.DRAFT_ACCOUNT_ID
    p = captured["params"]
    assert p["campaign_name"].startswith("Кения")
    assert p["geo_locations"] == ["Кения"]
    assert p["languages"] == ["English"]
    assert p["bidding"]["strategy"] == "maximize_conversions"
    assert p["path1"] == "Used-Cars"
    assert p["url_options"] is None  # не задавали
    # B9: черновик визарда НЕ гасится на «Создать черновик» — остаётся active до УСПЕШНОГО
    # подтверждения proposal (при reject возобновляем «▶️ Продолжить»). Гасится в _do_confirm.
    snap = await bm.CDRAFTS.get(sid)
    assert snap.status == "active"
    # Связка cid→draft — в params proposal (БД, а не память процесса): переживает рестарт
    assert p["_cc_draft"] == sid


@pytest.mark.asyncio
async def test_launch_button_mints_launch_proposal():
    """§19.8 (legacy-кнопка без sub): «🚀 Запустить» → launch_campaign proposal (confirm-гейт),
    не прямой запуск. launch_campaign включает кампанию ПОЛНОСТЬЮ (кампания+группы+объявления),
    в отличие от resume_campaign (только кампания) — иначе PAUSED группа/RSA ⇒ 0 показов.
    Кэш имени одноразовый (не запустить дважды по старой кнопке)."""
    await init_db()
    chat = 7700206
    bm._CC_LAUNCH_CACHE[chat] = "Кения · Авто · Search"
    captured = {}

    async def fake_present(message, *, chat_id, operation, params, summary, cid, customer_id=None):
        captured.update(operation=operation, params=params)

    cq = FakeCallbackQuery(FakeMessage(chat_id=chat))
    with patched(bm, "_present_proposal", fake_present):
        await bm.cc_launch(cq, CcCB(action="launch"))
    assert captured["operation"] == "launch_campaign"
    assert captured["params"]["campaign"] == "Кения · Авто · Search"
    assert chat not in bm._CC_LAUNCH_CACHE  # одноразово

    # повторный клик по старой кнопке → нечего запускать (show_alert), proposal не минтится
    captured.clear()
    cq2 = FakeCallbackQuery(FakeMessage(chat_id=chat))
    with patched(bm, "_present_proposal", fake_present):
        await bm.cc_launch(cq2, CcCB(action="launch"))
    assert not captured
    assert cq2.answers and cq2.answers[-1][1] is True  # show_alert


async def _applied_create_proposal(chat: int, name: str, extra_params: dict | None = None) -> str:
    """Применённый create_search_campaign proposal в БД (симуляция успешного создания)."""
    import uuid as _uuid

    cid = _uuid.uuid4().hex
    await bm.STORE.save_proposal(
        confirmation_id=cid,
        operation="create_search_campaign",
        customer_id=DRAFT_ACCOUNT_ID,
        params={"campaign_name": name, **(extra_params or {})},
        summary="тест",
        chat_id=chat,
        user_initiated=True,
    )
    await bm.STORE.confirm(cid, chat_id=chat)
    claimed = await bm.STORE.claim(cid, operation="create_search_campaign")
    assert claimed is not None
    await bm.STORE.finalize(cid, result="created")  # status → applied
    return cid


@pytest.mark.asyncio
async def test_launch_button_survives_restart_via_sub():
    """§19.8 restart-durability: кнопка с sub=confirmation_id создания работает при ПУСТЫХ кэшах
    процесса (симуляция рестарта) — имя из применённого proposal в БД. Одноразовость и гард
    владения: повтор → stale; чужой chat_id → stale."""
    await init_db()
    chat = 7700216
    cid = await _applied_create_proposal(chat, "Кения · Рестарт · Search")
    bm._CC_LAUNCH_CACHE.clear()  # рестарт: процессные кэши пусты
    bm._CC_LAUNCH_DONE.clear()
    captured = {}

    async def fake_present(message, *, chat_id, operation, params, summary, cid, customer_id=None):
        captured.update(operation=operation, params=params)

    cq = FakeCallbackQuery(FakeMessage(chat_id=chat))
    with patched(bm, "_present_proposal", fake_present):
        await bm.cc_launch(cq, CcCB(action="launch", sub=cid))
    assert captured["operation"] == "launch_campaign"
    assert captured["params"]["campaign"] == "Кения · Рестарт · Search"

    # одноразовость в процессе: повторный клик той же кнопкой → stale
    captured.clear()
    cq2 = FakeCallbackQuery(FakeMessage(chat_id=chat))
    with patched(bm, "_present_proposal", fake_present):
        await bm.cc_launch(cq2, CcCB(action="launch", sub=cid))
    assert not captured
    assert cq2.answers and cq2.answers[-1][1] is True

    # гард владения: чужой chat_id не запускает чужую кампанию
    bm._CC_LAUNCH_DONE.clear()
    cq3 = FakeCallbackQuery(FakeMessage(chat_id=999_777), uid=999_777)
    with patched(bm, "_present_proposal", fake_present):
        await bm.cc_launch(cq3, CcCB(action="launch", sub=cid))
    assert not captured
    assert cq3.answers and cq3.answers[-1][1] is True


@pytest.mark.asyncio
async def test_confirm_create_finishes_draft_after_restart():
    """B9 restart-durability: связка proposal→draft в params (БД) — успешный confirm гасит черновик
    даже если процессные кэши потеряны (рестарт между «Создать черновик» и ✅)."""
    await init_db()
    chat = 7700217
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    import uuid as _uuid

    cid = _uuid.uuid4().hex
    await bm.STORE.save_proposal(
        confirmation_id=cid,
        operation="create_search_campaign",
        customer_id=DRAFT_ACCOUNT_ID,
        params={"campaign_name": "Кения · Финиш · Search", "_cc_draft": sid},
        summary="тест",
        chat_id=chat,
        user_initiated=True,
    )

    async def fake_exec(store, c):
        return "created"

    cq = FakeCallbackQuery(FakeMessage(chat_id=chat))
    with patched(bm, "execute_confirmed", fake_exec):
        ok = await bm._do_confirm(cq, cid)
    assert ok is True
    snap = await bm.CDRAFTS.get(sid)
    assert snap.status == "done"  # черновик погашен ПОСЛЕ успешного создания (из params, не кэша)


class RecFSM(FakeFSM):
    """FakeFSM с записью set_state — для проверок resume-подсостояний (B3)."""

    def __init__(self, data=None):
        super().__init__(data)
        self.states: list = []

    async def set_state(self, st=None, *a, **k):
        self.states.append(st)


@pytest.mark.asyncio
async def test_stage2_resume_reenters_kw_verify_with_sheet_link():
    """B3-resume: выгруженная и НЕ верифицированная таблица → resume Этапа 2 возвращает в kw_verify
    и пере-показывает ссылку (раньше молча падал в пустой ввод, теряя round-trip)."""
    await init_db()
    chat = 7700218
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    await bm.CDRAFTS.patch(
        sid,
        lambda st: st["keywords"].update(
            {
                "sheet_id": "SHEET123",
                "sheet_url": "https://docs.google.com/spreadsheets/d/SHEET123/edit",
            }
        ),
    )
    fsm = RecFSM({"cc_session": sid})
    msg = FakeMessage(chat_id=chat)
    await bm._cc_present_stage2(msg, chat, sid, fsm)
    assert fsm.states and fsm.states[-1] is bm.CreateCampaignWizard.kw_verify
    assert any("SHEET123" in (t or "") for t, _ in msg.answers)  # ссылка пере-показана
    # тот-же-sheet гард работает после resume: чужая ссылка отклоняется
    bad = FakeMessage("https://docs.google.com/spreadsheets/d/OTHER99/edit", chat_id=chat)
    await bm.cc_kw_verify(bad, fsm)
    assert bad.answers  # cc_kw_wrong_sheet
    snap = await bm.CDRAFTS.get(sid)
    assert not snap.wizard_state["keywords"].get("verified")


@pytest.mark.asyncio
async def test_stage2_resume_verified_list_shows_confirm_gate():
    """B3-resume: верифицированный список → resume показывает обзор с гейтом «✅ Подтвердить»."""
    await init_db()
    chat = 7700219
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    await bm.CDRAFTS.patch(
        sid,
        lambda st: st["keywords"].update(
            {"list": ["used cars nairobi", "cheap cars"], "match_type": "phrase", "verified": True}
        ),
    )
    fsm = RecFSM({"cc_session": sid})
    msg = FakeMessage(chat_id=chat)
    await bm._cc_present_stage2(msg, chat, sid, fsm)
    assert any("used cars nairobi" in (t or "") for t, _ in msg.answers)  # обзор списка
    # клавиатура гейта прикреплена (cc_kw_confirm_kb)
    assert any(kw.get("reply_markup") is not None for _t, kw in msg.answers)
    assert fsm.states and fsm.states[-1] is bm.CreateCampaignWizard.keywords


@pytest.mark.asyncio
async def test_stage0_account_search_filters_with_global_indices():
    """§19.2 «поиск по названию»: текст на Этапе 0 фильтрует кэш аккаунтов; кнопки несут
    ГЛОБАЛЬНЫЙ индекс — выбор из результатов поиска фиксирует ПРАВИЛЬНЫЙ аккаунт."""
    await init_db()
    chat = 7700220
    mk = lambda i, n: type("A", (), {"id": f"11100{i}", "name": n})()  # noqa: E731
    bm._CC_ACCT_CACHE[chat] = [
        mk(0, "Alpha Motors"),
        mk(1, "Beta Shoes"),
        mk(2, "Kasi Motors"),
        mk(3, "Gamma Cafe"),
    ]
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    fsm = FakeFSM({"cc_session": sid})
    msg = FakeMessage("motors", chat_id=chat)
    await bm.cc_account_search(msg, fsm)
    assert msg.answers
    kb = msg.answers[-1][1].get("reply_markup")
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]
    # совпали Alpha Motors (idx 0) и Kasi Motors (idx 2) — глобальные индексы, не 0/1 результатов
    assert any(
        ":0:" in cb or cb.endswith(":0:") or ":0" in cb.split(":")[2]
        for cb in cbs
        if cb.startswith("cc:acct")
    )
    acct_idx = sorted(int(cb.split(":")[2]) for cb in cbs if cb.startswith("cc:acct"))
    assert acct_idx == [0, 2]
    # выбор из результатов → правильный preview_customer_id (Kasi = 111002)
    cq = FakeCallbackQuery(FakeMessage(chat_id=chat))
    await bm.cc_account_cb(cq, CcCB(action="acct", idx=2), fsm)
    snap = await bm.CDRAFTS.get(sid)
    assert snap.preview_customer_id == "111002"


@pytest.mark.asyncio
async def test_stage0_account_search_no_match_reshows_picker():
    await init_db()
    chat = 7700221
    bm._CC_ACCT_CACHE[chat] = [type("A", (), {"id": "111000", "name": "Alpha"})()]
    msg = FakeMessage("zzz-нет-такого", chat_id=chat)
    await bm.cc_account_search(msg, FakeFSM())
    assert msg.answers  # cc_acct_search_empty + полный пикер
    assert msg.answers[-1][1].get("reply_markup") is not None


@pytest.mark.asyncio
async def test_stage7_create_blocks_without_ad():
    await init_db()
    chat = 7700205
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    await bm.CDRAFTS.set_step(sid, 7)
    fsm = FakeFSM({"cc_session": sid})
    cq = FakeCallbackQuery(FakeMessage(chat_id=chat))
    called = {"present": False}

    async def fake_present(*a, **k):
        called["present"] = True

    with patched(bm, "_present_proposal", fake_present):
        await bm.cc_create(cq, CcCB(action="create"), fsm)
    assert called["present"] is False  # без объявления — нет proposal
    assert cq.answers and cq.answers[-1][1] is True  # show_alert


# ── 3D: «‹ Назад» — навигация без потери данных черновика ──────────────────────────
async def test_cc_back_navigates_without_data_loss():
    """back с этапа 7 → этап 6; сохранённые ключи/настройки целы; двойной back на 0 не падает."""
    import bot.main as bm
    from db.session import init_db
    from types import SimpleNamespace

    await init_db()
    chat_id = 93_001
    sid = await bm.CDRAFTS.create(chat_id=chat_id, customer_id=bm.DRAFT_ACCOUNT_ID)
    await bm.CDRAFTS.patch(
        sid,
        lambda st: st["keywords"].update({"list": ["авто"], "verified": True}),
        expected_chat_id=chat_id,
    )
    await bm.CDRAFTS.set_step(sid, 7, expected_chat_id=chat_id)

    class _Msg:
        def __init__(self):
            self.chat = SimpleNamespace(id=chat_id)
            self.answers: list = []

        async def answer(self, text: str = "", **kw):
            self.answers.append(text)
            return self

    class _CQ:
        def __init__(self, msg):
            self.message = msg
            self.from_user = SimpleNamespace(id=chat_id)

        async def answer(self, *a, **k):
            pass

    class _State:
        def __init__(self):
            self._data, self._state = {}, None

        async def get_data(self):
            return dict(self._data)

        async def update_data(self, **kw):
            self._data.update(kw)

        async def set_state(self, s):
            self._state = s

        async def clear(self):
            self._data, self._state = {}, None

    msg = _Msg()
    await bm.cc_back(_CQ(msg), bm.CcCB(action="back"), _State())
    snap = await bm.CDRAFTS.get(sid, expected_chat_id=chat_id)
    assert snap.current_step == 6
    assert snap.wizard_state["keywords"]["list"] == ["авто"]  # данные целы

    # прыгаем на 0 и жмём back ещё раз — клампится в 0, не падает
    await bm.CDRAFTS.set_step(sid, 0, expected_chat_id=chat_id)
    await bm.cc_back(_CQ(_Msg()), bm.CcCB(action="back"), _State())
    snap = await bm.CDRAFTS.get(sid, expected_chat_id=chat_id)
    assert snap.current_step == 0


def test_cc_keyboards_have_back_button():
    """3D: клавиатуры этапов 1–7 несут «‹ Назад» (CcCB back); финал после создания — без back."""
    from bot import keyboards as kb

    def _texts(markup):
        return [b.text for row in markup.inline_keyboard for b in row]

    for factory in (
        kb.cc_settings_kb,
        kb.cc_kw_kb,
        kb.cc_kw_confirm_kb,
        kb.cc_assets_kb,
        kb.cc_skip_kb,
    ):
        assert any("Назад" in t or "Back" in t for t in _texts(factory())), factory.__name__
    assert any("Назад" in t for t in _texts(kb.cc_final_kb()))  # до создания — есть
    assert not any(
        "Назад" in t for t in _texts(kb.cc_final_kb(can_launch=True, launch_cid="a" * 32))
    )  # после создания кампании back бессмыслен


# ── W4 (живой тест 2026-07-06): «Вперёд ›» + возврат на шаг 0 без перезаписи ───────
class _NavMsg:
    def __init__(self, chat_id):
        from types import SimpleNamespace

        self.chat = SimpleNamespace(id=chat_id)
        self.answers: list = []
        self.edits: list = []

    async def answer(self, text="", **kw):
        self.answers.append((text, kw))
        return self

    async def edit_text(self, text="", **kw):
        self.edits.append((text, kw))
        return self

    async def delete(self):
        pass


class _NavState:
    def __init__(self, data=None):
        self._data, self._state = dict(data or {}), None

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kw):
        self._data.update(kw)

    async def set_state(self, s):
        self._state = s

    async def clear(self):
        self._data, self._state = {}, None


@pytest.mark.asyncio
async def test_cc_forward_restores_stage_after_back():
    """Назад с этапа ключей → настройки → «Вперёд ›» возвращает на этап 2 с теми же ключами."""
    await init_db()
    chat = 93_101
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    await bm.CDRAFTS.patch(
        sid,
        lambda st: (
            st.__setitem__("settings", {"campaign_name": "Уганда · авто · Search"}),
            st["keywords"].update(
                {
                    "list": ["kw"] * 187,
                    "sheet_id": "sid123",
                    "verified": False,
                    "source": "generated",
                }
            ),
        ),
        expected_chat_id=chat,
    )
    await bm.CDRAFTS.set_step(sid, 2, expected_chat_id=chat)  # high-water = 2

    # «Назад» на настройки
    msg = _NavMsg(chat)
    await bm.cc_back(FakeCallbackQuery(msg, uid=chat), CcCB(action="back"), _NavState())
    snap = await bm.CDRAFTS.get(sid, expected_chat_id=chat)
    assert snap.current_step == 1
    # карточка настроек несёт «Вперёд ›» (этап 2 уже пройден)
    kb_texts = [
        b.text
        for _, kw in msg.answers
        if kw.get("reply_markup")
        for row in kw["reply_markup"].inline_keyboard
        for b in row
    ]
    assert any("Вперёд" in t or "Forward" in t for t in kb_texts)

    # «Вперёд ›» возвращает на этап 2, ключи целы, verify-экран заново показан
    msg2 = _NavMsg(chat)
    await bm.cc_forward(FakeCallbackQuery(msg2, uid=chat), CcCB(action="fwd"), _NavState())
    snap = await bm.CDRAFTS.get(sid, expected_chat_id=chat)
    assert snap.current_step == 2
    assert len(snap.wizard_state["keywords"]["list"]) == 187  # данные не потеряны
    assert any("187" in (t or "") for t, _ in msg2.answers)  # verify-экран с тем же списком


@pytest.mark.asyncio
async def test_cc_forward_never_exceeds_max_step():
    """Вперёд с максимума маршрута — вежливый alert, шаг не двигается."""
    await init_db()
    chat = 93_102
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    await bm.CDRAFTS.set_step(sid, 2, expected_chat_id=chat)
    msg = _NavMsg(chat)
    cq = FakeCallbackQuery(msg, uid=chat)
    await bm.cc_forward(cq, CcCB(action="fwd"), _NavState())
    snap = await bm.CDRAFTS.get(sid, expected_chat_id=chat)
    assert snap.current_step == 2  # не сдвинулся
    assert cq.answers and cq.answers[-1][1] is True  # show_alert


@pytest.mark.asyncio
async def test_stage0_reentry_with_settings_keeps_them():
    """Возврат на шаг 0 и повторный выбор аккаунта НЕ перезаписывает собранные настройки:
    показывается сохранённая сводка (settings_confirm), а не пустой промпт описания."""
    from types import SimpleNamespace

    await init_db()
    chat = 93_103
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    settings0 = {
        "campaign_name": "Уганда · авто · Search",
        "geo_locations": ["Уганда"],
        "budget_daily_micros": 1_900_000_000,
        "cpc_bid_micros": 75_000_000,
        "by_analogy": [],
        "by_default": [],
    }
    await bm.CDRAFTS.patch(
        sid, lambda st: st.__setitem__("settings", dict(settings0)), expected_chat_id=chat
    )
    await bm.CDRAFTS.set_step(sid, 1, expected_chat_id=chat)
    bm._CC_ACCT_CACHE[chat] = [
        SimpleNamespace(id=DRAFT_ACCOUNT_ID, name="Aimash (Draft)", manager=False)
    ]
    msg = _NavMsg(chat)
    state = _NavState({"cc_session": sid})
    await bm.cc_account_cb(FakeCallbackQuery(msg, uid=chat), CcCB(action="acct", idx=0), state)
    snap = await bm.CDRAFTS.get(sid, expected_chat_id=chat)
    assert snap.wizard_state["settings"] == settings0  # настройки целы
    assert state._state == bm.CreateCampaignWizard.settings_confirm  # НЕ settings_desc
    assert any("Кампания (черновик)" in (t or "") for t, _ in msg.answers)  # сводка показана


@pytest.mark.asyncio
async def test_settings_desc_with_existing_settings_merges_not_overwrites():
    """Belt-and-brace: текст в settings_desc при уже собранных настройках — merge-правка,
    полный assemble (перетирание пользовательских значений дефолтами) не выполняется."""
    await init_db()
    chat = 93_104
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    settings0 = {
        "campaign_name": "Уганда · авто · Search",
        "geo_locations": ["Уганда"],
        "budget_daily_micros": 1_900_000_000,
        "cpc_bid_micros": 75_000_000,
        "by_analogy": [],
        "by_default": [],
    }
    await bm.CDRAFTS.patch(
        sid, lambda st: st.__setitem__("settings", dict(settings0)), expected_chat_id=chat
    )
    await bm.CDRAFTS.set_step(sid, 1, expected_chat_id=chat)

    from agent.campaign_settings import CampaignSettings

    async def _budget_only(text, *, language="ru"):
        return CampaignSettings(budget_daily_units=60)

    called = {"assemble": False}

    def _no_assemble(*a, **k):
        called["assemble"] = True
        raise AssertionError("assemble_settings не должен вызываться при merge-правке")

    msg = FakeMessage("поставь бюджет 60", chat_id=chat)
    fsm = _NavState({"cc_session": sid})
    with (
        patched(bm, "extract_campaign_settings", _budget_only),
        patched(bm, "assemble_settings", _no_assemble),
    ):
        await bm.cc_settings_desc(msg, fsm)

    snap = await bm.CDRAFTS.get(sid, expected_chat_id=chat)
    s = snap.wizard_state["settings"]
    assert s["budget_daily_micros"] == 60_000_000  # правка применена
    assert s["geo_locations"] == ["Уганда"]  # остальное цело
    assert s["cpc_bid_micros"] == 75_000_000
    assert called["assemble"] is False


@pytest.mark.asyncio
async def test_stage0_repick_other_account_reseeds_currency_defaults():
    """Ревью 2026-07-07: смена аккаунта на Этапе 0 при собранных настройках пересеивает валюту и
    by_default-деньги под НОВЫЙ аккаунт; пользовательские значения не трогаются."""
    from types import SimpleNamespace

    await init_db()
    chat = 93_105
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    settings0 = {
        "campaign_name": "Уганда · авто · Search",
        "budget_daily_micros": 1_900_000_000,  # задан пользователем — НЕ трогаем
        "cpc_bid_micros": 75_000_000,  # by_default (JPY) — пересеется под USD
        "currency": "JPY",
        "by_analogy": [],
        "by_default": ["cpc_bid_micros"],
    }
    await bm.CDRAFTS.patch(
        sid, lambda st: st.__setitem__("settings", dict(settings0)), expected_chat_id=chat
    )
    await bm.CDRAFTS.set_step(sid, 1, expected_chat_id=chat)
    bm._CC_ACCT_CACHE[chat] = [SimpleNamespace(id="1234567890", name="USD acct", manager=False)]

    async def _usd(cid):
        return "USD"

    msg = _NavMsg(chat)
    state = _NavState({"cc_session": sid})
    with patched(bm, "_cc_account_currency", _usd):
        await bm.cc_account_cb(FakeCallbackQuery(msg, uid=chat), CcCB(action="acct", idx=0), state)

    snap = await bm.CDRAFTS.get(sid, expected_chat_id=chat)
    s = snap.wizard_state["settings"]
    assert s["currency"] == "USD"  # метка валюты — нового аккаунта
    assert s["cpc_bid_micros"] == 500_000  # by_default CPC пересеян под USD (0.5)
    assert s["budget_daily_micros"] == 1_900_000_000  # пользовательский бюджет цел
