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


# ── P0-4: относительные даты резолвит КОД, если модель их не дала ─────────────────
@pytest.mark.asyncio
async def test_extract_resolves_relative_dates_when_model_null():
    content = json.dumps({"geo_locations": ["Украина"], "start_date": None, "end_date": None})
    with patched(CS, "chat", _fake_chat(content)):
        s = await extract_campaign_settings("дата от сегодня до завтра, язык украинский")
    # обе даты проставлены и end > start (конкретные значения зависят от «сегодня»)
    assert s.start_date and s.end_date and s.end_date > s.start_date


@pytest.mark.asyncio
async def test_extract_keeps_model_iso_dates_over_relative():
    content = json.dumps({"start_date": "2027-01-10", "end_date": "2027-01-20"})
    with patched(CS, "chat", _fake_chat(content)):
        s = await extract_campaign_settings(
            "старт завтра"
        )  # текст относительный, но модель дала ISO
    assert s.start_date == "2027-01-10"  # явную ISO-дату модели НЕ перетираем
    assert s.end_date == "2027-01-20"


def test_parse_relative_dates_offline():
    from datetime import date

    from agent.campaign_settings import parse_relative_dates

    t = date(2026, 7, 4)
    assert parse_relative_dates("от сегодня до завтра", t) == ("2026-07-04", "2026-07-05")
    assert parse_relative_dates("до завтра", t) == (None, "2026-07-05")
    assert parse_relative_dates("старт завтра", t) == ("2026-07-05", None)
    assert parse_relative_dates("через 7 дней", t) == ("2026-07-11", None)
    assert parse_relative_dates("с 01.08 по 15.08", t) == ("2026-08-01", "2026-08-15")
    # строка расписания «пн-пт 9-18» НЕ должна ложно распознаться как дата
    assert parse_relative_dates("пн-пт 9-18", t) == (None, None)
    assert parse_relative_dates("доставка цветов украина", t) == (None, None)


def test_parse_relative_dates_does_not_take_price_for_a_date():
    """D2: «цена за клик 1.5» раньше давала start_date=2027-05-01 (1 мая уже прошло → следующий
    год) — кампания стартовала через 10 месяцев. Дробь ≠ дата."""
    from datetime import date

    from agent.campaign_settings import parse_relative_dates

    t = date(2026, 7, 4)
    assert parse_relative_dates("цена за клик 1.5", t) == (None, None)
    assert parse_relative_dates("бюджет 50, ставка 2.75", t) == (None, None)
    assert parse_relative_dates("ставка 1.05 usd", t) == (
        None,
        None,
    )  # 2-значный «месяц», но деньги
    assert parse_relative_dates("1.5 грн за клик", t) == (None, None)
    # даты не сломались: 2-значный месяц и явный год работают
    assert parse_relative_dates("старт 1.08", t) == ("2026-08-01", None)
    assert parse_relative_dates("с 1.5.2027", t) == ("2027-05-01", None)


# ── P0-5: язык по умолчанию выводится из ГЕО (Украина → uk), а не «русский» ───────
def test_assemble_language_derived_from_geo_when_not_named():
    # модель НЕ вернула язык (пустой список) → код берёт язык страны (UA → uk → Ukrainian)
    extracted = CampaignSettings(geo_locations=["Украина"], geo_country_code="UA")
    out = assemble_settings(extracted, topic="ресторан")
    assert out["target_language"] == "uk"
    assert out["languages"] == ["Ukrainian"]


def test_assemble_language_respects_explicit_choice():
    # пользователь ЯВНО назвал русский для Украины → уважаем выбор
    extracted = CampaignSettings(
        geo_locations=["Украина"], geo_country_code="UA", languages=["русский"]
    )
    out = assemble_settings(extracted, topic="ресторан")
    assert out["target_language"] == "ru"


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


# ── W1 (живой тест 2026-07-06): max CPC редактируем end-to-end ─────────────────────
def test_assemble_explicit_max_cpc_wins_over_median_and_default():
    out = assemble_settings(CampaignSettings(max_cpc_units=75), avg_cpc_micros=180_000, topic="т")
    assert out["cpc_bid_micros"] == 75_000_000
    # задан пользователем → БЕЗ тегов источника (сводка покажет «Макс. CPC» без «≈»)
    assert "cpc_bid_micros" not in out["by_analogy"]
    assert "cpc_bid_micros" not in out["by_default"]


def test_assemble_nonpositive_max_cpc_ignored():
    # <=0 — мусор от модели: не даём занулить бид, откатываемся на медиану/дефолт
    out = assemble_settings(CampaignSettings(max_cpc_units=0), avg_cpc_micros=180_000, topic="т")
    assert out["cpc_bid_micros"] == 180_000
    out2 = assemble_settings(CampaignSettings(max_cpc_units=-5), topic="т")
    assert out2["cpc_bid_micros"] == 500_000  # фолбэк-дефолт (валюта неизвестна)


def test_patch_max_cpc_applies_and_clears_source_tags():
    """«максимальная цена за клик 75» на Этапе 1 реально меняет черновик (раньше ветки не было)."""
    import bot.main as bm

    cur = {
        "cpc_bid_micros": 500_000,
        "by_analogy": [],
        "by_default": ["cpc_bid_micros"],
    }
    merged = bm._cc_apply_settings_patch(cur, CampaignSettings(max_cpc_units=75))
    assert merged["cpc_bid_micros"] == 75_000_000
    assert "cpc_bid_micros" not in merged["by_default"]
    # <=0 не зануляет бид
    same = bm._cc_apply_settings_patch(dict(merged), CampaignSettings(max_cpc_units=0))
    assert same["cpc_bid_micros"] == 75_000_000


def test_cpc_phrases_are_settings_markers():
    """3B: «цена за клик»/«стоимость клика» — правка НАСТРОЕК, не literal-replace по тексту RSA."""
    from agent.campaign_edit import is_settings_edit

    assert is_settings_edit("Максимальная цена за клик 75 йен")
    assert is_settings_edit("поставь стоимость клика 20")
    assert is_settings_edit("set cost per click to 0.7")
    assert not is_settings_edit("смени „Быстро“ на „Надёжно“")


# ── W3 (живой тест 2026-07-06): валютно-осознанные дефолты денег ──────────────────
def test_assemble_currency_aware_defaults_jpy():
    # валюта аккаунта JPY → дефолты в йенах (1500/75), а не абсурд «0.50 JPY»
    out = assemble_settings(CampaignSettings(), topic="т", account_currency="JPY")
    assert out["currency"] == "JPY"
    assert out["budget_daily_micros"] == 1_500_000_000
    assert out["cpc_bid_micros"] == 75_000_000
    for key in ("budget_daily_micros", "cpc_bid_micros"):
        assert key in out["by_default"]


def test_assemble_account_currency_beats_explicit():
    # Ревью 2026-07-07: Google бронирует деньги в валюте АККАУНТА — она авторитетна для
    # сводки/дефолтов; «йен» в тексте на USD-аккаунте не должна врать в карточке.
    out = assemble_settings(CampaignSettings(currency="jpy"), topic="т", account_currency="USD")
    assert out["currency"] == "USD"
    assert out["cpc_bid_micros"] == 500_000  # дефолты по USD
    # валюта аккаунта неизвестна (тест-аккаунт) → явная из текста используется (и нормализуется)
    out2 = assemble_settings(CampaignSettings(currency="jpy"), topic="т")
    assert out2["currency"] == "JPY"
    assert out2["cpc_bid_micros"] == 75_000_000  # дефолты по JPY


def test_assemble_unknown_currency_falls_back_to_legacy_defaults():
    out = assemble_settings(CampaignSettings(), topic="т", account_currency="XXX")
    assert out["budget_daily_micros"] == 10_000_000
    assert out["cpc_bid_micros"] == 500_000


def test_settings_summary_zero_decimal_currency_and_user_set_cpc_label():
    from bot.texts import fmt_cc_settings_summary

    s = {
        "campaign_name": "Тест",
        "budget_daily_micros": 1_900_000_000,
        "cpc_bid_micros": 75_000_000,
        "currency": "JPY",
        "by_analogy": [],
        "by_default": [],  # CPC задан пользователем
    }
    out = fmt_cc_settings_summary(s, lang="ru")
    assert "1 900 JPY" in out  # без «.00» у zero-decimal валюты (NBSP-разделитель)
    assert "Макс. CPC: 75 JPY" in out  # точное значение без «≈»
    assert "≈" not in out.split("Макс. CPC")[1].split("\n")[0]
    # дефолтный CPC — прежний рендер «Ср. CPC: ≈ …»
    s2 = {**s, "by_default": ["cpc_bid_micros"]}
    out2 = fmt_cc_settings_summary(s2, lang="ru")
    assert "Ср. CPC: ≈ 75 JPY" in out2


def test_wizard_default_money_units_table_consistency():
    """Класс-гард: таблица дефолтов покрывает все zero-decimal валюты и не содержит нулей."""
    from core.limits import (
        WIZARD_DEFAULT_MONEY_UNITS,
        ZERO_DECIMAL_CURRENCIES,
        wizard_default_money_units,
    )

    assert ZERO_DECIMAL_CURRENCIES <= set(WIZARD_DEFAULT_MONEY_UNITS)
    for cur, (budget, cpc) in WIZARD_DEFAULT_MONEY_UNITS.items():
        assert budget > 0 and cpc > 0, cur
        assert budget > cpc, cur  # дневной бюджет всегда больше клика
    assert wizard_default_money_units(None) == (10.0, 0.5)
    assert wizard_default_money_units("jpy") == WIZARD_DEFAULT_MONEY_UNITS["JPY"]


def test_final_summary_zero_decimal_currency():
    """Ревью 2026-07-07: финальная сводка (Этап 7) тоже без копеек у zero-decimal валют."""
    from bot.texts import fmt_cc_final_summary

    state = {
        "settings": {
            "campaign_name": "Тест",
            "budget_daily_micros": 1_500_000_000,
            "currency": "JPY",
            "geo_locations": ["Уганда"],
        },
        "ad": {"final_url": "https://x.example", "headlines": [], "descriptions": []},
        "keywords": {"list": []},
    }
    out = fmt_cc_final_summary(state, lang="ru")
    assert "1 500 JPY" in out
    assert "1 500.00" not in out
