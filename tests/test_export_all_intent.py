"""P1-8: детектор NL «экспорт статистики всех аккаунтов [за N дней]» → сводный MCC-экспорт.

Одно-аккаунтный get_stats это не покрывал (агент отдавал только один аккаунт); детектор роутит
такие фразы в /mcc (полный xlsx по всем дочерним). Чистая функция — офлайн, без SDK/сети.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402


@pytest.mark.parametrize(
    "text,exp_days",
    [
        ("мне надо эскпорт статистики всех кампаний что есть за 20 дней", "20"),
        ("экспорт всех аккаунтов", None),
        ("все аккаунта экспорт", None),
        ("статистика по всем аккаунтам за 7 дней", "7"),
        ("export all accounts for 30 days", "30"),
        ("выгрузи данные по всем аккаунтам", None),
    ],
)
def test_detects_export_all(text, exp_days):
    ok, days = bm.is_export_all_accounts(text)
    assert ok is True
    assert days == exp_days


@pytest.mark.parametrize(
    "text",
    [
        "измени бюджет доставка цветов на 2",
        "покажи отчёт по кампании X",
        "экспорт кампании доставка",  # одна кампания — НЕ все аккаунты
        "подбери ключи для доставки цветов",
        "",
    ],
)
def test_ignores_non_export_all(text):
    ok, days = bm.is_export_all_accounts(text)
    assert ok is False
    assert days is None
