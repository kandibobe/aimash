"""Единый источник числовых порогов денежного пути (defense-in-depth БЕЗ дублирования констант).

Зачем: один и тот же «очевидно неверно» потолок суммы жил в ДВУХ слоях безопасности —
agent.tools.schemas (валидация ввода модели) и ads.mutations (граница у самого SDK-вызова) —
как два независимых магических числа `1_000_000`. Дублирование = риск рассинхрона при правке.

Защита остаётся двухслойной: оба слоя ПО-ПРЕЖНЕМУ независимо проверяют сумму (схема — в единицах
валюты, мутация — в micros у границы SDK). Меняется лишь то, что обе границы ВЫЧИСЛЯЮТСЯ из одного
значения здесь, а не повторяют литерал. golden rule #4: пороги считает КОД, не модель.

НЕ бизнес-лимиты, а граница абсурда (галлюцинация/инъекция модели сверх gt=0). 1 micro = 1e-6
единицы валюты — так Google Ads представляет деньги.
"""

from __future__ import annotations

# «Очевидно неверно» потолок суммы в ЕДИНИЦАХ валюты аккаунта (бюджет/ставка/target_cpa).
MONEY_MAX_UNITS: int = 1_000_000
# Та же граница в micros — для слоя у границы SDK (1 единица = 1e6 micros). Выводится, не дублируется.
MONEY_MAX_MICROS: int = MONEY_MAX_UNITS * 1_000_000

# Потолок радиуса proximity-таргетинга (км) — лимит Google Ads. Один источник для схемы и мутации.
MAX_RADIUS_KM: int = 2000

# Минимальная биллинг-единица Google Ads для бид/бюджета ЗАВИСИТ ОТ ВАЛЮТЫ аккаунта: значения, не
# кратные ей, API отклоняет (NON_MULTIPLE_OF_MINIMUM_CURRENCY_UNIT) — уже ПОСЛЕ «да» пользователя,
# то есть claim сожжён, а операция не выполнена.
#
# Источник — сам Google: CurrencyConstant.billable_unit_micros (v24). Снято живьём с API 2026-07-13
# (147 валют, GAQL `SELECT currency_constant.code, currency_constant.billable_unit_micros
# FROM currency_constant`) — таблица ниже держит только те, где единица ≠ дефолтной.
#
# ⚠️ Это НЕ то же, что ZERO_DECIMAL_CURRENCIES (там — про ОТОБРАЖЕНИЕ): TWD и HUF показываются без
# копеек, но биллинг-единица у них 10 000, а не 1 000 000. Выводить одно из другого нельзя —
# получилось бы округление в 100 раз грубее, чем нужно.
BILLING_UNIT_MICROS: int = 10_000  # дефолт (122 валюты: USD, EUR, UAH, PLN, TWD, HUF, …)
BILLING_UNIT_MICROS_BY_CURRENCY: dict[str, int] = {
    # 1 единица валюты (нет минорных единиц в биллинге). Ошибка здесь = отказ API после ✅.
    **dict.fromkeys(
        ("CLP COP DJF GNF IDR ISK JPY KRW PKR PYG ROL RSD RWF TRL UGX VND VUV XAF XOF XPF").split(),
        1_000_000,
    ),
    # 0.001 единицы — трёхдесятичные валюты Залива (округление до 10 000 их бы не сломало —
    # оно кратно 1 000, — но огрубило бы ставку в 10 раз).
    **dict.fromkeys("BHD IQD KWD LYD OMR".split(), 1_000),
}


def billing_unit_micros(currency: str | None) -> int:
    """Минимальная биллинг-единица (micros) для валюты аккаунта. Неизвестная/пустая → дефолт
    10 000. UGX (страна деплоя — Уганда) = 1 000 000: валютно-слепое округление отклонялось API."""
    return BILLING_UNIT_MICROS_BY_CURRENCY.get(
        (currency or "").strip().upper(), BILLING_UNIT_MICROS
    )


# P1-9: типичный ДНЕВНОЙ минимум бюджета для Demand Gen / Video (валюто-зависимый). Google требует
# бюджет ≥ порога, иначе BUDGET…per_day_minimum. Точные пороги Google меняет — это КОНСЕРВАТИВНАЯ
# оценка для АПФРОНТ-предупреждения (не хардблок: финальную правду говорит API, а мы гуманизируем
# per_day_minimum-ошибку). AUD=8 подтверждён живым смоуком (scripts/live_smoke_video_dg.py).
DG_VIDEO_MIN_DAILY_UNITS: dict[str, float] = {
    "USD": 10.0,
    "EUR": 10.0,
    "GBP": 8.0,
    "AUD": 8.0,
    "CAD": 10.0,
    "PLN": 40.0,
    "CZK": 200.0,
    "UAH": 300.0,
}
DG_VIDEO_MIN_DAILY_DEFAULT_UNITS: float = 10.0


def dg_video_min_daily_units(currency: str | None) -> float:
    """Консервативный дневной минимум бюджета DG/Video в единицах валюты аккаунта (для апфронт-
    предупреждения). Неизвестная валюта → дефолт. Не хардблок — Google валидирует окончательно."""
    return DG_VIDEO_MIN_DAILY_UNITS.get(
        (currency or "").strip().upper(), DG_VIDEO_MIN_DAILY_DEFAULT_UNITS
    )


# Валюты без минорных единиц (копеек) в биллинге Google Ads: показывать «1 500 JPY», а не «0.50 JPY».
ZERO_DECIMAL_CURRENCIES: frozenset[str] = frozenset(
    {"JPY", "KRW", "CLP", "VND", "ISK", "TWD", "HUF", "IDR", "UGX"}
)

# Дефолты денежных полей визарда §19 (дневной бюджет, max CPC) В ЕДИНИЦАХ валюты аккаунта.
# Валютно-слепой «0.5 units» давал абсурд «0.50 JPY» (пол-йены за клик). Это seed-значения
# для черновика (менеджер правит перед confirm), НЕ бизнес-рекомендация; консервативные порядки.
WIZARD_DEFAULT_MONEY_UNITS: dict[str, tuple[float, float]] = {
    "USD": (10.0, 0.5),
    "EUR": (10.0, 0.5),
    "GBP": (8.0, 0.4),
    "CAD": (12.0, 0.6),
    "AUD": (14.0, 0.7),
    "PLN": (40.0, 2.0),
    "CZK": (250.0, 12.0),
    "UAH": (400.0, 20.0),
    "UGX": (
        40000.0,
        2000.0,
    ),  # страна деплоя (Уганда); ~1 USD ≈ 3 700 UGX, единица биллинга = 1 UGX
    "INR": (800.0, 25.0),
    "JPY": (1500.0, 75.0),
    "KRW": (14000.0, 700.0),
    "CLP": (9000.0, 450.0),
    "VND": (250000.0, 12000.0),
    "ISK": (1300.0, 65.0),
    "TWD": (300.0, 15.0),
    "HUF": (3500.0, 180.0),
    "IDR": (150000.0, 7500.0),
}
# Неизвестная/пустая валюта → прежнее поведение (10 units бюджет, 0.5 units CPC).
WIZARD_DEFAULT_MONEY_FALLBACK_UNITS: tuple[float, float] = (10.0, 0.5)


def wizard_default_money_units(currency: str | None) -> tuple[float, float]:
    """Дефолт (дневной_бюджет, max_cpc) визарда в единицах валюты аккаунта. Неизвестная валюта →
    fallback (сегодняшние 10/0.5). Считает КОД (golden rule #4) — модель деньги не выбирает."""
    return WIZARD_DEFAULT_MONEY_UNITS.get(
        (currency or "").strip().upper(), WIZARD_DEFAULT_MONEY_FALLBACK_UNITS
    )


def round_micros(value: int, *, currency: str | None) -> int:
    """Округлить денежную величину (micros) до кратной биллинг-единице ВАЛЮТЫ АККАУНТА.

    Единый источник для всех денежных путей (медианы «по аналогии» в ads.read, SDK-граница в
    ads.mutations, пользовательский ввод в agent.campaign_settings). Положительное значение не
    обнуляем (минимум — одна единица); 0/отрицательное возвращаем как есть (валидируется выше).

    `currency` — ОБЯЗАТЕЛЬНЫЙ kwarg (можно `None`), а не дефолт: валютно-слепое округление до
    10 000 давало для JPY/KRW/UGX (единица = 1 000 000) значение, которое Google отвергает ПОСЛЕ
    подтверждения. Требование явного аргумента заставляет новый call-site решить, знает ли он
    валюту. `None` = «валюта неизвестна» → дефолтная единица; это допустимо только для ЧЕРНОВЫХ
    значений: авторитетное округление всё равно делается у границы SDK (ads.mutations), где валюта
    аккаунта известна точно."""
    v = int(value)
    if v <= 0:
        return v
    unit = billing_unit_micros(currency)
    r = round(v / unit) * unit
    return r if r > 0 else unit
