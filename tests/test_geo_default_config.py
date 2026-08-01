"""D7 (удобство 2026-07): гео-дефолт страны/языка — из конфига деплоя, НЕ захардкоженная Украина.

Раньше «UA»/«ru» были зашиты в 20+ местах; заказчик на Уганде получал Украину. Теперь ЕДИНЫЙ
источник (settings.geo_default_country/locale, env DEFAULT_GEO_COUNTRY_CODE/LOCALE): деплой Уганды
ставит UG → и схемы (default_factory), и резолв в service читают его. Дефолт СТРАНЫ — ПУСТО (без
биаса, гео из запроса); locale — «ru» (совместимость).
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import settings  # noqa: E402


@contextmanager
def geo_env(country: str, locale: str):
    a, b = settings.default_geo_country_code, settings.default_geo_locale
    settings.default_geo_country_code, settings.default_geo_locale = country, locale
    try:
        yield
    finally:
        settings.default_geo_country_code, settings.default_geo_locale = a, b


def test_default_country_is_empty_no_bias():
    # Дефолт страны ПУСТ (2026-07): без хардкод-биаса «UA» — страна берётся ИЗ ЗАПРОСА, а при её
    # отсутствии создание кампании показывает «глобально!» в превью и требует «да» (confirm-гейт).
    # env DEFAULT_GEO_COUNTRY_CODE=UG переопределяет глобально. locale по-прежнему «ru».
    assert settings.geo_default_country == ""
    assert settings.geo_default_locale == "ru"


def test_env_override_switches_country():
    with geo_env("UG", "en"):
        assert settings.geo_default_country == "UG"
        assert settings.geo_default_locale == "en"


def test_empty_locale_derives_from_country():
    # деплой ставит только страну (locale пусто) → язык выводится из страны (Уганда → en)
    with geo_env("UG", ""):
        assert settings.geo_default_locale == "en"
    with geo_env("DE", ""):
        assert settings.geo_default_locale == "de"


def test_unknown_country_locale_falls_back_to_ru():
    with geo_env("ZZ", ""):  # неизвестная страна → фолбэк «ru» (а не пусто/ошибка)
        assert settings.geo_default_locale == "ru"


def test_schema_default_factory_reads_config():
    from agent.tools.schemas import SetGeoLocation, SetGeoProximity

    with geo_env("UG", "en"):
        g = SetGeoLocation(campaign="X", locations=["Kampala"])
        assert g.country_code == "UG" and g.locale == "en"  # env дошёл до схемы NL-пути
        p = SetGeoProximity(campaign="X", radius_km=10, city_name="Kampala")
        assert p.country_code == "UG"


def test_create_campaign_schema_geo_default_from_config():
    # гео-поля создающих схем берут конфиг-дефолт через default_factory (не литерал «UA»)
    from agent.tools.schemas import CreateGdnCampaign, CreateSearchCampaign

    for model in (CreateGdnCampaign, CreateSearchCampaign):
        cc = model.model_fields["geo_country_code"].default_factory
        loc = model.model_fields["geo_locale"].default_factory
        assert cc is not None and loc is not None  # именно factory, не статичный «UA»
        with geo_env("UG", "en"):
            assert cc() == "UG" and loc() == "en"


# ── A1: подбор ключей без UA-биаса (keyword_plan/_kw_run) ──────────────────────────
def test_keyword_plan_default_geo_and_language_are_neutral():
    # Модульные дефолты БЕЗ биаса: пусто → generate_keyword_ideas не задаёт гео/язык (глобально).
    import ads.keyword_plan as kp

    assert kp.DEFAULT_GEO_IDS == ()  # не (2804 Украина)
    assert kp.DEFAULT_LANGUAGE == ""  # не «ru»


def test_call_and_price_asset_schemas_read_config():
    """D7: у CallAsset страна и у PriceAsset язык — из конфига, а не литералы «UA»/«uk». Литерал
    материализовался в model_dump() → env-фолбэк в ads/service.py становился мёртвым кодом, и
    украинский код номера уезжал в кампанию любой страны."""
    from agent.tools.schemas import AddCallAsset, AddPriceAsset, PriceOfferingItem

    for model, field in ((AddCallAsset, "country_code"), (AddPriceAsset, "language_code")):
        assert model.model_fields[field].default_factory is not None  # factory, не литерал

    offer = PriceOfferingItem(header="h", description="d", price_units=5, final_url="https://e.com")
    with geo_env("UG", "en"):
        assert AddCallAsset(campaign="X", phone_number="+256700000000").country_code == "UG"
        p = AddPriceAsset(campaign="X", currency="UGX", offerings=[offer, offer, offer])
        assert p.language_code == "en"
    with geo_env("DE", ""):
        assert AddCallAsset(campaign="X", phone_number="+4930000000").country_code == "DE"


def test_call_asset_without_country_fails_closed():
    """Страна номера обязательна для Google. Дефолта нет (env пуст) ⇒ ОТКАЗ с просьбой назвать
    страну — не подстановка «какой-нибудь» (прежний литерал «UA» тихо слал украинский код)."""
    import pytest
    from pydantic import ValidationError

    from agent.tools.schemas import AddCallAsset

    with geo_env("", "ru"):
        with pytest.raises(ValidationError):
            AddCallAsset(campaign="X", phone_number="+256700000000")
        # страна из запроса пользователя — проходит и без env-дефолта
        assert (
            AddCallAsset(campaign="X", phone_number="+256700", country_code="ug").country_code
            == "UG"
        )


def test_profile_assets_default_to_config_not_ua():
    """§19.7.2: строитель ассетов из профиля клиента не подставляет «UA»/«uk» своими дефолтами."""
    from clients.profile_assets import build_profile_asset

    profile = {
        "contacts": [{"kind": "phone", "value": "+256700000000"}],
        "services": [
            {"name": f"Услуга {i}", "description": "описание", "price": f"{100 + i}"}
            for i in range(3)
        ],
    }
    with geo_env("UG", "en"):
        call = build_profile_asset("call", profile)
        assert call["params"]["country_code"] == "UG"
        # currency_hint — валюта аккаунта: ЛЮБОЙ ISO-код (прежний список USD/UAH/EUR ронял UGX
        # → цены без символа отбрасывались → «<3 услуг» на угандийском аккаунте).
        price = build_profile_asset(
            "price", profile, final_url="https://e.com", currency_hint="UGX"
        )
        assert price["params"]["language_code"] == "en"
        assert price["params"]["currency"] == "UGX"


def test_no_hardcoded_ua_locale_literals():
    """Греп-гард класса D7: литералы «UA»/«uk» как ДЕФОЛТ в схемах/ассетах/визарде. Новый
    `country_code: str = "UA"` или `or "uk"` валит тест — именно так UA-биас возвращался в обход
    settings (в т.ч. в описаниях тулов, которые читает LLM)."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    # Дефолт-литерал: `= "UA"`, `or "uk"`, `("uk")`, `, "UA")` — но НЕ таблицы маппинга (ads/geo.py)
    # и не значения, пришедшие из запроса/профиля.
    pat = re.compile(r"""(=|or|,)\s*["'](UA|uk)["']""")
    skip = {
        "geo.py",
        "keyword_plan.py",
    }  # таблицы ISO→id / язык→id: там «UA»/«uk» — ключи, не дефолт
    offenders = []
    for pkg in ("ads", "bot", "agent", "clients", "adcopy", "keywords", "advisor"):
        for py in (root / pkg).rglob("*.py"):
            if "__pycache__" in str(py) or py.name in skip:
                continue
            for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
                s = line.strip()
                if s.startswith("#") or "по умолчанию UA" in s:
                    continue
                if pat.search(s):
                    offenders.append(f"{py.relative_to(root)}:{i}: {s}")
    assert not offenders, "хардкод гео-дефолта UA/uk вне settings-пути:\n" + "\n".join(offenders)


def test_no_hardcoded_ua_geo_id_in_ads_and_bot():
    """Греп-гард класса A1: нет module-level литерала украинского geo id (2804) вне settings-пути.
    Новый хардкод гео-биаса (как исходный DEFAULT_GEO_IDS=(2804,)) валит тест."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for pkg in ("ads", "bot"):
        for py in (root / pkg).rglob("*.py"):
            if "__pycache__" in str(py):
                continue
            for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
                s = line.strip()
                if s.startswith("#"):
                    continue
                # 2804 = geoTargetConstants Украины. Разрешён только в таблицах маппинга ISO→id
                # (ads/geo.py) — там он привязан к "UA", а не задаётся дефолтом.
                if "2804" in s and "geo.py" not in py.name:
                    offenders.append(f"{py.relative_to(root)}:{i}: {s}")
    assert not offenders, "хардкод UA geo id (2804) вне settings-пути:\n" + "\n".join(offenders)
