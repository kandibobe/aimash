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
    report_campaigns_kb,
    rsa_pick_campaigns_kb,
)


def _buttons(markup) -> list:
    return [b for row in markup.inline_keyboard for b in row]


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


def test_all_list_pickers_bounded_class_guard():
    """Гард класса: каждый пикер списков держит страницу в пределах _CAP при 200 элементах."""
    cases = [
        campaigns_kb(_camps(200)),
        report_campaigns_kb(_camps(200), "report"),
        rsa_pick_campaigns_kb(_camps(200)),
        audiences_kb(_auds(200), camp_idx=0),
    ]
    for markup in cases:
        assert len(_buttons(markup)) <= _CAP, [b.text for b in _buttons(markup)][:20]
