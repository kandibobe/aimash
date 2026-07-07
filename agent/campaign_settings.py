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
import re
from typing import Literal

from pydantic import BaseModel, Field

from agent.router import chat
from core.limits import (
    MONEY_MAX_UNITS,
    WIZARD_DEFAULT_MONEY_FALLBACK_UNITS,
    round_micros,
    wizard_default_money_units,
)

# Дефолты, если ни описание, ни медианы аккаунта не дали значения (нейтральные, безопасные на
# тест-аккаунте). Денежные дефолты — ВАЛЮТО-ЗАВИСИМЫЕ (core.limits.wizard_default_money_units:
# «0.50 JPY» — абсурд, у JPY нет копеек); константы ниже — фолбэк неизвестной валюты, имена
# сохранены для обратной совместимости импортов.
DEFAULT_DAILY_BUDGET_UNITS, DEFAULT_CPC_UNITS = WIZARD_DEFAULT_MONEY_FALLBACK_UNITS
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
    max_cpc_units: float | None = None  # макс. цена за клик — если ЯВНО названа («75 за клик»)
    currency: str | None = None  # USD/UAH/EUR — если ЯВНО назван
    goal: Goal | None = None
    bidding_strategy: Bidding | None = None
    target_cpa_units: float | None = None
    payment_model: Literal["cpc", "cpa"] | None = None
    # §19.3 (таблица Этапа 1): сети, расписание показов, даты запуска. Все опциональны — None ⇒
    # дефолты в assemble_settings (Search-only, 24/7, старт сегодня без даты конца).
    networks: Literal["search", "search_partners"] | None = None
    ad_schedule: str | None = None  # «пн-пт 9-18» / «24/7» — строку в структуру переводит КОД
    start_date: str | None = None  # ISO YYYY-MM-DD (валидирует КОД в assemble_settings)
    end_date: str | None = None

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

    def is_empty(self) -> bool:
        """Извлечение ничего не дало (все поля None/пустые списки) — merge был бы no-op.
        Нужен cc_final_edit (3B): пустой settings-патч → фолбэк на буквальную замену."""
        return all(v is None or (isinstance(v, list) and not v) for v in self.model_dump().values())


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
    '"languages": ["язык объявлений — ТОЛЬКО если пользователь ЯВНО назвал язык (напр. '
    "'на украинском', 'английские объявления'); иначе оставь ПУСТОЙ список [] — язык код подберёт "
    'сам по стране (Украина→украинский, Кения→английский). НЕ угадывай язык за пользователя"], '
    '"budget_daily_units": число дневного бюджета или null, '
    '"max_cpc_units": число максимальной цены за клик (max CPC, «цена за клик», «ставка за клик») '
    "ТОЛЬКО если ЯВНО названа, иначе null, "
    '"currency": "3-буквенный ISO-код валюты (USD/EUR/AUD/PLN/CZK…) ТОЛЬКО если ЯВНО назван '
    '(в т.ч. словом: «йен»→JPY, «гривен»→UAH), иначе null", '
    '"goal": "calls|leads|sales|traffic|awareness или null", '
    '"bidding_strategy": "manual_cpc|maximize_conversions|maximize_conversion_value|target_spend '
    'или null", '
    '"target_cpa_units": число целевой цены за конверсию или null, '
    '"payment_model": "cpc|cpa или null", '
    '"networks": "search|search_partners (search_partners — если явно просят партнёрские сети) '
    'или null", '
    "\"ad_schedule\": \"расписание показов как в тексте (напр. 'пн-пт 9-18', '24/7') или null\", "
    '"start_date": "дата старта ISO ГГГГ-ММ-ДД или null", '
    '"end_date": "дата окончания ISO ГГГГ-ММ-ДД или null"}. '
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
    from datetime import date as _date

    today = _date.today()
    try:
        msg = await chat(
            [
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    # Сегодняшняя дата — чтобы модель могла резолвить относительные даты, если код-
                    # парсер не покрыл фразу (P0-4). Формат даты в JSON всё равно ISO.
                    "content": (
                        f"Язык интерфейса: {language}. Сегодня: {today.isoformat()}.\n"
                        f"Описание:\n{text}"
                    ),
                },
            ],
            role="parsing",
            temperature=0.2,
        )
        data = _extract_json_object(getattr(msg, "content", "") or "")
    except Exception:  # noqa: BLE001 — разбор не критичен, есть fallback на медианы/дефолты
        data = None
    settings = _coerce(data) if data is not None else CampaignSettings()
    # P0-4: детерминированно резолвим относительные/короткие даты из текста, если модель их не дала
    # («от сегодня до завтра» → ISO). Явные ISO-даты модели приоритетнее — не перетираем.
    if settings.start_date is None or settings.end_date is None:
        rel_start, rel_end = parse_relative_dates(text, today)
        if settings.start_date is None and rel_start:
            settings.start_date = rel_start
        if settings.end_date is None and rel_end:
            settings.end_date = rel_end
    return settings


# ── §19.3: расписание показов — строку в структуру переводит КОД (golden rule #4) ──
_DAY_TOKENS = {
    "пн": "MONDAY",
    "вт": "TUESDAY",
    "ср": "WEDNESDAY",
    "чт": "THURSDAY",
    "пт": "FRIDAY",
    "сб": "SATURDAY",
    "вс": "SUNDAY",
    "mon": "MONDAY",
    "tue": "TUESDAY",
    "wed": "WEDNESDAY",
    "thu": "THURSDAY",
    "fri": "FRIDAY",
    "sat": "SATURDAY",
    "sun": "SUNDAY",
}
_DAY_ORDER = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
_ALWAYS = {"", "24/7", "24x7", "24х7", "круглосуточно", "всегда", "always", "all day", "24-7"}


def parse_ad_schedule(text: str | None) -> list[dict] | None:
    """«пн-пт 9-18» / «будни 09:00-18:00» / «24/7» → список блоков [{day, start_hour, end_hour}].

    [] ⇒ показ 24/7 (критерии расписания НЕ создаются — дефолт Google). None ⇒ строка НЕ распознана
    (вызывающий честно откатится на 24/7 и не будет показывать нераспознанное как применённое).
    Часы 0–24, day — имя enum DayOfWeek. Считает КОД, не модель."""
    import re as _re

    s = (text or "").strip().lower().replace("–", "-").replace("—", "-")
    if s in _ALWAYS:
        return []
    m = _re.fullmatch(
        r"(?P<days>[a-zа-я,\-\s]+?)\s+(?P<h1>\d{1,2})(?::(?P<m1>\d{2}))?\s*-\s*"
        r"(?P<h2>\d{1,2})(?::(?P<m2>\d{2}))?",
        s,
    )
    if not m:
        return None
    h1, h2 = int(m.group("h1")), int(m.group("h2"))
    if not (0 <= h1 < h2 <= 24):
        return None
    days_raw = m.group("days").strip()
    if days_raw in ("будни", "weekdays", "рабочие дни"):
        days = _DAY_ORDER[:5]
    elif days_raw in ("выходные", "weekend", "weekends"):
        days = _DAY_ORDER[5:]
    elif "-" in days_raw:  # диапазон «пн-пт»
        a, _, b = days_raw.partition("-")
        da, db = _DAY_TOKENS.get(a.strip()[:3]), _DAY_TOKENS.get(b.strip()[:3])
        if not da or not db:
            return None
        ia, ib = _DAY_ORDER.index(da), _DAY_ORDER.index(db)
        if ia > ib:
            return None
        days = _DAY_ORDER[ia : ib + 1]
    else:  # перечисление «пн, ср, пт» или один день
        days = []
        for tok in _re.split(r"[,\s]+", days_raw):
            d = _DAY_TOKENS.get(tok.strip()[:3])
            if not d:
                return None
            if d not in days:
                days.append(d)
        if not days:
            return None
    return [{"day": d, "start_hour": h1, "end_hour": h2} for d in days]


def schedule_human(blocks: list[dict] | None, raw: str | None = None) -> str:
    """Человекочитаемое расписание для сводки Этапа 1: [] / None ⇒ «24/7»; иначе исходная строка
    (если дана) или компактный рендер блоков."""
    if not blocks:
        return "24/7"
    if raw and raw.strip():
        return raw.strip()
    days = [b.get("day", "")[:3].capitalize() for b in blocks]
    b0 = blocks[0]
    return f"{', '.join(days)} {b0.get('start_hour', 0)}-{b0.get('end_hour', 24)}"


def _valid_iso_date(s: str | None) -> str | None:
    """ISO ГГГГ-ММ-ДД или None (мусор от модели не должен доехать до SDK)."""
    from datetime import date as _date

    if not s:
        return None
    try:
        return _date.fromisoformat(str(s).strip()).isoformat()
    except ValueError:
        return None


# Относительные/короткие даты в свободном тексте (P0-4). Модель не знает «сегодня» и не считает
# арифметику дат → «от сегодня до завтра» давало null. Код резолвит детерминированно.
_REL_WORD_RE = re.compile(
    r"послезавтра|day\s+after\s+tomorrow|завтра|tomorrow|сегодн\w*|today", re.IGNORECASE
)
_REL_IN_N_DAYS_RE = re.compile(
    r"через\s+(\d{1,3})\s*(?:дн\w*|день)|in\s+(\d{1,3})\s*days?", re.IGNORECASE
)
# Явная дата с ТОЧКАМИ (DD.MM или DD.MM.YYYY) — точки, чтобы не путать с расписанием «9-18»/«9:00».
_DATE_DOTS_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b")
# Предлог «конца» рядом с единственным якорем ⇒ это дата ОКОНЧАНИЯ, а не старта.
_END_PREP_RE = re.compile(r"\b(до|по|until|till|end|конц\w*)\b", re.IGNORECASE)


def parse_relative_dates(text: str | None, today=None) -> tuple[str | None, str | None]:
    """Свободный текст → (start_iso, end_iso). Понимает «сегодня/завтра/послезавтра», «через N дней»,
    DD.MM(.YYYY). Два якоря по порядку = старт→конец; один якорь с предлогом «до/until» = конец.
    Ничего не нашли → (None, None). today=None ⇒ date.today() (runtime; в тестах передаётся явно)."""
    from datetime import date as _date, timedelta as _td

    if today is None:
        today = _date.today()
    t = text or ""
    anchors: list[tuple[int, _date]] = []
    for m in _REL_WORD_RE.finditer(t):
        w = m.group(0).lower()
        if w.startswith("послезавтра") or "after" in w:
            d = today + _td(days=2)
        elif w.startswith("завтра") or w == "tomorrow":
            d = today + _td(days=1)
        else:  # сегодня/today
            d = today
        anchors.append((m.start(), d))
    for m in _REL_IN_N_DAYS_RE.finditer(t):
        n = int(m.group(1) or m.group(2))
        anchors.append((m.start(), today + _td(days=min(n, 3650))))
    for m in _DATE_DOTS_RE.finditer(t):
        dd, mm, yy = int(m.group(1)), int(m.group(2)), m.group(3)
        try:
            if yy:
                year = int(yy) if len(yy) == 4 else 2000 + int(yy)
                d = _date(year, mm, dd)
            else:
                d = _date(today.year, mm, dd)
                if d < today:  # без года и в прошлом → ближайший будущий год
                    d = _date(today.year + 1, mm, dd)
            anchors.append((m.start(), d))
        except ValueError:  # 32.13 и прочий мусор
            continue
    if not anchors:
        return (None, None)
    anchors.sort(key=lambda a: a[0])
    if len(anchors) == 1 and _END_PREP_RE.search(t):
        return (None, anchors[0][1].isoformat())
    start = anchors[0][1].isoformat()
    end = anchors[1][1].isoformat() if len(anchors) > 1 else None
    return (start, end)


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
    """Единицы валюты → micros: кламп «границы абсурда» (core.limits) + округление до
    биллинг-единицы 10 000 micros — иначе Google Ads отклонит бид/бюджет с суб-центовой
    точностью и «превью ≠ созданное». 0 остаётся 0 (валидируется выше)."""
    u = max(0.0, min(float(units), float(MONEY_MAX_UNITS)))
    return round_micros(int(round(u * 1_000_000)))


def assemble_settings(
    extracted: CampaignSettings,
    *,
    median_budget_micros: int | None = None,
    avg_cpc_micros: int | None = None,
    common_match_type: str | None = None,
    topic: str | None = None,
    ui_language: str = "ru",
    account_currency: str | None = None,
) -> dict:
    """Слить извлечённое + медианы «по аналогии» + дефолты в финальный settings-словарь визарда.

    Источник каждого подставленного поля помечается ЧЕСТНО (§19.3 — менеджер видит, откуда число):
    - by_analogy — РЕАЛЬНАЯ история аккаунта (GAQL-медианы search_campaign_medians): бюджет/CPC/
      match_type; в сводке — «(по аналогии)»;
    - by_default — статический дефолт кода (не из описания и не из истории): сети/расписание/
      стратегия/бюджет/CPC/match_type без медиан; в сводке — «(по умолчанию)».
    Деньги/match_type — КОД. Возвращает wizard_state.settings.

    §19: помимо прежнего, вычисляет product (ЧТО рекламируем — драйвер seed/RSA), target_language
    (язык АУДИТОРИИ целевой страны — Кения→en, а не язык интерфейса ui_language) и страну — чтобы
    Discover/тексты были релевантны гео, а не выдавали чужой-язычную чепуху."""
    from ads import geo  # локальный импорт: extract-путь не тянет ads/google-ads

    by_analogy: list[str] = []  # из истории аккаунта (GAQL-медианы)
    by_default: list[str] = []  # статический дефолт кода

    # Валюта: явная из описания («75 йен» → JPY) → валюта аккаунта → неизвестна. Денежные ДЕФОЛТЫ
    # валюто-зависимые (0.5 JPY за клик — абсурд); явные значения пользователя не пересчитываются —
    # он пишет в валюте аккаунта (golden rule #4: деньги считает КОД).
    currency = (
        (extracted.currency or "").strip().upper()
        or (account_currency or "").strip().upper()
        or None
    )
    default_budget_units, default_cpc_units = wizard_default_money_units(currency)

    # бюджет: описание → медиана(by_analogy) → дефолт(by_default)
    if extracted.budget_daily_units is not None:
        budget_micros = _units_to_micros(extracted.budget_daily_units)
    elif median_budget_micros:
        budget_micros = int(median_budget_micros)
        by_analogy.append("budget_daily_micros")
    else:
        budget_micros = _units_to_micros(default_budget_units)
        by_default.append("budget_daily_micros")

    # cpc: описание («75 за клик») → медиана(by_analogy) → дефолт(by_default)
    if extracted.max_cpc_units is not None and extracted.max_cpc_units > 0:
        cpc_micros = _units_to_micros(extracted.max_cpc_units)
    elif avg_cpc_micros:
        cpc_micros = int(avg_cpc_micros)
        by_analogy.append("cpc_bid_micros")
    else:
        cpc_micros = _units_to_micros(default_cpc_units)
        by_default.append("cpc_bid_micros")

    # тип соответствия: частый в аккаунте(by_analogy) → дефолт phrase(by_default)
    if common_match_type:
        match_type = str(common_match_type).lower()
        by_analogy.append("match_type")
    else:
        match_type = DEFAULT_MATCH_TYPE
        by_default.append("match_type")

    strat, tcpa_micros, payment = _derive_bidding(extracted)
    if not extracted.bidding_strategy and not extracted.goal:
        by_default.append("bidding_strategy")  # статический дефолт, НЕ история аккаунта

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

    # §19.3: сети / расписание / даты — из описания, иначе статический дефолт «по умолчанию».
    networks = extracted.networks or "search"
    if not extracted.networks:
        by_default.append("networks")
    schedule_blocks = parse_ad_schedule(extracted.ad_schedule)
    if schedule_blocks is None:  # нераспознанная строка → честно откатываемся на 24/7
        schedule_blocks = []
    if not extracted.ad_schedule:
        by_default.append("ad_schedule")
    start_date = _valid_iso_date(extracted.start_date)
    end_date = _valid_iso_date(extracted.end_date)
    if start_date and end_date and end_date < start_date:
        end_date = None  # конец раньше старта — мусор от модели, не несём в SDK

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
        "currency": currency,
        "networks": networks,
        "ad_schedule": schedule_human(schedule_blocks, extracted.ad_schedule),
        "ad_schedule_blocks": schedule_blocks,  # [] ⇒ 24/7 (критерии не создаются)
        "start_date": start_date,  # None ⇒ старт сегодня (дефолт Google)
        "end_date": end_date,  # None ⇒ без даты конца
        "by_analogy": by_analogy,
        "by_default": by_default,
    }
