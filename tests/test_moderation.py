"""§10: эвристики редакторской политики Google Ads считает КОД, не промпт (как длину). Advisory —
не блок. Проверяем коды замечаний (КАПС/пунктуация/повторы), что аббревиатуры ≤3 не ловятся,
кириллица/латиница единообразны, и что fmt_rsa_diagnostics добавляет ⚠️ при рискованных текстах."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adcopy.validate import count_flagged, moderation_issues  # noqa: E402
from bot import ux
from core import i18n  # noqa: E402


# ── чистые тексты — без замечаний ───────────────────────────────────────────────────
def test_clean_text_no_issues():
    assert moderation_issues("Купите цветы со скидкой сегодня") == []
    assert moderation_issues("Buy fresh flowers today") == []
    assert moderation_issues("Доставка за 2 часа!") == []  # один «!» — норма


# ── КАПС ────────────────────────────────────────────────────────────────────────────
def test_all_caps_flagged_latin_and_cyrillic():
    assert "all_caps" in moderation_issues("SALE only today")  # SALE=4 заглавных
    assert "all_caps" in moderation_issues("СКИДКА только сегодня")


def test_short_acronyms_not_flagged_as_caps():
    # аббревиатуры ≤3 букв (SEO, USA, ЦБ) — норма, не «капс»
    assert "all_caps" not in moderation_issues("SEO и SMM для USA")
    assert "all_caps" not in moderation_issues("ЦБ снизил ставку")


def test_digits_break_word_so_no_caps():
    # «B2B» и «01001» — цифры рвут слово, isupper по буквенным кускам ≤1 → не капс
    assert "all_caps" not in moderation_issues("B2B решения, индекс 01001")


# ── пунктуация ──────────────────────────────────────────────────────────────────────
def test_excess_punctuation_flagged():
    assert "excess_punct" in moderation_issues("Распродажа!!")
    assert "excess_punct" in moderation_issues("Что?! Уже всё")
    assert "excess_punct" in moderation_issues("Купи сейчас! Сэкономь больше!")  # два «!»


def test_single_exclamation_ok():
    assert "excess_punct" not in moderation_issues("Налетай!")


# ── повторы ─────────────────────────────────────────────────────────────────────────
def test_repeat_char_flagged():
    assert "repeat_char" in moderation_issues("Saaale today")
    assert "repeat_char" in moderation_issues("ооочень выгодно")


def test_repeat_word_flagged():
    assert "repeat_word" in moderation_issues("deal deal of the day")
    assert "repeat_word" in moderation_issues("купи купи прямо сейчас")


def test_multiple_issues_collected():
    issues = set(moderation_issues("SALE SALE!!!"))
    assert {"all_caps", "excess_punct", "repeat_word"} <= issues


# ── count_flagged ──────────────────────────────────────────────────────────────────
def test_count_flagged_counts_only_flagged_and_ignores_nonstr():
    texts = ["Чистый текст", "КРИЧАЩИЙ текст", "ещё чистый", "Saaale"]
    assert count_flagged(texts) == 2
    # нестроки (дакт-фейки в тестах передают счётчики) игнорируются, не падаем
    assert count_flagged([1, 2, 3]) == 0


# ── интеграция: fmt_rsa_diagnostics добавляет ⚠️ при рискованных текстах ─────────────
def _draft(headlines: list[str], descriptions: list[str]):
    return SimpleNamespace(
        headlines=headlines, descriptions=descriptions, dropped_headlines=0, dropped_descriptions=0
    )


def test_diagnostics_appends_advisory_ru_when_flagged():
    d = _draft(["КУПИ сейчас", "обычный заголовок", "ещё один"], ["норм описание", "и второе"])
    s = ux.fmt_rsa_diagnostics(d, 15, 4, lang="ru")
    assert s.startswith("✅ Сгенерировано")
    assert "⚠️" in s and "модерац" in s  # advisory о риске модерации


def test_diagnostics_appends_advisory_en_when_flagged():
    d = _draft(["SAVE big today", "normal headline", "third one"], ["fine description", "second"])
    s = ux.fmt_rsa_diagnostics(d, 15, 4, lang="en")
    assert s.startswith("✅ Generated")
    assert "⚠️" in s and "ad review" in s


def test_diagnostics_no_advisory_when_clean():
    d = _draft(["Обычный заголовок", "Второй заголовок"], ["Спокойное описание"])
    s = ux.fmt_rsa_diagnostics(d, 15, 4, lang="ru")
    assert "⚠️" not in s  # чисто → без предупреждения


def test_diagnostics_follows_contextvar_with_advisory():
    """Без явного lang язык берётся из contextvar (как у форматтеров) — и advisory тоже на нём."""
    d = _draft(["SAVE big today", "ok one", "ok two"], ["fine", "second"])
    tok = i18n.set_current_lang("en")
    try:
        assert "ad review" in ux.fmt_rsa_diagnostics(d, 15, 4)
    finally:
        i18n.reset_current_lang(tok)


# ── §10 CTA-эвристика (advisory: хотя бы один призыв к действию в наборе) ─────────────
def test_has_cta_detects_multilingual():
    from adcopy.validate import any_cta, has_cta

    assert has_cta("Купите цветы сегодня")  # RU стем «куп»
    assert has_cta("Order now and save")  # EN order
    assert has_cta("Замовляйте зараз")  # UK замов
    assert not has_cta("Красивые свежие цветы")  # без призыва
    assert any_cta(["описание", "Звоните нам"])  # хотя бы один
    assert not any_cta(["просто текст", "ещё букет"])


def test_diagnostics_hints_when_no_cta_ru():
    d = _draft(
        ["Свежие цветы", "Большой выбор", "Доставка по городу"], ["Красивые букеты", "Много сортов"]
    )
    s = ux.fmt_rsa_diagnostics(d, 15, 4, lang="ru")
    assert "💡" in s and "действию" in s  # подсказка добавить CTA


def test_diagnostics_no_cta_hint_when_present():
    d = _draft(["Купите цветы", "Большой выбор", "Доставка"], ["Заказать букет", "Много сортов"])
    s = ux.fmt_rsa_diagnostics(d, 15, 4, lang="ru")
    assert "💡" not in s  # CTA есть → без подсказки
