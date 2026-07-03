"""Схема БД: гарды на dead schema и целостность цепочки миграций (2G).

- Таблицы whitelist больше нет ни в моделях (иллюзия БД-allow-list снята: доступ — env
  TELEGRAM_WHITELIST_CHAT_IDS), ни в актуальной схеме;
- Alembic-цепочка линейна, ровно один head — 0016_drop_whitelist.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.models import Base  # noqa: E402

_REPO = Path(__file__).resolve().parents[1]
_VERSIONS = _REPO / "migrations" / "versions"


def test_whitelist_table_removed_from_models():
    assert "whitelist" not in Base.metadata.tables, (
        "таблица whitelist вернулась в модели — она dead schema (allow-list живёт в env, "
        "см. миграцию 0016_drop_whitelist)"
    )


def _revisions() -> dict[str, str | None]:
    """revision → down_revision из файлов migrations/versions (regex по литералам)."""
    out: dict[str, str | None] = {}
    for py in _VERSIONS.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        rev = re.search(r'^revision(?::\s*str)?\s*=\s*["\']([^"\']+)["\']', text, re.M)
        down = re.search(
            r'^down_revision(?::\s*[\w\s|\[\]]+)?\s*=\s*(?:["\']([^"\']+)["\']|None)', text, re.M
        )
        if rev:
            out[rev.group(1)] = down.group(1) if (down and down.group(1)) else None
    return out


def test_single_alembic_head_is_0016():
    revs = _revisions()
    assert revs, "не найдено ни одной миграции — сломан парс migrations/versions"
    downs = {d for d in revs.values() if d}
    heads = [r for r in revs if r not in downs]
    assert heads == ["0016_drop_whitelist"], (
        f"ожидался ровно один head=0016_drop_whitelist, получено {heads} — "
        "ветвление/забытый down_revision в migrations/versions"
    )
