"""§19.7.2: ассеты Call/Price/Promotion строятся из РЕАЛЬНОГО профиля клиента и НЕ выдумываются.

Гард против фабрикации: без телефона нет call-ассета, без ≥3 числовых цен нет price, без явной
скидки нет promotion — вместо выдумки строитель бросает ValueError (вызывающий пропускает семейство).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clients.profile_assets import (  # noqa: E402
    _parse_price,
    build_profile_asset,
)


def test_parse_price_variants():
    assert _parse_price("от $4000") == (4000.0, "USD")
    assert _parse_price("10 000 грн") == (10000.0, "UAH")
    assert _parse_price("€20.50") == (20.5, "EUR")
    assert _parse_price("по договорённости") == (None, None)
    assert _parse_price(None) == (None, None)


def test_call_asset_from_real_phone():
    profile = {"contacts": [{"kind": "phone", "value": "+254 712 345 678"}]}
    spec = build_profile_asset("call", profile, country_code="KE")
    assert spec["family"] == "call"
    assert spec["params"]["phone_number"] == "+254 712 345 678"
    assert spec["params"]["country_code"] == "KE"


def test_call_asset_no_phone_raises_not_fabricates():
    with pytest.raises(ValueError, match="телефон"):
        build_profile_asset("call", {"contacts": []}, country_code="KE")


def test_price_asset_from_three_numeric_services():
    profile = {
        "services": [
            {"name": "Седаны", "price": "от $4000"},
            {"name": "Внедорожники", "price": "$7000"},
            {"name": "Хэтчбеки", "price": "$3000"},
            {"name": "Консультация", "price": "по договорённости"},  # без числа — пропущена
        ]
    }
    spec = build_profile_asset("price", profile, final_url="https://ex.com/cars")
    assert spec["family"] == "price"
    assert spec["params"]["currency"] == "USD"
    offers = spec["params"]["offerings"]
    assert len(offers) == 3  # только с числовой ценой
    assert all(o["final_url"] == "https://ex.com/cars" for o in offers)
    assert offers[0]["price_units"] == 4000.0


def test_price_asset_too_few_prices_raises():
    profile = {"services": [{"name": "Седаны", "price": "$4000"}]}  # только 1
    with pytest.raises(ValueError, match="<3"):
        build_profile_asset("price", profile, final_url="https://ex.com/")


def test_promotion_from_explicit_discount():
    profile = {"notes": "Акция: скидка 20% на все седаны в июле"}
    spec = build_profile_asset("promotion", profile, final_url="https://ex.com/")
    assert spec["family"] == "promotion"
    assert spec["params"]["percent_off"] == 20.0
    assert spec["params"]["promotion_target"]  # реальный объект акции из текста
    assert spec["params"]["money_off_units"] is None


def test_promotion_without_discount_raises_not_fabricates():
    profile = {"business_desc": "автодилер поддержанных авто", "services": []}
    with pytest.raises(ValueError, match="акци|скидк"):
        build_profile_asset("promotion", profile, final_url="https://ex.com/")
