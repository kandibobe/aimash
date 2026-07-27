"""FastMCP-реестр READ + WRITE инструментов + construction-time гарды И4/И5 + lifespan-bootstrap.

Импорт этого модуля РОНЯЕТ процесс, если в READ-слой просочилась мутация (гард И4) или если
WRITE-слой пересекается с READ (гард И5) — зерно инвариантов изоляции, тот же паттерн, что
защищает `ANALYSIS_TOOLS` в `agent/tools/schemas.py`. `agent.tools.schemas` bot-free (гард
`tests/test_hermes_isolation.py` / `test_headless_bootstrap.py`), поэтому импорт `MUTATION_TOOLS`
не нарушает развязку headless-контура.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from agent.tools.schemas import MUTATION_TOOLS
from core.guards import require_no_mutations
from mcp_server.tools_read import READ_MCP_TOOLS, READ_TOOL_FUNCS
from mcp_server.tools_writes import WRITE_MCP_TOOLS, WRITE_TOOL_FUNCS

# ── И4 (зерно): READ-инструменты НЕ пересекаются с 39 мутационными ──────────────────
# Провал роняет импорт — лучше, чем тихо открыть мутацию в read-фазе. Полные И1–И8 + injection-корпус
# — шаг ПЕРЕД WRITE; здесь только read-релевантное зерно (construction-time, как S4 для ANALYSIS_TOOLS).
# Не `assert`: под `-O` он вырезается из байткода, и гард исчезает молча — см. `core/guards.py`.
require_no_mutations(
    READ_MCP_TOOLS, MUTATION_TOOLS, rule="И4", subject="READ-инструменты MCP (READ_MCP_TOOLS)"
)

# ── И5 (write-зерно): WRITE-инструменты НЕ пересекаются с READ ─────────────────────
# WRITE ∩ READ = ∅. Два слоя с непересекающимися именами: модель не может вызвать read-инструмент
# под видом write, и наоборот. Имена READ_MCP_TOOLS (get_*/list_*/keyword_ideas) заведомо
# не пересекаются с WRITE_MCP_TOOLS (propose_*/execute_confirmed). Если пересечение есть — баг
# конфигурации, роняем импорт немедленно.
require_no_mutations(
    WRITE_MCP_TOOLS, READ_MCP_TOOLS, rule="И5", subject="WRITE-инструменты MCP (WRITE_MCP_TOOLS)"
)


@asynccontextmanager
async def _lifespan(server):  # noqa: ARG001 — FastMCP передаёт сам сервер; он нам не нужен
    """Поднять ads-слой headless НА ТОМ ЖЕ event loop, что и сервер (иначе asyncpg «attached to a
    different loop», §20 — co-hosting/asyncio.run из другого потока даёт этот баг). init_db
    критичен-raise (сервер не стартует полу-инициализированным, fail-closed); сидеры OAuth/клиента/
    дочерних — fail-soft (Draft/тест-MCC на едином .env-токене). Импорт `app.bootstrap` — ленивый
    (держит импорт `server.py` лёгким, а assert И4 — быстрым)."""
    from app.bootstrap import bootstrap_ads_layer

    await bootstrap_ads_layer()
    yield {}


def build_server():
    """FastMCP с зарегистрированными READ + WRITE инструментами. structured_output=False: конверт-словарь
    отдаём как JSON-текст, без навязанной output-схемы из generic `-> dict`. Имя/описание/входную
    схему FastMCP берёт из функции (docstring + аннотации сигнатуры)."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("aimash", lifespan=_lifespan)

    # READ-инструменты (get_*, list_*, keyword_ideas, get_account_audit, get_change_history)
    for name, fn in READ_TOOL_FUNCS.items():
        mcp.tool(name=name, structured_output=False)(fn)

    # WRITE-инструменты (propose_*, execute_confirmed)
    for name, fn in WRITE_TOOL_FUNCS.items():
        mcp.tool(name=name, structured_output=False)(fn)

    return mcp