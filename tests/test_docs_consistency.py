"""Дрейф живых entrypoint/runbook-документов относительно MCP tool registry.

Число READ-инструментов MCP выросло 12 → 13 (`recall_client`,
4b28017) → 15 (`get_mcc_summary`/`get_mcc_deep`, 1723395), а «12» осталось в 16 местах девяти
файлов, включая `README.md` и `CLAUDE.md` — то есть лицо репозитория и инструкция агенту врали
об уже шипнутом. `test_docs_read_tool_count_matches_registry` сверяет с `READ_TOOL_FUNCS`.

Старые ручные каталоги БД/таблиц удалены: Alembic-head и schema metadata проверяются напрямую в
`test_db_schema.py`, `test_verify_postgres_head.py` и model/migration тестах, без второго списка в docs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mcp_server.tools_meta import META_TOOL_FUNCS
from mcp_server.tools_read import READ_TOOL_FUNCS

_ROOT = Path(__file__).resolve().parents[1]

# Файлы, заявляющие ЧИСЛО READ-инструментов MCP `aimash`. Список явный, а не обход всех `*.md`:
# у дашбордного `hermes_ops/` СВОЙ реестр, в котором инструментов ровно 12 — совпадение со старым
# числом aimash случайное, и обход репозитория целиком краснел бы на правде.
_READ_COUNT_DOCS = (
    "README.md",
    "CHANGELOG.md",
    "mcp_server/__init__.py",
    "deploy/hermes/README.md",
    "deploy/hermes/OPERATIONS.md",
)

# Канонические формы: «15 READ-инструментов», «15 READ ✅». Форма обязана быть узкой — рядом живёт
# «12 READ-вызовов» (HERMES_SPEC.md:1338, оценка ИТЕРАЦИЙ прогона, а не размера реестра), и regex
# по одному слову READ принял бы её за счётчик инструментов и потребовал бы сломать смету.
_READ_COUNT_RE = re.compile(r"(\d+)\s+READ(?:(/meta)-инструмент\w*|-инструмент\w*|\s+✅)")


@pytest.mark.parametrize("rel", _READ_COUNT_DOCS)
def test_docs_read_tool_count_matches_registry(rel):
    """Число READ-инструментов в доке ≡ `len(READ_TOOL_FUNCS)`, и каждый файл обязан его называть.

    Второе условие важнее первого: без него любая переформулировка («READ-слой готов» вместо
    «15 READ-инструментов») делает тест вакуумно зелёным — он перестаёт что-либо охранять, не
    покраснев ни разу. Так это и прожило: реестр рос дважды, а «12» никто не тронул.
    """
    expected = len(READ_TOOL_FUNCS)
    assert expected, "READ_TOOL_FUNCS пуст — сверять число в доках не с чем"

    text = (_ROOT / rel).read_text(encoding="utf-8")
    found = [
        (
            int(m.group(1)),
            expected + len(META_TOOL_FUNCS) if m.group(2) else expected,
            m.group(0),
        )
        for m in _READ_COUNT_RE.finditer(text)
    ]

    assert found, (
        f"{rel} не называет число READ-инструментов ни в одной из канонических форм "
        f"(«N READ-инструментов» / «N READ ✅»). Либо число вернуть, либо файл убрать из "
        "_READ_COUNT_DOCS: молча ненаблюдаемая дока — то же, что дока, которая врёт."
    )
    wrong = sorted({frag for n, target, frag in found if n != target})
    assert not wrong, (
        f"{rel}: заявлено {wrong}, а в реестре mcp_server.tools_read.READ_TOOL_FUNCS "
        f"{expected} инструментов ({', '.join(sorted(READ_TOOL_FUNCS))}). Реестр — истина, "
        f"дока — производное: поправь число на {expected}."
    )
