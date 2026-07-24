"""A10: критичные секрет/данные-паттерны из .gitignore обязаны быть и в .dockerignore.

Dockerfile делает `COPY . .` из контекста сборки. На VPS backup-сайдкар кладёт в ./backups
pg_dump -Fc ВСЕЙ БД (audit_log + шифрованные oauth_tokens). Без backups/ в .dockerignore эти
дампы запекались бы в образ при каждом деплое (кто получил образ — получил всю БД). Дрейф двух
списков (добавили в .gitignore, забыли в .dockerignore) — тихая утечка; этот тест его ловит.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]

# Паттерны, чьё присутствие в .dockerignore КРИТИЧНО (секреты/данные не должны попасть в образ).
CRITICAL = [
    ".env",
    "secrets/",
    "backups/",
    "*.dump",
    "*.sqlite3",
    "*.db",
    "dumps/",
    "*.pem",
    "*.key",
]


def _patterns(path: Path) -> set[str]:
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.split("#", 1)[0].strip()  # отсекаем inline-комментарии
        if s:
            out.add(s)
    return out


def test_dockerignore_covers_critical_secret_and_data_patterns():
    docker = _patterns(ROOT / ".dockerignore")
    missing = [p for p in CRITICAL if p not in docker]
    assert not missing, f".dockerignore не покрывает секрет/данные-паттерны: {missing}"


def test_gitignore_data_patterns_also_in_dockerignore():
    """Каждый КРИТИЧНЫЙ паттерн, попавший в .gitignore, обязан быть и в .dockerignore."""
    git = _patterns(ROOT / ".gitignore")
    docker = _patterns(ROOT / ".dockerignore")
    drift = [p for p in CRITICAL if p in git and p not in docker]
    assert not drift, f"паттерны есть в .gitignore, но НЕ в .dockerignore (дрейф): {drift}"
