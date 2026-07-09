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


def test_default_kw_geo_from_settings():
    """_default_kw_geo: пусто → () (глобально); env-страна → её geo id; неизвестная → ()."""
    import bot.main as bm
    from ads.geo import geo_id_for_country

    with geo_env("", "ru"):
        assert bm._default_kw_geo() == ()  # без биаса
    with geo_env("UG", "en"):
        assert bm._default_kw_geo() == (geo_id_for_country("UG"),)  # Уганда, не Украина
    with geo_env("ZZ", "ru"):
        assert bm._default_kw_geo() == ()  # неизвестная страна → глобально, не падаем


def test_resolve_kw_geo_distinguishes_none_and_empty():
    """Ключевой инвариант A1: geo_ids=() (явно «все страны») НЕ схлопывается в страну; None берёт
    «домашний» дефолт из settings; непустой кортеж — как есть. Раньше falsy-() давало Украину."""
    import bot.main as bm
    from ads.geo import geo_id_for_country

    with geo_env("UG", "en"):
        ug = geo_id_for_country("UG")
        assert bm._resolve_kw_geo(None) == (ug,)  # None → домашний дефолт (Уганда, не Украина)
        assert bm._resolve_kw_geo(()) == ()  # явно «все страны» → глобально (НЕ схлоп в страну)
        assert bm._resolve_kw_geo((1234,)) == (1234,)  # конкретное гео — как есть
    with geo_env("", "ru"):
        assert bm._resolve_kw_geo(None) == ()  # гео не задано, дефолта нет → глобально, без биаса
        assert bm._resolve_kw_geo(()) == ()


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
