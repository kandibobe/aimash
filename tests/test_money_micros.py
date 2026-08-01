"""Exact conversion and strict-float guards for core.money.MoneyMicros."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from core.money import MICROS_PER_UNIT, MoneyMicros


def test_from_units_converts_exactly_without_float_math():
    amount = MoneyMicros.from_units("12.345678")

    assert amount.micros == 12_345_678
    assert amount.to_units() == Decimal("12.345678")
    assert int(amount) == 12_345_678
    assert MICROS_PER_UNIT == 1_000_000


@pytest.mark.parametrize("value", [1.5, True, "1000000"])
def test_raw_micros_reject_non_integer_values(value):
    with pytest.raises((TypeError, ValidationError)):
        MoneyMicros(micros=value)


def test_units_and_multiplier_reject_float():
    with pytest.raises(TypeError, match="float"):
        MoneyMicros.from_units(1.25)  # type: ignore[arg-type]

    amount = MoneyMicros.from_units("10")
    with pytest.raises(TypeError, match="float"):
        amount.scale(1.2)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="float"):
        amount * 1.2  # type: ignore[operator]


def test_decimal_scaling_returns_new_immutable_value():
    amount = MoneyMicros.from_units("10")
    scaled = amount * Decimal("1.25")

    assert amount.micros == 10_000_000
    assert scaled == MoneyMicros(micros=12_500_000)
    with pytest.raises(ValidationError):
        amount.micros = 1  # type: ignore[misc]


def test_sub_micro_precision_is_rejected_instead_of_rounded():
    with pytest.raises(ValueError, match="smaller than one micro"):
        MoneyMicros.from_units("0.0000001")

    with pytest.raises(ValueError, match="smaller than one micro"):
        MoneyMicros(micros=1).scale(Decimal("0.5"))


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", ""])
def test_invalid_or_non_finite_units_are_rejected(value):
    with pytest.raises(ValueError):
        MoneyMicros.from_units(value)
