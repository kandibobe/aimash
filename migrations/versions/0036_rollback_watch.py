"""rollback_watch: наблюдение за применённой мутацией (контур автооткатa, режим shadow)

Волна 4. Таблица фиксирует НАМЕРЕНИЕ наблюдать за изменением, а не право его откатить: строка
заводится после успешного `finalize()` для операций, у которых `confirm.reverse.reverse_spec`
вернул конкретную обратную мутацию. Провенанса она не несёт и нести не может — компенсация,
когда до неё дойдёт очередь (Волна 6a), проходит те же девять проверок плюс шесть своих.

Почему `confirmation_id` UNIQUE, а не просто индекс: повторный проход `finalize` (ретрай доставки,
дубль джобы, рестарт между коммитом и ответом) завёл бы ВТОРОЕ наблюдение за тем же изменением.
В `shadow` это лишний вердикт, в `alert` — двойное сообщение человеку, а в `auto` — ДВОЙНАЯ
компенсация: бюджет уехал бы ниже исходного. Уникальность здесь дешевле любой проверки в коде,
потому что она работает и при гонке двух процессов.

Ретеншн — `rollback_watch_retain_days`. Денежного следа в таблице нет (он в `audit_log` и
`agent_run_events`), поэтому пола хранения и триггеров ей не нужно.

Цепочка: down_revision = 0035_event_integrity (один head).

Revision ID: 0036_rollback_watch
Revises: 0035_event_integrity
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_rollback_watch"
down_revision: str | None = "0035_event_integrity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("rollback_watch"):
        return  # уже есть (dev-SQLite create_all) — идемпотентно
    op.create_table(
        "rollback_watch",
        sa.Column("id", sa.Integer(), primary_key=True),
        # UNIQUE: одно наблюдение на применённую мутацию (см. шапку — защита от двойной компенсации).
        sa.Column("confirmation_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("customer_id", sa.String(length=20), nullable=False),
        # Числовой id кампании: имя могли переименовать между применением и проверкой, и наблюдение
        # молча начало бы мерить другую кампанию (или ничего).
        sa.Column("campaign_id", sa.String(length=32), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_until", sa.DateTime(timezone=True), nullable=False),
        # Ожидаемый эффект изменения на расход (after/before из снимка). NULL — не вычислим; тогда
        # база не масштабируется и вердикт выносится по единичному отношению.
        sa.Column("expected_ratio", sa.Float(), nullable=True),
        sa.Column(
            "mode", sa.String(length=8), nullable=False, server_default="shadow"
        ),  # shadow|alert|auto
        sa.Column(
            "state", sa.String(length=20), nullable=False, server_default="watching"
        ),  # watching|verdict_ok|verdict_degraded|acted|expired|skipped
        sa.Column("verdict_json", sa.Text(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acted_confirmation_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_rollback_watch_confirmation_id", "rollback_watch", ["confirmation_id"])
    op.create_index("ix_rollback_watch_customer_id", "rollback_watch", ["customer_id"])
    op.create_index("ix_rollback_watch_state", "rollback_watch", ["state"])


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("rollback_watch"):
        op.drop_table("rollback_watch")
