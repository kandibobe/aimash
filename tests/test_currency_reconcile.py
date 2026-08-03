"""P0: денежная команда трактуется/сверяется в валюте аккаунта (FX НЕ делаем; golden rule #4).

Закрывает находку аудита: схема принимала currency и подтверждение её показывало, но
compute_new_micros считал value*1e6 как «в валюте аккаунта» — «было→станет» мог соврать
(«100 USD» на UAH-аккаунте записал бы 100 грн). Теперь:
- схема отвергает currency='percent' с абсолютным режимом (set_to/increase_by_amount);
- ads.resolve.currency_mismatch отклоняет сумму в валюте ≠ валюте аккаунта (на предпросмотре).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.resolve import currency_mismatch, detect_currency_token  # noqa: E402
from llm.schemas import (  # noqa: E402
    AddPriceAsset,
    AddPromotion,
    PriceOfferingItem,
    UpdateBid,
    UpdateBudget,
)


# ── Схема: связка mode↔currency (без SDK, ловит галлюцинацию currency='percent'+set_to) ──
def test_schema_rejects_percent_currency_with_absolute_mode():
    with pytest.raises(ValidationError):
        UpdateBudget(campaign="X", mode="set_to", value=50, currency="percent")
    with pytest.raises(ValidationError):
        UpdateBudget(campaign="X", mode="increase_by_amount", value=10, currency="percent")
    with pytest.raises(ValidationError):
        UpdateBid(campaign="X", mode="set_to", value=1.5, currency="percent")


def test_schema_currency_optional_defaults_none():
    assert UpdateBudget(campaign="X", mode="set_to", value=50).currency is None
    assert UpdateBudget(campaign="X", mode="set_to", value=50, currency="USD").currency == "USD"
    assert UpdateBudget(campaign="X", mode="increase_by_percent", value=20).currency is None
    assert UpdateBid(campaign="X", mode="set_to", value=2, currency="UAH").currency == "UAH"


# ── Сверка с валютой аккаунта (без FX) ────────────────────────────────────────────
def test_mismatch_foreign_currency_rejected():
    msg = currency_mismatch(
        "update_budget",
        {"campaign": "X", "mode": "set_to", "value": 100, "currency": "USD"},
        "UAH",
    )
    assert msg and "грн" in msg  # уточнение в валюте аккаунта


def test_bid_foreign_currency_rejected():
    msg = currency_mismatch(
        "update_bid",
        {"campaign": "X", "mode": "set_to", "value": 2, "currency": "EUR"},
        "UAH",
    )
    assert msg and "грн" in msg


def test_match_account_currency_ok():
    assert (
        currency_mismatch(
            "update_budget",
            {"campaign": "X", "mode": "set_to", "value": 100, "currency": "UAH"},
            "UAH",
        )
        is None
    )


def test_unspecified_currency_treated_as_account():
    assert (
        currency_mismatch("update_budget", {"campaign": "X", "mode": "set_to", "value": 100}, "UAH")
        is None
    )


def test_percent_mode_not_blocked():
    assert (
        currency_mismatch(
            "update_budget",
            {"campaign": "X", "mode": "increase_by_percent", "value": 20, "currency": "USD"},
            "UAH",
        )
        is None
    )


def test_unknown_account_currency_does_not_block():
    # read валюты не удался ('') — деградация, не блокируем (чужую валюту на показе не печатаем).
    assert (
        currency_mismatch(
            "update_budget",
            {"campaign": "X", "mode": "set_to", "value": 100, "currency": "USD"},
            "",
        )
        is None
    )


def test_non_money_op_ignored():
    assert currency_mismatch("add_keywords", {"campaign": "X", "currency": "USD"}, "UAH") is None


def test_case_insensitive_match():
    assert (
        currency_mismatch(
            "update_budget",
            {"campaign": "X", "mode": "set_to", "value": 100, "currency": "USD"},
            "usd",
        )
        is None
    )


# ── P0-1: ЛЮБОЙ ISO-код валюты принимается схемой (реальные аккаунты AUD/CZK/PLN не роняют) ──
@pytest.mark.parametrize("code", ["AUD", "CZK", "PLN", "GBP", "CAD", "aud", "Pln"])
def test_schema_accepts_any_iso_currency(code):
    # Раньше Literal["USD","UAH","EUR","percent"] ронял AUD/CZK/PLN literal_error'ом ДО валютной
    # сверки — самый частый краш в живом тесте (Draft ведётся в AUD).
    b = UpdateBudget(campaign="X", mode="set_to", value=2, currency=code)
    assert b.currency == code.upper()
    assert UpdateBid(campaign="X", mode="set_to", value=2, currency=code).currency == code.upper()


def test_schema_currency_symbol_and_word_aliases():
    assert UpdateBudget(campaign="X", mode="set_to", value=2, currency="$").currency == "USD"
    assert UpdateBudget(campaign="X", mode="set_to", value=2, currency="€").currency == "EUR"
    assert UpdateBudget(campaign="X", mode="set_to", value=2, currency="A$").currency == "AUD"
    assert UpdateBudget(campaign="X", mode="set_to", value=2, currency="грн").currency == "UAH"


def test_schema_rejects_garbage_currency():
    with pytest.raises(ValidationError):
        UpdateBudget(campaign="X", mode="set_to", value=2, currency="доллары США")
    with pytest.raises(ValidationError):
        UpdateBudget(campaign="X", mode="set_to", value=2, currency="")


def test_matching_aud_account_ok():
    # «2 aud» на AUD-аккаунте (Draft) должно проходить БЕЗ предупреждения.
    assert (
        currency_mismatch(
            "update_budget",
            {"campaign": "X", "mode": "set_to", "value": 2, "currency": "AUD"},
            "AUD",
        )
        is None
    )


def test_money_assets_accept_iso_reject_percent():
    AddPromotion(
        campaign="X",
        promotion_target="скидка",
        final_url="https://ex.com",
        money_off_units=10,
        currency="AUD",
    )
    with pytest.raises(ValidationError):
        AddPromotion(
            campaign="X",
            promotion_target="скидка",
            final_url="https://ex.com",
            money_off_units=10,
            currency="percent",
        )
    offer = PriceOfferingItem(
        header="h", description="d", price_units=5, final_url="https://ex.com"
    )
    AddPriceAsset(campaign="X", currency="PLN", offerings=[offer, offer, offer])
    with pytest.raises(ValidationError):
        AddPriceAsset(campaign="X", currency="percent", offerings=[offer, offer, offer])


# ── P0-1: детектор валютного токена в свободном тексте ───────────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        "измени бюджет доставка цветов на 2 aud в день",
        "поставь 100 usd",
        "бюджет 50 евро",
        "put budget to 30 EUR",
        "сделай 100 грн",
        "ставка 2 $",
        "цена 100 zł",
    ],
)
def test_detect_currency_token_true(text):
    assert detect_currency_token(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "измени бюджет доставка цветов на 2 в день",
        "поставь бюджет 100",
        "сделать бюджет этой кампании 2 в день",
        "подними ставку до 3",
        "",
    ],
)
def test_detect_currency_token_false(text):
    assert detect_currency_token(text) is False
