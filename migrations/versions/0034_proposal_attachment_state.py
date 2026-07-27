"""proposals.attachment_state: состояние обещанного .xlsx-вложения черновика

Волна 1b. Карточка черновика печатала «…ещё N — полный список во вложении .xlsx», а набор операций,
для которых файл реально отправлялся, задавался в другом месте (bot/main.py::_KEYWORD_OPS) и
`add_negatives_to_shared_set` в нём не было: обещание уходило человеку И в `summary` черновика, то
есть в audit-row, из которого правило 15 репортит «выполнено». Колонка делает обещание проверяемым:
NULL = не обещано, 'pending' = обещано и не доставлено, 'sending' = застолблено курьером,
'sent'/'failed' = терминальные.

Обещание в `summary` и это состояние пишутся ОДНОЙ вставкой (confirm.store.save_proposal) — двух
мест, где решение принимается, нет по построению. Доставляет процесс `scheduler` (единственный вне
`bot/`, у кого есть Bot-токен), поэтому вечный 'pending' — наблюдаемая величина, а не тишина.

Ретеншна не требует: живёт в строке черновика и уходит вместе с ней (purge_stale_rows).
Без секретов/PII — только слово состояния.

Nullable без server_default намеренно: у существующих строк вложение не обещалось, и NULL это и
означает; ставить им 'pending' значило бы задним числом пообещать файл, которого никто не собирал.

upgrade — add_column с inspector-гардом (dev-SQLite мог создать через create_all). downgrade — drop.
На SQLite drop_column работает через batch-режим Alembic; здесь он не нужен — прод Postgres, а
dev-база пересоздаётся из create_all.

Цепочка: down_revision = 0033_circuit_state (один head). Номер — по порядку приземления, а не по
номеру волны в плане (Волна 1b приземляется после Волны 2).

Revision ID: 0034_proposal_attachment_state
Revises: 0033_circuit_state
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_proposal_attachment_state"
down_revision: str | None = "0033_circuit_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    if not insp.has_table(table):
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "proposals", "attachment_state"):
        return  # уже есть (dev-SQLite create_all) — идемпотентно
    op.add_column(
        "proposals",
        sa.Column("attachment_state", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "proposals", "attachment_state"):
        op.drop_column("proposals", "attachment_state")
