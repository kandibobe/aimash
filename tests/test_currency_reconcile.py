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

from ads.resolve import currency_mismatch  # noqa: E402
from agent.tools.schemas import UpdateBid, UpdateBudget  # noqa: E402


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
