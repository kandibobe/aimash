"""Регресс self-heal схемы (db.session.heal_sqlite_schema).

`Base.metadata.create_all` НЕ добавляет колонки в уже существующие таблицы, поэтому после
аддитивной миграции 0004 (audit_log.actor_user_id/actor_username) dev-БД на SQLite дрейфовала
от модели. Симптомы у пользователя: `OperationalError: no such column: audit_log.actor_user_id`
на /journal (SELECT) и ТИХИЙ обрыв confirm-колбэка (INSERT audit-строки падал РАНЬШЕ cq.answer →
кнопка «крутилась», «ничего не происходит»). Тест воспроизводит дрейф на СТАРОЙ схеме audit_log
и проверяет, что heal аддитивно её чинит и actor-INSERT снова проходит. Офлайн, без сети/SDK.
"""

from __future__ import annotations

import sys
from pathlib import Path

import sqlalchemy as sa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.models import AuditLog  # noqa: E402
from db.session import heal_sqlite_schema  # noqa: E402

# audit_log ДО миграции 0004 — без actor_user_id/actor_username (как в дрейфнувшей dev-БД).
_OLD_AUDIT_LOG = """
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY,
    confirmation_id VARCHAR(64) NOT NULL,
    operation VARCHAR(64) NOT NULL,
    customer_id VARCHAR(20) NOT NULL,
    chat_id BIGINT NOT NULL,
    status VARCHAR(16) NOT NULL,
    result JSON,
    created_at DATETIME NOT NULL
)
"""


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}


def test_heal_adds_missing_nullable_columns(tmp_path):
    db = tmp_path / "drift.db"
    engine = sa.create_engine(f"sqlite:///{db.as_posix()}")
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(_OLD_AUDIT_LOG)
            assert "actor_user_id" not in _columns(conn, "audit_log")  # дрейф воспроизведён

            actions = heal_sqlite_schema(conn)

            cols = _columns(conn, "audit_log")
            assert "actor_user_id" in cols and "actor_username" in cols
            added = [a for a in actions if a.startswith("ADD audit_log.")]
            assert any("actor_user_id" in a for a in added)
            assert any("actor_username" in a for a in added)

            # INSERT с actor-колонками теперь проходит (раньше → OperationalError → «ничего не происходит»).
            conn.exec_driver_sql(
                "INSERT INTO audit_log (confirmation_id, operation, customer_id, chat_id, "
                "status, actor_user_id, actor_username, created_at) "
                "VALUES ('c1','pause_campaign','7753643025',100,'confirmed',7,'anton',CURRENT_TIMESTAMP)"
            )
            row = conn.exec_driver_sql(
                "SELECT actor_user_id, actor_username FROM audit_log WHERE confirmation_id='c1'"
            ).fetchone()
            assert row == (7, "anton")
    finally:
        engine.dispose()


def test_heal_idempotent_on_current_schema(tmp_path):
    """На уже-актуальной (полной) схеме audit_log heal ничего не добавляет — без ложных ALTER."""
    db = tmp_path / "ok.db"
    engine = sa.create_engine(f"sqlite:///{db.as_posix()}")
    try:
        with engine.begin() as conn:
            AuditLog.__table__.create(conn)  # текущая схема из модели (все колонки на месте)
            actions = heal_sqlite_schema(conn)
            assert not [a for a in actions if "audit_log" in a]
    finally:
        engine.dispose()
