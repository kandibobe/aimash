"""Офлайн-тесты §19.3 (Этап 1): извлечение настроек кампании из описания + сборка «по аналогии».

LLM подменяется заглушкой (без сети). Деньги/диапазоны/by_analogy считает КОД (golden rule #4).
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent.campaign_settings as CS  # noqa: E402
from agent.campaign_settings import (  # noqa: E402
    CampaignSettings,
    assemble_settings,
    extract_campaign_settings,
)


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


def _fake_chat(content: str | None = None, *, raises: bool = False):
    async def _chat(messages, **kwargs):
        if raises:
            raise RuntimeError("LLM down")
        return SimpleNamespace(content=content)

    return _chat


# ── extract_campaign_settings: строгий JSON → CampaignSettings ────────────────────
@pytest.mark.asyncio
async def test_extract_parses_full_description():
    content = json.dumps(
        {
            "campaign_name": None,
            "geo_locations": ["Кения"],
            "geo_country_code": "KE",
            "languages": ["English", "Swahili"],
            "budget_daily_units": 40,
            "currency": "USD",
            "goal": "calls",
            "bidding_strategy": None,
            "target_cpa_units": None,
            "payment_model": None,
        }
    )
    with patched(CS, "chat", _fake_chat(content)):
        s = await extract_campaign_settings("Кампания на Кению, б/у авто, $40/день, цель звонки")
    assert s.geo_locations == ["Кения"]
    assert s.geo_country_code == "KE"
    assert s.budget_daily_units == 40
    assert s.goal == "calls"
    assert s.currency == "USD"


@pytest.mark.asyncio
async def test_extract_empty_input_skips_llm():
    # пустой ввод → пустой объект без вызова модели (chat бросил бы — но не должен вызваться)
    with patched(CS, "chat", _fake_chat(raises=True)):
        s = await extract_campaign_settings("   ")
    assert s == CampaignSettings()


@pytest.mark.asyncio
async def test_extract_llm_failure_falls_back_empty():
    with patched(CS, "chat", _fake_chat(raises=True)):
        s = await extract_campaign_settings("что-то")
    assert s == CampaignSettings()


@pytest.mark.asyncio
async def test_extract_garbage_json_is_safe():
    with patched(CS, "chat", _fake_chat("не json вовсе")):
        s = await extract_campaign_settings("текст")
    assert s == CampaignSettings()


# ── assemble_settings: медианы «по аналогии» + дефолты + теги ─────────────────────
def test_assemble_tags_by_analogy_when_budget_from_median():
    extracted = CampaignSettings(geo_locations=["Кения"], goal="calls")  # бюджет/cpc не заданы
    out = assemble_settings(
        extracted,
        median_budget_micros=40_000_000,
        avg_cpc_micros=180_000,
        common_match_type="exact",
        topic="поддержанные авто",
    )
    assert out["budget_daily_micros"] == 40_000_000
    assert out["cpc_bid_micros"] == 180_000
    assert out["match_type"] == "exact"
    # все три подставлены из медиан → помечены «по аналогии» (и НЕ «по умолчанию»)
    for key in ("budget_daily_micros", "cpc_bid_micros", "match_type"):
        assert key in out["by_analogy"]
        assert key not in out["by_default"]
    # стратегия выведена из цели (calls) → без тегов источника
    assert "bidding_strategy" not in out["by_analogy"]
    assert "bidding_strategy" not in out["by_default"]
    # цель calls → maximize_conversions, оплата cpa (§19.3)
    assert out["bidding_strategy"] == "maximize_conversions"
    assert out["payment_model"] == "cpa"
    # авто-имя из geo + topic
    assert "Кения" in out["campaign_name"] and out["campaign_name"].endswith("Search")


def test_assemble_kenya_uses_product_and_audience_language():
    """§19: страна Кения ⇒ язык аудитории en (НЕ интерфейс ru), гео Кении, имя из product.
    Гард против регресса «тур в кению»/русские тексты для англоязычной кампании."""
    from ads import geo

    extracted = CampaignSettings(product="поддержанные авто", geo_locations=["Кения"], goal="calls")
    out = assemble_settings(extracted, topic="…полное описание…", ui_language="ru")
    assert out["product"] == "поддержанные авто"  # драйвер seed/RSA, не имя кампании
    assert out["target_language"] == "en"  # аудитория Кении, НЕ ru
    assert out["geo_country_code"] == "KE"
    assert out["geo_locale"] == "ru"  # язык названий локаций (менеджер писал «Кения»)
    assert out["campaign_name"] == "Кения · поддержанные авто · Search"
    assert geo.geo_ids_for_settings(out) == (2404,)  # Discover по Кении, НЕ Украине


def test_assemble_product_absent_falls_back_to_description_theme():
    # product не извлечён → тема seed/RSA = всё описание (не обрезано), имя — чистое geo+Search
    out = assemble_settings(
        CampaignSettings(geo_locations=["Кения"]),
        topic="длинное описание про авто",
        ui_language="ru",
    )
    assert out["product"] == "длинное описание про авто"
    assert out["campaign_name"] == "Кения · Search"  # без мусора в имени


def test_assemble_explicit_budget_not_by_analogy():
    extracted = CampaignSettings(budget_daily_units=60)
    out = assemble_settings(extracted, median_budget_micros=40_000_000)
    assert out["budget_daily_micros"] == 60_000_000
    assert "budget_daily_micros" not in out["by_analogy"]
    assert "budget_daily_micros" not in out["by_default"]  # задан пользователем — без тегов


def test_assemble_defaults_when_no_median():
    out = assemble_settings(CampaignSettings(), topic="тема")
    # без описания и медиан → дефолты (бюджет 10, cpc 0.5, phrase)
    assert out["budget_daily_micros"] == 10_000_000
    assert out["cpc_bid_micros"] == 500_000
    assert out["match_type"] == "phrase"
    # …и все помечены ЧЕСТНО «по умолчанию», а не «по аналогии» (истории аккаунта не было)
    for key in ("budget_daily_micros", "cpc_bid_micros", "match_type", "bidding_strategy"):
        assert key in out["by_default"], key
        assert key not in out["by_analogy"], key


# ── §19.3: сети / расписание / даты (таблица Этапа 1) ─────────────────────────────
def test_parse_ad_schedule_variants():
    from agent.campaign_settings import parse_ad_schedule

    assert parse_ad_schedule("24/7") == []  # круглосуточно — критерии не создаются
    assert parse_ad_schedule(None) == []
    assert parse_ad_schedule("круглосуточно") == []
    blocks = parse_ad_schedule("пн-пт 9-18")
    assert blocks is not None and len(blocks) == 5
    assert blocks[0] == {"day": "MONDAY", "start_hour": 9, "end_hour": 18}
    assert blocks[-1]["day"] == "FRIDAY"
    wk = parse_ad_schedule("будни 09:00-18:00")
    assert wk is not None and [b["day"] for b in wk] == [
        "MONDAY",
        "TUESDAY",
        "WEDNESDAY",
        "THURSDAY",
        "FRIDAY",
    ]
    lst = parse_ad_schedule("пн, ср, пт 10-20")
    assert lst is not None and [b["day"] for b in lst] == ["MONDAY", "WEDNESDAY", "FRIDAY"]
    assert parse_ad_schedule("каждый второй вторник") is None  # нераспознано → None (не молчим)
    assert parse_ad_schedule("пн-пт 18-9") is None  # start >= end — мусор


def test_assemble_networks_schedule_dates():
    from agent.campaign_settings import CampaignSettings as CS

    # Заданы явно → без «по аналогии», расписание распарсено в блоки, даты валидны
    out = assemble_settings(
        CS(
            networks="search_partners",
            ad_schedule="пн-пт 9-18",
            start_date="2026-08-01",
            end_date="2026-09-01",
        ),
        topic="тема",
    )
    assert out["networks"] == "search_partners" and "networks" not in out["by_default"]
    assert len(out["ad_schedule_blocks"]) == 5 and "ad_schedule" not in out["by_default"]
    assert out["ad_schedule"] == "пн-пт 9-18"
    assert out["start_date"] == "2026-08-01" and out["end_date"] == "2026-09-01"
    # Дефолты → Search-only, 24/7 «по умолчанию» (статический дефолт — НЕ история аккаунта)
    dflt = assemble_settings(CampaignSettings(), topic="тема")
    assert dflt["networks"] == "search" and "networks" in dflt["by_default"]
    assert "networks" not in dflt["by_analogy"] and "ad_schedule" not in dflt["by_analogy"]
    assert "ad_schedule" in dflt["by_default"]
    assert dflt["ad_schedule_blocks"] == [] and dflt["ad_schedule"] == "24/7"
    assert dflt["start_date"] is None and dflt["end_date"] is None
    # Конец раньше старта / мусорная дата → отброшены КОДОМ
    bad = assemble_settings(
        CS(start_date="2026-09-01", end_date="2026-08-01"),
        topic="т",
    )
    assert bad["end_date"] is None
    assert assemble_settings(CS(start_date="не дата"), topic="т")["start_date"] is None


# ── §B.3: честный показ пустых денежных метрик в сводке настроек ──────────────────
def test_settings_summary_shows_no_data_for_zero_cpc():
    from bot.texts import fmt_cc_settings_summary

    s = {
        "campaign_name": "Тест",
        "budget_daily_micros": 40_000_000,
        "cpc_bid_micros": 0,  # нет истории/тест-аккаунт → CPC неизвестен
        "currency": "USD",
    }
    out = fmt_cc_settings_summary(s, lang="ru")
    assert "нет данных" in out  # честный прочерк для CPC
    assert "≈ 0.00" not in out  # НЕ ложный ноль


# ── merge: пред-confirm правка перекрывает только непустые поля ───────────────────
def test_merge_overrides_only_nonempty():
    base = CampaignSettings(geo_locations=["Кения"], budget_daily_units=40, goal="calls")
    patch = CampaignSettings(budget_daily_units=60)  # «поставь бюджет 60»
    merged = base.merge(patch)
    assert merged.budget_daily_units == 60
    assert merged.geo_locations == ["Кения"]  # не затёрто пустым
    assert merged.goal == "calls"
