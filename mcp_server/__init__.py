"""mcp_server/ — Контур A (Hermes/пивот): тонкий MCP-слой поверх существующих READ-функций.

Инкремент «MCP READ» (`deploy/hermes/HERMES_SPEC.md`): 25 READ-инструментов над `reports.queries` /
`ads.read` / `ads.keyword_plan` / `audit.collect` / `db.history`, запускаемых `python -m mcp_server`
по stdio. Пакет АДДИТИВЕН: `bot/**` и денежное ядро (`ads/mutations.py`, `ads/client.py`,
`confirm/**`, `core/secrets.py`) он НЕ трогает и НЕ импортирует `bot`/`aiogram` (гард
`tests/test_headless_bootstrap.py` + `tests/test_hermes_isolation.py`).

Инварианты слоя:
  • И4 (зерно): construction-time assert `READ_MCP_TOOLS.isdisjoint(MUTATION_TOOLS)` в `server.py`
    роняет импорт, если сюда просочилась мутация. Мутаций тут нет по построению — только READ.
  • Правило 5: наружу (в ответ клиенту и в лог) — ТОЛЬКО редактированный текст (`mcp_server.redact`),
    сырой `str(e)` не отдаём. FastMCP по умолчанию кладёт `str(e)` в `ToolError`, поэтому КАЖДЫЙ
    инструмент ловит всё сам и возвращает редактированный error-конверт, не даёт исключению всплыть.
  • Замок ЧТЕНИЯ живёт в самих ридерах (`ensure_read_allowed`/`ensure_manager_allowed`) — слой его
    не дублирует и не ослабляет; отказ доступа приходит как error-конверт (fail-closed).
"""

from __future__ import annotations
