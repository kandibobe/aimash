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
