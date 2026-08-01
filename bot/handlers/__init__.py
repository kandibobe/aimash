"""Доменные модули хендлеров aiogram (вынос из bot/main.py).

4A: ЕДИНСТВЕННЫЙ источник порядка регистрации — HANDLER_MODULES (порядок импорта модуля =
порядок диспатча aiogram). Раньше порядок задавали 12 хрупких `from bot.handlers.X import *`
в хвосте bot/main.py: случайная перестановка строк (ruff/IDE/мерж) тихо ломала диспатч —
on_text переставал быть последним и глотал команды. Инварианты:
  • react_gateway — СТРОГО первым: любой non-command текст раньше legacy FSM handlers;
  • fallback — СТРОГО последним: документы, неизвестные slash-команды и error boundary.
Закреплено tests/test_handler_order.py. Регистрация по-прежнему происходит декораторами
@bm.dp.message(...) при импорте модуля — register_all лишь импортирует в детерминированном порядке.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType

from aiogram.fsm.state import State

UNBOUND_HANDLER_MODULES: frozenset[str] = frozenset({"campaign_wizard", "menu_guard"})

HANDLER_MODULES: tuple[str, ...] = (
    "react_gateway",  # agent-first non-command catch-all раньше ЛЮБЫХ legacy FSM handlers
    "menu_guard",  # импорт для совместимости; button/FSM handler снимается в register_all
    "commands",
    "reports",
    "keywords_flow",
    "campaigns_menu",
    "rsa_flow",
    "search_media",
    "campaign_wizard",  # импорт для совместимости; handlers снимаются в register_all
    "clients_kb",
    "templates_recent",
    "advise_flow",  # advisor: 👍/👎 фидбек (AdviseCB) — только запись, без мутаций
    "competitors",  # Ф5б: /competitors — CSV аукциона ловится ПО СОСТОЯНИЮ раньше on_document
    "bug_report",  # §6: /reportbug + /bugs (админ-триаж) — только локальная БД, без мутаций
    "confirm_flow",
    "fallback",  # документы, неизвестные slash-команды и глобальный error boundary
)


def register_all() -> list[ModuleType]:
    """Импортировать все хендлер-модули в порядке HANDLER_MODULES (регистрация — побочный
    эффект импорта). Legacy FSM modules импортируются для обратной совместимости функций, но их
    callbacks снимаются с Dispatcher. Любые message handlers с конкретным FSM ``State`` также
    удаляются: свободный текст имеет ровно один вход — ``react_gateway.on_text``. Возвращает модули
    для ре-экспорта имён в bot.main."""
    modules = [importlib.import_module(f"bot.handlers.{name}") for name in HANDLER_MODULES]
    main = sys.modules["bot.main"]
    blocked = {f"bot.handlers.{name}" for name in UNBOUND_HANDLER_MODULES}
    main.dp.message.handlers[:] = [
        handler
        for handler in main.dp.message.handlers
        if handler.callback.__module__ not in blocked
        and not any(isinstance(item.callback, State) for item in handler.filters)
    ]
    main.dp.callback_query.handlers[:] = [
        handler
        for handler in main.dp.callback_query.handlers
        if handler.callback.__module__ not in blocked
    ]
    return modules
