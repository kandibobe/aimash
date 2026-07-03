"""Инвариант порядка регистрации хендлеров (критично при разбиении bot/main.py на модули).

aiogram диспатчит по принципу «первый совпавший в порядке регистрации». Catch-all `on_text`
(@dp.message(F.text)) обязан быть ПОСЛЕДНИМ message-хендлером: любой хендлер, зарегистрированный
после него, никогда не получит текст (state-визарды §19/§20, /команды с текстом и т.д.).
Офлайн-тесты зовут хендлеры напрямую и НЕ видят регрессий порядка — этот тест закрывает дыру.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402


def _message_handler_names() -> list[str]:
    return [h.callback.__name__ for h in bm.dp.message.handlers]


def test_on_text_catchall_is_last_message_handler():
    names = _message_handler_names()
    assert names, "нет message-хендлеров — сломана регистрация dp?"
    assert names[-1] == "on_text", (
        f"catch-all on_text должен быть ПОСЛЕДНИМ message-хендлером, а он на позиции "
        f"{names.index('on_text') if 'on_text' in names else 'ОТСУТСТВУЕТ'} из {len(names)}; "
        f"последний: {names[-1]}. Хендлеры после on_text никогда не получат текст."
    )


def test_state_and_command_handlers_precede_catchall():
    """Выборочные критичные хендлеры (визард §19, клиенты §20, команды) — ДО on_text."""
    names = _message_handler_names()
    idx_text = names.index("on_text")
    for critical in (
        "cc_settings_desc",  # §19 Этап 1 (state-текст)
        "cc_account_search",  # §19 Этап 0 (поиск аккаунта)
        "cc_kw_verify",  # §19 Этап 2 (ссылка на таблицу)
        "cli_accumulate_text",  # §20 приём текста профиля
        "rsa_list_edited",  # §10 list-UX paste-back
        "report_",  # /report
        "on_document",  # файлы (ключи XLSX/CSV)
    ):
        assert critical in names, f"хендлер {critical} не зарегистрирован"
        assert names.index(critical) < idx_text, (
            f"{critical} зарегистрирован ПОСЛЕ catch-all on_text — текст до него не дойдёт"
        )


# ── 4A: HANDLER_MODULES — единственный источник порядка (star-импорты выпилены) ──────
def test_handler_modules_registry_invariants():
    """menu_guard — первым (3A), fallback — последним; без дублей."""
    from bot.handlers import HANDLER_MODULES

    assert HANDLER_MODULES[0] == "menu_guard", "3A: гард кнопок меню обязан идти первым"
    assert HANDLER_MODULES[-1] == "fallback", "catch-all on_text обязан регистрироваться последним"
    assert len(set(HANDLER_MODULES)) == len(HANDLER_MODULES), "дубль модуля в HANDLER_MODULES"


def test_no_star_imports_in_main():
    """Гард класса: порядко-зависимые `from bot.handlers.X import *` не должны вернуться в
    bot/main.py (их перестановка тихо скрамблила диспатч — prod-инцидент 2026-07-03).
    Матчим ИМПОРТ-СТЕЙТМЕНТ (начало строки), не подстроку — докстринги не триггерят."""
    import re

    src = (Path(__file__).resolve().parents[1] / "bot" / "main.py").read_text(encoding="utf-8")
    assert not re.search(r"^from\s+\S+\s+import\s+\*", src, re.M), (
        "star-импорт вернулся в bot/main.py — порядок диспатча снова хрупкий (см. HANDLER_MODULES)"
    )


def test_menu_guard_precedes_state_handlers():
    """3A: гард кнопок меню — раньше текстовых state-хендлеров визардов (иначе кнопку съест ввод)."""
    names = _message_handler_names()
    gpos = names.index("btn_guard_menu")
    for state_handler in ("cc_settings_desc", "cli_accumulate_text", "gdn_brief", "kw_seeds"):
        if state_handler in names:
            assert gpos < names.index(state_handler), (
                f"btn_guard_menu должен стоять РАНЬШЕ {state_handler}"
            )


def test_reexported_handler_names_present():
    """Ре-экспорт (4A): тесты/скрипты зовут хендлеры как bot.main.<handler> — выборочная проверка."""
    for name in ("on_text", "account_cmd", "btn_report", "cc_final_edit", "grant_cmd"):
        assert hasattr(bm, name), f"bot.main.{name} пропал после рефактора ре-экспорта"
