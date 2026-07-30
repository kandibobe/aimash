"""A10: критичные секрет/данные-паттерны из .gitignore обязаны быть и в .dockerignore.

Dockerfile делает `COPY . .` из контекста сборки. На VPS backup-сайдкар кладёт в ./backups
pg_dump -Fc ВСЕЙ БД (audit_log + шифрованные oauth_tokens). Без backups/ в .dockerignore эти
дампы запекались бы в образ при каждом деплое (кто получил образ — получил всю БД). Дрейф двух
списков (добавили в .gitignore, забыли в .dockerignore) — тихая утечка; этот тест его ловит.

⚠️ Второй класс, из-за которого тест 20 дней светил зелёным над реальной дырой (29.07.2026).
В `.gitignore` строка была написана как `backups/    # C1: авто-дампы pg_dump …`, а **git не
знает trailing-комментариев**: `#` начинает комментарий ТОЛЬКО в начале строки, в остальных
позициях это обычный символ. Git читал весь литерал целиком, `git check-ignore backups/x.sql`
не срабатывал — при ПУБЛИЧНОМ репозитории первый же `git add -A` на VPS выложил бы дампы БД
наружу (правило 5).

Тест этого не видел, потому что его собственный парсер был ДОБРЕЕ git: `line.split("#", 1)[0]`
срезал inline-комментарий и «находил» паттерн `backups/`, которого для git не существовало.
Парсер, отличающийся от проверяемого движка, валидирует фикцию — поэтому здесь он теперь
повторяет правило git/docker дословно, а отдельный тест краснеет на САМО наличие `#` внутри
строки-паттерна. Это и закрывает класс: следующая такая строка не доживёт до коммита.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]

IGNORE_FILES = (".gitignore", ".dockerignore")

# Паттерны, чьё присутствие в .dockerignore КРИТИЧНО (секреты/данные не должны попасть в образ).
CRITICAL = [
    ".env",
    "secrets/",
    "backups/",
    "*.dump",
    "*.sqlite3",
    "*.sqlite",
    "*.db",
    "dumps/",
    "*.pem",
    "*.key",
    # Рантайм-состояние агента в деплой-чекауте (замер на VPS 30.07.2026): Hermes работает с
    # cwd=/opt/aimash и пишет туда `.hermes/plans/*.md` и `cache/context_summaries_pending.json` —
    # сводки контекста, то есть переписку по клиентам. Untracked + не игнорируемые = та же мина,
    # что backups/: публичный репозиторий, один `git add -A` на сервере, и они снаружи.
    ".hermes/",
    "cache/",
]


def _patterns(path: Path) -> set[str]:
    """Паттерны так, как их видит САМ движок игнора, а не как удобно тесту.

    Правило одно и то же у git (gitignore(5)) и docker (.dockerignore): комментарий — это строка,
    начинающаяся с `#`; внутри строки `#` литерален. Trailing-пробелы git отбрасывает, если они не
    экранированы, — `.rstrip()` это и повторяет.
    """
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.rstrip()
        if not s.strip() or s.lstrip().startswith("#"):
            continue
        out.add(s)
    return out


def test_dockerignore_covers_critical_secret_and_data_patterns():
    docker = _patterns(ROOT / ".dockerignore")
    missing = [p for p in CRITICAL if p not in docker]
    assert not missing, f".dockerignore не покрывает секрет/данные-паттерны: {missing}"


def test_gitignore_covers_critical_secret_and_data_patterns():
    """Симметрично: дыра в .gitignore при ПУБЛИЧНОМ репозитории — утечка прямо в мир."""
    git = _patterns(ROOT / ".gitignore")
    missing = [p for p in CRITICAL if p not in git]
    assert not missing, f".gitignore не покрывает секрет/данные-паттерны: {missing}"


def test_gitignore_data_patterns_also_in_dockerignore():
    """Каждый КРИТИЧНЫЙ паттерн, попавший в .gitignore, обязан быть и в .dockerignore."""
    git = _patterns(ROOT / ".gitignore")
    docker = _patterns(ROOT / ".dockerignore")
    drift = [p for p in CRITICAL if p in git and p not in docker]
    assert not drift, f"паттерны есть в .gitignore, но НЕ в .dockerignore (дрейф): {drift}"


@pytest.mark.parametrize("name", IGNORE_FILES)
def test_no_inline_comments_in_ignore_patterns(name):
    """Класс, а не случай: `#` внутри строки-паттерна — молча неработающий паттерн.

    Не «стиль»: строка выглядит как правило, читается как правило на ревью и не действует.
    Комментарий нужен — он ставится на СВОЮ строку выше.
    """
    offenders = [
        (i, line)
        for i, line in enumerate((ROOT / name).read_text(encoding="utf-8").splitlines(), start=1)
        if line.strip() and not line.lstrip().startswith("#") and "#" in line
    ]
    assert not offenders, (
        f"{name}: `#` внутри строки-паттерна — движок читает её целиком как литерал, "
        "паттерн не срабатывает. Комментарий — на отдельную строку. Найдено: "
        + "; ".join(f"строка {i}: {line!r}" for i, line in offenders)
    )
