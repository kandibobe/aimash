"""Дрейф доков ↔ репо: docs/DATABASE.md обязан упоминать текущий head миграций.

Класс бага (аудит 2026-07-08): цепочка ревизий в доке отстала (док говорил `0017`, факт —
`0021`, +0018_recommendations…0021_admins_runtime) → следуя доку, берёшь неверный
`down_revision` для новой миграции. Тест закрывает класс: голова цепочки в migrations/versions/
должна присутствовать в доке. Офлайн, без БД — читает только файлы.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _migration_head_num() -> int:
    versions = list((_ROOT / "migrations" / "versions").glob("[0-9][0-9][0-9][0-9]_*.py"))
    assert versions, "не найдены файлы миграций migrations/versions/NNNN_*.py"
    return max(int(re.match(r"(\d+)", p.name).group(1)) for p in versions)


def test_database_doc_mentions_current_migration_head():
    head = f"{_migration_head_num():04d}"
    doc = (_ROOT / "docs" / "DATABASE.md").read_text(encoding="utf-8")
    assert f"`{head}`" in doc, (
        f"docs/DATABASE.md не упоминает текущий head миграций `{head}` "
        "(последняя ревизия в migrations/versions/). Обнови цепочку ревизий и метку **head** "
        "в разделе «Миграции (Alembic)»."
    )
