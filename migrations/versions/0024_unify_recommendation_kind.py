"""recommendation.kind: снять префикс 'audit_' — слияние бакетов обучения.

До 2026-07-13 рекомендации рождались в ДВУХ местах: чеки audit/engine.py и 6 детекторов
advisor/rules.py. /audit писал kind='audit_high_cpa', /advise — 'high_cpa', а
advisor.experience.load_experience читает kind БУКВАЛЬНО: 👍/👎 под карточками /audit копились в
бакете, который никто никогда не читал (обучение молча пропадало), и один и тот же чек имел два
разных веса. Теперь источник находок один (audit.engine → advisor.from_findings), kind == check_id
без префикса; эта миграция переносит НАКОПЛЕННУЮ историю в общий бакет.

Безопасно по построению: все 6 старых advisor-kind (spend_no_conv, high_cpa, budget_imbalance,
wasteful_keyword, low_ctr_ad, single_campaign) байт-в-байт равны check_id соответствующих чеков, и
НИ ОДИН check_id не начинается с 'audit_' — таблица псевдонимов не нужна, коллизий не возникает.

substr(), а не LIKE 'audit\\_%': в SQLite (dev/тесты) у LIKE нет ESCAPE по умолчанию — такой WHERE
молча не сматчил бы ничего, и миграция «прошла бы» вхолостую. substr одинаково работает на обоих
диалектах.

downgrade возвращает префикс ТОЛЬКО строкам source='audit' (по source отличаем происхождение —
именно так они и писались), т.е. обратим.

Цепочка: down_revision = 0023_recommendation_topic_width (один head).

Revision ID: 0024_unify_recommendation_kind
Revises: 0023_recommendation_topic_width
Create Date: 2026-07-13
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0024_unify_recommendation_kind"
down_revision: str | None = "0023_recommendation_topic_width"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE recommendation SET kind = substr(kind, 7) WHERE substr(kind, 1, 6) = 'audit_'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE recommendation SET kind = 'audit_' || kind "
        "WHERE source = 'audit' AND substr(kind, 1, 6) <> 'audit_'"
    )
