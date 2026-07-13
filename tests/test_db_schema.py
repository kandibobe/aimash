"""Схема БД: гарды на целостность цепочки миграций и подключённость таблиц (2G).

- Таблица whitelist ВОЗВРАЩЕНА (0017) и теперь ПОДКЛЮЧЕНА к рантайму (env ∪ БД,
  bot.main.WhitelistMiddleware / core.access.is_whitelisted) — не dead schema;
- Alembic-цепочка линейна, ровно один head — 0017_whitelist_runtime.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.models import Base  # noqa: E402

_REPO = Path(__file__).resolve().parents[1]
_VERSIONS = _REPO / "migrations" / "versions"


def test_whitelist_table_present_and_wired():
    """whitelist ВОЗВРАЩЕНА (0017) и подключена к рантайму: is_whitelisted читает env ∪ БД.
    Гард против случайного повторного дропа — теперь это НЕ dead schema (P0-A)."""
    assert "whitelist" in Base.metadata.tables, (
        "таблица whitelist должна быть в моделях — она подключена к рантайму "
        "(bot.main.WhitelistMiddleware / core.access.is_whitelisted), см. 0017_whitelist_runtime"
    )
    from core.access import is_whitelisted  # noqa: F401 — рантайм-потребитель существует


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


def test_every_migration_has_real_downgrade():
    """2.12: каждая миграция обратима — downgrade() существует и содержит реальные op.* вызовы
    (не pass-заглушку). Гард гигиены: расхождение upgrade/downgrade всплывает на ревью, а не при
    аварийном откате прода."""
    bad = []
    for py in _VERSIONS.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        m = re.search(r"def downgrade\(\)[^:]*:\n(.*?)(?=\ndef |\Z)", text, re.S)
        if not m or "op." not in m.group(1):
            bad.append(py.name)
    assert not bad, f"миграции без реального downgrade: {bad}"


def test_single_alembic_head_is_0025():
    revs = _revisions()
    assert revs, "не найдено ни одной миграции — сломан парс migrations/versions"
    downs = {d for d in revs.values() if d}
    heads = [r for r in revs if r not in downs]
    assert heads == ["0025_sheet_exports"], (
        f"ожидался ровно один head=0025_sheet_exports, получено {heads} — "
        "ветвление/забытый down_revision в migrations/versions"
    )


def test_recommendation_columns_fit_audit_taxonomy():
    """/audit персистит находки в recommendation: topic=СЕМЬЯ чека, kind=check_id, severity.
    На Postgres VARCHAR — ЖЁСТКОЕ ограничение (22001 StringDataRightTruncation), на dev-SQLite
    длина не проверяется вовсе ⇒ переполнение всплывает только в проде. Этот гард ловит его в CI.

    Класс бага, который он закрывает (0023): семья 'conversion_tracking' (19) не влезала в
    topic VARCHAR(16) → /audit падал после карточки score на аккаунтах без трекинга конверсий.
    Добавишь семью/чек длиннее колонки — тест упадёт здесь, а не у клиента."""
    from audit.engine import CHECK_REGISTRY
    from audit.thresholds import FAMILY_WEIGHT

    cols = Base.metadata.tables["recommendation"].c
    limits = {name: cols[name].type.length for name in ("topic", "kind", "severity")}

    too_long = [
        (col, value, len(value), limits[col])
        for col, values in (
            ("topic", set(FAMILY_WEIGHT) | {f for f, _s in CHECK_REGISTRY.values()}),
            ("kind", set(CHECK_REGISTRY)),
            ("severity", {s for _f, s in CHECK_REGISTRY.values()}),
        )
        for value in values
        if len(value) > limits[col]
    ]
    assert not too_long, (
        "значения таксономии аудита не влезают в колонки recommendation "
        f"(col, value, len, limit): {sorted(too_long)} — на Postgres это уронит /audit"
    )
