"""agent_run_events: неизменяемость средствами СУБД + хэш-цепочка событий

Волна 3 (event sourcing). Таблица событий существует с 0032, но защищена не была ничем: любой
скрипт, миграция или psql-сессия могла переписать строку — то есть «неизменяемое событие» держалось
на дисциплине вызывающего, а это не неизменяемость, а обещание. Здесь появляются оба рубежа:

1. ТРИГГЕР. UPDATE запрещён всегда и для всех kind (легальной причины переписать событие нет ни
   одной; разрешить «иногда» значит завести путь, по которому подмену не отличить от штатной
   правки). DELETE разрешён, КРОМЕ событий денежного пути (kind ∈ ads_mutate|compensation) моложе
   пола хранения: ретеншн обязан работать — таблица растёт монотонно, — но денежный след не должен
   уходить вместе с мусором. Аварийного флага обхода нет намеренно: он понадобился бы ровно тому, от
   кого пол и защищает. Ошибочное событие исправляется КОМПЕНСИРУЮЩИМ, а не правкой журнала.

2. ХЭШ-ЦЕПОЧКА (`prev_digest`/`payload_digest`). Триггер запрещает изменение, цепочка делает
   изменение ВИДНЫМ — включая случай, когда правивший имел права на таблицу и триггер снял. Звено
   считается по каноническому JSON (`core.observe.canonical_json`: сортированные ключи, фиксированная
   точность float), иначе дайджест не воспроизводится другим процессом и проверять его нечем.
   Проверка — `core.observe.verify_chain`.

Обе колонки NULLABLE намеренно: строки, записанные до этой ревизии, никто не подписывал, и ставить
им дайджест задним числом значило бы выдать непроверенное за проверенное. `verify_chain` считает их
отдельно (`unchained`) и начинает сверку с первой подписанной.

Текст SQL берётся из `db.models.event_immutability_ddl(dialect)` — ОДИН источник правил на оба
диалекта: этот файл (Postgres, прод) и `db.session.init_db` (SQLite, dev/test). Иначе гард жил бы
только на проде и проверялся бы только там; сейчас он покрыт локальными тестами
(`tests/test_event_integrity.py`).

Цепочка: down_revision = 0034_proposal_attachment_state (один head).

Revision ID: 0035_event_integrity
Revises: 0034_proposal_attachment_state
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_event_integrity"
down_revision: str | None = "0034_proposal_attachment_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = ("prev_digest", "payload_digest")


def _has_column(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    if not insp.has_table(table):
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    from db.models import event_immutability_ddl

    bind = op.get_bind()
    if not sa.inspect(bind).has_table("agent_run_events"):
        return  # таблицы нет (0032 не накатана) — ставить триггер не на что
    for name in _COLUMNS:
        if not _has_column(bind, "agent_run_events", name):
            op.add_column("agent_run_events", sa.Column(name, sa.String(length=64), nullable=True))
    for stmt in event_immutability_ddl(bind.dialect.name):
        op.execute(stmt)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_agent_run_events_immutable ON agent_run_events;")
        op.execute("DROP FUNCTION IF EXISTS agent_run_events_immutable();")
    elif bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_agent_run_events_no_update;")
        op.execute("DROP TRIGGER IF EXISTS trg_agent_run_events_money_retention;")
    # Колонки снимаем ПОСЛЕ триггера: пока он висит, ALTER по таблице событий трогать не стоит.
    for name in _COLUMNS:
        if _has_column(bind, "agent_run_events", name):
            op.drop_column("agent_run_events", name)
