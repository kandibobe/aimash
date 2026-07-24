"""C8 (аудит 2026-07): пикеры длинных списков ОБЯЗАНЫ пагинировать.

Кнопка на каждый элемент без страниц на крупном аккаунте (50+ кампаний/аудиторий) даёт
REPLY_MARKUP_TOO_LONG — Telegram отвергает всю клавиатуру, и флоу мёртв. Класс-гард: у всех
пикеров списков число кнопок на странице ограничено (_CAMP_PAGE + навигация + служебные).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.keyboards import (  # noqa: E402
    _CAMP_PAGE,
    audiences_kb,
    campaigns_kb,
    kw_add_campaigns_kb,
    report_campaigns_kb,
    rsa_pick_campaigns_kb,
    slash_mutate_campaigns_kb,
)


def _buttons(markup) -> list:
    return [b for row in markup.inline_keyboard for b in row]


class _Msg:
    def __init__(self, chat_id):
        self.chat = SimpleNamespace(id=chat_id)


class _Cq:
    """Минимальный callback_query для on_page_nav: даёт chat_id и считает show_alert."""

    def __init__(self, chat_id):
        self.message = _Msg(chat_id)
        self.from_user = SimpleNamespace(id=chat_id, username="op")
        self.alerts = 0

    async def answer(self, *a, **kw):
        if kw.get("show_alert"):
            self.alerts += 1


def _camps(n: int) -> list[dict]:
    return [{"name": f"Кампания {i}", "status": "ENABLED", "id": str(i)} for i in range(n)]


def _auds(n: int) -> list:
    return [
        SimpleNamespace(resource_name=f"customers/1/userLists/{i}", name=f"Ауд {i}", size=10)
        for i in range(n)
    ]


_CAP = _CAMP_PAGE + 6  # страница + ряд навигации + служебные кнопки (назад и т.п.)


def test_rsa_picker_paginates_large_list():
    markup = rsa_pick_campaigns_kb(_camps(120))
    assert len(_buttons(markup)) <= _CAP  # раньше 120 кнопок → REPLY_MARKUP_TOO_LONG
    # последняя страница добирается навигацией и несёт ХВОСТ списка (глобальные idx)
    last = rsa_pick_campaigns_kb(_camps(120), page=11)
    texts = [b.text for b in _buttons(last)]
    assert any("119" in t for t in texts)


def test_audiences_picker_paginates_large_list():
    markup = audiences_kb(_auds(120), camp_idx=0)
    assert len(_buttons(markup)) <= _CAP
    # прикреплённые (немного) показываются всегда, поверх страницы доступных
    with_att = audiences_kb(_auds(120), camp_idx=0, attached=_auds(2))
    labels = [b.text for b in _buttons(with_att)]
    assert sum("Открепить" in t or "Detach" in t for t in labels) == 2


def test_addkeys_picker_paginates_large_list():
    """#2: /addkeys на крупном аккаунте показывал лишь первые 10 кампаний — остальные были
    невидимы (кнопки нет, навигации нет). Пагинация PageCB kind='kwadd', target=token."""
    from bot.callbacks import KwAddCB, PageCB

    markup = kw_add_campaigns_kb(_camps(120), "tok123")
    assert len(_buttons(markup)) <= _CAP  # раньше 10 + Отмена, хвост недостижим
    # nav-ряд несёт token в target, чтобы on_page_nav перерисовал ту же сессию
    navs = [
        PageCB.unpack(b.callback_data) for b in _buttons(markup) if b.callback_data.startswith("pg")
    ]
    assert navs and all(p.kind == "kwadd" and p.target == "tok123" for p in navs)
    # последняя страница добирается навигацией и несёт ХВОСТ (глобальные idx)
    last = kw_add_campaigns_kb(_camps(120), "tok123", page=11)
    picks = [
        KwAddCB.unpack(b.callback_data)
        for b in _buttons(last)
        if b.callback_data.startswith("kwadd") and KwAddCB.unpack(b.callback_data).action == "camp"
    ]
    assert any(p.idx == 119 for p in picks)  # глобальный индекс, не 0..9


def test_slash_mutate_picker_paginates_large_list():
    """#2: /pause · /resume без аргумента на крупном аккаунте — тот же баг видимости."""
    from bot.callbacks import PageCB, SlashMutCB

    markup = slash_mutate_campaigns_kb(_camps(120), "pause_campaign")
    assert len(_buttons(markup)) <= _CAP
    navs = [
        PageCB.unpack(b.callback_data) for b in _buttons(markup) if b.callback_data.startswith("pg")
    ]
    assert navs and all(p.kind == "smut" and p.target == "pause_campaign" for p in navs)
    last = slash_mutate_campaigns_kb(_camps(120), "pause_campaign", page=11)
    picks = [
        SlashMutCB.unpack(b.callback_data)
        for b in _buttons(last)
        if b.callback_data.startswith("smut")
    ]
    assert any(p.idx == 119 for p in picks)


async def test_page_nav_rerenders_addkeys_and_slash_mutate(monkeypatch):
    """#2: клик по nav-ряду (PageCB kind='kwadd'/'smut') перерисовывает СЛЕДУЮЩУЮ страницу того же
    кэша — иначе кнопки '‹ ›' были бы мёртвыми и хвост списка недостижим."""
    import bot.main as bm
    from bot.callbacks import KwAddCB, PageCB, SlashMutCB
    from bot.handlers.reports import on_page_nav

    captured: dict = {}

    async def fake_edit(cq, markup):
        captured["markup"] = markup

    monkeypatch.setattr(bm, "_safe_edit_markup", fake_edit)

    chat_id = 7101
    cq = _Cq(chat_id)

    # kwadd: кэш есть, gen совпадает → страница 1 несёт глобальные idx 10..19
    bm._KW_ADD_CAMP_CACHE[chat_id] = _camps(120)
    bm._KW_ADD_CAMP_GEN[chat_id] = 3
    await on_page_nav(cq, PageCB(kind="kwadd", target="tok", page=1))
    picks = [
        KwAddCB.unpack(b.callback_data)
        for b in _buttons(captured["markup"])
        if b.callback_data.startswith("kwadd") and KwAddCB.unpack(b.callback_data).action == "camp"
    ]
    assert {p.idx for p in picks} == set(range(10, 20)) and all(p.gen == 3 for p in picks)

    # smut: кэш есть → страница 1, глобальные idx 10..19, op пробрасывается из target
    captured.clear()
    bm._SLASH_MUT_CACHE[chat_id] = _camps(120)
    bm._SLASH_MUT_GEN[chat_id] = 5
    await on_page_nav(cq, PageCB(kind="smut", target="pause_campaign", page=1))
    smuts = [
        SlashMutCB.unpack(b.callback_data)
        for b in _buttons(captured["markup"])
        if b.callback_data.startswith("smut")
    ]
    assert {p.idx for p in smuts} == set(range(10, 20))
    assert all(p.op == "pause_campaign" and p.gen == 5 for p in smuts)


async def test_page_nav_stale_when_cache_lost(monkeypatch):
    """#2: кэш потерян (рестарт) → stale-alert, без правки разметки (не воскрешаем пустой пикер)."""
    import bot.main as bm
    from bot.callbacks import PageCB
    from bot.handlers.reports import on_page_nav

    edited = {"n": 0}

    async def fake_edit(cq, markup):
        edited["n"] += 1

    monkeypatch.setattr(bm, "_safe_edit_markup", fake_edit)
    chat_id = 7102
    bm._KW_ADD_CAMP_CACHE.pop(chat_id, None)
    bm._SLASH_MUT_CACHE.pop(chat_id, None)

    for kind, target in (("kwadd", "tok"), ("smut", "pause_campaign")):
        cq = _Cq(chat_id)
        await on_page_nav(cq, PageCB(kind=kind, target=target, page=1))
        assert cq.alerts == 1  # stale-alert показан
    assert edited["n"] == 0  # разметку не трогали


def test_all_list_pickers_bounded_class_guard():
    """Гард класса: каждый пикер списков держит страницу в пределах _CAP при 200 элементах."""
    cases = [
        campaigns_kb(_camps(200)),
        report_campaigns_kb(_camps(200), "report"),
        rsa_pick_campaigns_kb(_camps(200)),
        audiences_kb(_auds(200), camp_idx=0),
        kw_add_campaigns_kb(_camps(200), "tok"),
        slash_mutate_campaigns_kb(_camps(200), "pause_campaign"),
    ]
    for markup in cases:
        assert len(_buttons(markup)) <= _CAP, [b.text for b in _buttons(markup)][:20]
