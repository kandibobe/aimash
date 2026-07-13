"""Волна 2.1 — биллинг-единица ЗАВИСИТ ОТ ВАЛЮТЫ аккаунта (класс NON_MULTIPLE_OF_MINIMUM_CURRENCY_UNIT).

Раньше округление денег было валюто-слепым (всегда 10 000 micros). Для аккаунта в UGX (страна
деплоя — Уганда), JPY, KRW и ещё 17 валют минимальная биллинг-единица = 1 000 000 micros: значение
вроде 1 234 560 micros Google отвергает — причём уже ПОСЛЕ «да» пользователя (claim сожжён,
операция не применена, менеджер видит невнятный failed).

Таблица единиц снята живьём с API (CurrencyConstant.billable_unit_micros, v24, 2026-07-13).
Здесь пиннится: (1) сама таблица, (2) что округление у границы SDK берёт валюту АККАУНТА,
(3) что превью («станет» на карточке) считается той же единицей, что и исполнение.
"""

from __future__ import annotations

import pytest

from ads.resolve import compute_new_micros
from core.limits import (
    BILLING_UNIT_MICROS,
    billing_unit_micros,
    round_micros,
    wizard_default_money_units,
)


# ── 1. Таблица единиц ────────────────────────────────────────────────────────────
def test_billing_unit_by_currency():
    assert billing_unit_micros("USD") == 10_000
    assert billing_unit_micros("EUR") == 10_000
    assert billing_unit_micros("UAH") == 10_000
    assert billing_unit_micros("UGX") == 1_000_000  # страна деплоя
    assert billing_unit_micros("JPY") == 1_000_000
    assert billing_unit_micros("KRW") == 1_000_000
    assert billing_unit_micros("KWD") == 1_000  # трёхдесятичные валюты Залива
    assert billing_unit_micros("ugx") == 1_000_000  # регистр/пробелы не важны
    assert billing_unit_micros(" JPY ") == 1_000_000
    assert billing_unit_micros(None) == BILLING_UNIT_MICROS  # неизвестна → дефолт
    assert billing_unit_micros("") == BILLING_UNIT_MICROS
    assert billing_unit_micros("XXX") == BILLING_UNIT_MICROS


def test_zero_decimal_display_is_not_billing_unit():
    """Гард против соблазна вывести единицу из ZERO_DECIMAL_CURRENCIES: TWD/HUF показываются без
    копеек, но биллинг-единица у них 10 000 (живые данные API). Вывод «zero-decimal ⇒ 1 000 000»
    огрубил бы ставку в 100 раз."""
    from core.limits import ZERO_DECIMAL_CURRENCIES

    assert "TWD" in ZERO_DECIMAL_CURRENCIES and billing_unit_micros("TWD") == 10_000
    assert "HUF" in ZERO_DECIMAL_CURRENCIES and billing_unit_micros("HUF") == 10_000


# ── 2. round_micros / compute_new_micros ─────────────────────────────────────────
def test_round_micros_respects_currency():
    assert round_micros(1_234_560, currency="USD") == 1_230_000
    assert round_micros(1_234_560, currency="UGX") == 1_000_000  # единица 1e6
    assert round_micros(1_600_000, currency="JPY") == 2_000_000
    assert round_micros(400_000, currency="UGX") == 1_000_000  # >0, но < единицы → одна единица
    assert round_micros(0, currency="UGX") == 0


@pytest.mark.parametrize("currency", ["USD", "EUR", "UGX", "JPY", "KWD", None])
@pytest.mark.parametrize(
    ("mode", "value"),
    [
        ("increase_by_percent", 7),
        ("increase_by_percent", 12.5),
        ("increase_by_amount", 1.234),
        ("set_to", 5.555),
        ("set_to", 4321.0),
    ],
)
def test_compute_new_micros_multiple_of_currency_unit(currency, mode, value):
    """ЛЮБОЙ выход compute_new_micros кратен биллинг-единице СВОЕЙ валюты — иначе API отклонит
    подтверждённую операцию."""
    unit = billing_unit_micros(currency)
    for base in (5_550_000, 1_000_000, 7_777_777):
        out = compute_new_micros(base, mode, value, currency=currency)
        assert out % unit == 0, f"{currency}/{base}/{mode}/{value} → {out} не кратно {unit}"
        assert out > 0


def test_compute_new_micros_requires_currency_kwarg():
    """`currency` — ОБЯЗАТЕЛЬНЫЙ kwarg: новый call-site обязан решить, знает ли он валюту, а не
    молча унаследовать USD-единицу."""
    with pytest.raises(TypeError):
        compute_new_micros(1_000_000, "set_to", 5)  # type: ignore[call-arg]


# ── 3. Дефолты денег визарда ─────────────────────────────────────────────────────
def test_wizard_defaults_are_above_billing_unit():
    """Дефолтный CPC не может быть НИЖЕ минимальной биллинг-единицы валюты (иначе ставка либо
    отвергается, либо бессмысленна). Проверяем все валюты таблицы + дефолт."""
    from core.limits import WIZARD_DEFAULT_MONEY_UNITS

    for cur in [*WIZARD_DEFAULT_MONEY_UNITS, None, "XXX"]:
        budget_units, cpc_units = wizard_default_money_units(cur)
        unit = billing_unit_micros(cur)
        assert int(round(cpc_units * 1_000_000)) >= unit, f"{cur}: CPC {cpc_units} < единицы"
        assert int(round(budget_units * 1_000_000)) >= unit, f"{cur}: бюджет {budget_units}"


def test_ugx_wizard_default_is_sane():
    """UGX (Уганда — страна деплоя): дефолт «0.5 единицы за клик» = пол-шиллинга — абсурд и ниже
    биллинг-единицы. Должен быть валюто-зависимым."""
    budget_units, cpc_units = wizard_default_money_units("UGX")
    assert cpc_units >= 1.0 and budget_units >= 100.0


# ── 4. Форматирование карточки «было→станет» ─────────────────────────────────────
def test_fmt_micros_zero_decimal_currency():
    from bot.texts import _fmt_micros

    assert _fmt_micros(1_500_000_000, "UGX") == "1 500"  # zero-decimal → без копеек
    assert _fmt_micros(75_000_000, "JPY") == "75"
    assert _fmt_micros(12_480_000, "USD") == "12.48"  # обычная валюта — 2 знака, как раньше
    assert _fmt_micros(12_000_000) == "12.00"  # валюта не передана → прежнее поведение
