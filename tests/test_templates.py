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
