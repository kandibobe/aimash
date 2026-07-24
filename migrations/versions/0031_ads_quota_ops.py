"""ads_quota_ops: распределённый счётчик дневной квоты Google Ads (общий стор вместо per-process)

Волна 1.1 (Этап 2). `core.quota` держал счётчик IN-PROCESS (deque в модуле, один на процесс).
С появлением второго ads-процесса (scheduler) и per-session MCP-процесса счётчики стали слепы друг
к другу: каждый в пределах лимита, а дневной потолок Basic (15 000 операций) пробивается молча.
SPEC §9.2 (`ads_quota_ops`), §11.3; HERMES_SPEC §31.3. Общий стор — единственное, что работает для
per-session MCP (in-process счётчик умирает вместе с процессом на закрытии stdio).

Одна строка = один вызов `quota.record()` (НЕ операция): батч из 50 mutate-операций = одна строка
`op_count=50`. Скользящее 24ч-окно считается в SQL: `SUM(op_count) WHERE ts > now-24h`.
Ретеншн — scheduler.jobs.purge_stale_rows (settings.ads_quota_ops_retain_days).

upgrade — create с inspector-гардом (dev-SQLite мог создать через create_all). downgrade — drop.
Без секретов/PII: customer_id — идентификатор аккаунта, не секрет.

Цепочка: down_revision = 0030_proposal_provenance (один head).

Revision ID: 0031_ads_quota_ops
Revises: 0030_proposal_provenance
Create Date: 2026-07-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_ads_quota_ops"
down_revision: str | None = "0030_proposal_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("ads_quota_ops"):
        return  # уже создана (dev-SQLite create_all) — идемпотентно
    op.create_table(
        "ads_quota_ops",
        sa.Column("id", sa.Integer(), primary_key=True),
        # wall-clock UTC момента фиксации батча; фильтр окна — в SQL (db.session.db_dt диалект-корректно).
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("account", sa.String(length=32), nullable=True),  # customer_id или NULL
        sa.Column("kind", sa.String(length=8), nullable=False),  # 'read' | 'mutate'
        sa.Column("op_count", sa.Integer(), nullable=False, server_default="1"),
    )
    # Глобальный счёт окна + prune: WHERE ts > cutoff.
    op.create_index("ix_ads_quota_ops_ts", "ads_quota_ops", ["ts"])
    # Пер-аккаунтный счёт: WHERE account = ? AND ts > cutoff.
    op.create_index("ix_ads_quota_ops_account_ts", "ads_quota_ops", ["account", "ts"])


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("ads_quota_ops"):
        op.drop_index("ix_ads_quota_ops_account_ts", table_name="ads_quota_ops")
        op.drop_index("ix_ads_quota_ops_ts", table_name="ads_quota_ops")
        op.drop_table("ads_quota_ops")
