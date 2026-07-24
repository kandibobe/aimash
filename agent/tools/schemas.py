"""Типизированные схемы инструментов агента (Pydantic) + tool-описания для модели.

Модель ЗАПОЛНЯЕТ схему → код ВАЛИДИРУЕТ диапазоны → дальше confirm-гейт.
Read-инструменты выполняются сразу; mutation-инструменты только предлагают proposal.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator

from adcopy.validate import (
    ASSET_LIMITS,
    RSA_MAX_DESCRIPTIONS,
    RSA_MAX_HEADLINES,
    RSA_MIN_DESCRIPTIONS,
    RSA_MIN_HEADLINES,
    STRUCTURED_SNIPPET_HEADERS,
    assert_asset_len,
)
from adcopy.validate import validate as _rsa_validate
from ads.keyword_plan import MAX_SEEDS  # жёсткий лимит Google на число сид-ключей
from ads.validation import assert_keyword_ok, dedup_keyword_pairs, normalize_keywords
from core.guards import require_no_mutations
from core.limits import MONEY_MAX_MICROS, MONEY_MAX_UNITS


# D7: гео-дефолты страны/языка — из конфига деплоя (env DEFAULT_GEO_COUNTRY_CODE/LOCALE), НЕ
# захардкоженная Украина. default_factory (а не литерал) ⇒ env-переопределение доходит и до схем
# (NL-путь set_geo_*/создание). Ленивый импорт settings — schemas грузится очень рано.
def _default_geo_country() -> str:
    from core.config import settings

    return settings.geo_default_country


def _default_geo_locale() -> str:
    from core.config import settings

    return settings.geo_default_locale


# Валюта: ЛЮБОЙ 3-буквенный ISO-код (не Literal из 4 значений — иначе реальные аккаунты в
# AUD/CZK/PLN/… роняли update_budget/update_bid ValidationError'ом ДО валютной сверки, golden rule
# #4/§9). Плюс сентинел "percent" для процентных режимов. Конвертации валют НЕТ (ads.resolve): код
# нормализует к верхнему регистру и сверяет с валютой аккаунта на предпросмотре, FX не делает.
_CURRENCY_ALIASES = {
    "$": "USD",
    "US$": "USD",
    "USD": "USD",
    "€": "EUR",
    "EUR": "EUR",
    "ГРН": "UAH",
    "UAH": "UAH",
    "A$": "AUD",
    "AU$": "AUD",
    "AUD": "AUD",
    "ZŁ": "PLN",
    "PLN": "PLN",
    "KČ": "CZK",
    "CZK": "CZK",
}
_CURRENCY_ISO_RE = re.compile(r"^[A-Z]{3}$")


def _norm_currency(v: str) -> str:
    """Нормализует валюту: 'percent' сентинел; символ/слово-алиас → ISO; иначе любой 3-буквенный
    ISO-код в верхнем регистре. Пусто/мусор → ValidationError (модель обязана дать код или опустить)."""
    s = str(v).strip()
    if not s:
        raise ValueError("пустая валюта — укажи 3-буквенный ISO-код (USD/EUR/AUD/…) или опусти")
    if s.lower() == "percent":
        return "percent"
    su = s.upper()
    if su in _CURRENCY_ALIASES:
        return _CURRENCY_ALIASES[su]
    if _CURRENCY_ISO_RE.match(su):
        return su
    raise ValueError(
        f"неизвестная валюта '{v}' — укажи 3-буквенный ISO-код (USD/EUR/AUD/…) или опусти "
        "(тогда сумма трактуется в валюте аккаунта)"
    )


def _norm_money_currency(v: str) -> str:
    """Как _norm_currency, но 'percent' запрещён — для денежных ассетов (промо/прайс) нужен ISO-код."""
    c = _norm_currency(v)
    if c == "percent":
        raise ValueError("для денежной суммы нужен ISO-код валюты (USD/EUR/AUD/…), не 'percent'")
    return c


Currency = Annotated[str, AfterValidator(_norm_currency)]
MoneyCurrency = Annotated[str, AfterValidator(_norm_money_currency)]
MatchType = Literal["broad", "phrase", "exact"]

# §19: потолок числа ключевых слов на кампанию (одна ad group). Верифицированный менеджером список
# из Google Sheets бывает в сотни строк — прежний max_length=50 ронял «Создать черновик»
# ValidationError'ом (напр. 81 ключ). Google допускает тысячи ключей на группу; держим щедрый, но
# конечный потолок (защита от абсурда/одного гигантского mutate). Бот обрезает список до него.
MAX_CAMPAIGN_KEYWORDS = 2000

# 2.6: потолок батча add/remove(-negative)_keywords — ЕДИНЫЙ источник (раньше «50» дублировался
# литералом в bot/handlers/keywords_flow при обрезке списка; при смене лимита схемы срез и текст
# молча разошлись бы).
ADD_KEYWORDS_MAX = 50

# Абсолютный «очевидно неверно» потолок суммы (в единицах валюты аккаунта) для set_to/
# increase_by_amount. Защита от галлюцинации модели сверх gt=0. Единый источник — core.limits
# (mutations использует ту же границу в micros: MONEY_MAX_MICROS). Имя MAX_AMOUNT сохранено.
MAX_AMOUNT = MONEY_MAX_UNITS

# Какие инструменты — изменяющие (mutation), какие read-only.
MUTATION_TOOLS = {
    "update_budget",
    "update_bid",
    "update_keyword_bid",
    "add_keywords",
    "remove_keywords",
    "add_negative_keywords",
    "remove_negative_keywords",
    "add_negatives_to_shared_set",
    "attach_shared_set",
    "pause_campaign",
    "resume_campaign",
    "update_campaign",
    "set_campaign_network",
    "set_campaign_display_network",
    "set_campaign_geo_target_type",
    "remove_campaign",
    "remove_ad_group",
    "pause_ad_group",
    "resume_ad_group",
    "pause_ad",
    "resume_ad",
    "remove_ad",
    "set_geo_proximity",
    "set_geo_location",
    "set_bidding_strategy",
    "attach_audience",
    "detach_audience",
    "create_rsa",
    "create_gdn_campaign",
    "create_search_campaign",
    "create_demand_gen_campaign",
    "create_video_campaign",
    "add_sitelinks",
    "add_callouts",
    "add_structured_snippets",
    "add_call_asset",
    "add_promotion",
    "add_price_asset",
    "remove_asset_link",
}
READ_TOOLS = {"get_stats", "generate_rsa", "keyword_research", "clone_campaign", "analyze_account"}


# ── Pydantic-схемы (валидация в коде, не на доверии к модели) ───────────────────
ABSOLUTE_MONEY_MODES = ("set_to", "increase_by_amount", "decrease_by_amount")


def _assert_mode_currency(mode: str | None, currency: str | None) -> None:
    """Связка mode↔currency (golden rule #4): абсолютная сумма (set_to/increase_by_amount/
    decrease_by_amount) НЕ может нести currency='percent' — иначе процент умножился бы на 1e6 как
    абсолют (галлюцинация модели вида currency='percent' + mode='set_to'). Сверка валюты с валютой
    аккаунта (без FX) — на пути предпросмотра (ads.resolve.currency_mismatch), т.к. требует чтения
    аккаунта."""
    if mode in ABSOLUTE_MONEY_MODES and currency == "percent":
        raise ValueError(
            "абсолютная сумма не может иметь currency='percent' — укажи валюту аккаунта "
            "или опусти currency (тогда сумма трактуется в валюте аккаунта)"
        )


def _value_sane(v: float, mode: str | None) -> float:
    """Диапазон суммы считает КОД (golden rule #4): процент ≤1000%, абсолютная сумма ≤MAX_AMOUNT.
    Без верхней границы set_to/increase_by_amount пропустили бы галлюцинацию вида 999_999_999.

    value ВСЕГДА положительное (Field(gt=0)); направление несёт mode. Уменьшение на ≥100% —
    не «обнуление», а бессмыслица (ноль/минус): бюджет и ставка не могут быть нулевыми, обнулять
    надо паузой кампании, а задать новое значение — set_to."""
    if mode == "increase_by_percent" and v > 1000:
        raise ValueError("процент изменения подозрительно большой (>1000%)")
    if mode == "decrease_by_percent" and v >= 100:
        raise ValueError(
            "уменьшить на 100% и более нельзя (значение стало бы нулевым): задай новое значение "
            "(«поставь бюджет N») или поставь кампанию на паузу"
        )
    if mode in ABSOLUTE_MONEY_MODES and v > MAX_AMOUNT:
        raise ValueError(f"сумма подозрительно большая (>{MAX_AMOUNT}) — проверь команду")
    return v


class UpdateBudget(BaseModel):
    campaign: str
    # §5: направление несёт mode, а НЕ знак value (value всегда >0). Без decrease_* модель на «снизь
    # бюджет на 20%» выбирала increase_by_percent → карточка «100 → 120 (+20%)», т.е. ровно наоборот.
    mode: Literal[
        "increase_by_percent",
        "increase_by_amount",
        "decrease_by_percent",
        "decrease_by_amount",
        "set_to",
    ]
    value: float = Field(gt=0)
    # None = «в валюте аккаунта» (модель указывает валюту ТОЛЬКО если её явно назвал пользователь).
    # Сверка с реальной валютой аккаунта (без FX) — в ads.resolve.currency_mismatch на предпросмотре.
    currency: Currency | None = None

    @field_validator("value")
    @classmethod
    def _val(cls, v, info):
        return _value_sane(v, info.data.get("mode"))

    @model_validator(mode="after")
    def _mode_currency(self):
        _assert_mode_currency(self.mode, self.currency)
        return self


class UpdateBid(BaseModel):
    # campaign обязателен: ставка живёт на уровне ad group, нужно знать какой кампании.
    # Отсутствие кампании => ValidationError в loop ДО кнопок (а не raise после «да»).
    campaign: str
    mode: Literal["increase_by_percent", "decrease_by_percent", "set_to"]
    value: float = Field(gt=0)
    currency: Currency | None = None

    @field_validator("value")
    @classmethod
    def _val(cls, v, info):
        return _value_sane(v, info.data.get("mode"))

    @model_validator(mode="after")
    def _mode_currency(self):
        _assert_mode_currency(self.mode, self.currency)
        return self


class UpdateKeywordBid(BaseModel):
    """Ставка CPC на уровне КЛЮЧА (ad_group_criterion.cpc_bid_micros), а не всей группы.

    Точечный инструмент под /bids: поднимаем ровно тот ключ, что недобирает позицию, не двигая
    ставку соседних ключей группы (update_bid двигает ВСЕ группы кампании разом).
    ad_group/match_type — необязательные СУЖЕНИЯ: без них меняем ключ во всех группах кампании,
    где он есть (симметрично update_bid, который берёт все группы). Как и бюджет/групповая ставка,
    исполняется только прямой командой пользователя (golden rule #3)."""

    campaign: str
    keyword: str
    ad_group: str | None = None
    match_type: MatchType | None = None
    mode: Literal["increase_by_percent", "decrease_by_percent", "set_to"]
    value: float = Field(gt=0)
    currency: Currency | None = None

    @field_validator("keyword")
    @classmethod
    def _kw(cls, v):  # длину/форму ключа считает КОД (кириллица = 1 символ) — ДО кнопок
        return normalize_keywords([v])[0]

    @field_validator("value")
    @classmethod
    def _val(cls, v, info):
        return _value_sane(v, info.data.get("mode"))

    @model_validator(mode="after")
    def _mode_currency(self):
        _assert_mode_currency(self.mode, self.currency)
        return self


class AddKeywords(BaseModel):
    """Позитивные ключи в кампанию. ad_group (опц., Ф4 «сбор урожая», 2026-07-14) СУЖАЕТ адрес до
    одной группы: по умолчанию ключ ложится во ВСЕ группы кампании, а это своими руками делает ту
    самую каннибализацию (один ключ в нескольких группах конкурирует сам с собой), которую флажит
    check_keyword_cannibalization. Группы нет в кампании → отказ в execute_confirmed, не тихий
    веер по всем группам."""

    campaign: str  # обязателен: ключи добавляются в группы этой кампании
    ad_group: str | None = None  # опц.: только эта группа (иначе — все группы кампании)
    keywords: list[str] = Field(min_length=1, max_length=ADD_KEYWORDS_MAX)
    match_type: MatchType

    @field_validator("keywords")
    @classmethod
    def _kw(cls, v):  # длину/форму/дубли считает КОД ДО кнопок (а не после «да»)
        return normalize_keywords(v)


class RemoveKeywords(BaseModel):
    campaign: str  # обязателен: ключи удаляются из групп этой кампании (по тексту+типу)
    keywords: list[str] = Field(min_length=1, max_length=ADD_KEYWORDS_MAX)
    match_type: MatchType

    @field_validator("keywords")
    @classmethod
    def _kw(cls, v):
        return normalize_keywords(v)


class AddNegativeKeywords(BaseModel):
    """Минус-слова кампании. ad_group (опц., 3.2б 2026-07-17, зеркало Ф4 в AddKeywords) СУЖАЕТ
    уровень до одной группы: минус-слово ляжет негативным AdGroupCriterion этой группы, а не
    CampaignCriterion. Группы нет в кампании → отказ в execute_confirmed (fail-closed), не тихий
    откат на уровень кампании."""

    # campaign обязателен: минус-слова добавляются на уровне кампании (или её группы — ad_group).
    campaign: str
    ad_group: str | None = None  # опц.: минус-слово только в этой группе (не вся кампания)
    keywords: list[str] = Field(min_length=1, max_length=ADD_KEYWORDS_MAX)
    match_type: MatchType = "broad"

    @field_validator("keywords")
    @classmethod
    def _kw(cls, v):
        return normalize_keywords(v)


class RemoveNegativeKeywords(BaseModel):
    # Симметрично AddNegativeKeywords: снять минус-слова кампании по тексту+типу.
    campaign: str
    keywords: list[str] = Field(min_length=1, max_length=ADD_KEYWORDS_MAX)
    match_type: MatchType = "broad"

    @field_validator("keywords")
    @classmethod
    def _kw(cls, v):
        return normalize_keywords(v)


class AddNegativesToSharedSet(BaseModel):
    """3.2б (2026-07-17): минус-слова в ОБЩИЙ СПИСОК аккаунта (NEGATIVE_KEYWORDS shared set), а не
    в кампанию. Списка с таким именем нет → он будет СОЗДАН (создание — внутри apply ПОСЛЕ claim;
    дифф подтверждения говорит об этом явно). Сам по себе список ни на что не действует, пока не
    привязан к кампании — привязка ОТДЕЛЬНОЙ операцией attach_shared_set (раздельные последствия —
    раздельные подтверждения)."""

    shared_set: str = Field(min_length=1, max_length=255)  # имя списка; длину считает КОД
    keywords: list[str] = Field(min_length=1, max_length=ADD_KEYWORDS_MAX)
    match_type: MatchType = "broad"

    @field_validator("keywords")
    @classmethod
    def _kw(cls, v):
        return normalize_keywords(v)


class AttachSharedSet(BaseModel):
    """3.2б: привязать СУЩЕСТВУЮЩИЙ общий список минус-слов к кампании (CampaignSharedSet).
    Списка с таким именем нет → отказ в execute_confirmed (fail-closed), НЕ автосоздание: пустой
    список, привязанный молча, «работал бы в никуда» — создание/наполнение только явной
    add_negatives_to_shared_set."""

    campaign: str
    shared_set: str = Field(min_length=1, max_length=255)


class PauseCampaign(BaseModel):
    campaign: str


class ResumeCampaign(BaseModel):
    campaign: str


class LaunchCampaign(BaseModel):
    # §19.8/§11 «Запустить»: включить кампанию ПОЛНОСТЬЮ (кампания + группы + объявления). Только
    # UI-кнопка визарда (как attach_image_asset — не в MUTATION_TOOLS/TOOLS, агент её не вызывает
    # сам). Отдельная от resume_campaign, которая включает лишь кампанию (см. ads.mutations).
    campaign: str


class UpdateCampaign(BaseModel):
    # §3 «изменение» кампании: переименование. campaign — текущее имя, new_name — новое.
    # Диапазон длины дублирует валидатор мутации (defense-in-depth: не доверяем модели).
    campaign: str
    new_name: str = Field(min_length=1, max_length=255)


class SetCampaignNetwork(BaseModel):
    """Сети кампании (§19.3): вкл/выкл ПОИСКОВЫХ ПАРТНЁРОВ на существующей кампании.
    Дефолт проекта — партнёры ВЫКЛ (включение только явной командой менеджера). КМС и
    ограниченную target_partner_search_network этот инструмент НЕ трогает (код)."""

    campaign: str
    search_partners: bool  # True = включить поисковых партнёров, False = выключить


class SetCampaignDisplayNetwork(BaseModel):
    """G12: КМС (контекстно-медийная сеть) на ПОИСКОВОЙ кампании — баннеры съедают поисковый бюджет.
    Отдельная операция, а НЕ второй флаг у set_campaign_network: у тумблера партнёров своя кнопка,
    свой откат и свой текст карточки; расширение его схемы поменяло бы смысл уже подтверждённых
    черновиков. Меняется ТОЛЬКО target_content_network."""

    campaign: str
    display_network: bool  # True = включить КМС, False = выключить (дефолт для Search-кампаний)


class SetCampaignGeoTargetType(BaseModel):
    """G11: кого считать «в регионе». PRESENCE — только физически находящиеся в целевых регионах;
    PRESENCE_OR_INTEREST (Google-дефолт) — плюс «интересующиеся» регионом, т.е. клики людей вне
    его. Для локального бизнеса дефолт молча жжёт бюджет. SEARCH_INTEREST в allow-list НЕ пускаем:
    он ещё шире дефолта, а чинить мы пришли обратное."""

    campaign: str
    geo_target_type: Literal["PRESENCE", "PRESENCE_OR_INTEREST"]


class PauseAdGroup(BaseModel):
    # Пауза/возобновление НА УРОВНЕ ГРУППЫ (§16 AdGroupService): группу ищем по имени внутри кампании.
    campaign: str  # имя кампании, в которой искать группу
    ad_group: str  # имя группы объявлений


class ResumeAdGroup(BaseModel):
    campaign: str
    ad_group: str


class PauseAd(BaseModel):
    """C6 (§16 AdGroupAdService): пауза ОТДЕЛЬНОГО объявления. ad — числовой id объявления или
    фрагмент его первого заголовка (несколько совпадений → код попросит уточнить id)."""

    campaign: str
    ad_group: str
    ad: str = Field(min_length=1, max_length=200)


class ResumeAd(BaseModel):
    campaign: str
    ad_group: str
    ad: str = Field(min_length=1, max_length=200)


class RemoveAd(BaseModel):
    # Необратимое удаление объявления — в UI двойное подтверждение (_DESTRUCTIVE_OPS).
    campaign: str
    ad_group: str
    ad: str = Field(min_length=1, max_length=200)


class RemoveCampaign(BaseModel):
    # §3 удаление кампании (необратимо, status→REMOVED). Двойное подтверждение — в UI (bot).
    # Замок аккаунта — на исполнении (ensure_allowed). campaign — имя кампании.
    campaign: str


class RemoveAdGroup(BaseModel):
    # Удаление группы объявлений внутри кампании (необратимо). Группу ищем по имени в кампании.
    campaign: str
    ad_group: str


class SetGeoProximity(BaseModel):
    """Радиус-таргетинг кампании (proximity). Адрес — СТРУКТУРНЫЙ (city_name + country_code),
    Google сам геокодит точку — клиентский геокодинг не нужен. country_code — из запроса
    (опц. env-дом-дефолт), НЕ захардкожен; пусто = без страны-биаса."""

    campaign: str  # обязателен: радиус-таргетинг привязывается к кампании
    radius_km: float = Field(gt=0, le=2000)  # лимит Google Ads
    city_name: str = Field(min_length=1, max_length=80)
    country_code: str = Field(default_factory=_default_geo_country)  # D7: env-конфиг, не «UA»
    street_address: str | None = None
    postal_code: str | None = None

    @field_validator("country_code")
    @classmethod
    def _cc(cls, v):
        # Пусто РАЗРЕШЕНО (нет хардкода «UA»): пустой country_code = без страны-биаса при резолве
        # названий локаций. Непустой обязан быть валидным ISO alpha-2.
        v = str(v or "").strip().upper()
        if v and (len(v) != 2 or not v.isalpha()):
            raise ValueError("country_code — ISO-3166 alpha-2 (напр. UG) или пусто")
        return v


class SetGeoLocation(BaseModel):
    """Гео-таргетинг кампании по стране/городу/региону через geoTargetConstants (§3). Модель даёт
    НАЗВАНИЯ локаций РОВНО ИЗ ЗАПРОСА пользователя (напр. ['Германия', 'Берлин']) — своих стран не
    добавляет; КОД резолвит их в geoTargetConstant и заменяет весь географический таргетинг кампании
    (remove-before-create). country_code сужает поиск названий (из запроса, НЕ захардкожен; пусто =
    без биаса), locale — язык названий."""

    campaign: str  # обязателен: гео-таргетинг привязывается к кампании
    locations: list[str] = Field(min_length=1, max_length=20)
    country_code: str = Field(default_factory=_default_geo_country)  # D7: env-конфиг, не «UA»
    locale: str = Field(default_factory=_default_geo_locale)  # язык названий (env/из страны)

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
        # Пусто РАЗРЕШЕНО (нет хардкода «UA»): пустой country_code = без страны-биаса при резолве
        # названий локаций. Непустой обязан быть валидным ISO alpha-2.
        v = str(v or "").strip().upper()
        if v and (len(v) != 2 or not v.isalpha()):
            raise ValueError("country_code — ISO-3166 alpha-2 (напр. UG) или пусто")
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


class DetachAudience(BaseModel):
    """Открепить ранее прикреплённые аудитории от кампании (§3). Обратная к AttachAudience;
    минтуется ботом (resource_name из ads.read.list_audiences). Не деньги → user_initiated не нужен."""

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
    """C5: период — либо последние period_days, либо ЯВНЫЙ диапазон date_from/date_to (ISO,
    когда пользователь сам назвал даты), либо period_text — фраза периода КАК В ТЕКСТЕ
    («вчера», «за прошлую неделю», «июнь», «с 1 по 15 июня»): модель не знает «сегодня» и не
    считает даты — их резолвит КОД (reports.period.parse_period_text)."""

    account: str | None = None
    period_days: int = Field(default=30, gt=0, le=400)
    date_from: str | None = None  # ISO YYYY-MM-DD (только если пользователь назвал дату явно)
    date_to: str | None = None
    period_text: str | None = Field(default=None, max_length=100)

    @field_validator("date_from", "date_to")
    @classmethod
    def _iso(cls, v):
        if v is None or str(v).strip() == "":
            return None
        from datetime import date as _d

        try:
            return _d.fromisoformat(str(v).strip()).isoformat()
        except ValueError as e:
            raise ValueError("дата должна быть в ISO-формате ГГГГ-ММ-ДД") from e


class AnalyzeAccount(BaseModel):
    """Read-намерение: проанализировать аккаунт и дать РЕКОМЕНДАЦИИ (advisory). Модель лишь роутит
    намерение — контент рекомендаций генерит advisor/ (КОД + advisory-LLM), а любое действие по
    совету идёт ОТДЕЛЬНОЙ командой через confirm-гейт. Ничего не меняет в аккаунте (в READ_TOOLS)."""

    account: str | None = None
    period_days: int = Field(default=30, gt=0, le=400)
    topic: Literal["optimize", "keywords", "rsa", "structure", "all"] = "all"


class KeywordResearch(BaseModel):
    """Read-tool: подбор ключевых слов (advisory). Модель заполняет сиды и/или URL; объём/
    конкуренцию и кластеризацию считает КОД (ничего в аккаунте не меняется).

    §7-параметры (P1-F): geo (страна ISO-2 или название — КОД резолвит в geoTargetConstant),
    network (Search / Search+Partners), months (историческое окно метрик 1..48) — раньше были
    доступны только в визарде /keywords, теперь и через NL («ключи для X по Кении, Search+партнёры»)."""

    # Потолок = жёсткий лимит Google (KeywordSeed/KeywordAndUrlSeed: «no more than 20 keywords»).
    # Раньше схема допускала 25 → модель могла собрать батч, который API отвергает ЦЕЛИКОМ.
    seeds: list[str] = Field(default_factory=list, max_length=MAX_SEEDS)
    url: str | None = None
    # §7: ЛЮБОЙ язык (ISO 639-1 или название: 'de'/'немецкий'/'japanese'). КОД резолвит downstream
    # (ads.geo.keyword_ideas_lang → keyword_plan.LANGUAGE_IDS); неизвестный/пусто → язык не задаётся.
    # Пусто → выведем из гео (_kw_run). Раньше было Literal ru/uk/en (зажим).
    language: str | None = None
    # страна (ISO-2 или название) ИЗ ЗАПРОСА; None → дефолт деплоя, а при пустом env — глобальный
    # подбор без страны-биаса (bot.main._default_kw_geo). Свою страну не подставляй.
    geo: str | None = None
    network: Literal["search", "search_partners"] = "search"
    months: int | None = Field(default=None, ge=1, le=48)

    @field_validator("url")
    @classmethod
    def _url(cls, v):
        if v and not str(v).startswith(("http://", "https://")):
            raise ValueError("url должен быть http/https")
        return v

    @field_validator("language")
    @classmethod
    def _lang(cls, v):
        return (str(v).strip() or None) if v else None

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


class CloneCampaign(BaseModel):
    """Read-намерение (§2A): «сделай кампанию N с настройками как в кампании X». Модель лишь
    заполняет new_name + source_campaign (+ опц. оверрайды); живое чтение исходной кампании и
    сборку черновика create_search_campaign делает бот (confirm-гейт обязателен). НЕ мутация —
    в MUTATION_TOOLS/SUPPORTED_OPERATIONS не входит."""

    new_name: str = Field(min_length=1, max_length=120)
    source_campaign: str = Field(min_length=1)
    budget_daily_units: float | None = Field(default=None, gt=0, le=MONEY_MAX_UNITS)
    geo_override: list[str] | None = Field(default=None, max_length=20)
    final_url: str | None = None

    @field_validator("final_url")
    @classmethod
    def _url(cls, v):
        if v is not None and not str(v).startswith(("http://", "https://")):
            raise ValueError("final_url должен быть http/https")
        return v


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
    business_name: str = Field(min_length=1, max_length=ASSET_LIMITS["business_name"])
    final_url: str
    budget_daily_micros: int = Field(
        gt=0, le=MONEY_MAX_MICROS
    )  # потолок из core.limits (=1M единиц)
    media_id: str = Field(min_length=1, max_length=64)
    # §11: ГЕО-таргетинг как часть авто-формируемой структуры (аудит: раньше GDN создавался БЕЗ гео →
    # показ по всем локациям). Опционально (пусто ⇒ поведение как раньше). Резолв названий →
    # geoTargetConstant делает КОД (reuse _set_geo_location_via_sdk, живо сверенный на Search/§19).
    geo_locations: list[str] = Field(default_factory=list, max_length=50)
    geo_country_code: str = Field(default_factory=_default_geo_country)  # D7: env-конфиг, не «UA»
    geo_locale: str = Field(default_factory=_default_geo_locale)

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

    @field_validator("business_name")
    @classmethod
    def _bn(cls, v):
        return assert_asset_len(v, "business_name")  # ШИРИНА (CJK=2) — как считает Google

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


class _MediaVideoCampaignBase(BaseModel):
    """Общая форма §11-кампаний из видео (Demand Gen / Video). Минтуется ботом после визарда, не из
    LLM. Длины/составы считает КОД — зеркалит ads.mutations._validate_video_campaign_inputs
    (defense-in-depth). Бинарь логотипа НЕ здесь — logo_media_id ссылается на временное хранилище."""

    campaign_name: str = Field(min_length=1, max_length=120)
    youtube_video_id: str = Field(min_length=1, max_length=200)  # ссылка YouTube или 11-симв. id
    headlines: list[str] = Field(min_length=1, max_length=5)  # каждый ≤30
    long_headline: str = Field(min_length=1)  # ≤90
    descriptions: list[str] = Field(min_length=1, max_length=5)  # ≤90 (Video: КОД сузит до ≤70)
    business_name: str = Field(min_length=1, max_length=ASSET_LIMITS["business_name"])
    final_url: str
    budget_daily_micros: int = Field(gt=0, le=MONEY_MAX_MICROS)  # потолок из core.limits
    geo_locations: list[str] = Field(default_factory=list, max_length=50)
    geo_country_code: str = Field(default_factory=_default_geo_country)  # D7: env-конфиг, не «UA»
    geo_locale: str = Field(default_factory=_default_geo_locale)

    @field_validator("youtube_video_id")
    @classmethod
    def _yt(cls, v):
        from ads.assets import parse_youtube_video_id

        if not parse_youtube_video_id(v):
            raise ValueError("нужна ссылка на YouTube-видео или его 11-символьный id")
        return v

    @field_validator("business_name")
    @classmethod
    def _bn(cls, v):
        return assert_asset_len(v, "business_name")  # ШИРИНА (CJK=2) — как считает Google

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


class CreateDemandGenCampaign(_MediaVideoCampaignBase):
    """§11: Demand Gen кампания из YouTube-видео. goal: clicks (Maximize Clicks — работает без
    conversion tracking) | conversions (Maximize Conversions). Логотип опционален (media_id)."""

    goal: Literal["clicks", "conversions"] = "clicks"
    logo_media_id: str | None = Field(default=None, max_length=64)

    @field_validator("logo_media_id")
    @classmethod
    def _lmid(cls, v):
        if v is not None and not str(v).isalnum():  # идёт в имя файла — защита от traversal
            raise ValueError("logo_media_id должен быть буквенно-цифровым")
        return v


class CreateVideoCampaign(_MediaVideoCampaignBase):
    """§11: Video-кампания (YouTube, video responsive ad, target CPM). Описания дополнительно
    сужает КОД (≤70, ads.mutations.VIDEO_DESCRIPTION_MAX)."""


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
    budget_daily_micros: int = Field(
        gt=0, le=MONEY_MAX_MICROS
    )  # потолок из core.limits (=1M единиц)
    keywords: list[str] = Field(default_factory=list, max_length=MAX_CAMPAIGN_KEYWORDS)
    match_type: MatchType = "phrase"
    # §19.4.1: per-keyword типы соответствия (смешанный список [exact] + "phrase" + broad).
    # Пусто ⇒ все ключи получают match_type (поведение прежнее). Длина обязана совпадать с keywords.
    keyword_match_types: list[MatchType] = Field(
        default_factory=list, max_length=MAX_CAMPAIGN_KEYWORDS
    )
    # None = «ставку не задавали» → код подставит дефолт ПО ВАЛЮТЕ аккаунта у границы SDK
    # (литерал 500 000 micros = 0.5 единицы: для UGX/JPY это ниже минимальной биллинг-единицы).
    cpc_bid_micros: int | None = Field(default=None, gt=0, le=MONEY_MAX_MICROS)
    # §19 (composite, опциональные — без них поведение прежнее). Глубокую валидацию дублирует
    # ads.mutations._validate_search_inputs (defense-in-depth); здесь — форма + длины path.
    geo_locations: list[str] = Field(default_factory=list, max_length=50)
    geo_country_code: str = Field(default_factory=_default_geo_country)  # D7: env-конфиг, не «UA»
    geo_locale: str = Field(default_factory=_default_geo_locale)
    languages: list[str] = Field(default_factory=list, max_length=10)
    bidding: dict | None = None
    path1: str | None = None
    path2: str | None = None
    url_options: dict | None = None
    asset_specs: list[dict] = Field(default_factory=list, max_length=30)
    existing_asset_links: list[dict] = Field(default_factory=list, max_length=50)
    image_media_ids: list[str] = Field(default_factory=list, max_length=10)
    # §19.3 (таблица Этапа 1): сети / расписание / даты. Все опциональны — дефолты прежние
    # (Search-only, 24/7, старт сегодня). ad_schedule_blocks — структура из parse_ad_schedule (КОД).
    networks: Literal["search", "search_partners"] | None = None
    ad_schedule_blocks: list[dict] = Field(default_factory=list, max_length=7)
    start_date: str | None = None  # ISO ГГГГ-ММ-ДД
    end_date: str | None = None

    @field_validator("start_date", "end_date")
    @classmethod
    def _dates(cls, v):
        if v is None:
            return None
        from datetime import date as _date

        try:
            return _date.fromisoformat(str(v).strip()).isoformat()
        except ValueError as e:
            raise ValueError("дата в формате ISO ГГГГ-ММ-ДД") from e

    @field_validator("ad_schedule_blocks")
    @classmethod
    def _sched(cls, v):
        for b in v:
            day = str(b.get("day", ""))
            h1, h2 = int(b.get("start_hour", -1)), int(b.get("end_hour", -1))
            if day not in (
                "MONDAY",
                "TUESDAY",
                "WEDNESDAY",
                "THURSDAY",
                "FRIDAY",
                "SATURDAY",
                "SUNDAY",
            ):
                raise ValueError(f"неизвестный день расписания: {day!r}")
            if not (0 <= h1 < h2 <= 24):
                raise ValueError(f"часы расписания вне диапазона 0–24: {h1}-{h2}")
        return v

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
        # Только форма каждого ключа. Дедуп — в _kw_mts_match: при смешанном списке он идёт ПАРАМИ
        # (текст, тип), а дедуп по тексту здесь порвал бы склейку 1:1 с keyword_match_types.
        return [assert_keyword_ok(k) for k in v] if v else []

    @field_validator("path1", "path2")
    @classmethod
    def _path(cls, v):
        if v:
            _assert_rsa_len([v], "path")  # ≤15, кириллица=1 (§19.5.1 — doc error игнорируем)
        return v

    @model_validator(mode="after")
    def _kw_mts_match(self) -> "CreateSearchCampaign":
        """§19.4.1: per-keyword типы соответствия либо пусты (все = match_type), либо строго 1:1 к
        keywords. Считает КОД — рассинхрон длин не должен доехать до SDK (склейка по индексу).

        C2: дедуп смешанного списка — ПАРАМИ (`dedup_keyword_pairs`). Раньше keywords дедуплицировались
        по тексту в field-валидаторе, а keyword_match_types — нет: «ремонт»[exact] + «ремонт» broad
        (законные РАЗНЫЕ критерии Google) давали 1 ключ против 2 типов → ValueError → кнопка
        «Создать черновик» не срабатывала НИКОГДА при смешанных типах."""
        if not self.keywords:
            return self
        if self.keyword_match_types:
            self.keywords, self.keyword_match_types = dedup_keyword_pairs(
                self.keywords, self.keyword_match_types
            )
        else:
            self.keywords = normalize_keywords(self.keywords)
        return self


# ── §3-assets: текстовые расширения (sitelinks/callouts/structured snippets) ─────────
class Sitelink(BaseModel):
    """Один sitelink: текст-ссылка (≤25) + final_url + опц. два описания (≤35). description2
    нельзя без description1. Длину считает КОД (кириллица=1)."""

    link_text: str = Field(min_length=1)
    final_url: str
    description1: str | None = None
    description2: str | None = None

    @field_validator("link_text")
    @classmethod
    def _lt(cls, v):
        return assert_asset_len(v, "sitelink_text")

    @field_validator("final_url")
    @classmethod
    def _u(cls, v):
        if not v or not str(v).startswith(("http://", "https://")):
            raise ValueError("нужен валидный final_url (http/https)")
        return v

    @field_validator("description1", "description2")
    @classmethod
    def _d(cls, v):
        if v:
            assert_asset_len(v, "sitelink_desc")
        return v

    @model_validator(mode="after")
    def _order(self):
        if self.description2 and not self.description1:
            raise ValueError("description2 нельзя задавать без description1")
        return self


class AddSitelinks(BaseModel):
    campaign: str
    sitelinks: list[Sitelink] = Field(min_length=1, max_length=20)


class AddCallouts(BaseModel):
    campaign: str
    callouts: list[str] = Field(min_length=1, max_length=20)  # каждый ≤25, trim+dedup

    @field_validator("callouts")
    @classmethod
    def _c(cls, v):
        out: list[str] = []
        seen: set[str] = set()
        for t in v:
            t = (t or "").strip()
            if not t:
                continue
            assert_asset_len(t, "callout")
            key = t.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(t)
        if not out:
            raise ValueError("нужен хотя бы один непустой callout")
        return out


class AddStructuredSnippets(BaseModel):
    campaign: str
    header: str  # из канонического англ. списка (иначе HEADER_NOT_FOUND)
    values: list[str] = Field(min_length=3, max_length=10)  # каждое ≤25

    @field_validator("header")
    @classmethod
    def _h(cls, v):
        if v not in STRUCTURED_SNIPPET_HEADERS:
            raise ValueError(
                "header должен быть из канонического списка Google: "
                + ", ".join(sorted(STRUCTURED_SNIPPET_HEADERS))
            )
        return v

    @field_validator("values")
    @classmethod
    def _v(cls, v):
        out = [(t or "").strip() for t in v if (t or "").strip()]
        for t in out:
            assert_asset_len(t, "snippet_value")
        if len(out) < 3:
            raise ValueError("нужно минимум 3 непустых значения структурного описания")
        return out


class AttachImageAsset(BaseModel):
    """Прикрепить изображение-ассет к кампании (§3-assets, семейство 2). Минтуется ботом после
    приёма фото (как GDN): бинарь НЕ здесь — media_id ссылается на временно сохранённое
    подготовленное изображение. Не LLM-tool (нужно фото) — только bot-визард через SCHEMAS."""

    campaign: str
    media_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)

    @field_validator("media_id")
    @classmethod
    def _mid(cls, v):
        if not str(v).isalnum():  # идёт в имя файла — защита от path-traversal
            raise ValueError("media_id должен быть буквенно-цифровым")
        return v


class RemoveAssetLink(BaseModel):
    """Удаление СВЯЗИ ассета с кампанией (campaign_asset), НЕ самого ассета. resource_names — из
    list_campaign_assets (должны содержать /campaignAssets/)."""

    link_resource_names: list[str] = Field(min_length=1, max_length=50)

    @field_validator("link_resource_names")
    @classmethod
    def _rn(cls, v):
        for rn in v:
            if "/campaignAssets/" not in str(rn):
                raise ValueError(f"ожидался campaign_asset resource_name: {rn}")
        return v


# ── §3-assets семейство 3: Call / Promotion / Price (LLM-заполняемые, за confirm-гейтом) ──
class AddCallAsset(BaseModel):
    """Телефон-расширение (CallAsset). country_code — ISO alpha-2 страны НОМЕРА; phone_number —
    сырой номер. Конверс-трекинг звонков в MVP не подключаем (по умолчанию аккаунтное)."""

    campaign: str
    phone_number: str = Field(min_length=3, max_length=30)
    # D7: дефолт — из конфига деплоя, НЕ «UA» литералом. Литерал материализовался в model_dump()
    # и делал мёртвым env-фолбэк в ads/service.py: украинский код улетал в CallAsset любой страны.
    # validate_default: дефолт тоже проходит _cc — пустой env ⇒ честный отказ, а не пустая страна.
    country_code: str = Field(default_factory=_default_geo_country, validate_default=True)

    @field_validator("country_code")
    @classmethod
    def _cc(cls, v):
        # Google требует страну у CallAsset — пусто здесь (в отличие от гео-таргетинга) НЕЛЬЗЯ.
        # Fail-closed: не подставляем «какую-нибудь», а честно просим назвать страну номера.
        v = str(v or "").strip().upper()
        if not v:
            raise ValueError(
                "укажи страну номера (ISO alpha-2, напр. UG) — телефон-расширение без страны "
                "Google не принимает; дефолт можно задать в env DEFAULT_GEO_COUNTRY_CODE"
            )
        if len(v) != 2 or not v.isalpha():
            raise ValueError("country_code — двухбуквенный ISO-код (напр. UG, US)")
        return v

    @field_validator("phone_number")
    @classmethod
    def _pn(cls, v):
        v = (v or "").strip()
        if not re.fullmatch(r"[+()\-\s\d]{3,30}", v):
            raise ValueError("телефон: цифры, +, пробел, (), - (3–30 символов)")
        return v


class AddPromotion(BaseModel):
    """Промо-расширение (PromotionAsset). РОВНО одна скидка: percent_off (1–100%) ИЛИ
    money_off_units (+currency). final_url — посадочная (ставится на сам Asset). Длину
    promotion_target считает КОД."""

    campaign: str
    promotion_target: str = Field(min_length=1)
    final_url: str
    percent_off: float | None = Field(default=None, gt=0, le=100)
    money_off_units: float | None = Field(default=None, gt=0, le=MONEY_MAX_UNITS)
    currency: MoneyCurrency | None = None
    promo_code: str | None = Field(default=None, max_length=40)

    @field_validator("promotion_target")
    @classmethod
    def _pt(cls, v):
        return assert_asset_len(v, "promotion_target")

    @field_validator("final_url")
    @classmethod
    def _u(cls, v):
        if not v or not str(v).startswith(("http://", "https://")):
            raise ValueError("нужен валидный final_url (http/https)")
        return v

    @model_validator(mode="after")
    def _discount(self):
        has_pct = self.percent_off is not None
        has_money = self.money_off_units is not None
        if has_pct == has_money:  # оба или ни одного
            raise ValueError("укажи РОВНО одно: percent_off (1–100) ИЛИ money_off_units+currency")
        if has_money and not self.currency:
            raise ValueError("для money_off_units укажи currency (USD/UAH/EUR)")
        return self


class PriceOfferingItem(BaseModel):
    header: str = Field(min_length=1)  # ≤25 (asset_len)
    description: str = Field(min_length=1)  # ≤25
    price_units: float = Field(gt=0, le=MONEY_MAX_UNITS)  # в валюте прайса
    final_url: str
    unit: (
        Literal["per_hour", "per_day", "per_week", "per_month", "per_year", "per_night"] | None
    ) = None

    @field_validator("header")
    @classmethod
    def _h(cls, v):
        return assert_asset_len(v, "price_header")

    @field_validator("description")
    @classmethod
    def _d(cls, v):
        return assert_asset_len(v, "price_desc")

    @field_validator("final_url")
    @classmethod
    def _u(cls, v):
        if not v or not str(v).startswith(("http://", "https://")):
            raise ValueError("нужен валидный final_url (http/https) у каждого price-офера")
        return v


class AddPriceAsset(BaseModel):
    """Прайс-расширение (PriceAsset): 3–8 оферов (header≤25, description≤25, цена, URL). Валюта
    общая на все оферы. type_/unit — из канонического enum (lower-case)."""

    campaign: str
    price_type: Literal[
        "brands",
        "events",
        "locations",
        "neighborhoods",
        "product_categories",
        "product_tiers",
        "services",
        "service_categories",
        "service_tiers",
    ] = "services"
    currency: MoneyCurrency
    # D7: язык оферов — из конфига деплоя (env DEFAULT_GEO_LOCALE / выводится из страны), НЕ «uk»
    # литералом: литерал материализовался в model_dump() и уезжал в PriceAsset любой страны.
    language_code: str = Field(default_factory=_default_geo_locale, validate_default=True)
    offerings: list[PriceOfferingItem] = Field(min_length=3, max_length=8)

    @field_validator("language_code")
    @classmethod
    def _lang(cls, v):
        v = str(v or "").strip().lower()
        if (
            not v
        ):  # geo_default_locale всегда даёт хотя бы «ru», но схема не должна на это полагаться
            raise ValueError(
                "укажи язык прайс-расширения (напр. en) — Google требует language_code"
            )
        return v


SCHEMAS: dict[str, type[BaseModel]] = {
    "update_budget": UpdateBudget,
    "update_bid": UpdateBid,
    "update_keyword_bid": UpdateKeywordBid,
    "add_keywords": AddKeywords,
    "remove_keywords": RemoveKeywords,
    "add_negative_keywords": AddNegativeKeywords,
    "remove_negative_keywords": RemoveNegativeKeywords,
    "add_negatives_to_shared_set": AddNegativesToSharedSet,
    "attach_shared_set": AttachSharedSet,
    "pause_campaign": PauseCampaign,
    "resume_campaign": ResumeCampaign,
    "launch_campaign": LaunchCampaign,
    "update_campaign": UpdateCampaign,
    "set_campaign_network": SetCampaignNetwork,
    "set_campaign_display_network": SetCampaignDisplayNetwork,
    "set_campaign_geo_target_type": SetCampaignGeoTargetType,
    "pause_ad_group": PauseAdGroup,
    "resume_ad_group": ResumeAdGroup,
    "pause_ad": PauseAd,
    "resume_ad": ResumeAd,
    "remove_ad": RemoveAd,
    "remove_campaign": RemoveCampaign,
    "remove_ad_group": RemoveAdGroup,
    "set_geo_proximity": SetGeoProximity,
    "set_geo_location": SetGeoLocation,
    "set_bidding_strategy": SetBiddingStrategy,
    "attach_audience": AttachAudience,
    "detach_audience": DetachAudience,
    "create_rsa": CreateRsa,
    "create_gdn_campaign": CreateGdnCampaign,
    "create_search_campaign": CreateSearchCampaign,
    "create_demand_gen_campaign": CreateDemandGenCampaign,
    "create_video_campaign": CreateVideoCampaign,
    "get_stats": GetStats,
    "analyze_account": AnalyzeAccount,
    "generate_rsa": GenerateRsa,
    "keyword_research": KeywordResearch,
    "clone_campaign": CloneCampaign,
    "add_sitelinks": AddSitelinks,
    "add_callouts": AddCallouts,
    "add_structured_snippets": AddStructuredSnippets,
    "attach_image_asset": AttachImageAsset,
    "add_call_asset": AddCallAsset,
    "add_promotion": AddPromotion,
    "add_price_asset": AddPriceAsset,
    "remove_asset_link": RemoveAssetLink,
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
    _tool(
        "update_budget",
        "Изменить дневной бюджет кампании. Направление задаёт mode, а не знак value: "
        "«подними на 20%» → increase_by_percent; «снизь на 20%» → decrease_by_percent; "
        "«урежь на 50 (сумма)» → decrease_by_amount; «поставь 100» → set_to. "
        "value ВСЕГДА положительное число.",
        UpdateBudget,
    ),
    _tool(
        "update_bid",
        "Изменить ставку CPC на уровне групп объявлений указанной кампании "
        "(требуется ручная стратегия Manual CPC). Всегда указывай campaign. Направление задаёт mode: "
        "«снизь ставку на 15%» → decrease_by_percent, «поставь ставку 0.5» → set_to. "
        "value ВСЕГДА положительное число.",
        UpdateBid,
    ),
    _tool(
        "update_keyword_bid",
        "Изменить ставку CPC у КОНКРЕТНОГО ключевого слова (не всей группы) — «подними ставку по "
        "ключу «ремонт окон» до 1.5», «снизь ставку по ключу X на 20%». Требуется ручная стратегия "
        "Manual CPC. Всегда указывай campaign и keyword; ad_group и match_type — только если "
        "пользователь их назвал (иначе ключ меняется во всех группах кампании, где он есть). "
        "Направление задаёт mode, value ВСЕГДА положительное. Для ставки ВСЕЙ группы — update_bid.",
        UpdateKeywordBid,
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
        "Добавить минус-слова на уровне указанной кампании. Всегда указывай campaign. "
        "ad_group — ТОЛЬКО если пользователь явно назвал группу («добавь минус-слова в группу X»): "
        "тогда минус-слова лягут на уровень этой группы, а не кампании.",
        AddNegativeKeywords,
    ),
    _tool(
        "remove_negative_keywords",
        "Удалить минус-слова (по тексту+типу) с уровня указанной кампании. Обратная к "
        "add_negative_keywords. Всегда указывай campaign.",
        RemoveNegativeKeywords,
    ),
    _tool(
        "add_negatives_to_shared_set",
        "Добавить минус-слова в ОБЩИЙ СПИСОК минус-слов аккаунта (shared negative list), а не в "
        "кампанию. Только если пользователь явно говорит про общий список («в общий список», "
        "«shared list»). Списка с таким именем нет — он будет создан. Список действует лишь на "
        "кампании, к которым привязан (привязка — attach_shared_set).",
        AddNegativesToSharedSet,
    ),
    _tool(
        "attach_shared_set",
        "Привязать СУЩЕСТВУЮЩИЙ общий список минус-слов к кампании: его минус-слова начнут "
        "действовать на неё. Для команд «примени список X к кампании Y», «подключи общий список». "
        "Несуществующий список — отказ (сначала add_negatives_to_shared_set).",
        AttachSharedSet,
    ),
    _tool("pause_campaign", "Поставить кампанию на паузу.", PauseCampaign),
    _tool("resume_campaign", "Возобновить (включить) кампанию из паузы.", ResumeCampaign),
    _tool(
        "update_campaign",
        "Переименовать кампанию: campaign — текущее имя, new_name — новое. Для команд вида "
        "«переименуй кампанию X в Y» / «rename campaign X to Y».",
        UpdateCampaign,
    ),
    _tool(
        "set_campaign_network",
        "Включить/выключить сеть ПОИСКОВЫХ ПАРТНЁРОВ Google у существующей кампании. Для команд "
        "«отключи поисковых партнёров на кампании X», «включи партнёрскую сеть». "
        "search_partners=false — выключить (рекомендованный дефолт), true — включить.",
        SetCampaignNetwork,
    ),
    _tool(
        "set_campaign_display_network",
        "Включить/выключить КОНТЕКСТНО-МЕДИЙНУЮ СЕТЬ (КМС, Display) у существующей кампании. Для "
        "команд «отключи КМС на кампании X», «выключи медийную сеть», «turn off display network». "
        "display_network=false — выключить (правильно для поисковых кампаний), true — включить.",
        SetCampaignDisplayNetwork,
    ),
    _tool(
        "set_campaign_geo_target_type",
        "Тип гео-таргетинга кампании: PRESENCE — показывать только тем, кто ФИЗИЧЕСКИ в целевых "
        "регионах; PRESENCE_OR_INTEREST — ещё и тем, кто просто интересуется регионом (клики "
        "из-за рубежа). Для команд «показывай только тем, кто в моём регионе», «убери интерес "
        "из гео», «switch geo targeting to presence».",
        SetCampaignGeoTargetType,
    ),
    _tool(
        "pause_ad_group",
        "Поставить ОТДЕЛЬНУЮ группу объявлений на паузу. Укажи campaign (кампанию) и ad_group "
        "(имя группы).",
        PauseAdGroup,
    ),
    _tool(
        "resume_ad_group",
        "Возобновить (включить) ОТДЕЛЬНУЮ группу объявлений из паузы. Укажи campaign и ad_group.",
        ResumeAdGroup,
    ),
    _tool(
        "pause_ad",
        "Поставить на паузу ОДНО объявление. Укажи campaign, ad_group и ad — числовой id "
        "объявления или фрагмент его заголовка.",
        PauseAd,
    ),
    _tool(
        "resume_ad",
        "Возобновить (включить) ОДНО объявление из паузы. Укажи campaign, ad_group и ad "
        "(id или фрагмент заголовка).",
        ResumeAd,
    ),
    _tool(
        "remove_ad",
        "УДАЛИТЬ одно объявление (необратимо, статус REMOVED). Только предлагает — исполнение "
        "после ДВОЙНОГО подтверждения. Укажи campaign, ad_group и ad (id или фрагмент заголовка).",
        RemoveAd,
    ),
    _tool(
        "remove_campaign",
        "УДАЛИТЬ кампанию целиком (необратимо, статус REMOVED). Для команд «удали кампанию X», "
        "«delete campaign X». Только предлагает — исполнение после ДВОЙНОГО подтверждения "
        "пользователя. Укажи campaign (имя).",
        RemoveCampaign,
    ),
    _tool(
        "remove_ad_group",
        "УДАЛИТЬ группу объявлений внутри кампании (необратимо). Укажи campaign и ad_group. "
        "Только предлагает — исполнение после двойного подтверждения.",
        RemoveAdGroup,
    ),
    _tool(
        "set_geo_proximity",
        "Радиус-таргетинг (км) вокруг города для кампании. Укажи campaign, city_name и radius_km. "
        "country_code (ISO alpha-2) — ТОЛЬКО если страну назвал пользователь; не подставляй свою "
        "(дефолт подставит код из конфига деплоя).",
        SetGeoProximity,
    ),
    _tool(
        "set_geo_location",
        "Гео-таргетинг кампании по СТРАНЕ/ГОРОДУ/региону (не радиус). Укажи campaign и список "
        "locations — НАЗВАНИЯ из запроса пользователя, свои страны не добавляй. country_code "
        "(ISO alpha-2) сужает поиск названий — указывай, только если страна названа явно.",
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
    _tool(
        "get_stats",
        "Прочитать статистику (read-only). Период: по умолчанию period_days (последние N дней); "
        "если пользователь назвал ЯВНЫЕ даты — date_from/date_to в ISO; если период словами "
        "(«вчера», «за прошлую неделю», «июнь», «с 1 по 15 июня») — передай фразу КАК ЕСТЬ в "
        "period_text, даты НЕ выдумывай (их посчитает код).",
        GetStats,
    ),
    _tool(
        "analyze_account",
        "Проанализировать аккаунт и дать РЕКОМЕНДАЦИИ по улучшению (read-only, advisory): где "
        "расход без конверсий, дорогие по CPA кампании, дисбаланс бюджета. Для команд вида «что "
        "улучшить?», «как оптимизировать аккаунт?», «what to improve?». НИЧЕГО не меняет — "
        "исполнение любого совета идёт отдельной командой с подтверждением. topic сужает тему "
        "(optimize/keywords/rsa/structure/all).",
        AnalyzeAccount,
    ),
    _tool(
        "keyword_research",
        "Подобрать ключевые слова по сид-словам и/или URL (read-only, advisory): объёмы, "
        "конкуренция, кластеризация по интенту. Ничего не меняет в аккаунте. Укажи seeds "
        "и/или url. geo — страна (ISO-2 или название, любая). language — ЛЮБОЙ язык (ISO 639-1 "
        "'de'/'ja'/… или название 'немецкий'); опусти — выведется из страны/интерфейса.",
        KeywordResearch,
    ),
    _tool(
        "clone_campaign",
        "Клонировать настройки существующей поисковой кампании в НОВУЮ («сделай кампанию N "
        "с настройками как в кампании X»). Укажи new_name (имя новой) и source_campaign (имя "
        "образца). Можно переопределить budget_daily_units / final_url / geo_override. "
        "Ничего не создаёт сразу — бот покажет черновик и спросит подтверждение.",
        CloneCampaign,
    ),
    _tool(
        "add_sitelinks",
        "Добавить быстрые ссылки (sitelinks) в кампанию: для каждой link_text (≤25), final_url и "
        "опц. два описания (≤35). Укажи campaign. Применяется после подтверждения.",
        AddSitelinks,
    ),
    _tool(
        "add_callouts",
        "Добавить уточнения (callouts) в кампанию — короткие фразы (≤25) списком. Укажи campaign. "
        "Применяется после подтверждения.",
        AddCallouts,
    ),
    _tool(
        "add_structured_snippets",
        "Добавить структурные описания в кампанию: header из канонического англ. списка "
        "(Brands/Types/Models/Styles/Amenities/…) + values (3–10, каждое ≤25). Укажи campaign.",
        AddStructuredSnippets,
    ),
    _tool(
        "add_call_asset",
        "Добавить телефон-расширение (звонок) в кампанию: phone_number и country_code — страна "
        "НОМЕРА (ISO alpha-2) из запроса пользователя или из его профиля; свою не выдумывай. "
        "Укажи campaign. Применяется после подтверждения.",
        AddCallAsset,
    ),
    _tool(
        "add_promotion",
        "Добавить промо-расширение в кампанию: promotion_target (что по акции), final_url и РОВНО "
        "одну скидку — percent_off (1–100) ИЛИ money_off_units+currency; опц. promo_code. Укажи campaign.",
        AddPromotion,
    ),
    _tool(
        "add_price_asset",
        "Добавить прайс-расширение в кампанию: price_type, currency, language_code и 3–8 оферов "
        "(header ≤25, description ≤25, price_units, final_url, опц. unit). Укажи campaign.",
        AddPriceAsset,
    ),
    _tool(
        "remove_asset_link",
        "Открепить ассет(ы)-расширения от кампании по resource_name связи (из списка ассетов). "
        "Удаляется СВЯЗЬ, не сам ассет. Применяется после подтверждения.",
        RemoveAssetLink,
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


# ── P3: ANALYSIS_TOOLS — отдельный READ-ONLY набор для агентного НАРРАТИВА /audit ────────
# НИКОГДА не мёржится в TOOLS (парс-путь денег). Аккаунт ЗАЛОЧЕН на проаудированный customer_id —
# у схем НЕТ поля account (GR9: цель читает bot-слой, модель её не выбирает). Инвариант S4: набор
# НЕ пересекается с MUTATION_TOOLS (ноль пути к деньгам из нарратив-цикла, GR6/GR8) — assert ниже.
class GetCampaignDetailArgs(BaseModel):
    """get_campaign_detail (read-only): имя кампании из аудита для чтения деталей."""

    campaign: str = Field(min_length=1, description="Точное имя кампании из данных аудита")


class GetSearchTermsArgs(BaseModel):
    """get_search_terms (read-only): без параметров — топ поисковых запросов аккаунта по расходу."""


class GetCompetitorsArgs(BaseModel):
    """get_competitors (read-only): без параметров — последний загруженный срез «Статистики
    аукционов» (домены соперников и доли показов из /competitors)."""


class GetBidLandscapeArgs(BaseModel):
    """get_bid_landscape (read-only): без параметров — топ ключей по расходу со ставкой, оценками
    Google (сколько нужно для верха страницы) и долями верхних позиций."""


ANALYSIS_TOOLS: list[dict] = [
    _tool(
        "get_campaign_detail",
        "Прочитать детали ОДНОЙ кампании (бюджет, группы объявлений, ключевые слова, тексты RSA) по "
        "её имени — чтобы объяснить проблему конкретикой. READ-ONLY: ничего не меняет.",
        GetCampaignDetailArgs,
    ),
    _tool(
        "get_search_terms",
        "Прочитать топ реальных поисковых запросов аккаунта по расходу (что люди вводили в Google) — "
        "чтобы показать, на что уходят деньги, и найти кандидатов в минус-слова. READ-ONLY, без аргументов.",
        GetSearchTermsArgs,
    ),
    _tool(
        "get_competitors",
        "Прочитать срез конкурентов («Статистика аукционов»): домены соперников и их доли показов, "
        "пересечения, доля обгона. Отвечает «как аккаунт выглядит против конкурентов». READ-ONLY, "
        "без аргументов. Пусто (has_data:false) = отчёт ещё не загружали командой /competitors.",
        GetCompetitorsArgs,
    ),
    _tool(
        "get_bid_landscape",
        "Прочитать ставки топ-ключей по расходу и оценки Google (ставка для верха страницы / первой "
        "позиции) + доли верхних позиций и потерю верха по рангу. Отвечает «какие ставки поднять и "
        "почему теряю верх». Осмысленно только на РУЧНЫХ стратегиях (strategy_type). READ-ONLY, без аргументов.",
        GetBidLandscapeArgs,
    ),
]
ANALYSIS_TOOL_NAMES = frozenset(t["function"]["name"] for t in ANALYSIS_TOOLS)

# S4 (construction-time): ни один аналитический инструмент не является мутацией. Провал = баг
# конфигурации → роняем импорт немедленно (лучше, чем тихо открыть денежный путь в read-only цикле).
# Не `assert`: под `-O` он вырезается из байткода, и гард исчезает молча — см. `core/guards.py`.
require_no_mutations(ANALYSIS_TOOL_NAMES, MUTATION_TOOLS, rule="S4/GR6", subject="ANALYSIS_TOOLS")
