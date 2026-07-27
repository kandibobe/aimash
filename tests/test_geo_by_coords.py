"""Тесты для geo-by-coordinates (широта/долгота + радиус)."""

import pytest
from pydantic import ValidationError

from agent.tools.schemas import (
    MUTATION_TOOLS,
    SCHEMAS,
    SetGeoProximityByCoords,
)
from ads.service import SUPPORTED_OPERATIONS
from ads.mutations import MAX_RADIUS_KM


# ── Валидация схемы SetGeoProximityByCoords ──────────────────────────────


def test_valid_coords():
    """Хорошие координаты — схема принимает."""
    obj = SetGeoProximityByCoords(
        campaign="Test Campaign",
        latitude=50.4501,
        longitude=30.5234,
        radius_km=15.0,
    )
    assert obj.latitude == 50.4501
    assert obj.longitude == 30.5234
    assert obj.radius_km == 15.0


def test_valid_boundary_coords():
    """Граничные значения координат — принимаются."""
    obj = SetGeoProximityByCoords(
        campaign="Test",
        latitude=90.0,
        longitude=-180.0,
        radius_km=1.0,
    )
    assert obj.latitude == 90.0
    assert obj.longitude == -180.0

    obj = SetGeoProximityByCoords(
        campaign="Test",
        latitude=-90.0,
        longitude=180.0,
        radius_km=2000.0,  # max
    )
    assert obj.latitude == -90.0
    assert obj.longitude == 180.0
    assert obj.radius_km == 2000.0


def test_bad_latitude():
    """Широта за пределами -90..90 — ValidationError."""
    with pytest.raises(ValidationError):
        SetGeoProximityByCoords(
            campaign="Test",
            latitude=91.0,
            longitude=30.0,
            radius_km=5.0,
        )

    with pytest.raises(ValidationError):
        SetGeoProximityByCoords(
            campaign="Test",
            latitude=-91.0,
            longitude=30.0,
            radius_km=5.0,
        )


def test_bad_longitude():
    """Долгота за пределами -180..180 — ValidationError."""
    with pytest.raises(ValidationError):
        SetGeoProximityByCoords(
            campaign="Test",
            latitude=50.0,
            longitude=181.0,
            radius_km=5.0,
        )

    with pytest.raises(ValidationError):
        SetGeoProximityByCoords(
            campaign="Test",
            latitude=50.0,
            longitude=-181.0,
            radius_km=5.0,
        )


def test_radius_too_large():
    """Радиус > 2000 — ValidationError."""
    with pytest.raises(ValidationError):
        SetGeoProximityByCoords(
            campaign="Test",
            latitude=50.0,
            longitude=30.0,
            radius_km=2001.0,
        )


def test_radius_zero_or_negative():
    """Радиус ≤ 0 — ValidationError."""
    with pytest.raises(ValidationError):
        SetGeoProximityByCoords(
            campaign="Test",
            latitude=50.0,
            longitude=30.0,
            radius_km=0.0,
        )

    with pytest.raises(ValidationError):
        SetGeoProximityByCoords(
            campaign="Test",
            latitude=50.0,
            longitude=30.0,
            radius_km=-5.0,
        )


def test_missing_campaign():
    """Без campaign — ValidationError."""
    with pytest.raises(ValidationError):
        SetGeoProximityByCoords(
            latitude=50.0,
            longitude=30.0,
            radius_km=5.0,
        )


# ── Регистрация в реестрах ───────────────────────────────────────────────


def test_in_mutation_tools():
    """Операция должна быть в MUTATION_TOOLS."""
    assert "set_geo_proximity_by_coords" in MUTATION_TOOLS


def test_in_schemas():
    """Операция должна быть в SCHEMAS."""
    assert "set_geo_proximity_by_coords" in SCHEMAS
    assert SCHEMAS["set_geo_proximity_by_coords"] is SetGeoProximityByCoords


def test_in_supported_operations():
    """Операция должна быть в SUPPORTED_OPERATIONS."""
    assert "set_geo_proximity_by_coords" in SUPPORTED_OPERATIONS


# ── Валидация в apply-функции (код, не схема) ────────────────────────────


@pytest.mark.asyncio
async def test_apply_rejects_zero_radius():
    """apply_set_geo_proximity_by_coords: радиус 0 → ValueError до SDK."""
    from ads.mutations import apply_set_geo_proximity_by_coords

    try:
        await apply_set_geo_proximity_by_coords(
            customer_id="7753643025",
            campaign_id="23",
            latitude=50.0,
            longitude=30.0,
            radius_km=0.0,
            confirmation_id="test",
            confirm_store=None,
            ads_client=None,
        )
        raise AssertionError("ожидался ValueError")
    except ValueError as e:
        assert "радиус должен быть > 0" in str(e)


@pytest.mark.asyncio
async def test_apply_rejects_huge_radius():
    """apply_set_geo_proximity_by_coords: радиус > MAX_RADIUS_KM → ValueError."""
    from ads.mutations import apply_set_geo_proximity_by_coords

    try:
        await apply_set_geo_proximity_by_coords(
            customer_id="7753643025",
            campaign_id="23",
            latitude=50.0,
            longitude=30.0,
            radius_km=MAX_RADIUS_KM + 1,
            confirmation_id="test",
            confirm_store=None,
            ads_client=None,
        )
        raise AssertionError("ожидался ValueError")
    except ValueError as e:
        assert "подозрительно большой" in str(e)


@pytest.mark.asyncio
async def test_apply_rejects_bad_latitude():
    """apply_set_geo_proximity_by_coords: широта вне -90..90 → ValueError."""
    from ads.mutations import apply_set_geo_proximity_by_coords

    try:
        await apply_set_geo_proximity_by_coords(
            customer_id="7753643025",
            campaign_id="23",
            latitude=95.0,
            longitude=30.0,
            radius_km=5.0,
            confirmation_id="test",
            confirm_store=None,
            ads_client=None,
        )
        raise AssertionError("ожидался ValueError")
    except ValueError as e:
        assert "широта" in str(e)