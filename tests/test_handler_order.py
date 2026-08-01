"""Agent-first инварианты регистрации aiogram handlers.

Свободный non-command текст обязан встретить ReAct catch-all раньше любого legacy FSM handler.
Slash-команды не входят в фильтр catch-all и продолжают жить в command handlers.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402
from aiogram.fsm.state import State  # noqa: E402


def _message_handler_names() -> list[str]:
    return [h.callback.__name__ for h in bm.dp.message.handlers]


def test_react_catchall_precedes_legacy_text_handlers():
    names = _message_handler_names()
    assert names, "нет message-хендлеров — сломана регистрация dp?"
    assert names[:2] == ["newcampaign_react", "on_text"]
    idx_text = names.index("on_text")
    for legacy in ("kw_seeds", "gdn_brief", "cli_accumulate_text", "rsa_list_edited"):
        if legacy in names:
            assert idx_text < names.index(legacy)


def test_startup_dispatcher_guard_accepts_react_first_order():
    bm._assert_dispatcher_ready()


def test_system_commands_remain_registered_behind_non_command_filter():
    names = _message_handler_names()
    for command in ("start", "help_", "cancel_cmd", "on_unknown_command"):
        assert command in names


def test_react_filter_accepts_free_text_and_excludes_slash_commands():
    handler = next(h for h in bm.dp.message.handlers if h.callback.__name__ == "on_text")
    match = handler.filters[0].callback
    assert match(SimpleNamespace(text="создай кампанию")) is True
    assert match(SimpleNamespace(text="/start")) is False


# ── 4A: HANDLER_MODULES — единственный источник порядка (star-импорты выпилены) ──────
def test_handler_modules_registry_invariants():
    """ReAct gateway — первым; campaign FSM/menu guard сняты; fallback — последним."""
    from bot.handlers import HANDLER_MODULES, UNBOUND_HANDLER_MODULES

    assert HANDLER_MODULES[0] == "react_gateway"
    assert HANDLER_MODULES[-1] == "fallback"
    assert "campaign_wizard" in HANDLER_MODULES
    assert "campaign_wizard" in UNBOUND_HANDLER_MODULES
    assert "menu_guard" in HANDLER_MODULES
    assert "menu_guard" in UNBOUND_HANDLER_MODULES
    assert len(set(HANDLER_MODULES)) == len(HANDLER_MODULES), "дубль модуля в HANDLER_MODULES"


def test_create_campaign_wizard_has_no_registered_handlers():
    handlers = [*bm.dp.message.handlers, *bm.dp.callback_query.handlers]
    assert all(h.callback.__module__ != "bot.handlers.campaign_wizard" for h in handlers)


def test_no_fsm_state_message_handlers_are_registered():
    assert not [
        handler.callback.__name__
        for handler in bm.dp.message.handlers
        if any(isinstance(item.callback, State) for item in handler.filters)
    ]


def test_no_star_imports_in_main():
    """Гард класса: порядко-зависимые `from bot.handlers.X import *` не должны вернуться в
    bot/main.py (их перестановка тихо скрамблила диспатч — prod-инцидент 2026-07-03).
    Матчим ИМПОРТ-СТЕЙТМЕНТ (начало строки), не подстроку — докстринги не триггерят."""
    import re

    src = (Path(__file__).resolve().parents[1] / "bot" / "main.py").read_text(encoding="utf-8")
    assert not re.search(r"^from\s+\S+\s+import\s+\*", src, re.M), (
        "star-импорт вернулся в bot/main.py — порядок диспатча снова хрупкий (см. HANDLER_MODULES)"
    )


def test_reexported_handler_names_present():
    """Ре-экспорт (4A): тесты/скрипты зовут хендлеры как bot.main.<handler> — выборочная проверка."""
    for name in ("on_text", "forward_to_react", "account_cmd", "btn_report", "grant_cmd"):
        assert hasattr(bm, name), f"bot.main.{name} пропал после рефактора ре-экспорта"


def test_reexport_survives_handler_imported_before_main():
    """Гард класса (круговой импорт): если хендлер-модуль импортирован ДО bot.main, его
    `import bot.main as bm` втягивал bot.main на середине себя, eager-реэкспорт видел ПОЛУ-собранный
    модуль и терял имена (btn_report). Прод не задет (bot.main — точка входа), но тесты ловили это
    как «хендлер пропал». Страховка — module __getattr__ (bot/main.py). Проверяем в ПОДПРОЦЕССЕ:
    в основном процессе bot.main уже импортирован conftest'ом, порядок не воспроизвести."""
    import subprocess

    code = (
        "import bot.handlers.reports\n"  # хендлер ПЕРВЫМ — триггерит круговой импорт bot.main
        "import bot.main as bm\n"
        "missing = [n for n in ('btn_report', 'on_text', 'account_cmd', 'grant_cmd') "
        "if not hasattr(bm, n)]\n"
        "assert not missing, missing\n"
        "print('OK')\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert r.returncode == 0, (
        f"реэкспорт потерял имена при импорте хендлера до bot.main:\n{r.stdout}\n{r.stderr}"
    )
