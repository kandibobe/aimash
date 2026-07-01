"""§19.3 (Этап 1): извлечение настроек кампании из СВОБОДНОГО ОПИСАНИЯ менеджера через LLM.

Менеджер пишет «Создай кампанию на Кению, поддержанные авто, бюджет $40/день, цель — звонки».
Модель (роль parsing — дёшево, function-calling-путь) раскладывает это в типизированную
CampaignSettings; ДИАПАЗОНЫ/деньги/имя считает КОД (golden rule #4), модель лишь заполняет поля.
Это advisory-разбор: SDK не трогается, confirm-гейт не тратится — реальная мутация (создание
кампании) минтуется отдельным proposal в конце визарда.

Чего модель не извлекла (None) — заполняется «по аналогии» из медиан прошлых кампаний аккаунта
(ads.read.search_campaign_medians) в assemble_settings(); такие поля помечаются тегом by_analogy,
чтобы менеджер видел подставленное значение. Паттерн строгого JSON зеркалит keywords.cluster.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from agent.router import chat
from core.limits import MONEY_MAX_UNITS

# Дефолты, если ни описание, ни медианы аккаунта не дали значения (нейтральные, безопасные на
# тест-аккаунте; cpc зеркалит дефолт ads.mutations.apply_create_search_campaign).
DEFAULT_DAILY_BUDGET_UNITS = 10.0
DEFAULT_CPC_UNITS = 0.5
DEFAULT_MATCH_TYPE = "phrase"

Goal = Literal["calls", "leads", "sales", "traffic", "awareness"]
Bidding = Literal["manual_cpc", "maximize_conversions", "maximize_conversion_value", "target_spend"]


class CampaignSettings(BaseModel):
    """Извлечённые из описания настройки. Все поля опциональны: None ⇒ «не указано» (заполнит
    assemble_settings из медиан/дефолтов). Модель НЕ считает деньги — только переносит из текста."""

    campaign_name: str | None = None
    product: str | None = None  # ЧТО рекламируется (товар/услуга): «поддержанные авто» — драйвер
    geo_locations: list[str] = Field(default_factory=list)  # ["Кения"] / ["Украина","Киев"]
    geo_country_code: str | None = None  # ISO alpha-2, если модель уверенно вывела
    languages: list[str] = Field(default_factory=list)  # ["English","Swahili"]
    budget_daily_units: float | None = None
    currency: str | None = None  # USD/UAH/EUR — если ЯВНО назван
    goal: Goal | None = None
    bidding_strategy: Bidding | None = None
    target_cpa_units: float | None = None
    payment_model: Literal["cpc", "cpa"] | None = None

    def merge(self, patch: "CampaignSettings") -> "CampaignSettings":
        """Наложить правку (пред-confirm: «поставь бюджет 60»): непустые поля patch перекрывают.
        Списки перекрываются целиком если непусты. Возвращает НОВЫЙ объект."""
        base = self.model_dump()
        for k, v in patch.model_dump().items():
            if v is None:
                continue
            if isinstance(v, list) and not v:
                continue
            base[k] = v
        return CampaignSettings(**base)


_SYSTEM = (
    "Ты — специалист по Google Ads. Извлеки из описания рекламной кампании настройки и верни "
    "СТРОГО один JSON-объект без пояснений и markdown со СЛЕДУЮЩИМИ полями (любое неизвестное — "
    "null, не выдумывай): "
    '{"campaign_name": "короткое имя или null", '
    '"product": "ЧТО именно рекламируется — товар или услуга, кратко и по сути, БЕЗ слов '
    "'создай кампанию'/страны/бюджета (напр. 'поддержанные авто', 'доставка цветов'); "
    'null если неясно", '
    '"geo_locations": ["страна/город как в тексте"], '
    '"geo_country_code": "ISO alpha-2 страны таргетинга (Кения→KE, Украина→UA) или null", '
    '"languages": ["язык(и), на которых ищет аудитория ЦЕЛЕВОЙ страны, напр. для Кении '
    'English, Swahili"], '
    '"budget_daily_units": число дневного бюджета или null, '
    '"currency": "USD|UAH|EUR если ЯВНО назван, иначе null", '
    '"goal": "calls|leads|sales|traffic|awareness или null", '
    '"bidding_strategy": "manual_cpc|maximize_conversions|maximize_conversion_value|target_spend '
    'или null", '
    '"target_cpa_units": число целевой цены за конверсию или null, '
    '"payment_model": "cpc|cpa или null"}. '
    "Деньги указывай числом без символа валюты. Не добавляй полей сверх перечисленных."
)


def _extract_json_object(content: str) -> dict | None:
    """Достать первый JSON-объект из ответа модели (как keywords.cluster для массива)."""
    s = content or ""
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        data = json.loads(s[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _coerce(data: dict) -> CampaignSettings:
    """dict от модели → CampaignSettings, отбрасывая мусор (Pydantic-валидация, лишнее игнор)."""
    try:
        return CampaignSettings.model_validate(data)
    except Exception:  # noqa: BLE001 — кривой ответ модели не должен ронять визард
        # поле-за-полем спасение: оставить только валидные ключи
        safe = {k: data.get(k) for k in CampaignSettings.model_fields if k in data}
        try:
            return CampaignSettings.model_validate(safe)
        except Exception:  # noqa: BLE001
            return CampaignSettings()


async def extract_campaign_settings(description: str, *, language: str = "ru") -> CampaignSettings:
    """Распарсить свободное описание в CampaignSettings (роль parsing). Fallback — пустой объект
    (всё заполнит assemble_settings из медиан/дефолтов). Пустой ввод → пустой объект без вызова LLM."""
    text = (description or "").strip()
    if not text:
        return CampaignSettings()
    try:
        msg = await chat(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": f"Язык интерфейса: {language}.\nОписание:\n{text}"},
            ],
            role="parsing",
            temperature=0.2,
        )
        data = _extract_json_object(getattr(msg, "content", "") or "")
    except Exception:  # noqa: BLE001 — разбор не критичен, есть fallback на медианы/дефолты
        data = None
    return _coerce(data) if data is not None else CampaignSettings()


# ── Сборка итоговых настроек: extracted + медианы «по аналогии» + дефолты ─────────
def _derive_bidding(s: CampaignSettings) -> tuple[str, int | None, str | None]:
    """Стратегия ставок из цели (§19.3, таблица): возвращает (strategy, target_cpa_micros, payment).
    Явная стратегия из описания приоритетна; иначе выводим из goal."""
    payment = s.payment_model
    if s.bidding_strategy:
        strat = s.bidding_strategy
    elif s.goal in ("calls", "leads"):
        strat = "maximize_conversions"
        payment = payment or "cpa"
    elif s.goal == "sales":
        strat = "maximize_conversion_value"
        payment = payment or "cpa"
    elif s.goal in ("traffic", "awareness"):
        strat = "target_spend"
        payment = payment or "cpc"
    else:
        strat = "manual_cpc"
        payment = payment or "cpc"
    tcpa = None
    if strat == "maximize_conversions" and s.target_cpa_units:
        tcpa = _units_to_micros(s.target_cpa_units)
    return strat, tcpa, payment


def derive_bidding(s: CampaignSettings) -> tuple[str, int | None, str | None]:
    """Публичная обёртка над _derive_bidding (для пред-confirm правок в боте)."""
    return _derive_bidding(s)


def units_to_micros(units: float) -> int:
    """Единицы валюты → micros, с клампом «границы абсурда» (core.limits). Публично — для бота."""
    return _units_to_micros(units)


def _units_to_micros(units: float) -> int:
    """Единицы валюты → micros, с клампом «границы абсурда» (core.limits)."""
    u = max(0.0, min(float(units), float(MONEY_MAX_UNITS)))
    return int(round(u * 1_000_000))


def assemble_settings(
    extracted: CampaignSettings,
    *,
    median_budget_micros: int | None = None,
    avg_cpc_micros: int | None = None,
    common_match_type: str | None = None,
    topic: str | None = None,
    ui_language: str = "ru",
) -> dict:
    """Слить извлечённое + медианы «по аналогии» + дефолты в финальный settings-словарь визарда.

    Поля, взятые из медиан (а не из описания), перечисляются в by_analogy — чтобы Этап-1 показал
    менеджеру «(по аналогии)». Деньги/match_type — КОД. Возвращает wizard_state.settings.

    §19: помимо прежнего, вычисляет product (ЧТО рекламируем — драйвер seed/RSA), target_language
    (язык АУДИТОРИИ целевой страны — Кения→en, а не язык интерфейса ui_language) и страну — чтобы
    Discover/тексты были релевантны гео, а не выдавали чужой-язычную чепуху."""
    from ads import geo  # локальный импорт: extract-путь не тянет ads/google-ads

    by_analogy: list[str] = []

    # бюджет: описание → медиана(by_analogy) → дефолт
    if extracted.budget_daily_units is not None:
        budget_micros = _units_to_micros(extracted.budget_daily_units)
    elif median_budget_micros:
        budget_micros = int(median_budget_micros)
        by_analogy.append("budget_daily_micros")
    else:
        budget_micros = _units_to_micros(DEFAULT_DAILY_BUDGET_UNITS)

    # cpc: медиана(by_analogy) → дефолт (из описания CPC обычно не задаётся)
    if avg_cpc_micros:
        cpc_micros = int(avg_cpc_micros)
        by_analogy.append("cpc_bid_micros")
    else:
        cpc_micros = _units_to_micros(DEFAULT_CPC_UNITS)

    # тип соответствия: частый в аккаунте(by_analogy) → дефолт phrase
    if common_match_type:
        match_type = str(common_match_type).lower()
        by_analogy.append("match_type")
    else:
        match_type = DEFAULT_MATCH_TYPE

    strat, tcpa_micros, payment = _derive_bidding(extracted)
    if not extracted.bidding_strategy and not extracted.goal:
        by_analogy.append("bidding_strategy")

    # Страна таргетинга: из ISO-кода модели, иначе из первого распознанного названия локации.
    country_iso = geo.country_iso(extracted.geo_country_code)
    if not country_iso:
        for loc in extracted.geo_locations:
            country_iso = geo.country_iso(loc)
            if country_iso:
                break

    # Язык АУДИТОРИИ (для seed-подбора и RSA-текстов): язык из описания → по стране → интерфейс.
    target_language = None
    for lang in extracted.languages:
        target_language = geo.lang_iso(lang)
        if target_language:
            break
    target_language = target_language or geo.language_for_country(country_iso) or ui_language

    # ЧТО рекламируем — драйвер seed/RSA. Нет product → всё описание (не обрезает товар, в отличие
    # от прежнего text[:60]); имя кампании при этом остаётся чистым: geo · product · Search.
    product = (extracted.product or "").strip()
    theme = product or (topic or "").strip()

    name = (extracted.campaign_name or "").strip()
    if not name:
        geo_name = (extracted.geo_locations[0] if extracted.geo_locations else "").strip()
        parts = [p for p in (geo_name, product) if p] + ["Search"]
        name = " · ".join(parts)[:120]

    # Языки таргетинга кампании (критерии): из описания, иначе имя языка страны (English), иначе [].
    languages = list(extracted.languages)
    if not languages:
        nm = geo.language_name(target_language)
        languages = [nm] if nm else []

    return {
        "campaign_name": name,
        "product": theme or None,
        "target_language": target_language,
        "geo_locations": list(extracted.geo_locations),
        "geo_country_code": extracted.geo_country_code or country_iso,
        # geo_locale — язык, на котором заданы НАЗВАНИЯ локаций (для их резолва в geoTargetConstant);
        # менеджер пишет «Кения»/«Найроби» на языке интерфейса, поэтому ui_language, а не target.
        "geo_locale": ui_language or "ru",
        "languages": languages,
        "budget_daily_micros": budget_micros,
        "cpc_bid_micros": cpc_micros,
        "bidding_strategy": strat,
        "target_cpa_micros": tcpa_micros,
        "payment_model": payment,
        "match_type": match_type,
        "currency": extracted.currency,
        "by_analogy": by_analogy,
    }
