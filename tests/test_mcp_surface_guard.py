"""Гард ЖИВОЙ MCP-поверхности (§15.2, И4-стиль): `build_server` выставляет РОВНО одобренный READ-набор.

confirm/execute/propose не выходят на живую поверхность, пока не подтверждён канал доставки/якоря
(§15.2). Это барьер, не комментарий: просочись такой инструмент в регистрацию — `build_server` падает
на СБОРКЕ (fail-fast), а не на первом вызове мутации рядом с живым аккаунтом.

Почему проверка читает ФАКТИЧЕСКИЙ реестр FastMCP, а не `READ_TOOL_FUNCS`: иначе она тавтологична —
`READ_MCP_TOOLS` выведено из того же словаря, что итерирует цикл, и любое равенство было бы истинным по
построению. Реальная угроза — регистрация МИМО READ-набора (второй цикл по `PROPOSE_TOOL_FUNCS`,
одиночный `mcp.tool()(execute_confirmed)`); её видно только в фактическом реестре сервера.

Существующий module-level гард `require_no_mutations(READ_MCP_TOOLS, MUTATION_TOOLS)` этот класс НЕ
ловит: execute_confirmed/confirm_by_reply в `MUTATION_TOOLS` нет (это гейт/исполнение, не 40 мутаций
Google Ads). Ровно эту брешь и закрывает гард поверхности.
"""

from __future__ import annotations

import pytest

from core.config import settings
from core.guards import require_registered_surface
from mcp_server.server import _registered_tool_names, build_server
from mcp_server.tools_read import READ_MCP_TOOLS
from mcp_server.trusted_transport import TOKEN_PARAM


def test_build_server_exposes_exactly_the_read_surface() -> None:
    """Позитив: собранный сервер несёт РОВНО READ-набор — ни больше (WRITE/confirm/execute), ни меньше."""
    mcp = build_server()
    assert _registered_tool_names(mcp) == READ_MCP_TOOLS


def test_write_mode_exposes_only_trusted_plan_write_surface(monkeypatch) -> None:
    from mcp_server.tools_write import PLAN_WRITE_MCP_TOOLS

    monkeypatch.setattr(settings, "hermes_write_enabled", True)
    mcp = build_server()
    assert _registered_tool_names(mcp) == READ_MCP_TOOLS | PLAN_WRITE_MCP_TOOLS
    for name in PLAN_WRITE_MCP_TOOLS:
        tool = next(t for t in mcp._tool_manager.list_tools() if t.name == name)
        assert TOKEN_PARAM in tool.parameters["properties"]


def test_surface_guard_rejects_confirm_execute_leak() -> None:
    """Барьер напрямую: имя execute/confirm на поверхности → отказ, с виновником в тексте (не тихо)."""
    leaked = set(READ_MCP_TOOLS) | {"execute_confirmed"}
    with pytest.raises(RuntimeError, match="execute_confirmed"):
        require_registered_surface(leaked, READ_MCP_TOOLS, subject="тест")


def test_surface_guard_rejects_propose_leak() -> None:
    """§15.2 буквально: propose-инструмент на живой поверхности запрещён так же, как execute."""
    leaked = set(READ_MCP_TOOLS) | {"propose_budget_change"}
    with pytest.raises(RuntimeError, match="propose_budget_change"):
        require_registered_surface(leaked, READ_MCP_TOOLS, subject="тест")


def test_surface_guard_rejects_missing_read_tool() -> None:
    """Равенство, не вхождение: разошёлся эталон с реальностью (READ-инструмент выключен, эталон не
    обновлён) — тоже отказ. Иначе эталон тихо разъезжается и гард перестаёт что-либо гарантировать."""
    partial = set(list(READ_MCP_TOOLS)[:-1])
    with pytest.raises(RuntimeError):
        require_registered_surface(partial, READ_MCP_TOOLS, subject="тест")


def test_build_server_rejects_a_non_read_tool_registered(monkeypatch) -> None:
    """Полный путь через `build_server` (load-bearing): кто-то добавил execute-функцию в регистрируемый
    набор → сборка сервера падает, а не отдаёт поверхность с гейтом/исполнением. Именно этот класс
    module-level гард мутаций пропустил бы (execute_confirmed ∉ MUTATION_TOOLS)."""
    import mcp_server.server as srv
    from mcp_server.tools_read import READ_TOOL_FUNCS

    async def execute_confirmed(x: int) -> dict:
        """поддельный execute-инструмент — на живой поверхности его быть не должно"""
        return {}

    monkeypatch.setattr(
        srv, "READ_TOOL_FUNCS", {**READ_TOOL_FUNCS, "execute_confirmed": execute_confirmed}
    )
    with pytest.raises(RuntimeError, match="execute_confirmed"):
        srv.build_server()


def test_registered_names_fail_closed_when_registry_unreadable() -> None:
    """Реестр непрочитаем (сменилось внутреннее API FastMCP) → отказ (fail-closed, правило 10), а не
    «пустая поверхность прошла проверку». Непроверяемая поверхность наружу не идёт."""

    class _NoManager:
        pass

    with pytest.raises(RuntimeError, match="реестр"):
        _registered_tool_names(_NoManager())
