"""`python -m mcp_server` — запуск MCP READ-сервера по stdio (Контур A, Hermes).

Форма запуска — как ждёт Hermes-конфиг (§11: `command: python`, `args: ["-m", "mcp_server"]`).
Транспорт stdio: JSON-RPC идёт по stdout, поэтому НИКАКОГО print() в этом пути — логи только в
stderr (StreamHandler `core.logging`, редакция секретов активна, правило 5). Bootstrap ads-слоя
выполняется в lifespan сервера (тот же event loop), не здесь — см. `server._lifespan`.
"""

from __future__ import annotations

from core.logging import setup_logging
from mcp_server.server import build_server


def main() -> None:
    setup_logging()  # логи → stderr (stdout занят JSON-RPC); редакция секретов включена
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
