"""Доменные модули хендлеров aiogram (вынос из bot/main.py).

4A: ЕДИНСТВЕННЫЙ источник порядка регистрации — HANDLER_MODULES (порядок импорта модуля =
порядок диспатча aiogram). Раньше порядок задавали 12 хрупких `from bot.handlers.X import *`
в хвосте bot/main.py: случайная перестановка строк (ruff/IDE/мерж) тихо ломала диспатч —
on_text переставал быть последним и глотал команды. Инварианты:
  • menu_guard — СТРОГО первым (3A: кнопки меню раньше state-хендлеров визардов);
  • fallback (catch-all on_text) — СТРОГО последним.
Закреплено tests/test_handler_order.py. Регистрация по-прежнему происходит декораторами
@bm.dp.message(...) при импорте модуля — register_all лишь импортирует в детерминированном порядке.
"""

from __future__ import annotations

import importlib
from types import ModuleType

HANDLER_MODULES: tuple[str, ...] = (
    "menu_guard",  # 3A: гард кнопок меню — раньше ЛЮБЫХ state-хендлеров
    "commands",
    "reports",
    "keywords_flow",
    "campaigns_menu",
    "rsa_flow",
    "search_media",
    "campaign_wizard",
    "clients_kb",
    "templates_recent",
    "confirm_flow",
    "fallback",  # catch-all on_text — ПОСЛЕДНИМ (иначе LLM-фолбэк глотает команды/визарды)
)


def register_all() -> list[ModuleType]:
    """Импортировать все хендлер-модули в порядке HANDLER_MODULES (регистрация — побочный
    эффект импорта). Возвращает модули для ре-экспорта имён в bot.main."""
    return [importlib.import_module(f"bot.handlers.{name}") for name in HANDLER_MODULES]
