"""client knowledge base (§20): профиль клиента + краулинг сайта

§20 «Информация о клиентах»: персональная база знаний на рекламный аккаунт (customer_id).
Профиль (бренд/бизнес/гео/язык/сайт/соцсети/заметки) + детали (контакты/услуги/страницы сайта) +
журнал краулинга (crawl_jobs) + история версий профиля (для отката/аудита, переживает clear).
Это ЛОКАЛЬНАЯ БД: изменения профиля идут через confirm-гейт как memory-операции (clients.execute),
НЕ через ads.mutations — замок Google Ads к ним неприменим. Секретов нет; PII в логи не пишем.
db/models.py обновлён симметрично; на SQLite (dev) таблицы создаёт create_all, здесь — истина для
Postgres (prod).

Цепочка: down_revision = 0012_campaign_drafts (один head).

Revision ID: 0013_client_kb
Revises: 0012_campaign_drafts
Create Date: 2026-07-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_client_kb"
down_revision: str | None = "0012_campaign_drafts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "client_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.String(length=20), nullable=False),
        sa.Column("brand", sa.String(length=255), nullable=True),
        sa.Column("business_desc", sa.Text(), nullable=True),
        sa.Column("geo", sa.String(length=255), nullable=True),
        sa.Column("language", sa.String(length=64), nullable=True),
        sa.Column("website", sa.String(length=2048), nullable=True),
        sa.Column("socials", sa.JSON(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("last_crawled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_client_profiles_customer_id", "client_profiles", ["customer_id"], unique=True
    )

    op.create_table(
        "client_contacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("value", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_client_contacts_profile", "client_contacts", ["profile_id"])

    op.create_table(
        "client_services",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("price", sa.String(length=128), nullable=True),
        sa.Column("category", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_client_services_profile", "client_services", ["profile_id"])

    op.create_table(
        "client_site_pages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=True),
        sa.Column("page_type", sa.String(length=32), nullable=True),
        sa.Column("key_links", sa.JSON(), nullable=True),
        sa.Column("crawled_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_client_site_pages_profile", "client_site_pages", ["profile_id"])

    op.create_table(
        "crawl_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("customer_id", sa.String(length=20), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="full"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("pages_crawled", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_crawl_jobs_job_id", "crawl_jobs", ["job_id"], unique=True)
    op.create_index("ix_crawl_jobs_customer_id", "crawl_jobs", ["customer_id"])
    op.create_index("ix_crawl_jobs_status_created", "crawl_jobs", ["status", "created_at"])

    op.create_table(
        "client_profile_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.String(length=20), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=True),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("confirmation_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_client_profile_history_customer",
        "client_profile_history",
        ["customer_id", "created_at"],
    )
    op.create_index(
        "ix_client_profile_history_confirmation_id",
        "client_profile_history",
        ["confirmation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_client_profile_history_confirmation_id", table_name="client_profile_history")
    op.drop_index("ix_client_profile_history_customer", table_name="client_profile_history")
    op.drop_table("client_profile_history")
    op.drop_index("ix_crawl_jobs_status_created", table_name="crawl_jobs")
    op.drop_index("ix_crawl_jobs_customer_id", table_name="crawl_jobs")
    op.drop_index("ix_crawl_jobs_job_id", table_name="crawl_jobs")
    op.drop_table("crawl_jobs")
    op.drop_index("ix_client_site_pages_profile", table_name="client_site_pages")
    op.drop_table("client_site_pages")
    op.drop_index("ix_client_services_profile", table_name="client_services")
    op.drop_table("client_services")
    op.drop_index("ix_client_contacts_profile", table_name="client_contacts")
    op.drop_table("client_contacts")
    op.drop_index("ix_client_profiles_customer_id", table_name="client_profiles")
    op.drop_table("client_profiles")
