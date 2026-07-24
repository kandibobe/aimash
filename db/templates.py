"""CRUD именованных шаблонов кампаний (§2B). Async SQLAlchemy, всё scoped по chat_id.

Шаблон = валидированные params create_search_campaign под именем (переиспользование прошлой
работы маркетолога). Секретов в params нет. Применение шаблона — только через confirm-гейт
(шаблон лишь ПРЕД-заполняет params, гейт не обходит). Стиль — как confirm/store.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select

from db.models import CampaignTemplate
from db.session import Session

NAME_MAX = 80


@dataclass
class TemplateRow:
    name: str
    params: dict
    source_campaign: str | None
    created_at: datetime | None


def _normalize_name(name: str) -> str:
    """Имя шаблона: трим + кап по NAME_MAX (колонка String(80)). Пустое → ValueError у вызывающего."""
    return (name or "").strip()[:NAME_MAX]


async def save_template(
    *, chat_id: int, name: str, params: dict, source_campaign: str | None = None
) -> None:
    """Upsert шаблона по (chat_id, name): существующий перезаписывается (имя/params/source)."""
    nm = _normalize_name(name)
    if not nm:
        raise ValueError("пустое имя шаблона")
    async with Session() as s:
        row = (
            await s.execute(
                select(CampaignTemplate).where(
                    CampaignTemplate.chat_id == chat_id, CampaignTemplate.name == nm
                )
            )
        ).scalar_one_or_none()
        if row is None:
            s.add(
                CampaignTemplate(
                    chat_id=chat_id, name=nm, params=params, source_campaign=source_campaign
                )
            )
        else:
            row.params = params
            row.source_campaign = source_campaign
        await s.commit()


async def list_templates(chat_id: int) -> list[TemplateRow]:
    """Шаблоны чата (новые сверху по updated_at)."""
    async with Session() as s:
        rows = (
            (
                await s.execute(
                    select(CampaignTemplate)
                    .where(CampaignTemplate.chat_id == chat_id)
                    .order_by(CampaignTemplate.updated_at.desc())
                )
            )
            .scalars()
            .all()
        )
    return [
        TemplateRow(
            name=r.name,
            params=dict(r.params or {}),
            source_campaign=r.source_campaign,
            created_at=r.created_at,
        )
        for r in rows
    ]


async def get_template(chat_id: int, name: str) -> TemplateRow | None:
    nm = _normalize_name(name)
    if not nm:
        return None
    async with Session() as s:
        r = (
            await s.execute(
                select(CampaignTemplate).where(
                    CampaignTemplate.chat_id == chat_id, CampaignTemplate.name == nm
                )
            )
        ).scalar_one_or_none()
    if r is None:
        return None
    return TemplateRow(
        name=r.name,
        params=dict(r.params or {}),
        source_campaign=r.source_campaign,
        created_at=r.created_at,
    )


async def delete_template(chat_id: int, name: str) -> bool:
    """Удалить шаблон по имени. True — что-то удалили; False — не найдено."""
    nm = _normalize_name(name)
    if not nm:
        return False
    async with Session() as s:
        res = await s.execute(
            delete(CampaignTemplate).where(
                CampaignTemplate.chat_id == chat_id, CampaignTemplate.name == nm
            )
        )
        await s.commit()
    return (res.rowcount or 0) > 0
