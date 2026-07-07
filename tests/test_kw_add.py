"""§7: флоу «добавить подобранные ключи в кампанию» (research → кампания → тип соответствия →
confirm-гейт). Проверяем: серверный токен-стор, клавиатуры, и что финал собирает ТОЛЬКО черновик
add_keywords (pending, user_initiated) — исполнение лишь после ✅. Реальный ConfirmStore на temp
SQLite (conftest); сеть/SDK не трогаем (add_keywords не diffable → read_before без вызова)."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402
from bot.callbacks import KwAddCB  # noqa: E402
from bot.keyboards import match_type_kb  # noqa: E402
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
    def __init__(self, chat_id: int = 100, text: str = "", bot=None):
        self.chat = type("C", (), {"id": chat_id})()
        self.text = text
        self.bot = bot
        self.answers: list = []

    async def answer(self, text: str = "", **kw):
        self.answers.append((text, kw))
        return self

    async def edit_text(self, text: str = "", **kw):
        return self


class FakeCallbackQuery:
    def __init__(self, message, uid: int = 100):
        self.message = message
        self.from_user = type("U", (), {"id": uid})()
        self.answers: list = []

    async def answer(self, text: str = "", show_alert: bool = False, **kw):
        self.answers.append((text, show_alert))


class FakeState:
    def __init__(self):
        self._data: dict = {}
        self._state = None

    async def clear(self):
        self._data = {}
        self._state = None

    async def set_state(self, s):
        self._state = s

    async def update_data(self, **kw):
        self._data.update(kw)

    async def get_data(self):
        return dict(self._data)

    async def get_state(self):
        return self._state


# ── токен-стор ────────────────────────────────────────────────────────────────────
def test_kw_add_put_stores_and_returns_token():
    bm._KW_ADD.clear()
    token = bm._kw_add_put(["a", "b"], "src")
    assert token in bm._KW_ADD
    assert bm._KW_ADD[token]["keywords"] == ["a", "b"]
    assert bm._KW_ADD[token]["src"] == "src"


def test_kw_add_put_caps_size_evicting_oldest():
    bm._KW_ADD.clear()
    with patched(bm, "_KW_ADD_MAX", 3):
        toks = [bm._kw_add_put([f"k{i}"], "s") for i in range(5)]
    assert len(bm._KW_ADD) <= 3
    assert toks[-1] in bm._KW_ADD  # новейший на месте
    assert toks[0] not in bm._KW_ADD  # старейший вытеснен


# ── клавиатуры ─────────────────────────────────────────────────────────────────────
def test_kw_add_button_removed_from_report():
    """P3 (фидбэк заказчика 2026-07-06): кнопки «➕ Добавить ключи» под отчётом больше нет —
    вход через /addkeys и меню «➕ Ещё» (класс-гард от возврата кнопки)."""
    from bot import keyboards as kbm

    assert not hasattr(kbm, "kw_add_kb")
    more = kbm.more_menu_kb()
    cbs = [b.callback_data for row in more.inline_keyboard for b in row]
    assert any("addkeys" in c for c in cbs)  # вход в меню «➕ Ещё» есть


def test_match_type_kb_has_three_match_types_plus_cancel():
    mt = match_type_kb("TOK")
    cbs = [KwAddCB.unpack(b.callback_data) for row in mt.inline_keyboard for b in row]
    assert {c.mt for c in cbs if c.action == "match"} == {"broad", "phrase", "exact"}
    assert any(c.action == "cancel" for c in cbs)
    assert all(c.token == "TOK" for c in cbs)


# ── полный флоу: research → кампания → тип → ТОЛЬКО черновик add_keywords ──────────
async def test_kw_add_flow_builds_pending_add_keywords_only():
    await init_db()
    bm._KW_ADD.clear()
    chat_id = 30_701
    token = bm._kw_add_put(["купить телефон", "смартфон цена", "телефон купить"], "телефон")

    # 1) старт: ставит состояние awaiting_campaign + кладёт токен
    state = FakeState()
    await bm.on_kw_add_start(
        FakeCallbackQuery(FakeMessage(chat_id=chat_id)), KwAddCB(action="start", token=token), state
    )
    assert await state.get_state() == bm.KwAdd.awaiting_campaign

    # 2) название кампании → показываем выбор типа соответствия, кампания в сессии
    await bm.kw_add_campaign(FakeMessage(chat_id=chat_id, text="Search Spring"), state)
    assert bm._KW_ADD[token]["campaign"] == "Search Spring"

    # 3) тип соответствия → собран ТОЛЬКО черновик add_keywords (pending), токен израсходован
    await bm.on_kw_add_match(
        FakeCallbackQuery(FakeMessage(chat_id=chat_id)),
        KwAddCB(action="match", token=token, mt="phrase"),
    )
    cid = bm._LAST_PENDING.get(chat_id)
    assert cid is not None
    snap = await ConfirmStore().get_confirmed(cid)
    assert snap.operation == "add_keywords"
    assert snap.status == "pending"  # НЕ исполнено — ждёт ✅
    assert snap.user_initiated is True  # прямая команда человека
    assert snap.params["campaign"] == "Search Spring"
    assert snap.params["match_type"] == "phrase"
    assert len(snap.params["keywords"]) >= 1
    assert token not in bm._KW_ADD  # сессия израсходована


async def test_kw_add_list_step_parses_edited_keywords_into_proposal():
    """§7 list-UX: кампания → редактируемый список → менеджер прислал СВОЙ список → тип → черновик
    add_keywords содержит ОТРЕДАКТИРОВАННЫЕ ключи (а не исходные), дедуплицированные, ≤50."""
    await init_db()
    bm._KW_ADD.clear()
    chat_id = 30_710
    token = bm._kw_add_put(["купить телефон", "смартфон"], "телефон")

    state = FakeState()
    await bm.on_kw_add_start(
        FakeCallbackQuery(FakeMessage(chat_id=chat_id)), KwAddCB(action="start", token=token), state
    )
    # кампания → теперь ведёт к списку ключей (list-UX), а не сразу к типу
    await bm.kw_add_campaign(FakeMessage(chat_id=chat_id, text="Search Spring"), state)
    assert await state.get_state() == bm.KwAdd.awaiting_keywords

    # менеджер прислал отредактированный список (по строке + запятая, с дублем)
    await bm.kw_add_keywords(
        FakeMessage(chat_id=chat_id, text="ремонт окон, окна пвх\nустановка окон\nремонт окон"),
        state,
    )
    assert bm._KW_ADD[token]["keywords"] == ["ремонт окон", "окна пвх", "установка окон"]

    await bm.on_kw_add_match(
        FakeCallbackQuery(FakeMessage(chat_id=chat_id)),
        KwAddCB(action="match", token=token, mt="exact"),
    )
    cid = bm._LAST_PENDING.get(chat_id)
    assert cid is not None
    snap = await ConfirmStore().get_confirmed(cid)
    assert snap.operation == "add_keywords"
    assert set(snap.params["keywords"]) == {"ремонт окон", "окна пвх", "установка окон"}
    assert snap.params["match_type"] == "exact"


async def test_kw_add_list_empty_stays_in_state():
    """Пустой присланный список → остаёмся в awaiting_keywords (менеджер пришлёт снова)."""
    await init_db()
    bm._KW_ADD.clear()
    chat_id = 30_711
    token = bm._kw_add_put(["купить телефон"], "телефон")
    state = FakeState()
    await bm.on_kw_add_start(
        FakeCallbackQuery(FakeMessage(chat_id=chat_id)), KwAddCB(action="start", token=token), state
    )
    await bm.kw_add_campaign(FakeMessage(chat_id=chat_id, text="Search"), state)
    await bm.kw_add_keywords(FakeMessage(chat_id=chat_id, text="   ,  \n "), state)
    assert await state.get_state() == bm.KwAdd.awaiting_keywords  # не вышли из шага


async def test_kw_add_match_with_stale_token_no_proposal():
    await init_db()
    bm._KW_ADD.clear()
    chat_id = 30_702
    cq = FakeCallbackQuery(FakeMessage(chat_id=chat_id))
    await bm.on_kw_add_match(cq, KwAddCB(action="match", token="missing", mt="broad"))
    assert bm._LAST_PENDING.get(chat_id) is None  # черновик не создан
    assert cq.answers and cq.answers[-1][1] is True  # show_alert=True (stale)


# ── P3: /addkeys — свой список файлом/ссылкой/текстом (фидбэк заказчика 2026-07-06) ─
async def test_addkeys_flow_text_list_to_pending_proposal():
    """/addkeys → кампания → текстовый список → тип → ТОЛЬКО pending-черновик add_keywords."""
    await init_db()
    bm._KW_ADD.clear()
    chat_id = 30_720
    state = FakeState()
    m = FakeMessage(chat_id=chat_id, text="/addkeys")
    await bm.addkeys_start(m, state)
    assert await state.get_state() == bm.KwAdd.awaiting_campaign
    token = (await state.get_data())["kw_add_token"]
    assert bm._KW_ADD[token]["keywords"] == []  # свой список, кандидатов нет

    m2 = FakeMessage(chat_id=chat_id, text="Search Spring")
    await bm.kw_add_campaign(m2, state)
    assert await state.get_state() == bm.KwAdd.awaiting_keywords
    # без кандидатов — просим файл/ссылку/текст, а не показываем пустой список
    assert any("xlsx" in (t or "") for t, _ in m2.answers)

    await bm.kw_add_keywords(
        FakeMessage(chat_id=chat_id, text="купить авто\nавто из японии, купить авто"), state
    )
    sess = bm._KW_ADD[token]
    assert sess["keywords"] == ["купить авто", "авто из японии"]  # дедуп, порядок

    await bm.on_kw_add_match(
        FakeCallbackQuery(FakeMessage(chat_id=chat_id)),
        KwAddCB(action="match", token=token, mt="exact"),
    )
    cid = bm._LAST_PENDING.get(chat_id)
    snap = await ConfirmStore().get_confirmed(cid)
    assert snap.operation == "add_keywords" and snap.status == "pending"
    assert snap.params["keywords"] == ["купить авто", "авто из японии"]


async def test_addkeys_accepts_sheet_link():
    """Ссылка на Google Sheets в awaiting_keywords → read_keyword_column → выбор типа."""
    await init_db()
    bm._KW_ADD.clear()
    chat_id = 30_721
    state = FakeState()
    await bm.addkeys_start(FakeMessage(chat_id=chat_id), state)
    token = (await state.get_data())["kw_add_token"]
    await bm.kw_add_campaign(FakeMessage(chat_id=chat_id, text="Search Spring"), state)

    import bot.handlers.keywords_flow as kf
    import reports.sheets as rs

    with patched(rs, "read_keyword_column", lambda sid: ["kw one", "kw two"]):
        assert kf  # ссылка идёт через reports.sheets, патчим модуль-источник
        await bm.kw_add_keywords(
            FakeMessage(
                chat_id=chat_id,
                text="https://docs.google.com/spreadsheets/d/ABC123_xyz-9/edit",
            ),
            state,
        )
    assert bm._KW_ADD[token]["keywords"] == ["kw one", "kw two"]
    assert await state.get_state() is None  # перешли к выбору типа (state очищен)


async def test_addkeys_sheet_read_failure_keeps_state_with_hint():
    """Приватная таблица → понятная подсказка про доступ, остаёмся в awaiting_keywords."""
    await init_db()
    bm._KW_ADD.clear()
    chat_id = 30_722
    state = FakeState()
    await bm.addkeys_start(FakeMessage(chat_id=chat_id), state)
    await bm.kw_add_campaign(FakeMessage(chat_id=chat_id, text="Search Spring"), state)

    import reports.sheets as rs

    def _boom(sid):
        raise RuntimeError("permission denied")

    m = FakeMessage(
        chat_id=chat_id, text="https://docs.google.com/spreadsheets/d/ABC123_xyz-9/edit"
    )
    with patched(rs, "read_keyword_column", _boom):
        await bm.kw_add_keywords(m, state)
    assert await state.get_state() == bm.KwAdd.awaiting_keywords  # остаёмся в шаге
    assert m.answers  # подсказка про доступ отправлена


async def test_addkeys_accepts_document():
    """Файл xlsx/csv/txt в awaiting_keywords → список ключей (не задача агенту)."""
    await init_db()
    bm._KW_ADD.clear()
    chat_id = 30_723
    state = FakeState()
    await bm.addkeys_start(FakeMessage(chat_id=chat_id), state)
    token = (await state.get_data())["kw_add_token"]
    await bm.kw_add_campaign(FakeMessage(chat_id=chat_id, text="Search Spring"), state)

    m = FakeMessage(chat_id=chat_id)
    await bm.kw_add_from_document(m, state, "kw alpha\nkw beta\n", "keys.txt")
    assert bm._KW_ADD[token]["keywords"] == ["kw alpha", "kw beta"]


# ── Ревью 2026-07-07: диспатч-уровень (кнопка меню, документ, ссылка, живучесть сессии) ─
def test_keyword_state_text_handlers_require_f_text():
    """Класс-гард: state-хендлеры этапов «жду ключи» ОБЯЗАНЫ иметь F.text — без него они
    перехватывают ДОКУМЕНТЫ раньше fallback.on_document (файл ключей молча умирал)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    kf = (root / "bot" / "handlers" / "keywords_flow.py").read_text(encoding="utf-8")
    cw = (root / "bot" / "handlers" / "campaign_wizard.py").read_text(encoding="utf-8")
    assert "@bm.dp.message(bm.KwAdd.awaiting_keywords, bm.F.text)" in kf
    assert "@bm.dp.message(bm.CreateCampaignWizard.keywords, bm.F.text)" in cw


async def test_on_more_addkeys_routes_to_flow():
    """Кнопка «➕ Ключи в кампанию» в хабе «Ещё» реально стартует /addkeys-флоу."""
    from bot.callbacks import MoreCB

    await init_db()
    bm._KW_ADD.clear()
    chat_id = 30_730
    state = FakeState()
    msg = FakeMessage(chat_id=chat_id)
    await bm.on_more(FakeCallbackQuery(msg, uid=chat_id), MoreCB(action="addkeys"), state)
    assert await state.get_state() == bm.KwAdd.awaiting_campaign
    token = (await state.get_data()).get("kw_add_token")
    assert token in bm._KW_ADD


async def test_on_document_routes_to_kw_add(monkeypatch):
    """Файл в состоянии KwAdd.awaiting_keywords проходит ЧЕРЕЗ on_document (реальный роутинг),
    а не только прямым вызовом kw_add_from_document."""
    from types import SimpleNamespace

    await init_db()
    bm._KW_ADD.clear()
    chat_id = 30_731
    state = FakeState()
    await bm.addkeys_start(FakeMessage(chat_id=chat_id), state)
    token = (await state.get_data())["kw_add_token"]
    await bm.kw_add_campaign(FakeMessage(chat_id=chat_id, text="Search Spring"), state)
    assert await state.get_state() == bm.KwAdd.awaiting_keywords

    class FakeBot:
        async def download(self, doc, destination):
            destination.write(b"stub")

    monkeypatch.setattr(bm.ingest, "extract_file_text", lambda name, data: "kw alpha\nkw beta")
    m = FakeMessage(chat_id=chat_id)
    m.document = SimpleNamespace(file_size=10, mime_type="text/plain", file_name="keys.txt")
    m.caption = None
    from bot.handlers.fallback import on_document

    await on_document(m, state, FakeBot())
    assert bm._KW_ADD[token]["keywords"] == ["kw alpha", "kw beta"]


async def test_match_validation_failure_keeps_session_alive():
    """Ревью 2026-07-07: ошибка валидации схемы НЕ расходует сессию — кнопки типа соответствия
    остаются рабочими (раньше pop до сборки превращал всё в kw_add_stale-тупик)."""
    await init_db()
    bm._KW_ADD.clear()
    chat_id = 30_732
    token = bm._kw_add_put(["x" * 200], "manual")  # заведомо невалидный ключ (>80 симв.)
    bm._KW_ADD[token]["campaign"] = "Search Spring"
    bm._KW_ADD[token]["keywords"] = ["x" * 200]
    cq = FakeCallbackQuery(FakeMessage(chat_id=chat_id))
    await bm.on_kw_add_match(cq, KwAddCB(action="match", token=token, mt="broad"))
    assert token in bm._KW_ADD  # сессия жива — можно исправить список и повторить
    assert cq.answers and cq.answers[-1][1] is True  # понятный alert


async def test_addkeys_sheet_cells_sanitized(monkeypatch):
    """B5-класс: мусорная ячейка колонки A (>10 слов) отбрасывается с пометкой, валидные идут дальше."""
    await init_db()
    bm._KW_ADD.clear()
    chat_id = 30_733
    state = FakeState()
    await bm.addkeys_start(FakeMessage(chat_id=chat_id), state)
    token = (await state.get_data())["kw_add_token"]
    await bm.kw_add_campaign(FakeMessage(chat_id=chat_id, text="Search Spring"), state)

    import reports.sheets as rs

    junk = "слово " * 12  # >10 слов — assert_keyword_ok отверг бы весь батч
    with patched(rs, "read_keyword_column", lambda sid: ["kw one", junk.strip(), "kw two"]):
        m = FakeMessage(
            chat_id=chat_id, text="https://docs.google.com/spreadsheets/d/ABC123_xyz-9/edit"
        )
        await bm.kw_add_keywords(m, state)
    assert bm._KW_ADD[token]["keywords"] == ["kw one", "kw two"]
    assert any("Отброшено" in (t or "") for t, _ in m.answers)  # честная пометка


async def test_addkeys_unparseable_sheets_url_gives_hint_not_keyword():
    """Ревью 2026-07-07: ссылка формы /u/<n>/d/… не парсится — подсказка, а НЕ literal-ключ."""
    await init_db()
    bm._KW_ADD.clear()
    chat_id = 30_734
    state = FakeState()
    await bm.addkeys_start(FakeMessage(chat_id=chat_id), state)
    token = (await state.get_data())["kw_add_token"]
    await bm.kw_add_campaign(FakeMessage(chat_id=chat_id, text="Search Spring"), state)
    m = FakeMessage(
        chat_id=chat_id,
        text="https://docs.google.com/spreadsheets/u/1/d/ABCDEFGHIJKLMNOPQRSTUVWX/edit#gid=0",
    )
    await bm.kw_add_keywords(m, state)
    assert await state.get_state() == bm.KwAdd.awaiting_keywords  # остаёмся в шаге
    assert bm._KW_ADD[token]["keywords"] == []  # URL НЕ стал «ключевым словом»
