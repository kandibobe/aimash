"""circuit_state: распределённый размыкатель Google Ads / LLM (аренда пробы вместо herd)

Волна 2. `core.resilience` уже ретраит с джиттером (AWS Full Jitter), но джиттер разносит попытки
ВНУТРИ вызова; после сбоя Google все три процесса (bot, scheduler, per-session MCP) × сотня аккаунтов
всё равно идут пробовать. Лечит аренда пробы: в half-open право на пробный запрос берёт РОВНО ОДИН
процесс — атомарным UPDATE по probe_lease_until (rowcount==1 ⇒ ты пробник), как ConfirmStore.claim.
Состояние обязано быть общим, иначе три процесса держат три слепых друг к другу размыкателя.

Одна строка = одна цепь: name = 'ads:<customer_id>' | 'llm:<model_slug>' (core.breaker.circuit_name).
Строки переиспользуются (единицы десятков), ретеншн не нужен. Без секретов/PII: id аккаунта и слаг
модели секретами проекта не являются.

Стор fail-OPEN (осознанное исключение из правила 10, docs/SECURITY.md): не применили эту миграцию —
размыкатель молча выключен и вызовы проходят, а не встают. Размыкатель — про доступность; отказав
закрыто, он сам стал бы аварией. Ни один гейт безопасности им не заменяется.

upgrade — create с inspector-гардом (dev-SQLite мог создать через create_all). downgrade — drop.

Цепочка: down_revision = 0032_agent_runs (один head). Номер — по порядку приземления, а не по
номеру волны в плане.

Revision ID: 0033_circuit_state
Revises: 0032_agent_runs
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_circuit_state"
down_revision: str | None = "0032_agent_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("circuit_state"):
        return  # уже создана (dev-SQLite create_all) — идемпотентно
    op.create_table(
        "circuit_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        # UNIQUE обязателен: на нём держится «одна строка на цепь» и атомарность аренды пробы.
        sa.Column("name", sa.String(length=96), nullable=False, unique=True),
        sa.Column(
            "state", sa.String(length=12), nullable=False, server_default="closed"
        ),  # closed|open|half_open
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        # Момент размыкания (wall-clock UTC через db_dt) — от него отсчитывается окно остывания.
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        # До этого момента проба арендована другим процессом; NULL/прошлое ⇒ аренда свободна.
        sa.Column("probe_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_circuit_state_name", "circuit_state", ["name"])


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("circuit_state"):
        op.drop_index("ix_circuit_state_name", table_name="circuit_state")
        op.drop_table("circuit_state")
