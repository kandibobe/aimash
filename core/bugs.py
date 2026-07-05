"""Хранилище пользовательских баг-репортов (/reportbug, §6 «сообщить об ошибке»).

Только ЛОКАЛЬНАЯ БД (таблица bug_reports) — ничего не мутирует в Google Ads и не создаёт proposal
(golden rule #1/#3). Текст РЕДАКТИРУЕТСЯ (core.logging.redact_text) в add_bug_report ПЕРЕД записью:
оператор мог вставить в описание секрето-подобное (golden rule #5). Репорт форвардится админам и
включается в еженедельный дайджест (scheduler.jobs.run_weekly_digest).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, func, select

from core.logging import redact_text
from db.models import BugReport
from db.session import Session

_TEXT_MAX = 4000  # потолок длины репорта в БД (защита от распухания; редактируем ДО среза)


async def add_bug_report(
    chat_id: int,
    text: str,
    *,
    username: str | None = None,
    context_request_id: str | None = None,
) -> int:
    """Сохранить баг-репорт (текст РЕДАКТИРОВАН перед записью — golden rule #5). Возвращает id строки
    (тикет для показа админам). context_request_id — сшивка с последним инцидентом /diag того же чата."""
    clean = redact_text((text or "").strip())[:_TEXT_MAX]
    async with Session() as s:
        row = BugReport(
            chat_id=int(chat_id),
            username=(username or None),
            text=clean,
            context_request_id=(context_request_id or None),
            status="new",
        )
        s.add(row)
        await s.commit()
        return int(row.id)


async def list_bug_reports(limit: int = 15, *, only_open: bool = False) -> list[BugReport]:
    """Последние баг-репорты (reverse-chron) для админ-триажа /bugs. only_open ⇒ статус ≠ closed."""
    async with Session() as s:
        q = select(BugReport).order_by(desc(BugReport.id))
        if only_open:
            q = q.where(BugReport.status != "closed")
        rows = (await s.execute(q.limit(limit))).scalars().all()
    return list(rows)


async def get_bug_report(bug_id: int) -> BugReport | None:
    async with Session() as s:
        return (
            await s.execute(select(BugReport).where(BugReport.id == int(bug_id)))
        ).scalar_one_or_none()


async def set_bug_status(bug_id: int, status: str, *, triaged_by: int | None = None) -> bool:
    """Сменить статус репорта (new|triaged|closed). Возвращает True, если строка найдена."""
    if status not in ("new", "triaged", "closed"):
        raise ValueError(f"неизвестный статус баг-репорта: {status!r}")
    async with Session() as s:
        row = (
            await s.execute(select(BugReport).where(BugReport.id == int(bug_id)))
        ).scalar_one_or_none()
        if row is None:
            return False
        row.status = status
        if triaged_by is not None:
            row.triaged_by = int(triaged_by)
        await s.commit()
        return True


async def bug_reports_since(days: int) -> list[BugReport]:
    """Баг-репорты за последние `days` дней (для еженедельного дайджеста). Reverse-chron."""
    start = datetime.now(timezone.utc) - timedelta(days=int(days))
    async with Session() as s:
        rows = (
            (
                await s.execute(
                    select(BugReport)
                    .where(BugReport.created_at >= start)
                    .order_by(desc(BugReport.id))
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


async def bug_status_counts_since(days: int) -> dict[str, int]:
    """Счётчики статусов баг-репортов за `days` дней (для сводки активности дайджеста)."""
    start = datetime.now(timezone.utc) - timedelta(days=int(days))
    async with Session() as s:
        rows = (
            await s.execute(
                select(BugReport.status, func.count())
                .where(BugReport.created_at >= start)
                .group_by(BugReport.status)
            )
        ).all()
    return {str(status): int(n) for status, n in rows}
