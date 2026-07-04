"""Глубокий отчёт по ОДНОМУ аккаунту: GAQL-разбивки + метрики. READ-ONLY.

Каждый fetch_* проходит через ads.client.ensure_read_allowed (замок ЧТЕНИЯ, §8) — отчёты по
разрешённому на чтение аккаунту (мутации этим не затрагиваются). Метрики из micros считает КОД
(cost/CPC/CPA/ROAS),
не модель. Большие разбивки (ключи/объявления) ограничиваются топ-N по расходу — усечение
помечается явно (Breakdown.note), без «тихого» обрезания.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ads.client import ensure_read_allowed

# Топ-N для потенциально огромных разбивок (ключи/объявления): сортируем по расходу.
TOP_N = 1000

# Колонки метрик (RU) — единый порядок для xlsx и текстовой сводки.
METRIC_HEADERS = [
    "Показы",
    "Клики",
    "CTR",
    "Сред. CPC",
    "Расход",
    "Конверсии",
    "Ценность",
    "CPA",
    "ROAS",
]

# Денежные колонки (значения в валюте аккаунта) — к ним добавляем код валюты в заголовок (§9).
# ROAS — отношение (conv_value/cost), безразмерное → без валюты.
_MONEY_HEADERS = frozenset({"Сред. CPC", "Расход", "Ценность", "CPA"})


def metric_headers(currency: str = "") -> list[str]:
    """METRIC_HEADERS с кодом валюты на денежных колонках (§9): «Расход» → «Расход, USD».
    Пустой currency → без суффикса (обратная совместимость: == METRIC_HEADERS)."""
    if not currency:
        return list(METRIC_HEADERS)
    return [f"{h}, {currency}" if h in _MONEY_HEADERS else h for h in METRIC_HEADERS]


_METRICS_SELECT = (
    "metrics.impressions, metrics.clicks, metrics.cost_micros, "
    "metrics.conversions, metrics.conversions_value"
)


@dataclass
class Metrics:
    impressions: int = 0
    clicks: int = 0
    cost_micros: int = 0
    conversions: float = 0.0
    conv_value: float = 0.0

    @property
    def cost(self) -> float:
        return self.cost_micros / 1_000_000

    @property
    def ctr(self) -> float:
        return self.clicks / self.impressions if self.impressions else 0.0

    @property
    def avg_cpc(self) -> float:
        return self.cost / self.clicks if self.clicks else 0.0

    @property
    def cpa(self) -> float:
        return self.cost / self.conversions if self.conversions else 0.0

    @property
    def roas(self) -> float:
        return self.conv_value / self.cost if self.cost else 0.0

    def add(self, other: "Metrics") -> None:
        self.impressions += other.impressions
        self.clicks += other.clicks
        self.cost_micros += other.cost_micros
        self.conversions += other.conversions
        self.conv_value += other.conv_value

    def as_row(self) -> list:
        """Значения метрик в порядке METRIC_HEADERS (производные считает КОД)."""
        return [
            self.impressions,
            self.clicks,
            round(self.ctr, 4),
            round(self.avg_cpc, 2),
            round(self.cost, 2),
            round(self.conversions, 2),
            round(self.conv_value, 2),
            round(self.cpa, 2),
            round(self.roas, 2),
        ]


@dataclass
class Breakdown:
    key: str  # "campaign" | "ad_group" | ...
    title: str  # человекочитаемый заголовок листа/секции (RU)
    dim_headers: list[str]  # названия колонок-измерений (перед колонками метрик)
    rows: list[tuple[tuple, Metrics]] = field(default_factory=list)  # ((dim values…), Metrics)
    note: str | None = None  # пометка об усечении и т.п. (без тихих обрезаний)


def _metrics(m) -> Metrics:
    return Metrics(
        impressions=int(m.impressions),
        clicks=int(m.clicks),
        cost_micros=int(m.cost_micros),
        conversions=float(m.conversions),
        conv_value=float(m.conversions_value),
    )


def _enum_name(v) -> str:
    return getattr(v, "name", str(v))


def _search(client, customer_id: str, query: str):
    return client.get_service("GoogleAdsService").search(customer_id=str(customer_id), query=query)


def _where(period, campaign_id: str | None) -> str:
    """WHERE-фрагмент периода + (опц.) фильтр по кампании. campaign_id приводим к int — защита от
    инъекции в GAQL (id всегда числовой). None ⇒ фрагмент БАЙТ-в-БАЙТ как раньше (обратная совм.)."""
    w = period.gaql_between()
    if campaign_id:
        w += f" AND campaign.id = {int(campaign_id)}"
    return w


def _totals_source(campaign_id: str | None) -> str:
    """Ресурс для агрегатов/сегментных разбивок: FROM customer (весь аккаунт) или FROM campaign
    (когда скоуп по кампании — campaign.id не выбирается из customer, а metrics/segments одинаковы)."""
    return "campaign" if campaign_id else "customer"


# ── Totals (агрегат за период; resource customer / campaign при скоупе) ──────────
def fetch_totals(client, customer_id: str, period, campaign_id: str | None = None) -> Metrics:
    ensure_read_allowed(customer_id)
    q = f"SELECT {_METRICS_SELECT} FROM {_totals_source(campaign_id)} WHERE {_where(period, campaign_id)}"
    total = Metrics()
    for row in _search(client, customer_id, q):
        total.add(_metrics(row.metrics))
    return total


def count_active_campaigns(client, customer_id: str) -> int:
    """§8 (P1-G): число АКТИВНЫХ (ENABLED) кампаний аккаунта — лёгкий запрос без метрик/периода
    (не тянет segments.date), для колонки «активных кампаний» в сводке /mcc. READ-ONLY."""
    ensure_read_allowed(customer_id)
    q = "SELECT campaign.id FROM campaign WHERE campaign.status = 'ENABLED'"
    return sum(1 for _ in _search(client, customer_id, q))


# ── Разбивки ────────────────────────────────────────────────────────────────────
def fetch_by_campaign(
    client, customer_id: str, period, campaign_id: str | None = None
) -> Breakdown:
    ensure_read_allowed(customer_id)
    q = (
        f"SELECT campaign.name, campaign.status, {_METRICS_SELECT} FROM campaign "
        f"WHERE {_where(period, campaign_id)} ORDER BY metrics.cost_micros DESC"
    )
    rows = [
        ((r.campaign.name, _enum_name(r.campaign.status)), _metrics(r.metrics))
        for r in _search(client, customer_id, q)
    ]
    return Breakdown("campaign", "Кампании", ["Кампания", "Статус"], rows)


def fetch_by_ad_group(
    client, customer_id: str, period, campaign_id: str | None = None
) -> Breakdown:
    ensure_read_allowed(customer_id)
    q = (
        f"SELECT campaign.name, ad_group.name, ad_group.status, {_METRICS_SELECT} "
        f"FROM ad_group WHERE {_where(period, campaign_id)} ORDER BY metrics.cost_micros DESC"
    )
    rows = [
        (
            (r.campaign.name, r.ad_group.name, _enum_name(r.ad_group.status)),
            _metrics(r.metrics),
        )
        for r in _search(client, customer_id, q)
    ]
    return Breakdown("ad_group", "Группы объявлений", ["Кампания", "Группа", "Статус"], rows)


def fetch_by_keyword(client, customer_id: str, period, campaign_id: str | None = None) -> Breakdown:
    ensure_read_allowed(customer_id)
    q = (
        "SELECT campaign.name, ad_group.name, ad_group_criterion.keyword.text, "
        f"ad_group_criterion.keyword.match_type, {_METRICS_SELECT} FROM keyword_view "
        f"WHERE {_where(period, campaign_id)} ORDER BY metrics.cost_micros DESC LIMIT {TOP_N}"
    )
    rows = [
        (
            (
                r.campaign.name,
                r.ad_group.name,
                r.ad_group_criterion.keyword.text,
                _enum_name(r.ad_group_criterion.keyword.match_type),
            ),
            _metrics(r.metrics),
        )
        for r in _search(client, customer_id, q)
    ]
    note = f"показаны топ-{TOP_N} ключей по расходу" if len(rows) >= TOP_N else None
    return Breakdown("keyword", "Ключевые слова", ["Кампания", "Группа", "Ключ", "Тип"], rows, note)


def fetch_by_ad(client, customer_id: str, period, campaign_id: str | None = None) -> Breakdown:
    ensure_read_allowed(customer_id)
    q = (
        "SELECT campaign.name, ad_group.name, ad_group_ad.ad.id, ad_group_ad.ad.type, "
        f"{_METRICS_SELECT} FROM ad_group_ad "
        f"WHERE {_where(period, campaign_id)} ORDER BY metrics.cost_micros DESC LIMIT {TOP_N}"
    )
    rows = [
        (
            (
                r.campaign.name,
                r.ad_group.name,
                str(r.ad_group_ad.ad.id),
                _enum_name(r.ad_group_ad.ad.type),
            ),
            _metrics(r.metrics),
        )
        for r in _search(client, customer_id, q)
    ]
    note = f"показаны топ-{TOP_N} объявлений по расходу" if len(rows) >= TOP_N else None
    return Breakdown("ad", "Объявления", ["Кампания", "Группа", "ID объявления", "Тип"], rows, note)


def fetch_by_device(client, customer_id: str, period, campaign_id: str | None = None) -> Breakdown:
    ensure_read_allowed(customer_id)
    q = (
        f"SELECT segments.device, {_METRICS_SELECT} FROM {_totals_source(campaign_id)} "
        f"WHERE {_where(period, campaign_id)}"
    )
    rows = [
        ((_enum_name(r.segments.device),), _metrics(r.metrics))
        for r in _search(client, customer_id, q)
    ]
    rows.sort(key=lambda t: t[1].cost_micros, reverse=True)
    return Breakdown("device", "Устройства", ["Устройство"], rows)


def fetch_by_network(client, customer_id: str, period, campaign_id: str | None = None) -> Breakdown:
    ensure_read_allowed(customer_id)
    q = (
        f"SELECT segments.ad_network_type, {_METRICS_SELECT} FROM {_totals_source(campaign_id)} "
        f"WHERE {_where(period, campaign_id)}"
    )
    rows = [
        ((_enum_name(r.segments.ad_network_type),), _metrics(r.metrics))
        for r in _search(client, customer_id, q)
    ]
    rows.sort(key=lambda t: t[1].cost_micros, reverse=True)
    return Breakdown("network", "Сети", ["Сеть"], rows)


def fetch_by_day(client, customer_id: str, period, campaign_id: str | None = None) -> Breakdown:
    ensure_read_allowed(customer_id)
    q = (
        f"SELECT segments.date, {_METRICS_SELECT} FROM {_totals_source(campaign_id)} "
        f"WHERE {_where(period, campaign_id)} ORDER BY segments.date"
    )
    rows = [((r.segments.date,), _metrics(r.metrics)) for r in _search(client, customer_id, q)]
    return Breakdown("day", "По дням", ["Дата"], rows)


# Порядок разбивок в отчёте (как в ТЗ §9).
BREAKDOWN_FETCHERS = [
    fetch_by_campaign,
    fetch_by_ad_group,
    fetch_by_keyword,
    fetch_by_ad,
    fetch_by_device,
    fetch_by_network,
    fetch_by_day,
]
