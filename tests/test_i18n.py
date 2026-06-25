"""Офлайн-тесты bot/i18n.py: каталог/мост к texts, фолбэк языка, in-memory выбор языка."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.i18n as i18n  # noqa: E402
from bot import texts  # noqa: E402


def test_t_returns_lang_variant():
    assert i18n.t("executing", "en") == "⏳ Working…"
    assert i18n.t("executing", "ru") == "⏳ Выполняю…"


def test_t_unknown_lang_falls_back_to_ru():
    assert i18n.t("rejected", "de") == "❌ Отменено"  # неизвестный язык → RU


def test_t_bridges_to_texts_for_unmigrated_key():
    # 'applied' нет в CATALOG → мост к texts.APPLIED
    assert i18n.t("applied", "ru") == texts.APPLIED


def test_t_unknown_key_returns_key_itself():
    assert i18n.t("definitely_unknown_key_xyz", "ru") == "definitely_unknown_key_xyz"


def test_t_formats_kwargs():
    # 'applied' не в CATALOG → texts.APPLIED с подстановкой
    assert i18n.t("applied", "ru", result="ok") == texts.APPLIED.format(result="ok")


def test_get_set_lang_in_memory():
    assert i18n.get_lang(424242) == "ru"  # дефолт
    assert i18n.set_lang(424242, "en") == "en"
    assert i18n.get_lang(424242) == "en"
    assert i18n.set_lang(424242, "xx") == "ru"  # нормализация неизвестного
