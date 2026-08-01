"""Strict monetary value object for Google Ads amounts stored in micros."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Self

from pydantic import BaseModel, ConfigDict, field_validator


MICROS_PER_UNIT = 1_000_000
_MICROS_PER_UNIT_DECIMAL = Decimal(MICROS_PER_UNIT)


def _decimal_value(value: str | int | Decimal, *, label: str) -> Decimal:
    """Parse an exact decimal value and explicitly reject binary floats."""
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{label} must be int, str, or Decimal; float is not allowed")
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise ValueError(f"{label} cannot be empty")
        try:
            parsed = Decimal(raw)
        except InvalidOperation as exc:
            raise ValueError(f"invalid {label}: {value!r}") from exc
    else:
        raise TypeError(f"{label} must be int, str, or Decimal")
    if not parsed.is_finite():
        raise ValueError(f"{label} must be finite")
    return parsed


def _exact_int(value: Decimal, *, label: str) -> int:
    integral = value.to_integral_value()
    if value != integral:
        raise ValueError(f"{label} has precision smaller than one micro")
    return int(integral)


class MoneyMicros(BaseModel):
    """Immutable amount represented by an exact integer number of micros.

    Use ``from_units`` at human/API boundaries and ``to_units`` for display. Raw
    floats are rejected both for construction and scaling so binary floating-point
    rounding cannot enter a Google Ads money path.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    micros: int

    @field_validator("micros", mode="before")
    @classmethod
    def _strict_micros(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("micros must be an int; raw float values are not allowed")
        return value

    @classmethod
    def from_micros(cls, value: int) -> Self:
        return cls(micros=value)

    @classmethod
    def from_units(cls, value: str | int | Decimal) -> Self:
        units = _decimal_value(value, label="money units")
        micros = _exact_int(units * _MICROS_PER_UNIT_DECIMAL, label="money amount")
        return cls(micros=micros)

    def to_units(self) -> Decimal:
        return Decimal(self.micros) / _MICROS_PER_UNIT_DECIMAL

    def scale(self, multiplier: str | int | Decimal) -> Self:
        factor = _decimal_value(multiplier, label="money multiplier")
        micros = _exact_int(Decimal(self.micros) * factor, label="scaled money amount")
        return type(self)(micros=micros)

    def __mul__(self, multiplier: int | Decimal) -> Self:
        return self.scale(multiplier)

    def __rmul__(self, multiplier: int | Decimal) -> Self:
        return self.scale(multiplier)

    def __int__(self) -> int:
        return self.micros
