"""proposals.risk_tier: тир риска черновика (L1/L2/L3), презентационный

Волна 5. Тир считает `confirm/risk.py` — чистая функция от (operation, params) — и он НЕ участвует
ни в одной из девяти проверок §2.2: ни в замке аккаунта, ни в claim, ни в провенансе. Он меняет
форму вопроса человеку: полноту карточки (блок «последствия»), наличие вложения с графиком
проекции, число независимых человеческих актов и срок жизни согласия.

Зачем тогда колонка, если тир пересчитывается из params в любой момент. Две причины, обе про
проверяемость:

* **аудит** — строка отвечает на вопрос «что человек видел, когда соглашался». Пороги в
  `confirm/risk.py` со временем поменяются, и пересчёт задним числом даст ответ про СЕГОДНЯШНИЕ
  пороги, а не про те, что действовали в момент показа;
* **TTL согласия** — единственное место, где колонку читает код: CAS-условие
  `confirm/store.py::_ttl_condition` даёт L3 более короткий срок (`PROPOSAL_TTL_HOURS_L3`).
  Условие обязано выполняться в SQL одним запросом (одноразовость — свойство хранилища), а
  считать в нём `risk_tier` из JSON-params нечем.

Nullable без server_default намеренно: у строк, созданных до миграции, тира не было, и NULL это и
означает. `_ttl_condition` трактует NULL через `coalesce` как обычный (не-L3) срок — не как L3:
задним числом укоротить чужое согласие значило бы отменить подтверждения, которые на момент
создания были действительны.

Ретеншна не требует: живёт в строке черновика и уходит вместе с ней (purge_stale_rows).
Без секретов/PII — две буквы.

upgrade — add_column с inspector-гардом (dev-SQLite мог создать через create_all). downgrade — drop.

Цепочка: down_revision = 0036_rollback_watch (один head).

Revision ID: 0037_proposal_risk_tier
Revises: 0036_rollback_watch
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037_proposal_risk_tier"
down_revision: str | None = "0036_rollback_watch"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    if not insp.has_table(table):
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "proposals", "risk_tier"):
        return  # уже есть (dev-SQLite create_all) — идемпотентно
    op.add_column("proposals", sa.Column("risk_tier", sa.String(length=2), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "proposals", "risk_tier"):
        op.drop_column("proposals", "risk_tier")
