"""`python -m hermes_ops` — READ-окно в дашборд Hermes по stdio (клиент — Claude Code).

Транспорт stdio: JSON-RPC идёт по stdout, поэтому НИКАКОГО print() в этом пути — логи только в
stderr (`core.logging.setup_logging`, редакция секретов активна, правило 5).
"""

from __future__ import annotations

from core.logging import setup_logging
from hermes_ops.server import build_server


def main() -> None:
    setup_logging()  # логи → stderr (stdout занят JSON-RPC); редакция секретов включена
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
