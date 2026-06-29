"""§2B: CRUD именованных шаблонов кампаний (db/templates.py). Реальный temp SQLite (conftest).

Проверяем: таблица создаётся init_db (create_all на SQLite); save→list→get→delete; upsert по
(chat_id, name) перезаписывает; изоляция по chat_id; нормализацию/кап имени.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.session import init_db  # noqa: E402
from db.templates import (  # noqa: E402
    delete_template,
    get_template,
    list_templates,
    save_template,
)

_PARAMS = {
    "campaign_name": "X",
    "final_url": "https://shop.ua",
    "headlines": ["A", "B", "C"],
    "descriptions": ["d1", "d2"],
    "budget_daily_micros": 40_000_000,
    "keywords": ["k1", "k2"],
    "match_type": "phrase",
    "cpc_bid_micros": 500_000,
}


async def test_table_created_by_init_db():
    await init_db()  # create_all на SQLite должен создать campaign_templates
    # если таблицы нет — следующий вызов бросит OperationalError
    assert await list_templates(99_999) == []


async def test_save_list_get_delete_roundtrip():
    await init_db()
    chat = 51_001
    await save_template(chat_id=chat, name="авто", params=_PARAMS, source_campaign="Search Spring")
    rows = await list_templates(chat)
    assert [r.name for r in rows] == ["авто"]
    assert rows[0].source_campaign == "Search Spring"

    got = await get_template(chat, "авто")
    assert got is not None and got.params["final_url"] == "https://shop.ua"

    assert await delete_template(chat, "авто") is True
    assert await list_templates(chat) == []
    assert await delete_template(chat, "авто") is False  # уже нет


async def test_upsert_overwrites_by_chat_and_name():
    await init_db()
    chat = 51_002
    await save_template(chat_id=chat, name="t", params=_PARAMS, source_campaign=None)
    p2 = {**_PARAMS, "budget_daily_micros": 99_000_000}
    await save_template(chat_id=chat, name="t", params=p2, source_campaign="Live")
    rows = await list_templates(chat)
    assert len(rows) == 1  # не дубль, а перезапись
    assert rows[0].params["budget_daily_micros"] == 99_000_000
    assert rows[0].source_campaign == "Live"


async def test_chat_scoping_isolation():
    await init_db()
    await save_template(chat_id=51_003, name="shared", params=_PARAMS)
    await save_template(chat_id=51_004, name="shared", params=_PARAMS)  # то же имя, другой чат
    assert [r.name for r in await list_templates(51_003)] == ["shared"]
    assert await get_template(51_004, "shared") is not None
    # удаление в одном чате не трогает другой
    await delete_template(51_003, "shared")
    assert await list_templates(51_003) == []
    assert await get_template(51_004, "shared") is not None


async def test_empty_name_rejected():
    await init_db()
    import pytest

    with pytest.raises(ValueError):
        await save_template(chat_id=51_005, name="   ", params=_PARAMS)


# ── UI-контракт: клавиатура + парс аргумента + /savetemplate из последнего черновика ──
def test_templates_kb_has_use_and_del():
    from types import SimpleNamespace

    from bot.callbacks import TemplateCB
    from bot.keyboards import templates_kb

    rows = [SimpleNamespace(name="авто"), SimpleNamespace(name="лето")]
    cbs = [b.callback_data for row in templates_kb(rows).inline_keyboard for b in row]
    parsed = [TemplateCB.unpack(c) for c in cbs if c.startswith("tpl:")]
    assert {p.action for p in parsed} == {"use", "del"}
    assert any(p.action == "use" and p.idx == 1 for p in parsed)


def test_parse_savetemplate_arg():
    import bot.main as bm

    assert bm._parse_savetemplate_arg("авто") == ("авто", None)
    assert bm._parse_savetemplate_arg("авто from Search Spring") == ("авто", "Search Spring")
    # « from » берётся по ПОСЛЕДНЕМУ вхождению (имя может содержать слово from)
    assert bm._parse_savetemplate_arg("from us from Live") == ("from us", "Live")


class _FakeMessage:
    def __init__(self, chat_id: int):
        self.chat = type("C", (), {"id": chat_id})()
        self.answers: list = []

    async def answer(self, text: str = "", **kw):
        self.answers.append((text, kw))
        return self


async def test_savetemplate_from_last_draft():
    import bot.main as bm

    await init_db()
    chat = 51_010
    bm._LAST_SEARCH_PARAMS[chat] = dict(_PARAMS)
    msg = _FakeMessage(chat)
    cmd = type("Cmd", (), {"args": "мой шаблон"})()
    await bm.savetemplate_cmd(msg, cmd)
    saved = await get_template(chat, "мой шаблон")
    assert saved is not None
    assert saved.params["final_url"] == "https://shop.ua"
    assert saved.source_campaign is None


async def test_savetemplate_without_last_draft_hints():
    import bot.main as bm

    await init_db()
    chat = 51_011
    bm._LAST_SEARCH_PARAMS.pop(chat, None)
    msg = _FakeMessage(chat)
    cmd = type("Cmd", (), {"args": "пусто"})()
    await bm.savetemplate_cmd(msg, cmd)
    assert await get_template(chat, "пусто") is None  # ничего не сохранили
    assert msg.answers  # дали подсказку
