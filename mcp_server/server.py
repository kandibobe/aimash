"""FastMCP-реестр READ-инструментов + construction-time assert И4 + lifespan-bootstrap.

Импорт этого модуля РОНЯЕТ процесс, если в READ-слой просочилась мутация (assert И4) — это зерно
инварианта изоляции, тот же паттерн, что защищает `ANALYSIS_TOOLS` в `agent/tools/schemas.py`.
`agent.tools.schemas` bot-free (гард `tests/test_hermes_isolation.py` / `test_headless_bootstrap.py`),
поэтому импорт `MUTATION_TOOLS` не нарушает развязку headless-контура.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from agent.tools.schemas import MUTATION_TOOLS
from mcp_server.tools_read import READ_MCP_TOOLS, READ_TOOL_FUNCS

# ── И4 (зерно): READ-инструменты НЕ пересекаются с 39 мутационными ──────────────────
# Провал роняет импорт — лучше, чем тихо открыть мутацию в read-фазе. Полные И1–И8 + injection-корпус
# — шаг ПЕРЕД WRITE; здесь только read-релевантное зерно (construction-time, как S4 для ANALYSIS_TOOLS).
_overlap = READ_MCP_TOOLS & MUTATION_TOOLS
assert not _overlap, (
    "И4 нарушен: READ-инструменты MCP пересекаются с мутационными "
    f"(agent.tools.schemas.MUTATION_TOOLS): {sorted(_overlap)}. "
    "MCP READ-слой не смеет содержать мутации — это денежный путь."
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
    """FastMCP с зарегистрированными READ-инструментами. structured_output=False: конверт-словарь
    отдаём как JSON-текст, без навязанной output-схемы из generic `-> dict`. Имя/описание/входную
    схему FastMCP берёт из функции (docstring + аннотации сигнатуры)."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("aimash", lifespan=_lifespan)
    for name, fn in READ_TOOL_FUNCS.items():
        mcp.tool(name=name, structured_output=False)(fn)
    return mcp
