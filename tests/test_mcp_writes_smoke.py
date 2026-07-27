"""Smoke-тест MCP WRITE-слоя: импорт, имена, инвариант И5, headless-режим.

Не требует Google Ads SDK (нет вызовов build_client), не трогает БД (нет confirm/store).
Проверяет construction-time свойства: что модуль импортируется, что все 40+1 инструментов
на месте, что WRITE ∩ READ = ∅, что _guarded_write и _new_cid работают.
"""

from __future__ import annotations

import pytest


class TestWriteImports:
    """Импорт tools_writes и server.py не роняет процесс."""

    def test_tools_writes_imports(self):
        """Импорт tools_writes успешен (без циклических зависимостей)."""
        from mcp_server.tools_writes import WRITE_TOOL_FUNCS, WRITE_MCP_TOOLS

        assert isinstance(WRITE_TOOL_FUNCS, dict)
        assert isinstance(WRITE_MCP_TOOLS, frozenset)

    def test_server_imports_with_write_layer(self):
        """Импорт server.py успешен с зарегистрированными WRITE-инструментами + гард И5."""
        from mcp_server.server import build_server

        server = build_server()
        assert server is not None


class TestWriteToolNames:
    """Все 40+1 WRITE-инструментов на месте."""

    def test_all_40_propose_tools_present(self):
        """Каждая операция из MUTATION_TOOLS имеет propose_{op} в WRITE_TOOL_FUNCS."""
        from agent.tools.schemas import MUTATION_TOOLS
        from mcp_server.tools_writes import WRITE_TOOL_FUNCS

        expected = {f"propose_{op}" for op in MUTATION_TOOLS}
        actual = set(WRITE_TOOL_FUNCS) - {"execute_confirmed"}

        missing = expected - actual
        assert not missing, f"Отсутствуют propose-инструменты: {sorted(missing)}"

    def test_execute_confirmed_present(self):
        """execute_confirmed есть в WRITE_TOOL_FUNCS."""
        from mcp_server.tools_writes import WRITE_TOOL_FUNCS

        assert "execute_confirmed" in WRITE_TOOL_FUNCS

    def test_exactly_41_tools(self):
        """40 propose + 1 execute = 41 инструментов."""
        from agent.tools.schemas import MUTATION_TOOLS
        from mcp_server.tools_writes import WRITE_TOOL_FUNCS

        assert len(WRITE_TOOL_FUNCS) == len(MUTATION_TOOLS) + 1, (
            f"Ожидалось {len(MUTATION_TOOLS) + 1} инструментов, "
            f"фактически {len(WRITE_TOOL_FUNCS)}"
        )


class TestInvariantI5:
    """И5: WRITE ∩ READ = ∅."""

    def test_write_read_disjoint(self):
        """Имена WRITE_MCP_TOOLS и READ_MCP_TOOLS не пересекаются."""
        from mcp_server.tools_read import READ_MCP_TOOLS
        from mcp_server.tools_writes import WRITE_MCP_TOOLS

        overlap = WRITE_MCP_TOOLS & READ_MCP_TOOLS
        assert not overlap, (
            f"И5 нарушен: WRITE ∩ READ = {sorted(overlap)}. "
            "WRITE и READ слои должны иметь непересекающиеся имена."
        )

    def test_server_import_enforces_i5(self):
        """Гард И5 в server.py: роняет импорт при пересечении."""
        from core.guards import require_no_mutations

        # Искусственное пересечение должно бросить RuntimeError
        with pytest.raises(RuntimeError):
            require_no_mutations(
                {"propose_update_budget"},
                {"propose_update_budget"},
                rule="И5",
                subject="test",
            )

    def test_i5_names_are_prefixed(self):
        """Все WRITE-инструменты имеют префикс propose_ или execute_."""
        from mcp_server.tools_writes import WRITE_MCP_TOOLS

        for name in WRITE_MCP_TOOLS:
            assert name.startswith("propose_") or name == "execute_confirmed", (
                f"WRITE-инструмент '{name}' не начинается с propose_ и не execute_confirmed"
            )


class TestGuardedWrite:
    """_guarded_write и _new_cid работают корректно."""

    def test_new_cid_format(self):
        """_new_cid возвращает mcp-{hex}."""
        from mcp_server.tools_writes import _new_cid

        cid = _new_cid()
        assert cid.startswith("mcp-")
        assert len(cid) == 4 + 12  # "mcp-" + 12 hex chars

    @pytest.mark.asyncio
    async def test_guarded_write_allows_valid_account(self):
        """_guarded_write пропускает разрешённый аккаунт."""
        from mcp_server.tools_writes import _guarded_write

        async def _work():
            return {"ok": True}

        result = await _guarded_write(_work, account="7753643025")  # Draft
        assert result["ok"] is True

    def test_make_params_filters_none(self):
        """_make_params отбрасывает None-значения."""
        from mcp_server.tools_writes import _make_params

        params = _make_params(campaign="test", currency=None, ad_group=None)
        assert params == {"campaign": "test"}
        assert "currency" not in params
        assert "ad_group" not in params


class TestProposeToolSignatures:
    """Каждый propose-инструмент принимает account (keyword-only обязательный)."""

    def test_all_propose_tools_have_account(self):
        """Все propose_* функции принимают account первым параметром."""
        import inspect

        from mcp_server.tools_writes import WRITE_TOOL_FUNCS

        for name, fn in WRITE_TOOL_FUNCS.items():
            sig = inspect.signature(fn)
            params = list(sig.parameters.keys())
            assert "account" in params, f"{name}: нет параметра account"
            # account должен быть первым (позиционно или keyword)
            assert params[0] == "account", (
                f"{name}: account не первый параметр (первый: {params[0]})"
            )