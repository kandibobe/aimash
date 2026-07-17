"""3A: кнопки главного меню работают ВО ВРЕМЯ визардов (menu_guard) без потери работы.

Исходный баг: state-хендлеры визардов глотали подпись кнопки как ввод, если хендлер кнопки жил
в позже импортируемом модуле («ℹ️ Клиенты» во время визарда §19 уходила в LLM как описание
кампании). Гард регистрируется первым, мягко сворачивает флоу (черновик §19 остаётся active,
буфер §20 сбрасывается в confirm-черновик) и выполняет кнопку.
"""

from __future__ import annotations

import inspect
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402
from bot import keyboards as kb  # noqa: E402
from bot.handlers import menu_guard  # noqa: E402
from db.session import init_db  # noqa: E402


def _menu_texts(markup) -> list[str]:
    return [b.text for row in markup.keyboard for b in row]


def test_main_menu_buttons_registered():
    """Аудит #4b: каждая ВИДИМАЯ кнопка главного экрана (RU и EN) обязана быть в ALL_MENU_BUTTONS —
    иначе во время визарда btn_guard_menu её не поймает и подпись уйдёт в LLM (мёртвая кнопка)."""
    for lang in ("ru", "en"):
        for text in _menu_texts(kb.main_menu(lang)):
            assert text in kb.ALL_MENU_BUTTONS, (
                f"{text!r} видима в main_menu({lang}), но не в ALL_MENU_BUTTONS — "
                "во время визарда станет мёртвой (гард её не перехватит)"
            )


def test_every_menu_button_has_dispatch_branch():
    """Аудит #4b (гард класса «мёртвая кнопка»): множество BTN_*_ALL, разводимых в
    _dispatch_menu_button, обязано СОВПАДАТЬ с ALL_MENU_BUTTONS. Добавил кнопку в ALL_MENU_BUTTONS,
    но забыл ветку диспатча → btn_guard_menu «съест» её без действия. Забыл убрать ветку у снятой
    кнопки → orphan. Оба случая ловятся этим равенством."""
    src = inspect.getsource(menu_guard._dispatch_menu_button)
    referenced = set(re.findall(r"bm\.(BTN_\w+_ALL)", src))
    dispatched: frozenset[str] = frozenset()
    for name in referenced:
        dispatched |= getattr(kb, name)
    missing = kb.ALL_MENU_BUTTONS - dispatched
    orphan = dispatched - kb.ALL_MENU_BUTTONS
    assert not missing, (
        f"в ALL_MENU_BUTTONS, но нет ветки в _dispatch_menu_button: {sorted(missing)}"
    )
    assert not orphan, (
        f"ветка в _dispatch_menu_button на кнопку вне ALL_MENU_BUTTONS: {sorted(orphan)}"
    )


def _hub_actions(markup) -> set[str]:
    out: set[str] = set()
    for row in markup.inline_keyboard:
        for b in row:
            if b.callback_data:
                out.add(bm.MoreCB.unpack(b.callback_data).action)
    return out


def test_hub_buttons_have_on_more_route():
    """Аудит #4b (гард класса «мёртвая инлайн-кнопка»): каждая MoreCB-кнопка хабов (Создать/Отчёты/
    Ещё/Сервис) обязана иметь ветку в on_more — иначе клик делает cq.answer() и молча ничего.
    Гарантирует, что 14 добавленных маршрутов покрывают все кнопки, и ловит будущий рассинхрон."""
    from bot.handlers import commands as cmd

    routed = set(re.findall(r'action == "(\w+)"', inspect.getsource(cmd.on_more)))
    hub_actions: set[str] = set()
    for factory in (kb.create_menu_kb, kb.reports_menu_kb, kb.more_menu_kb, kb.service_menu_kb):
        hub_actions |= _hub_actions(factory())
    missing = hub_actions - routed
    assert not missing, f"кнопки хаба без маршрута в on_more (мёртвые при клике): {sorted(missing)}"


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


class FakeBot:
    def __init__(self):
        self.sent: list = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class FakeMessage:
    def __init__(self, chat_id: int, text: str):
        self.chat = SimpleNamespace(id=chat_id)
        self.text = text
        self.bot = FakeBot()
        self.answers: list = []
        self.answer_kwargs: list[dict] = []

    async def answer(self, text: str = "", **kw):
        self.answers.append(text)
        self.answer_kwargs.append(kw)
        return self


class FakeState:
    def __init__(self, state=None, data=None):
        self._state = state
        self._data = dict(data or {})

    async def get_state(self):
        return self._state

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kw):
        self._data.update(kw)

    async def set_state(self, s):
        self._state = s

    async def clear(self):
        self._state = None
        self._data = {}


def test_guard_registered_before_state_handlers():
    """Инвариант порядка: btn_guard_menu — раньше state-хендлеров визардов (иначе кнопку съест
    ввод), on_text — последний (существующий инвариант не сломан)."""
    names = [h.callback.__name__ for h in bm.dp.message.handlers]
    assert "btn_guard_menu" in names
    gpos = names.index("btn_guard_menu")
    for state_handler in ("cc_settings_desc", "cli_accumulate_text", "gdn_brief"):
        if state_handler in names:
            assert gpos < names.index(state_handler), (
                f"btn_guard_menu должен стоять РАНЬШЕ {state_handler}"
            )
    assert names[-1] == "on_text"


async def test_button_interrupts_cc_wizard_preserves_draft(monkeypatch):
    """Кнопка «📊 Статистика» во время визарда §19: entry вызван, state очищен, черновик ЖИВ
    (active, возврат через «▶️ Продолжить»), пользователю — подсказка с шагом."""
    await init_db()
    chat_id = 91_001
    sid = await bm.CDRAFTS.create(chat_id=chat_id, customer_id=bm.DRAFT_ACCOUNT_ID)
    await bm.CDRAFTS.set_step(sid, 3, expected_chat_id=chat_id)

    called = {"n": 0}

    async def _fake_status(m):
        called["n"] += 1

    m = FakeMessage(chat_id, next(iter(kb.BTN_STATUS_ALL)))
    state = FakeState(state="CreateCampaignWizard:settings_desc", data={"cc_session": sid})
    with patched(bm, "_send_status", _fake_status):
        await bm.btn_guard_menu(m, state)

    assert called["n"] == 1  # кнопка сработала (раньше «съедалась» как ввод)
    assert await state.get_state() is None
    snap = await bm.CDRAFTS.get(sid, expected_chat_id=chat_id)
    assert snap is not None and snap.status == "active"  # работа НЕ потеряна
    assert any("шаг 3/7" in a or "step 3/7" in a for a in m.answers)  # подсказка о возврате


async def test_btn_clients_during_wizard_dispatches(monkeypatch):
    """Исходный баг: «ℹ️ Клиенты» во время визарда уходила в LLM как описание кампании.
    Теперь — вызывается entry клиентов."""
    await init_db()
    called = {"n": 0}

    async def _fake_clients(m):
        called["n"] += 1

    m = FakeMessage(91_002, next(iter(kb.BTN_CLIENTS_ALL)))
    state = FakeState(state="CreateCampaignWizard:settings_desc", data={})
    with patched(bm, "_cli_present_accounts", _fake_clients):
        await bm.btn_guard_menu(m, state)
    assert called["n"] == 1


async def test_client_buffer_flushed_to_proposal(monkeypatch):
    """Буфер §20 при нажатии кнопки НЕ теряется: сбрасывается через _cli_extract_and_propose."""
    await init_db()
    chat_id = 91_003
    bm._CLI_TEXT_BUF[chat_id] = ["Kasi Motors — автодилер"]

    flushed = {}

    async def _fake_extract(bot, cid, cust, buf):
        flushed.update(cid=cid, cust=cust, buf=list(buf))
        return True

    called = {"n": 0}

    async def _fake_status(m):
        called["n"] += 1

    m = FakeMessage(chat_id, next(iter(kb.BTN_STATUS_ALL)))
    state = FakeState(
        state="ClientInfoWizard:awaiting_text",
        data={"cli_customer_id": bm.DRAFT_ACCOUNT_ID},
    )
    with (
        patched(bm, "_cli_extract_and_propose", _fake_extract),
        patched(bm, "_send_status", _fake_status),
    ):
        await bm.btn_guard_menu(m, state)

    assert flushed["buf"] == ["Kasi Motors — автодилер"]  # буфер ушёл в черновик, не в мусор
    assert bm._CLI_TEXT_BUF.get(chat_id) is None
    assert called["n"] == 1
    assert any("черновик" in a.lower() or "draft" in a.lower() for a in m.answers)


async def test_en_captions_matched(monkeypatch):
    """EN-подпись кнопки тоже перехватывается (BTN_*_ALL — оба языка)."""
    await init_db()
    called = {"n": 0}

    async def _fake_status(m):
        called["n"] += 1

    m = FakeMessage(91_004, kb.BTN_STATUS["en"])
    state = FakeState(state="KwWizard:awaiting_seeds", data={})
    with patched(bm, "_send_status", _fake_status):
        await bm.btn_guard_menu(m, state)
    assert called["n"] == 1


async def test_no_state_peaceful_path(monkeypatch):
    """state=None (мирный путь): гард просто выполняет кнопку — suspend не нужен, подсказок нет."""
    await init_db()
    called = {"n": 0}

    async def _fake_status(m):
        called["n"] += 1

    m = FakeMessage(91_005, kb.BTN_STATUS["ru"])
    state = FakeState(state=None)
    with patched(bm, "_send_status", _fake_status):
        await bm.btn_guard_menu(m, state)
    assert called["n"] == 1
    assert m.answers == []  # никаких «черновик сохранён» на мирном пути


class FakeCQ:
    """Дакт-фейк CallbackQuery: _cq_msg пропускает не-InaccessibleMessage (см. его docstring)."""

    def __init__(self, m: FakeMessage):
        self.message = m
        self.from_user = SimpleNamespace(id=m.chat.id, username="t")

    async def answer(self, *a, **kw):
        return None


async def test_hub_titles_sent_as_html():
    """Замечания 1/8 (скриншоты 2026-07-17): Bot создан БЕЗ DefaultBotProperties ⇒ parse_mode=None
    по умолчанию, и заголовок хаба с <b> уходил в чат литеральным «</b>». Гард класса: каждый
    хаб-хендлер (Создать/Отчёты/Ещё/Сервис), чей заголовок содержит HTML, обязан явно передать
    parse_mode=HTML — на обоих языках."""
    from bot.handlers import commands as cmd

    async def run_service_hub(m: FakeMessage) -> None:
        await cmd.on_more(FakeCQ(m), bm.MoreCB(action="service"), FakeState())

    for lang in ("ru", "en"):
        token = bm.i18n.set_current_lang(lang)
        try:
            for run, key in (
                (cmd.btn_create, "create_menu_title"),
                (cmd.btn_reports, "reports_menu_title"),
                (cmd.btn_more, "more_menu_title"),
                (run_service_hub, "service_menu_title"),
            ):
                m = FakeMessage(91_006, "x")
                await run(m)
                assert m.answers, f"{key} ({lang}): хаб ничего не отправил"
                sent = m.answers[-1]
                if "<" in sent:
                    assert m.answer_kwargs[-1].get("parse_mode") == bm.ParseMode.HTML, (
                        f"{key} ({lang}): HTML-заголовок без parse_mode=HTML — "
                        "в чате будет литеральный тег"
                    )
        finally:
            bm.i18n.reset_current_lang(token)
