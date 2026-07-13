"""Волна 3: окно недельного дайджеста режется в SQL, а не в Python.

audit_activity_since тянул в память ВЕСЬ audit_log (единственный запрос без границы; таблица растёт
с каждой мутацией) и фильтровал по дате уже в Python — «потому что SQLite хранит наивный UTC, а
Postgres tz-aware». Границу окна теперь приводит к диалекту db.session.db_dt, и WHERE уходит в SQL.

Гард: строка ЗА окном не попадает в счётчики, строка внутри — попадает; created_campaigns считает
только applied create_*_campaign. Реальный ConfirmStore на temp SQLite (conftest)."""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from confirm.store import audit_activity_since  # noqa: E402
from db.models import AuditLog  # noqa: E402
from db.session import Session, db_dt, init_db  # noqa: E402

DRAFT = "7753643025"


async def _audit_row(*, status: str, operation: str, age_days: float) -> None:
    """Строка audit_log с ЯВНЫМ created_at (server_default перебиваем — нужен возраст)."""
    ts = datetime.now(timezone.utc) - timedelta(days=age_days)
    async with Session() as s:
        s.add(
            AuditLog(
                confirmation_id=uuid.uuid4().hex,
                operation=operation,
                customer_id=DRAFT,
                chat_id=1,
                status=status,
                result=None,
                created_at=db_dt(ts),  # тот же вид, в котором лежат строки этой БД
            )
        )
        await s.commit()


async def test_window_excludes_older_rows_and_counts_created_campaigns():
    await init_db()
    await _audit_row(status="applied", operation="create_search_campaign", age_days=1)
    await _audit_row(status="rejected", operation="update_budget", age_days=2)
    await _audit_row(status="applied", operation="update_budget", age_days=3)  # не «создание»
    await _audit_row(status="applied", operation="create_gdn_campaign", age_days=30)  # ЗА окном

    act = await audit_activity_since(7)

    assert act["statuses"] == {"applied": 2, "rejected": 1}  # 30-дневная строка не в счётчиках
    assert act["created_campaigns"] == 1  # только create_search_campaign внутри окна


async def test_db_dt_matches_dialect():
    """SQLite (тесты/dev) — наивный UTC; сравнение naive-колонки с tz-aware границей молча врало."""
    aware = datetime.now(timezone.utc)
    got = db_dt(aware)
    assert got.tzinfo is None and abs((got - aware.replace(tzinfo=None)).total_seconds()) < 1
