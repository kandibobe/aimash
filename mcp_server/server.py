"""FastMCP-реестр READ-инструментов + construction-time assert И4 + lifespan-bootstrap.

Импорт этого модуля РОНЯЕТ процесс, если в READ-слой просочилась мутация (assert И4) — это зерно
инварианта изоляции, тот же паттерн, что защищает `ANALYSIS_TOOLS` в `agent/tools/schemas.py`.
`agent.tools.schemas` bot-free (гард `tests/test_hermes_isolation.py` / `test_headless_bootstrap.py`),
поэтому импорт `MUTATION_TOOLS` не нарушает развязку headless-контура.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from agent.tools.schemas import MUTATION_TOOLS
from core.guards import require_no_mutations, require_registered_surface
from mcp_server.tools_read import READ_MCP_TOOLS, READ_TOOL_FUNCS

# ── И4 (зерно): READ-инструменты НЕ пересекаются с 39 мутационными ──────────────────
# Провал роняет импорт — лучше, чем тихо открыть мутацию в read-фазе. Полные И1–И8 + injection-корпус
# — шаг ПЕРЕД WRITE; здесь только read-релевантное зерно (construction-time, как S4 для ANALYSIS_TOOLS).
# Не `assert`: под `-O` он вырезается из байткода, и гард исчезает молча — см. `core/guards.py`.
require_no_mutations(
    READ_MCP_TOOLS, MUTATION_TOOLS, rule="И4", subject="READ-инструменты MCP (READ_MCP_TOOLS)"
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


def _registered_tool_names(mcp) -> frozenset[str]:
    """Имена, которые FastMCP ФАКТИЧЕСКИ зарегистрировал. Fail-closed (правило 10): не сумели прочитать
    реестр — бросаем, а не отдаём непроверенную поверхность. `_tool_manager.list_tools()` — синхронный
    аксессор FastMCP (внутренний: публичного sync-листинга нет; при смене API падаем громко здесь и в
    `tests/test_mcp_surface_guard.py`, а не тихо пропускаем гейт)."""
    tm = getattr(mcp, "_tool_manager", None)
    if tm is None or not hasattr(tm, "list_tools"):
        raise RuntimeError(
            "не удалось прочитать реестр инструментов FastMCP (_tool_manager.list_tools) — "
            "поверхность непроверяема, отказываю (§15.2, fail-closed)."
        )
    return frozenset(t.name for t in tm.list_tools())


def build_server():
    """FastMCP с зарегистрированными READ-инструментами. structured_output=False: конверт-словарь
    отдаём как JSON-текст, без навязанной output-схемы из generic `-> dict`. Имя/описание/входную
    схему FastMCP берёт из функции (docstring + аннотации сигнатуры).

    §15.2 (барьер, не комментарий): после регистрации сверяем ФАКТИЧЕСКУЮ поверхность с одобренным
    набором (сегодня — только READ). Просочился confirm/execute/propose на живую поверхность — старт
    падает здесь (fail-fast), а не на первом вызове мутации рядом с живым аккаунтом. Проверка читает
    реестр FastMCP, а не `READ_TOOL_FUNCS`, — иначе она тавтологична и не увидела бы регистрацию мимо
    READ-набора."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("aimash", lifespan=_lifespan)
    for name, fn in READ_TOOL_FUNCS.items():
        mcp.tool(name=name, structured_output=False)(fn)
    require_registered_surface(
        _registered_tool_names(mcp),
        READ_MCP_TOOLS,
        subject="mcp_server.server.build_server (живая MCP-поверхность)",
    )
    return mcp
