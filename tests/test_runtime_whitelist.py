"""P0-A: рантайм-whitelist (env ∪ БД) + admin /adduser bulk-грант чтения (§6/§12).

Закрывает разрыв аудита: whitelist был только env (новый оператор — правка .env + рестарт). Теперь
админ добавляет оператора командой /adduser без рестарта; is_whitelisted пускает env ∪ БД, fail-closed
(пустое объединение блокирует всех). Грант чтения НЕ открывает мутаций (инвариант сохраняется).

Реальная БД на temp SQLite (init_db создаёт таблицу whitelist из модели).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contextlib import contextmanager  # noqa: E402

from core.access import (  # noqa: E402
    add_whitelisted_user,
    ensure_account_allowed_for_user,
    is_whitelisted,
    list_whitelisted_users,
    remove_whitelisted_user,
    whitelisted_ids,
    _invalidate_whitelist_cache,
)
from core.config import settings  # noqa: E402
from db.session import init_db  # noqa: E402

DRAFT = "7753643025"
NEW_OP = 900900900


@contextmanager
def _env_whitelist(value: str):
    orig = settings.telegram_whitelist_chat_ids
    settings.telegram_whitelist_chat_ids = value
    _invalidate_whitelist_cache()
    try:
        yield
    finally:
        settings.telegram_whitelist_chat_ids = orig
        _invalidate_whitelist_cache()


@pytest.fixture(autouse=True)
async def _clean(monkeypatch):
    await init_db()
    await remove_whitelisted_user(NEW_OP)  # чистый старт (temp БД переживает между тестами)
    _invalidate_whitelist_cache()
    yield
    await remove_whitelisted_user(NEW_OP)


async def test_env_user_passes_without_db():
    with _env_whitelist("111,222"):
        assert await is_whitelisted(111) is True
        assert await is_whitelisted(222) is True


async def test_unknown_user_denied_fail_closed():
    with _env_whitelist(""):  # пустое объединение (env пуст, БД пуста) — блок ВСЕХ
        assert await is_whitelisted(NEW_OP) is False
        assert await is_whitelisted(None) is False


async def test_adduser_lets_operator_in_without_restart():
    with _env_whitelist("111"):  # NEW_OP не в env
        assert await is_whitelisted(NEW_OP) is False
        added = await add_whitelisted_user(NEW_OP, added_by=111, note="оператор")
        assert added is True
        assert await is_whitelisted(NEW_OP) is True  # кэш инвалидирован сразу (без рестарта)


async def test_adduser_idempotent():
    added1 = await add_whitelisted_user(NEW_OP, added_by=1)
    added2 = await add_whitelisted_user(NEW_OP, added_by=1)
    assert added1 is True and added2 is False
    rows = await list_whitelisted_users()
    assert sum(1 for r in rows if r.chat_id == NEW_OP) == 1


async def test_removeuser_revokes_access():
    with _env_whitelist("111"):
        await add_whitelisted_user(NEW_OP)
        assert await is_whitelisted(NEW_OP) is True
        await remove_whitelisted_user(NEW_OP)
        assert await is_whitelisted(NEW_OP) is False


async def test_whitelisted_ids_union_env_and_db():
    """D9: whitelisted_ids() = env ∪ БД (весь набор операторов). Планировщик берёт этот набор для
    плановых отчётов/аномалий/advise, поэтому рантайм-добавленный /adduser оператор получает рассылки
    БЕЗ рестарта (раньше scheduler._recipients был env-only)."""
    with _env_whitelist("111"):  # env-бутстрап оператор
        await add_whitelisted_user(NEW_OP, added_by=111)  # рантайм-оператор в БД
        ids = await whitelisted_ids()
        assert 111 in ids and NEW_OP in ids  # env ∪ БД


async def test_scheduler_recipients_include_runtime_operator():
    """D9 (интеграция): scheduler._recipients() (теперь корутина) отдаёт env ∪ БД — рантайм-оператор
    в списке получателей плановой рассылки."""
    from scheduler import jobs

    with _env_whitelist("111"):
        await add_whitelisted_user(NEW_OP, added_by=111)
        recips = await jobs._recipients()
        assert 111 in recips and NEW_OP in recips


async def test_grant_read_does_not_open_mutations(monkeypatch):
    """Даже добавленный оператор с грантом чтения НЕ получает право мутаций: ensure_allowed
    (замок Draft/потолок) — отдельный чокпойнт, грант account_access его не трогает."""
    from ads.client import ensure_allowed

    monkeypatch.setattr(settings, "account_access_mode", "enforced")
    await add_whitelisted_user(NEW_OP)
    # чтение Draft разрешено; мутация чужого аккаунта — всё равно отказ на ensure_allowed
    await ensure_account_allowed_for_user(NEW_OP, DRAFT)
    with pytest.raises(PermissionError):
        ensure_allowed("1234567890")  # не в мутационном потолке — блок
