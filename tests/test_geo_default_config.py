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
