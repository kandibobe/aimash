"""D1 (удобство 2026-07): поиск кампании по названию в пикерах (/campaigns, отчёт, /rsa).

«🔎 Найти» появляется только на длинном списке; текст-запрос фильтрует кэш по подстроке
имени/id и рисует совпадения ГЛОБАЛЬНЫМИ индексами (выбор работает теми же callback-ами).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.callbacks import CampCB, PickSearchCB, ReportCampCB, RsaPickCB  # noqa: E402
from bot.keyboards import (  # noqa: E402
    _CAMP_PAGE,
    campaigns_kb,
    picker_search_kb,
    report_campaigns_kb,
    rsa_pick_campaigns_kb,
)


def _btns(markup) -> list:
    return [b for row in markup.inline_keyboard for b in row]


def _camps(n: int) -> list[dict]:
    return [{"name": f"Кампания {i}", "status": "ENABLED", "id": str(1000 + i)} for i in range(n)]


def _has_search(markup) -> bool:
    for b in _btns(markup):
        try:
            PickSearchCB.unpack(b.callback_data)
            return True
        except (ValueError, TypeError):
            continue
    return False


# ── «🔎 Найти» — только когда есть что искать (> одной страницы) ──────────────────
def test_search_button_only_on_long_lists():
    assert not _has_search(campaigns_kb(_camps(_CAMP_PAGE)))  # ровно страница — искать нечего
    assert _has_search(campaigns_kb(_camps(_CAMP_PAGE + 1)))
    assert not _has_search(report_campaigns_kb(_camps(3), "report"))
    assert _has_search(report_campaigns_kb(_camps(_CAMP_PAGE + 5), "report"))
    assert not _has_search(rsa_pick_campaigns_kb(_camps(2)))
    assert _has_search(rsa_pick_campaigns_kb(_camps(_CAMP_PAGE + 2)))


# ── picker_search_kb: глобальные индексы + правильный callback на вид ─────────────
def test_search_kb_emits_correct_callback_per_kind():
    camps = _camps(50)
    idx = [3, 17, 42]
    camp = picker_search_kb("campaigns", "", camps, idx)
    picks = [
        CampCB.unpack(b.callback_data) for b in _btns(camp) if b.callback_data.startswith("camp")
    ]
    assert [p.idx for p in picks] == idx and all(p.action == "menu" for p in picks)

    rep = picker_search_kb("report", "export", camps, idx)
    rpicks = [
        ReportCampCB.unpack(b.callback_data)
        for b in _btns(rep)
        if b.callback_data.startswith("rptc")
    ]
    assert [p.idx for p in rpicks] == idx and all(p.target == "export" for p in rpicks)

    rsa = picker_search_kb("rsa", "", camps, idx)
    spicks = [
        RsaPickCB.unpack(b.callback_data) for b in _btns(rsa) if b.callback_data.startswith("rsap")
    ]
    assert [p.idx for p in spicks] == idx and all(p.what == "camp" for p in spicks)


def test_search_kb_caps_at_page_and_has_show_all():
    camps = _camps(300)
    markup = picker_search_kb("campaigns", "", camps, list(range(300)))
    camp_rows = [b for b in _btns(markup) if b.callback_data.startswith("camp")]
    assert len(camp_rows) == _CAMP_PAGE  # >страницы совпадений усечены (уточните запрос)
    modes = [
        PickSearchCB.unpack(b.callback_data).mode
        for b in _btns(markup)
        if b.callback_data.startswith("psr")
    ]
    assert "all" in modes  # «↩︎ Показать все» присутствует


def test_search_kb_empty_indices_is_just_nav():
    """Экран-приглашение поиска (пустые indices): только «Показать все» + «Отмена», без кампаний."""
    markup = picker_search_kb("rsa", "", _camps(50), [])
    assert not any(b.callback_data.startswith("rsap") for b in _btns(markup))


# ── чистые хелперы фильтра/резолва в bot.main ────────────────────────────────────
def test_match_indices_by_name_and_id():
    import bot.main as bm

    camps = [
        {"name": "Летняя распродажа", "id": "111"},
        {"name": "Зимняя коллекция", "id": "222"},
        {"name": "SUMMER promo", "id": "333"},
    ]
    assert bm._picker_match_indices(camps, "летн") == [0]
    assert bm._picker_match_indices(camps, "summer") == [2]  # casefold, регистронезависимо
    assert bm._picker_match_indices(camps, "222") == [1]  # по id
    assert bm._picker_match_indices(camps, "") == [0, 1, 2]  # пусто → всё
    assert bm._picker_match_indices(camps, "неттакого") == []


def test_rest_state_rsa_vs_stateless():
    import bot.main as bm

    assert bm._picker_rest_state("rsa") is bm.RsaWizard.picking  # /rsa — гард on_text
    assert bm._picker_rest_state("campaigns") is None
    assert bm._picker_rest_state("report") is None


# ── handler: текст в PickerSearch.awaiting = фильтр, одноразовый ──────────────────
class _FakeState:
    def __init__(self, data):
        self._data = dict(data)
        self.state = None

    async def get_data(self):
        return dict(self._data)

    async def set_state(self, state):
        self.state = state


class _FakeMsg:
    def __init__(self, text, chat_id):
        self.text = text
        self.chat = SimpleNamespace(id=chat_id)
        self.sent: list = []

    async def answer(self, text, **kw):
        self.sent.append((text, kw))


async def test_query_handler_filters_and_clears_state():
    import bot.main as bm
    from bot.handlers.reports import on_picker_search_query

    chat_id = 4242
    bm._CAMP_CACHE[chat_id] = [
        {"name": "Бренд A", "id": "10", "status": "ENABLED"},
        {"name": "Дистрибьютор B", "id": "20", "status": "PAUSED"},
    ]
    st = _FakeState({"psrch_kind": "campaigns", "psrch_target": ""})
    m = _FakeMsg("бренд", chat_id)
    await on_picker_search_query(m, st)
    assert st.state is None  # /campaigns — состояние снято (одноразовость)
    # в разметке — ровно одна кнопка выбора кампании (idx=0) + служебные
    markup = m.sent[-1][1]["reply_markup"]
    picks = [
        CampCB.unpack(b.callback_data)
        for row in markup.inline_keyboard
        for b in row
        if b.callback_data.startswith("camp")
    ]
    assert [p.idx for p in picks] == [0]


async def test_query_handler_empty_shows_full_list():
    import bot.main as bm
    from bot.handlers.reports import on_picker_search_query

    chat_id = 4343
    bm._RSA_CAMP_CACHE[chat_id] = [{"name": "X", "id": "1", "status": "ENABLED"}]
    st = _FakeState({"psrch_kind": "rsa", "psrch_target": ""})
    m = _FakeMsg("нетсовпадений", chat_id)
    await on_picker_search_query(m, st)
    assert st.state is bm.RsaWizard.picking  # /rsa — вернулись в picking (гард on_text)
    text = m.sent[-1][0]
    assert "нетсовпадений" in text  # сообщение «ничего не нашёл» с эхо запроса


async def test_query_handler_stale_cache():
    from bot.handlers.reports import on_picker_search_query

    st = _FakeState({"psrch_kind": "campaigns", "psrch_target": ""})
    m = _FakeMsg("что-то", 999999)  # кэша нет
    await on_picker_search_query(m, st)
    assert st.state is None
    assert m.sent  # ответил stale, не упал


@pytest.mark.parametrize("kind", ["campaigns", "report", "rsa"])
def test_full_kb_dispatch(kind):
    import bot.main as bm

    markup = bm._picker_full_kb(kind, "report", _camps(3))
    assert markup.inline_keyboard  # собралась разметка нужного вида без исключений
