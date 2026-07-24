"""client_dossiers: сведённое досье клиента (map-reduce по краулу), draft → current через confirm-гейт

Отдельная таблица, а НЕ поля в client_profiles: снапшот профиля пишется в client_profile_history,
которая переживает «🗑 Очистить профиль» (ключ — customer_id, не FK) — имена сотрудников (чужая PII)
остались бы в БД после удаления. Досье удаляется вместе с профилем (clients.store.apply_clear).

status: 'draft' (собрано, ждёт ✅) → 'current' (подтверждено; ровно одно на аккаунт). Перевод —
только внутри атомарного claim (clients.execute.execute_confirmed_memory), правила 1–2.

Цепочка: down_revision = 0028_site_page_text (один head).

Revision ID: 0029_client_dossiers
Revises: 0028_site_page_text
Create Date: 2026-07-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_client_dossiers"
down_revision: str | None = "0028_site_page_text"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "client_dossiers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.String(length=20), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("markdown", sa.Text(), nullable=True),  # файл владельцу (контакты ЕСТЬ)
        sa.Column("llm_context", sa.Text(), nullable=True),  # контекст генераторам (PII НЕТ)
        sa.Column("data", sa.JSON(), nullable=True),
        sa.Column("confirmation_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_client_dossiers_customer_id", "client_dossiers", ["customer_id"])
    op.create_index(
        "ix_client_dossiers_customer_status", "client_dossiers", ["customer_id", "status"]
    )
    op.create_index("ix_client_dossiers_profile", "client_dossiers", ["profile_id"])
    op.create_index("ix_client_dossiers_confirmation_id", "client_dossiers", ["confirmation_id"])


def downgrade() -> None:
    op.drop_index("ix_client_dossiers_confirmation_id", table_name="client_dossiers")
    op.drop_index("ix_client_dossiers_profile", table_name="client_dossiers")
    op.drop_index("ix_client_dossiers_customer_status", table_name="client_dossiers")
    op.drop_index("ix_client_dossiers_customer_id", table_name="client_dossiers")
    op.drop_table("client_dossiers")
