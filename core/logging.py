"""Структурное логирование БЕЗ секретов. Все логи проходят через redact() для токенов."""
from __future__ import annotations

import logging


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


log = logging.getLogger("aimash")
