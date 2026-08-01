"""Deterministic registration for the lightweight Hermes Telegram surface."""

from __future__ import annotations

import importlib
from types import ModuleType

HANDLER_MODULES: tuple[str, ...] = (
    "commands",
    "react_gateway",
)


def register_all() -> list[ModuleType]:
    return [importlib.import_module(f"bot.handlers.{name}") for name in HANDLER_MODULES]
