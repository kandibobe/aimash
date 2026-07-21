"""proposals: провенанс хода — origin_human_turn, author_user_id, run_id, tg_message_id

Волна 1.4. `user_initiated` — АРГУМЕНТ `save_proposal`, и сегодня он верен только по построению:
все три точки создания черновика лежат внутри aiogram-хендлеров за whitelist-мидлварью. В headless-
контуре (Hermes) точка создания становится вызываемой из MCP-инструмента, cron-джобы и self-
improvement-форка — там `user_initiated=True` окажется ровно тем, что напишет вызывающий, и золотое
правило 3 будет охранять только аккуратных. `origin_human_turn` аргументом не задаётся вовсе: стор
читает его из `core.provenance`, поднять который может только доверенный вход.

`origin_human_turn` — NOT NULL, `server_default=false`: существующие строки объявляются машинными.
Это осознанный отказ, а не проход (правило 10): вчерашний `pending`-черновик после апгрейда
денежную операцию не проведёт, человеку придётся отдать команду заново. Цена — одна повторная
команда; альтернатива (`server_default=true`) — бэкдор в самой миграции. Практически строк почти нет:
`PROPOSAL_TTL_HOURS`=24 ч теперь энфорсится в CAS (0.2 → Волна 1.2).

`author_user_id` НЕ дублирует `chat_id`: в группе chat_id — это чат, а не человек, и §8.4 №3
(«подтвердил тот же, кто заказал») по нему не проверяется. ⚠️ Отклонение от план-файла, где колонка
называлась `author_chat_id` — вторая колонка с тем же смыслом провенанс не усиливает.

`tg_message_id` заполнит транспорт подтверждения (Волна 2.6), сегодня всегда NULL.

`audit_log` НЕ трогаем намеренно: `actor_user_id` там уже есть, а остальной провенанс достаётся
джойном по `confirmation_id` (индексирован) — `proposals` ретеншном не чистятся
(`scheduler.jobs.purge_stale_rows` знает только error_events/crawl_jobs/account_health/site_page_text),
так что строка-источник переживает аудит-строку.

Цепочка: down_revision = 0029_client_dossiers (один head).

Revision ID: 0030_proposal_provenance
Revises: 0029_client_dossiers
Create Date: 2026-07-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_proposal_provenance"
down_revision: str | None = "0029_client_dossiers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "proposals",
        sa.Column(
            "origin_human_turn",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column("proposals", sa.Column("author_user_id", sa.BigInteger(), nullable=True))
    op.add_column("proposals", sa.Column("run_id", sa.String(length=16), nullable=True))
    op.add_column("proposals", sa.Column("tg_message_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("proposals", "tg_message_id")
    op.drop_column("proposals", "run_id")
    op.drop_column("proposals", "author_user_id")
    op.drop_column("proposals", "origin_human_turn")
