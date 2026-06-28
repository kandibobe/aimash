"""Типизированные схемы инструментов агента (Pydantic) + tool-описания для модели.

Модель ЗАПОЛНЯЕТ схему → код ВАЛИДИРУЕТ диапазоны → дальше confirm-гейт.
Read-инструменты выполняются сразу; mutation-инструменты только предлагают proposal.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from adcopy.validate import (
    RSA_MAX_DESCRIPTIONS,
    RSA_MAX_HEADLINES,
    RSA_MIN_DESCRIPTIONS,
    RSA_MIN_HEADLINES,
)
from adcopy.validate import validate as _rsa_validate
from ads.validation import normalize_keywords

Currency = Literal["USD", "UAH", "EUR", "percent"]
MatchType = Literal["broad", "phrase", "exact"]

# Абсолютный «очевидно неверно» потолок суммы (в единицах валюты аккаунта) для set_to/
# increase_by_amount. Защита от галлюцинации модели сверх gt=0. Зеркалит mutations.MAX_AMOUNT_MICROS.
MAX_AMOUNT = 1_000_000

# Какие инструменты — изменяющие (mutation), какие read-only.
MUTATION_TOOLS = {
    "update_budget",
    "update_bid",
    "add_keywords",
    "remove_keywords",
    "add_negative_keywords",
    "pause_campaign",
    "resume_campaign",
    "set_geo_proximity",
    "set_geo_location",
    "set_bidding_strategy",
    "attach_audience",
    "create_rsa",
    "create_gdn_campaign",
    "create_search_campaign",
}
READ_TOOLS = {"get_stats", "generate_rsa", "keyword_research"}


# ── Pydantic-схемы (валидация в коде, не на доверии к модели) ───────────────────
def _value_sane(v: float, mode: str | None) -> float:
    """Диапазон суммы считает КОД (golden rule #4): процент ≤1000%, абсолютная сумма ≤MAX_AMOUNT.
    Без верхней границы set_to/increase_by_amount пропустили бы галлюцинацию вида 999_999_999."""
    if mode == "increase_by_percent" and v > 1000:
        raise ValueError("процент изменения подозрительно большой (>1000%)")
    if mode in ("set_to", "increase_by_amount") and v > MAX_AMOUNT:
        raise ValueError(f"сумма подозрительно большая (>{MAX_AMOUNT}) — проверь команду")
    return v


class UpdateBudget(BaseModel):
    campaign: str
    mode: Literal["increase_by_percent", "increase_by_amount", "set_to"]
    value: float = Field(gt=0)
    currency: Currency = "USD"

    @field_validator("value")
    @classmethod
    def _val(cls, v, info):
        return _value_sane(v, info.data.get("mode"))


class UpdateBid(BaseModel):
    # campaign обязателен: ставка живёт на уровне ad group, нужно знать какой кампании.
    # Отсутствие кампании => ValidationError в loop ДО кнопок (а не raise после «да»).
    campaign: str
    mode: Literal["increase_by_percent", "set_to"]
    value: float = Field(gt=0)
    currency: Currency = "USD"

    @field_validator("value")
    @classmethod
    def _val(cls, v, info):
        return _value_sane(v, info.data.get("mode"))


class AddKeywords(BaseModel):
    campaign: str  # обязателен: ключи добавляются в группы этой кампании
    keywords: list[str] = Field(min_length=1, max_length=50)
    match_type: MatchType

    @field_validator("keywords")
    @classmethod
    def _kw(cls, v):  # длину/форму/дубли считает КОД ДО кнопок (а не после «да»)
        return normalize_keywords(v)


class RemoveKeywords(BaseModel):
    campaign: str  # обязателен: ключи удаляются из групп этой кампании (по тексту+типу)
    keywords: list[str] = Field(min_length=1, max_length=50)
    match_type: MatchType

    @field_validator("keywords")
    @classmethod
    def _kw(cls, v):
        return normalize_keywords(v)


class AddNegativeKeywords(BaseModel):
    # campaign обязателен: минус-слова добавляются на уровне кампании.
    campaign: str
    keywords: list[str] = Field(min_length=1, max_length=50)
    match_type: MatchType = "broad"

    @field_validator("keywords")
    @classmethod
    def _kw(cls, v):
        return normalize_keywords(v)


class PauseCampaign(BaseModel):
    campaign: str


class ResumeCampaign(BaseModel):
    campaign: str


class SetGeoProximity(BaseModel):
    """Радиус-таргетинг кампании (proximity). Адрес — СТРУКТУРНЫЙ (city_name + country_code),
    Google сам геокодит точку — клиентский геокодинг не нужен. country_code по умолчанию UA
    (проект ориентирован на Украину)."""

    campaign: str  # обязателен: радиус-таргетинг привязывается к кампании
    radius_km: float = Field(gt=0, le=2000)  # лимит Google Ads
    city_name: str = Field(min_length=1, max_length=80)
    country_code: str = "UA"  # ISO-3166 alpha-2 (UA, PL, US…)
    street_address: str | None = None
    postal_code: str | None = None

    @field_validator("country_code")
    @classmethod
    def _cc(cls, v):
        v = str(v or "UA").strip().upper()
        if len(v) != 2 or not v.isalpha():
            raise ValueError("country_code — ISO-3166 alpha-2 (напр. UA)")
        return v


class SetGeoLocation(BaseModel):
    """Гео-таргетинг кампании по стране/городу/региону через geoTargetConstants (§3). Модель даёт
    НАЗВАНИЯ локаций (напр. ['Украина', 'Киев']); КОД резолвит их в geoTargetConstant и заменяет
    весь географический таргетинг кампании (remove-before-create). country_code сужает поиск
    (по умолчанию UA), locale — язык названий."""

    campaign: str  # обязателен: гео-таргетинг привязывается к кампании
    locations: list[str] = Field(min_length=1, max_length=20)
    country_code: str = "UA"  # ISO-3166 alpha-2 — сужает поиск названий
    locale: str = "ru"  # язык названий локаций (ru/uk/en)

    @field_validator("locations")
    @classmethod
    def _loc(cls, v):
        out = [s.strip() for s in v if s and s.strip()]
        if not out:
            raise ValueError("нужна хотя бы одна локация (страна/город/регион)")
        for s in out:
            if len(s) > 80:
                raise ValueError(f"название локации слишком длинное (>80): «{s}»")
        return out

    @field_validator("country_code")
    @classmethod
    def _cc(cls, v):
        v = str(v or "UA").strip().upper()
        if len(v) != 2 or not v.isalpha():
            raise ValueError("country_code — ISO-3166 alpha-2 (напр. UA)")
        return v


class SetBiddingStrategy(BaseModel):
    """Смена стратегии назначения ставок кампании (§3). Деньги (управляет расходом) → как бюджет/
    ставка, применяется ТОЛЬКО прямой командой пользователя (user_initiated). Поддержаны стандартные
    (не портфельные) стратегии. target_cpa — в валюте аккаунта; target_roas — доля (4.0 = 400%)."""

    campaign: str
    strategy: Literal[
        "manual_cpc", "maximize_conversions", "maximize_conversion_value", "target_spend"
    ]
    target_cpa: float | None = None  # для maximize_conversions (валюта аккаунта)
    target_roas: float | None = None  # для maximize_conversion_value (доля, напр. 4.0 = 400%)
    enhanced_cpc: bool = False  # для manual_cpc

    @field_validator("target_cpa")
    @classmethod
    def _tcpa(cls, v):
        if v is not None and (v <= 0 or v > MAX_AMOUNT):
            raise ValueError(f"target_cpa должен быть в (0, {MAX_AMOUNT}]")
        return v

    @field_validator("target_roas")
    @classmethod
    def _troas(cls, v):
        if v is not None and (v <= 0 or v > 1000):
            raise ValueError("target_roas — доля в (0, 1000] (напр. 4.0 = 400%)")
        return v


class AttachAudience(BaseModel):
    """Прикрепить существующие аудитории (user_list/audience) к кампании (§3). Минтуется ботом после
    выбора из списка (resource_name из ads.read.list_audiences), не из LLM напрямую. Не деньги →
    user_initiated не требуется (как гео/ключи)."""

    campaign: str
    audience_resource_names: list[str] = Field(min_length=1, max_length=20)

    @field_validator("audience_resource_names")
    @classmethod
    def _rn(cls, v):
        out = [s.strip() for s in v if s and s.strip()]
        if not out:
            raise ValueError("нужна хотя бы одна аудитория")
        for rn in out:
            if "/userLists/" not in rn and "/audiences/" not in rn:
                raise ValueError(f"некорректный resource_name аудитории: {rn}")
        return out


class GetStats(BaseModel):
    account: str | None = None
    period_days: int = Field(default=30, gt=0, le=400)


class KeywordResearch(BaseModel):
    """Read-tool: подбор ключевых слов (advisory). Модель заполняет сиды и/или URL; объём/
    конкуренцию и кластеризацию считает КОД (ничего в аккаунте не меняется)."""

    seeds: list[str] = Field(default_factory=list, max_length=10)
    url: str | None = None
    language: Literal["ru", "uk", "en"] = "ru"

    @field_validator("url")
    @classmethod
    def _url(cls, v):
        if v and not str(v).startswith(("http://", "https://")):
            raise ValueError("url должен быть http/https")
        return v

    @field_validator("seeds")
    @classmethod
    def _seeds(cls, v):
        return [s.strip() for s in v if s and s.strip()]


def _assert_rsa_len(items: list[str], kind: str) -> list[str]:
    """Длину каждого элемента (кириллица=1) считает КОД — отбраковка ДО кнопок, а не raise после «да»."""
    for t in items:
        ok, n = _rsa_validate(t, kind)
        if not ok:
            raise ValueError(f"{kind} превышает лимит ({n}): «{t}»")
    return items


class GenerateRsa(BaseModel):
    """Read-tool: попросить сгенерировать тексты RSA. Применение — отдельно, через курацию
    и confirm-гейт (create_rsa). Кампания/группа/URL уточняются визардом, если не заданы."""

    topic: str
    keywords: list[str] = Field(default_factory=list, max_length=50)
    usp: str | None = None
    tone: str | None = None
    geo: str | None = None
    language: str = "ru"
    campaign: str | None = None
    final_url: str | None = None
    n_headlines: int = Field(default=15, ge=3, le=15)
    n_descriptions: int = Field(default=4, ge=2, le=4)


class CreateRsa(BaseModel):
    """Финальные параметры создания RSA (минтуются ботом после курации, не из LLM напрямую).
    Минимумы/максимумы и длину считает КОД — зеркалит ads.mutations (defense-in-depth)."""

    ad_group_id: str
    campaign: str
    final_url: str
    headlines: list[str] = Field(min_length=RSA_MIN_HEADLINES, max_length=RSA_MAX_HEADLINES)
    descriptions: list[str] = Field(
        min_length=RSA_MIN_DESCRIPTIONS, max_length=RSA_MAX_DESCRIPTIONS
    )
    path1: str | None = None
    path2: str | None = None

    @field_validator("final_url")
    @classmethod
    def _url(cls, v):
        if not v or not str(v).startswith(("http://", "https://")):
            raise ValueError("нужен валидный final_url (http/https)")
        return v

    @field_validator("headlines")
    @classmethod
    def _h(cls, v):
        return _assert_rsa_len(v, "headline")

    @field_validator("descriptions")
    @classmethod
    def _d(cls, v):
        return _assert_rsa_len(v, "description")

    @field_validator("path1", "path2")
    @classmethod
    def _p(cls, v):
        if v:
            _assert_rsa_len([v], "path")
        return v


class CreateGdnCampaign(BaseModel):
    """Финальные параметры GDN-кампании из фото (минтуется ботом после визарда, не из LLM).
    Длину/составы считает КОД — зеркалит ads.mutations._validate_gdn_inputs (defense-in-depth).
    Бинарь фото НЕ здесь: media_id ссылается на временно сохранённые подготовленные изображения."""

    campaign_name: str = Field(min_length=1, max_length=120)
    headlines: list[str] = Field(min_length=1, max_length=5)  # каждый ≤30
    long_headline: str = Field(min_length=1)  # ≤90
    descriptions: list[str] = Field(min_length=1, max_length=5)  # каждый ≤90
    business_name: str = Field(min_length=1, max_length=25)
    final_url: str
    budget_daily_micros: int = Field(gt=0, le=1_000_000_000_000)  # ≤1e12 micros (=1M единиц валюты)
    media_id: str = Field(min_length=1, max_length=64)

    @field_validator("headlines")
    @classmethod
    def _h(cls, v):
        for t in v:
            _assert_rsa_len([t], "headline")
        return v

    @field_validator("long_headline")
    @classmethod
    def _lh(cls, v):
        _assert_rsa_len([v], "description")  # длинный заголовок ≤90
        return v

    @field_validator("descriptions")
    @classmethod
    def _d(cls, v):
        for t in v:
            _assert_rsa_len([t], "description")
        return v

    @field_validator("final_url")
    @classmethod
    def _url(cls, v):
        if not v or not str(v).startswith(("http://", "https://")):
            raise ValueError("нужен валидный final_url (http/https)")
        return v

    @field_validator("media_id")
    @classmethod
    def _mid(cls, v):
        if not str(v).isalnum():  # идёт в имя файла — защита от path-traversal
            raise ValueError("media_id должен быть буквенно-цифровым")
        return v


class CreateSearchCampaign(BaseModel):
    """Финальные параметры поисковой (Search) кампании (§3). Минтуется ботом после визарда
    (/newsearch: генерация RSA), не из LLM напрямую. Длину/составы считает КОД — зеркалит
    ads.mutations._validate_search_inputs (defense-in-depth). Ключевые слова — опциональны."""

    campaign_name: str = Field(min_length=1, max_length=120)
    final_url: str
    headlines: list[str] = Field(min_length=RSA_MIN_HEADLINES, max_length=RSA_MAX_HEADLINES)
    descriptions: list[str] = Field(
        min_length=RSA_MIN_DESCRIPTIONS, max_length=RSA_MAX_DESCRIPTIONS
    )
    budget_daily_micros: int = Field(gt=0, le=1_000_000_000_000)  # ≤1e12 micros (=1M единиц валюты)
    keywords: list[str] = Field(default_factory=list, max_length=50)
    match_type: MatchType = "phrase"
    cpc_bid_micros: int = Field(default=500_000, gt=0, le=1_000_000_000_000)

    @field_validator("final_url")
    @classmethod
    def _url(cls, v):
        if not v or not str(v).startswith(("http://", "https://")):
            raise ValueError("нужен валидный final_url (http/https)")
        return v

    @field_validator("headlines")
    @classmethod
    def _h(cls, v):
        return _assert_rsa_len(v, "headline")

    @field_validator("descriptions")
    @classmethod
    def _d(cls, v):
        return _assert_rsa_len(v, "description")

    @field_validator("keywords")
    @classmethod
    def _kw(cls, v):
        return normalize_keywords(v) if v else []


SCHEMAS: dict[str, type[BaseModel]] = {
    "update_budget": UpdateBudget,
    "update_bid": UpdateBid,
    "add_keywords": AddKeywords,
    "remove_keywords": RemoveKeywords,
    "add_negative_keywords": AddNegativeKeywords,
    "pause_campaign": PauseCampaign,
    "resume_campaign": ResumeCampaign,
    "set_geo_proximity": SetGeoProximity,
    "set_geo_location": SetGeoLocation,
    "set_bidding_strategy": SetBiddingStrategy,
    "attach_audience": AttachAudience,
    "create_rsa": CreateRsa,
    "create_gdn_campaign": CreateGdnCampaign,
    "create_search_campaign": CreateSearchCampaign,
    "get_stats": GetStats,
    "generate_rsa": GenerateRsa,
    "keyword_research": KeywordResearch,
}


# ── OpenAI/OpenRouter tool-описания (генерим из Pydantic) ───────────────────────
def _tool(name: str, description: str, model: type[BaseModel]) -> dict:
    schema = model.model_json_schema()
    schema.pop("title", None)
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": schema},
    }


TOOLS: list[dict] = [
    _tool("update_budget", "Изменить дневной бюджет кампании.", UpdateBudget),
    _tool(
        "update_bid",
        "Изменить ставку CPC на уровне групп объявлений указанной кампании "
        "(требуется ручная стратегия Manual CPC). Всегда указывай campaign.",
        UpdateBid,
    ),
    _tool(
        "add_keywords",
        "Добавить ключевые слова (с типом соответствия) в группы указанной кампании. "
        "Всегда указывай campaign.",
        AddKeywords,
    ),
    _tool(
        "remove_keywords",
        "Удалить ключевые слова (с типом соответствия) из групп указанной кампании. "
        "Удаляются по тексту+типу. Всегда указывай campaign.",
        RemoveKeywords,
    ),
    _tool(
        "add_negative_keywords",
        "Добавить минус-слова на уровне указанной кампании. Всегда указывай campaign.",
        AddNegativeKeywords,
    ),
    _tool("pause_campaign", "Поставить кампанию на паузу.", PauseCampaign),
    _tool("resume_campaign", "Возобновить (включить) кампанию из паузы.", ResumeCampaign),
    _tool(
        "set_geo_proximity",
        "Радиус-таргетинг (км) вокруг города для кампании. Укажи campaign, city_name, "
        "country_code (ISO alpha-2, по умолчанию UA) и radius_km.",
        SetGeoProximity,
    ),
    _tool(
        "set_geo_location",
        "Гео-таргетинг кампании по СТРАНЕ/ГОРОДУ/региону (не радиус). Укажи campaign и список "
        "locations (названия, напр. ['Украина','Киев']); country_code (ISO alpha-2, по умолчанию "
        "UA) сужает поиск. Заменяет прежний географический таргетинг кампании.",
        SetGeoLocation,
    ),
    _tool(
        "set_bidding_strategy",
        "Сменить стратегию назначения ставок кампании: manual_cpc (ручная, опц. enhanced_cpc), "
        "maximize_conversions (опц. target_cpa в валюте аккаунта), maximize_conversion_value "
        "(опц. target_roas — доля, 4.0=400%), target_spend (максимум кликов). Деньги: только "
        "по прямой команде. Укажи campaign и strategy.",
        SetBiddingStrategy,
    ),
    _tool(
        "generate_rsa",
        "Сгенерировать рекламные тексты RSA (заголовки/описания) для кампании. Только "
        "ПРЕДЛАГАЕТ тексты — применение к объявлению идёт отдельно, после поэлементного "
        "подтверждения. Укажи topic; campaign/final_url можно уточнить позже.",
        GenerateRsa,
    ),
    _tool("get_stats", "Прочитать статистику (read-only).", GetStats),
    _tool(
        "keyword_research",
        "Подобрать ключевые слова по сид-словам и/или URL (read-only, advisory): объёмы, "
        "конкуренция, кластеризация по интенту. Ничего не меняет в аккаунте. Укажи seeds "
        "и/или url; язык ru/uk/en.",
        KeywordResearch,
    ),
    {
        "type": "function",
        "function": {
            "name": "ask_clarification",
            "description": "Команда неоднозначна (не указана кампания/сумма/направление) — переспросить, НЕ угадывать.",
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        },
    },
]
