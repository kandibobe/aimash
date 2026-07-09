"""A15: verify_postgres определяет head миграций ДИНАМИЧЕСКИ (не хардкод-литерал).

Раньше `_HEAD = "0019_bug_reports"` протухал при каждой новой ревизии (реальный head уже 0022)
→ шаг сверки alembic current всегда красный → верификация прод-БД ложно падала. Класс «обновлять
литерал вручную» закрыт вычислением head из дерева ревизий. Гард: нет литерала ревизии в scripts/,
и вычисленный head совпадает с alembic ScriptDirectory.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

ROOT = Path(__file__).resolve().parents[1]


def _alembic_head_from_tree() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head() or ""


def test_verify_postgres_head_is_dynamic_matches_tree():
    import verify_postgres

    assert verify_postgres._alembic_head() == _alembic_head_from_tree()
    assert verify_postgres._alembic_head()  # непустой (дерево ревизий читается)


def test_no_hardcoded_revision_literal_in_scripts():
    """Греп-гард: в scripts/verify_postgres.py нет захардкоженного `_HEAD = "0019..."`-литерала."""
    src = (ROOT / "scripts" / "verify_postgres.py").read_text(encoding="utf-8")
    for i, line in enumerate(src.splitlines(), 1):
        s = line.strip()
        if s.startswith("#"):
            continue
        # литерал ревизии в присваивании (напр. _HEAD = "0019_bug_reports")
        assert not re.search(r'=\s*["\']\d{4}_\w+["\']', s), (
            f"scripts/verify_postgres.py:{i}: хардкод ревизии миграции — брать head динамически: {s}"
        )
