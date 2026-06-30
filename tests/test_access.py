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
