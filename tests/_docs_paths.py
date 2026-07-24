"""Общий список документов и реестр переездов — инварианты доков правятся в одном месте.

Используют `tests/test_docs_links.py` и `tests/test_tz_sync.py`.
"""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

LEDGER_FILE = pathlib.Path(__file__).resolve()

# ── Реестр переездов ─────────────────────────────────────────────────────────────────────────
#
# Класс бага. Файл переезжает — ссылки на старый путь остаются, и ничего не падает: путь в
# бэктиках или в комментарии никто не резолвит. Спека Hermes переехала из `docs/` в
# `deploy/hermes/`, и после переезда старое имя осталось в семи местах (README ранбука,
# docstring `mcp_server/`, три места в `tests/test_hermes_isolation.py`, два в самой спеке).
#
# Ссылки-markdown закрывает `test_markdown_links_resolve`; здесь — упоминания в ЛЮБОМ виде
# (бэктики, комментарии, prose), для конечного списка путей, о которых точно известно, что их
# больше нет. Ложных срабатываний нет по построению: строка либо старая, либо нет.
#
# Переносишь файл — добавь строку сюда, прогони, почини найденное. Строку НЕ удалять: она и
# есть память о том, что старого пути больше нет.
MOVED_PATHS: dict[str, str] = {
    "docs/HERMES_SPEC.md": "deploy/hermes/HERMES_SPEC.md",
}

# ── Локальные по политике ────────────────────────────────────────────────────────────────────
#
# `.gitignore:64-65` — «client documents — keep local only, never commit»: `*.docx` и `ТЗ.md`.
# Оригиналы ТЗ и сводный текст из них ЖИВУТ только на машине, у которой есть документы заказчика;
# в свежем клоне и в CI их нет и не будет. Инварианты доков обязаны это учитывать: тест, который
# краснеет в CI из-за намеренно неотслеживаемого файла, отключат целиком — вместе с той частью,
# которая ловит настоящий дрейф.
#
# Поэтому: файл на месте ⇒ проверяем; файла нет ⇒ `skip` с явной причиной, НЕ «зелено».
LOCAL_ONLY = {"ТЗ.md"}


def is_local_only(target: str) -> bool:
    """Путь намеренно не попадает в репозиторий (политика `.gitignore`)."""
    t = target.replace("\\", "/").lstrip("./")
    return t in LOCAL_ONLY or t.endswith(".docx")


# Каталоги вне зоны инварианта: архив хранит код таким, каким он был на момент заморозки —
# «починка» ссылок в нём переписывала бы историю.
_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "archive",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "site-packages",
    ".mypy_cache",
    ".ruff_cache",
    "htmlcov",
    "scratchpad",
}


def doc_files() -> list[pathlib.Path]:
    """`CLAUDE.md`, `README.md`, `ТЗ.md`, весь `docs/` и весь `deploy/hermes/`."""
    files = [ROOT / "CLAUDE.md", ROOT / "README.md", ROOT / "ТЗ.md"]
    files += sorted((ROOT / "docs").glob("*.md"))
    files += sorted((ROOT / "deploy" / "hermes").rglob("*.md"))
    return [f for f in files if f.exists()]


def source_and_doc_files() -> list[pathlib.Path]:
    """Все `.md` и `.py` репозитория, кроме архива и служебных каталогов.

    Шире `doc_files()`: старый путь одинаково мёртв и в docstring модуля, и в комментарии теста."""
    out: list[pathlib.Path] = []
    for path in ROOT.rglob("*"):
        if path.suffix not in {".md", ".py"} or not path.is_file():
            continue
        if _SKIP_DIRS & set(path.relative_to(ROOT).parts):
            continue
        out.append(path)
    return sorted(out)
