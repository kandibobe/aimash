"""Реестр созданных ботом Google-таблиц (db.models.SheetExport): запись при экспорте, выдача в /mysheets.

Зачем: ссылка на таблицу раньше жила только в сообщении Telegram (и для ключей — в черновике визарда
с TTL 72ч). Здесь она переживает рестарт и закрытие визарда.

Файлы принадлежат Google-аккаунту OAuth-токена бота (GOOGLE_ADS_REFRESH_TOKEN) — реестр хранит лишь
ссылку и исход шаринга, никаких секретов (url уже ушёл в чат).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from core.logging import log
from db.models import SheetExport
from db.session import Session


@dataclass
class SheetExportRow:
    kind: str  # keywords|report|audit
    url: str
    title: str | None
    customer_id: str | None
    share: str  # роль (reader/writer) | off | failed — см. reports.sheets
    created_at: datetime | None


async def record(
    *,
    chat_id: int,
    kind: str,
    spreadsheet_id: str,
    url: str,
    title: str | None,
    share: str,
    customer_id: str | None = None,
) -> None:
    """Записать выданную таблицу в реестр. BEST-EFFORT: сбой БД НЕ роняет уже успешный экспорт
    (ссылка пользователю важнее строчки в реестре) — логируем и молчим, как и _share_anyone."""
    try:
        async with Session() as s:
            s.add(
                SheetExport(
                    chat_id=chat_id,
                    customer_id=customer_id,
                    kind=kind,
                    spreadsheet_id=spreadsheet_id,
                    url=url,
                    title=title,
                    share=share,
                )
            )
            await s.commit()
    except Exception as e:  # noqa: BLE001 — реестр вторичен по отношению к самому экспорту
        log.warning("sheet-registry: %s — таблица не записана в реестр", type(e).__name__)


async def list_recent(chat_id: int, limit: int = 10) -> list[SheetExportRow]:
    """Последние таблицы ЧАТА (новые сверху). Порядок по id убыванию — монотонный, надёжнее
    created_at. Только свой chat_id: ссылка anyone-with-link — это фактически ключ доступа."""
    async with Session() as s:
        rows = (
            (
                await s.execute(
                    select(SheetExport)
                    .where(SheetExport.chat_id == chat_id)
                    .order_by(SheetExport.id.desc())
                    .limit(max(1, limit))
                )
            )
            .scalars()
            .all()
        )
    return [
        SheetExportRow(
            kind=r.kind,
            url=r.url,
            title=r.title,
            customer_id=r.customer_id,
            share=r.share,
            created_at=r.created_at,
        )
        for r in rows
    ]
