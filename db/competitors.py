"""Ф5б: срезы «Статистики аукционов» → ЛОКАЛЬНАЯ таблица auction_insight_row (субстрат сравнения).

Данные приносит человек файлом (/competitors): метрики auction_insight_* в API есть, но доступ к ним
закрыт вайтлистом Google (подробнее — docstring reports/auction_insights.py).
Здесь только хранение и выдача: парсит reports/auction_insights.py, рисует core/texts.py.

Идемпотентно per (customer_id, snapshot_date): повторный импорт за ту же дату перезаписывает срез
ЦЕЛИКОМ (delete → insert), а не мержит. Домены между выгрузками появляются и исчезают; мерж оставил
бы в срезе «призраков», которых в новом отчёте Google уже нет, — и клиент сравнивал бы с выдумкой.

⚠️ Только локальная БД — Google Ads не трогаем. Секретов не пишем: домены конкурентов публичны.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, select

from db.models import AuctionInsightRow
from db.session import Session
from reports.auction_insights import AuctionInsights, CompetitorRow


@dataclass(frozen=True)
class Snapshot:
    """Импортированный срез аукциона на дату (ровно то, что было в файле)."""

    snapshot_date: str  # ISO YYYY-MM-DD (TZ аккаунта)
    period_label: str  # диапазон дат из преамбулы файла ('' — не нашли)
    you: CompetitorRow | None
    competitors: tuple[CompetitorRow, ...]  # по убыванию доли показов


def _to_row(r: AuctionInsightRow) -> CompetitorRow:
    return CompetitorRow(
        domain=r.domain,
        is_you=bool(r.is_you),
        impression_share=r.impression_share,
        overlap_rate=r.overlap_rate,
        position_above_rate=r.position_above_rate,
        top_of_page_rate=r.top_of_page_rate,
        abs_top_of_page_rate=r.abs_top_of_page_rate,
        outranking_share=r.outranking_share,
    )


async def save_snapshot(customer_id: str, snapshot_date: str, ins: AuctionInsights) -> int:
    """Записать срез (перезаписав срез этой же даты). Возвращает число строк. Сбой БД — ИСКЛЮЧЕНИЕ:
    в отличие от снапшота health-score (побочный продукт /audit), здесь сохранение — половина
    смысла команды, и «тихо не сохранил» было бы обманом. Карточку вызывающий покажет всё равно."""
    items = ([ins.you] if ins.you else []) + list(ins.competitors)
    async with Session() as s:
        await s.execute(
            delete(AuctionInsightRow).where(
                AuctionInsightRow.customer_id == str(customer_id),
                AuctionInsightRow.snapshot_date == snapshot_date,
            )
        )
        for it in items:
            s.add(
                AuctionInsightRow(
                    customer_id=str(customer_id),
                    snapshot_date=snapshot_date,
                    domain=it.domain,
                    is_you=it.is_you,
                    impression_share=it.impression_share,
                    overlap_rate=it.overlap_rate,
                    position_above_rate=it.position_above_rate,
                    top_of_page_rate=it.top_of_page_rate,
                    abs_top_of_page_rate=it.abs_top_of_page_rate,
                    outranking_share=it.outranking_share,
                    period_label=ins.period_label[:64],
                )
            )
        await s.commit()
    return len(items)


async def latest_snapshot(customer_id: str, *, before_date: str | None = None) -> Snapshot | None:
    """Последний срез аккаунта (строго РАНЬШЕ before_date, если задана) или None."""
    async with Session() as s:
        q = select(AuctionInsightRow.snapshot_date).where(
            AuctionInsightRow.customer_id == str(customer_id)
        )
        if before_date:
            q = q.where(AuctionInsightRow.snapshot_date < before_date)
        date = (
            await s.execute(q.order_by(AuctionInsightRow.snapshot_date.desc()).limit(1))
        ).scalar_one_or_none()
        if not date:
            return None
        rows = (
            (
                await s.execute(
                    select(AuctionInsightRow).where(
                        AuctionInsightRow.customer_id == str(customer_id),
                        AuctionInsightRow.snapshot_date == date,
                    )
                )
            )
            .scalars()
            .all()
        )
    you = next((_to_row(r) for r in rows if r.is_you), None)
    rivals = sorted(
        (_to_row(r) for r in rows if not r.is_you),
        key=lambda r: (r.impression_share or 0.0),
        reverse=True,
    )
    label = next((r.period_label for r in rows if r.period_label), "")
    return Snapshot(snapshot_date=date, period_label=label, you=you, competitors=tuple(rivals))


async def snapshot_series(customer_id: str, *, limit: int = 4) -> list[Snapshot]:
    """Последние `limit` срезов аккаунта ПО ВОЗРАСТАНИЮ даты — для мини-тренда в карточке.

    Два запроса на всю серию (даты → строки одним IN), не по запросу на срез: срезов у
    аккаунта может накопиться много, а карточке нужен только хвост."""
    async with Session() as s:
        dates = (
            (
                await s.execute(
                    select(AuctionInsightRow.snapshot_date)
                    .where(AuctionInsightRow.customer_id == str(customer_id))
                    .distinct()
                    .order_by(AuctionInsightRow.snapshot_date.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        if not dates:
            return []
        rows = (
            (
                await s.execute(
                    select(AuctionInsightRow).where(
                        AuctionInsightRow.customer_id == str(customer_id),
                        AuctionInsightRow.snapshot_date.in_(dates),
                    )
                )
            )
            .scalars()
            .all()
        )
    out: list[Snapshot] = []
    for date in sorted(dates):
        day = [r for r in rows if r.snapshot_date == date]
        you = next((_to_row(r) for r in day if r.is_you), None)
        rivals = sorted(
            (_to_row(r) for r in day if not r.is_you),
            key=lambda r: (r.impression_share or 0.0),
            reverse=True,
        )
        label = next((r.period_label for r in day if r.period_label), "")
        out.append(
            Snapshot(snapshot_date=date, period_label=label, you=you, competitors=tuple(rivals))
        )
    return out
