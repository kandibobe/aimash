"""Волна 3: длина business_name считается ШИРИНОЙ (CJK=2) во ВСЕХ трёх слоях, а не двумя способами.

Было: adcopy.assert_asset_len мерил ширину (CJK=2), а ads.mutations и Pydantic-схемы — голым len().
CJK-бренд («株式会社サンプル…», 20 символов = ширина 40) проходил схему и код-валидацию, а падал уже
в SDK — то есть ПОСЛЕ подтверждения: claim сожжён, audit-row 'failed', повтор требует нового «да».
Лимит теперь один — adcopy.validate.ASSET_LIMITS (реестр), не второе число в ads/mutations.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adcopy.validate import ASSET_LIMITS  # noqa: E402
from ads.mutations import GDN_BUSINESS_NAME_MAX, _validate_gdn_inputs  # noqa: E402
from agent.tools.schemas import CreateGdnCampaign, CreateVideoCampaign  # noqa: E402

CJK_NAME = "株式会社サンプル商事株式会社サンプル"  # 18 символов, ширина 36 > 25
CYR_NAME = "Ромашка Цветы Доставка 24"  # 25 символов, ширина 25 — влезает


def _gdn_kwargs(business_name: str) -> dict:
    return dict(
        headlines=["Купить цветы"],
        long_headline="Доставка цветов по городу за час",
        descriptions=["Свежие букеты каждый день"],
        business_name=business_name,
        final_url="https://example.com",
        budget_daily_micros=5_000_000,
    )


def test_limit_comes_from_single_registry():
    assert GDN_BUSINESS_NAME_MAX == ASSET_LIMITS["business_name"]


def test_code_gate_rejects_cjk_business_name_over_width():
    with pytest.raises(ValueError):
        _validate_gdn_inputs(**_gdn_kwargs(CJK_NAME))


def test_code_gate_accepts_cyrillic_at_limit():
    _validate_gdn_inputs(**_gdn_kwargs(CYR_NAME))  # кириллица = 1 символ (golden rule #4)


def test_code_gate_rejects_empty_business_name():
    with pytest.raises(ValueError):
        _validate_gdn_inputs(**_gdn_kwargs(""))


def test_schemas_reject_cjk_before_confirm():
    """Схема — первый рубеж: отказ ДО показа карточки, а не после «да» (claim не жжём)."""
    for model, extra in (
        (CreateGdnCampaign, {"media_id": "abc123"}),
        (CreateVideoCampaign, {"youtube_video_id": "dQw4w9WgXcQ"}),
    ):
        with pytest.raises(Exception) as e:  # pydantic.ValidationError
            model(
                campaign_name="Тест",
                headlines=["Заголовок"],
                long_headline="Длинный заголовок",
                descriptions=["Описание"],
                business_name=CJK_NAME,
                final_url="https://example.com",
                budget_daily_micros=5_000_000,
                **extra,
            )
        assert "business_name" in str(e.value)
