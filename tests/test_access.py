"""Пер-пользовательский доступ к аккаунтам + активный аккаунт чата (core.access, §8/§12).

Проверяют: Draft доступен без гранта (обратная совместимость), не-Draft — только по гранту
(fail-closed); активный аккаунт по умолчанию Draft, переключение персистится, а отзыв доступа/выход
аккаунта из read-list ⇒ тихий откат на Draft (НИКОГДА не чужой аккаунт). Реальная БД на temp SQLite.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.access import (  # noqa: E402
    ensure_account_allowed_for_user,
    get_active_account,
    grant_account_access,
    list_user_account_ids,
    revoke_account_access,
    set_active_account,
)
from core.config import settings  # noqa: E402
from db.session import init_db  # noqa: E402

DRAFT = "7753643025"
ACCT = "1112223334"
CHAT = 555


@contextmanager
def _read(read_ids: str, allowed: str = DRAFT):
    pa, pr = settings.google_ads_allowed_customer_ids, settings.google_ads_read_customer_ids
    settings.google_ads_allowed_customer_ids = allowed
    settings.google_ads_read_customer_ids = read_ids
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = pa
        settings.google_ads_read_customer_ids = pr


async def test_draft_allowed_without_grant_others_denied():
    await init_db()
    await ensure_account_allowed_for_user(CHAT, DRAFT)  # Draft — без гранта, не бросает
    with pytest.raises(PermissionError):
        await ensure_account_allowed_for_user(CHAT, ACCT)  # чужой без гранта — отказ


async def test_grant_makes_allowed_and_is_idempotent():
    await init_db()
    await revoke_account_access(CHAT, ACCT)  # чистый старт (temp БД переживает между тестами)
    await grant_account_access(CHAT, ACCT)
    await grant_account_access(CHAT, ACCT)  # повтор не должен падать/дублировать
    await ensure_account_allowed_for_user(CHAT, ACCT)  # теперь разрешён
    assert set(await list_user_account_ids(CHAT)) == {DRAFT, ACCT}
    await revoke_account_access(CHAT, ACCT)
    with pytest.raises(PermissionError):
        await ensure_account_allowed_for_user(CHAT, ACCT)


async def test_active_account_default_and_switch_and_revoke_fallback():
    await init_db()
    await revoke_account_access(CHAT, ACCT)
    await set_active_account(CHAT, DRAFT)
    assert await get_active_account(CHAT) == DRAFT  # дефолт/Draft

    # переключение на не-Draft требует И read-allowed, И гранта
    await grant_account_access(CHAT, ACCT)
    await set_active_account(CHAT, ACCT)
    with _read(read_ids=ACCT):
        assert await get_active_account(CHAT) == ACCT  # доступ есть → активен ACCT
        # отозвали грант → тихий откат на Draft (НЕ чужой аккаунт)
        await revoke_account_access(CHAT, ACCT)
        assert await get_active_account(CHAT) == DRAFT

    # даже с грантом, но БЕЗ read-list (вышел из доступа) → откат на Draft
    await grant_account_access(CHAT, ACCT)
    await set_active_account(CHAT, ACCT)
    with _read(read_ids=""):  # ACCT не в read-list → ensure_read_allowed бросит
        assert await get_active_account(CHAT) == DRAFT
    await revoke_account_access(CHAT, ACCT)


async def test_multi_chat_operator_isolation():
    """§12: грант ОДНОГО оператора не даёт доступ ДРУГОМУ — изоляция строго по chat_id."""
    await init_db()
    chat_a, chat_b = 701, 702
    await revoke_account_access(chat_a, ACCT)
    await revoke_account_access(chat_b, ACCT)
    await grant_account_access(chat_a, ACCT)  # доступ выдан ТОЛЬКО chat_a

    await ensure_account_allowed_for_user(chat_a, ACCT)  # chat_a — ок
    with pytest.raises(PermissionError):
        await ensure_account_allowed_for_user(chat_b, ACCT)  # chat_b — отказ (чужой грант не виден)
    assert set(await list_user_account_ids(chat_a)) == {DRAFT, ACCT}
    assert await list_user_account_ids(chat_b) == [DRAFT]  # у chat_b только Draft
    await revoke_account_access(chat_a, ACCT)


async def test_grant_and_check_normalize_dashed_ids():
    """normalize_customer_id: дефис-форма ≡ только цифры — и при гранте, и при проверке."""
    await init_db()
    chat = 703
    await revoke_account_access(chat, ACCT)
    await grant_account_access(chat, "111-222-3334")  # грант в человекочитаемой дефис-форме
    await ensure_account_allowed_for_user(chat, ACCT)  # проверка в цифрах — проходит
    await ensure_account_allowed_for_user(chat, "111-222-3334")  # и в дефисах — тоже
    await ensure_account_allowed_for_user(chat, "775-364-3025")  # Draft (дефис) без гранта
    await revoke_account_access(chat, ACCT)


async def test_list_user_account_ids_always_draft_sorted_dedup():
    """Draft всегда в списке (даже без грантов); выдача отсортирована, без дублей, нормализована."""
    await init_db()
    chat = 704
    await revoke_account_access(chat, ACCT)
    assert await list_user_account_ids(chat) == [DRAFT]  # ноль грантов → только Draft

    await grant_account_access(chat, "111-222-3334")  # дефис-форма
    await grant_account_access(chat, ACCT)  # тот же аккаунт после нормализации (не дубль)
    ids = await list_user_account_ids(chat)
    assert ids == sorted({DRAFT, ACCT})  # отсортировано + дедуп + нормализовано
    assert ids.count(ACCT) == 1
    await revoke_account_access(chat, ACCT)


async def test_set_active_account_upsert_branches():
    """Обе ветки upsert: первый set создаёт строку UserSettings, второй обновляет (без дубля)."""
    from sqlalchemy import func, select

    from db.models import UserSettings
    from db.session import Session

    await init_db()
    chat = 705
    await revoke_account_access(chat, ACCT)
    await set_active_account(chat, DRAFT)  # insert-ветка (строки ещё не было)
    assert await get_active_account(chat) == DRAFT

    await grant_account_access(chat, ACCT)
    await set_active_account(chat, ACCT)  # update-ветка (строка уже есть)
    with _read(read_ids=ACCT):
        assert await get_active_account(chat) == ACCT  # значение сменилось, доступ есть

    async with Session() as s:
        n = (
            await s.execute(
                select(func.count()).select_from(UserSettings).where(UserSettings.chat_id == chat)
            )
        ).scalar_one()
    assert n == 1  # ровно одна строка — update не сделал дубль
    await revoke_account_access(chat, ACCT)


async def test_get_active_account_corner_cases():
    """Углы резолва: нет строки → Draft; явный Draft → Draft; дефис-форма в БД → нормализованный возврат."""
    from sqlalchemy import select

    from db.models import UserSettings
    from db.session import Session

    await init_db()
    assert await get_active_account(70600) == DRAFT  # свежий чат без UserSettings → Draft

    chat = 706
    await revoke_account_access(chat, ACCT)
    await set_active_account(chat, DRAFT)
    assert await get_active_account(chat) == DRAFT  # явно сохранён Draft → Draft

    # Защитная нормализация на ЧТЕНИИ: если в БД лежит дефис-форма (легаси/миграция),
    # get_active_account всё равно вернёт цифры (строка 98 ads.client.normalize в core.access).
    await grant_account_access(chat, ACCT)
    async with Session() as s:
        row = (
            await s.execute(select(UserSettings).where(UserSettings.chat_id == chat))
        ).scalar_one()
        row.selected_customer_id = "111-222-3334"  # пишем дефис-форму напрямую (минуя set_active)
        await s.commit()
    with _read(read_ids=ACCT):
        assert await get_active_account(chat) == ACCT  # вернулось нормализованным
    await revoke_account_access(chat, ACCT)
