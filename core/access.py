"""Пер-пользовательский доступ к аккаунтам + активный аккаунт чата (§8/§12, мультиоператор).

Бэкенд (БД), который UI бота (/account) зовёт, чтобы:
  • узнать/сменить активный аккаунт чата (get/set_active_account → UserSettings.selected_customer_id);
  • проверить, имеет ли оператор право на аккаунт (ensure_account_allowed_for_user → account_access);
  • выдать/снять грант и перечислить доступные аккаунты.

Замки КОМПОЗИТНЫЕ и fail-closed:
  • глобальный read-замок ads.client.ensure_read_allowed (что боту вообще разрешено читать);
  • пер-пользовательский грант (этот модуль) — кто из операторов какой аккаунт ведёт.
Draft (DRAFT_ACCOUNT_ID) доступен всем whitelisted без отдельного гранта (обратная совместимость:
одно-операторный режим работает без записей в account_access). Прочие — только по явному гранту.
"""

from __future__ import annotations

from sqlalchemy import delete, select

from ads.client import DRAFT_ACCOUNT_ID, ensure_read_allowed
from core.config import normalize_customer_id
from core.logging import log
from db.models import AccountAccess, UserSettings
from db.session import Session


async def ensure_account_allowed_for_user(chat_id: int, customer_id: str) -> None:
    """Пер-пользовательский замок (fail-closed). Draft — всем whitelisted (без записи). Прочие —
    только при явном гранте в account_access. Нет гранта ⇒ PermissionError."""
    cid = normalize_customer_id(customer_id)
    if cid == DRAFT_ACCOUNT_ID:
        return
    async with Session() as s:
        row = (
            await s.execute(
                select(AccountAccess.id).where(
                    AccountAccess.chat_id == chat_id, AccountAccess.customer_id == cid
                )
            )
        ).first()
    if row is None:
        raise PermissionError(f"chat {chat_id} не имеет доступа к аккаунту {cid}")


async def grant_account_access(chat_id: int, customer_id: str) -> None:
    """Выдать оператору доступ к аккаунту (идемпотентно — повтор не создаёт дубль)."""
    cid = normalize_customer_id(customer_id)
    async with Session() as s:
        exists = (
            await s.execute(
                select(AccountAccess.id).where(
                    AccountAccess.chat_id == chat_id, AccountAccess.customer_id == cid
                )
            )
        ).first()
        if exists is None:
            s.add(AccountAccess(chat_id=chat_id, customer_id=cid))
            await s.commit()


async def revoke_account_access(chat_id: int, customer_id: str) -> None:
    """Снять доступ оператора к аккаунту (no-op, если гранта не было)."""
    cid = normalize_customer_id(customer_id)
    async with Session() as s:
        await s.execute(
            delete(AccountAccess).where(
                AccountAccess.chat_id == chat_id, AccountAccess.customer_id == cid
            )
        )
        await s.commit()


async def list_user_account_ids(chat_id: int) -> list[str]:
    """Аккаунты, доступные оператору: Draft (всегда) + явно выданные гранты. Отсортированы, без
    дублей. UI /account показывает их (дополнительно фильтруя глобальным ensure_read_allowed)."""
    async with Session() as s:
        rows = (
            (
                await s.execute(
                    select(AccountAccess.customer_id).where(AccountAccess.chat_id == chat_id)
                )
            )
            .scalars()
            .all()
        )
    return sorted({DRAFT_ACCOUNT_ID, *(normalize_customer_id(c) for c in rows)})


async def get_active_account(chat_id: int) -> str:
    """Активный аккаунт чата. NULL/нет строки → Draft. Если сохранён не-Draft — ПЕРЕПРОВЕРЯЕМ доступ
    (глобальный read-замок + пер-пользовательский грант): доступ отозвали ⇒ тихо откатываемся на
    Draft (fail-closed: НИКОГДА не на чужой аккаунт). Единственная точка резолва «активного» в боте."""
    async with Session() as s:
        sel = (
            await s.execute(
                select(UserSettings.selected_customer_id).where(UserSettings.chat_id == chat_id)
            )
        ).scalar_one_or_none()
    cid = normalize_customer_id(sel) if sel else ""
    if not cid or cid == DRAFT_ACCOUNT_ID:
        return DRAFT_ACCOUNT_ID
    try:
        ensure_read_allowed(cid)  # глобальный read-замок (fail-closed)
        await ensure_account_allowed_for_user(chat_id, cid)  # пер-пользователь
    except PermissionError:
        log.info("active-account: доступ к %s для chat %s отозван → откат на Draft", cid, chat_id)
        return DRAFT_ACCOUNT_ID
    return cid


async def set_active_account(chat_id: int, customer_id: str) -> None:
    """Установить активный аккаунт чата (upsert UserSettings.selected_customer_id). Валидацию права
    делает вызывающий (UI) до вызова — здесь только персист (переживает рестарт)."""
    cid = normalize_customer_id(customer_id)
    async with Session() as s:
        row = (
            await s.execute(select(UserSettings).where(UserSettings.chat_id == chat_id))
        ).scalar_one_or_none()
        if row is None:
            s.add(UserSettings(chat_id=chat_id, selected_customer_id=cid))
        else:
            row.selected_customer_id = cid
        await s.commit()
