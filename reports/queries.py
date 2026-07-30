"""Глубокий отчёт по ОДНОМУ аккаунту: GAQL-разбивки + метрики. READ-ONLY.

Каждый fetch_* проходит через ads.client.ensure_read_allowed (замок ЧТЕНИЯ, §8) — отчёты по
разрешённому на чтение аккаунту (мутации этим не затрагиваются). Метрики из micros считает КОД
(cost/CPC/CPA/ROAS),
не модель. Большие разбивки (ключи/объявления) ограничиваются топ-N по расходу — усечение
помечается явно (Breakdown.note), без «тихого» обрезания.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from ads.client import ensure_read_allowed

# Ярлыки (RU/EN) живут в reports.labels — один словарь на все экспорты. Ре-экспорт: внешние
# импорты `from reports.queries import METRIC_HEADERS, metric_headers` продолжают работать.
from reports.labels import METRIC_HEADERS, metric_headers, top_note  # noqa: F401

# Топ-N для потенциально огромных разбивок (ключи/объявления): сортируем по расходу.
TOP_N = 1000

# Числовые форматы колонок метрик — ОДИН реестр на все экспорты (порядок = METRIC_HEADERS).
# Паттерны в синтаксисе Excel/Sheets (он общий): xlsx кладёт их в number_format, Sheets — в
# userEnteredFormat.numberFormat. Раньше форматы жили только в reports/xlsx.py, и в Sheets CTR
# уезжал сырой долей (0.0523 вместо 5,23%), а деньги — без разделителей: та же таблица, но в
# Google Sheets она читалась хуже, чем в Excel.
METRIC_FORMATS = [
    "#,##0",  # Показы
    "#,##0",  # Клики
    "0.00%",  # CTR (значение — доля: 0.0523)
    "0.00",  # Сред. CPC
    "#,##0.00",  # Расход
    "0.00",  # Конверсии
    "#,##0.00",  # Ценность
    "0.00",  # CPA
    "0.00",  # ROAS
]


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
    note: str | None = None  # пометка об усечении и т.п., RU (без тихих обрезаний)
    note_kind: str | None = None  # «чего» усечение ("keyword"/"ad") — экспорт рисует note на lang


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


def campaign_status_counts(client, customer_id: str) -> dict[str, int]:
    """§8: РАЗБИВКА кампаний аккаунта ПО СТАТУСУ ({ENABLED: n, PAUSED: m, …}) — лёгкий запрос без
    метрик/периода, для сводки /mcc (спец-требование §8 «статус кампаний», не только счётчик
    активных). REMOVED исключаем (мусор для оператора). READ-ONLY."""
    ensure_read_allowed(customer_id)
    q = "SELECT campaign.status FROM campaign WHERE campaign.status != 'REMOVED'"
    out: dict[str, int] = {}
    for row in _search(client, customer_id, q):
        st = _enum_name(row.campaign.status)
        out[st] = out.get(st, 0) + 1
    return out


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
    truncated = len(rows) >= TOP_N
    return Breakdown(
        "keyword",
        "Ключевые слова",
        ["Кампания", "Группа", "Ключ", "Тип"],
        rows,
        top_note("keyword", TOP_N) if truncated else None,
        note_kind="keyword" if truncated else None,
    )


_AD_PREVIEW_MAXLEN = 120  # достаточно, чтобы узнать креатив, и не раздувает ячейку


def _ad_preview(ad) -> str:
    """§9: «какой креатив слил бюджет» — первые заголовки RSA. Раньше разбивка давала только
    ID+тип: по ней нельзя было понять, ЧТО именно показывалось. Не-RSA (GDN/видео) заголовков
    здесь не имеют → пусто (для них смысл несёт ссылка). getattr — фейк-строки в тестах."""
    rsa = getattr(ad, "responsive_search_ad", None)
    heads = [
        (getattr(h, "text", "") or "").strip() for h in (getattr(rsa, "headlines", None) or [])
    ]
    return " | ".join(h for h in heads if h)[:_AD_PREVIEW_MAXLEN]


def fetch_by_ad(client, customer_id: str, period, campaign_id: str | None = None) -> Breakdown:
    ensure_read_allowed(customer_id)
    q = (
        "SELECT campaign.name, ad_group.name, ad_group_ad.ad.id, ad_group_ad.ad.type, "
        "ad_group_ad.ad.responsive_search_ad.headlines, ad_group_ad.ad.final_urls, "
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
                _ad_preview(r.ad_group_ad.ad),
                next(iter(getattr(r.ad_group_ad.ad, "final_urls", None) or []), ""),
            ),
            _metrics(r.metrics),
        )
        for r in _search(client, customer_id, q)
    ]
    truncated = len(rows) >= TOP_N
    return Breakdown(
        "ad",
        "Объявления",
        ["Кампания", "Группа", "ID объявления", "Тип", "Заголовки", "Ссылка"],
        rows,
        top_note("ad", TOP_N) if truncated else None,
        note_kind="ad" if truncated else None,
    )


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


# ══════════════════════════════════════════════════════════════════════════════════
# Аудит-фетчеры (READ-ONLY). НАМЕРЕННО НЕ в BREAKDOWN_FETCHERS — не текут в обычный /report.
# GAQL-поля сверены живьём на TEST MCC v24 (P0, 2026-07-08). Метрики/доли считает КОД.
# ══════════════════════════════════════════════════════════════════════════════════

# Каналы, где impression-share осмыслен (Display/Video/PMax отдают null/content_* → пропускаем).
_IS_CHANNELS = ("SEARCH", "SHOPPING")


@dataclass
class ImpressionShareRow:
    campaign_id: str
    campaign_name: str
    channel_type: str
    search_is: float  # 0..1 (metrics.search_impression_share)
    budget_lost_is: float  # metrics.search_budget_lost_impression_share
    rank_lost_is: float  # metrics.search_rank_lost_impression_share

    @property
    def total_known(self) -> float:
        """Сумма долей: ≈1.0 → данные полны (можно классифицировать budget-vs-rank); иначе «нет данных»
        (proto3: отсутствующая доля = 0.0, неотличима от истинного 0 — разбирает движок аудита)."""
        return self.search_is + self.budget_lost_is + self.rank_lost_is


def fetch_impression_share(
    client, customer_id: str, period, campaign_id: str | None = None
) -> list[ImpressionShareRow]:
    """Impression share по Search/Shopping-кампаниям: доля показов + потери по бюджету/рангу (доли 0..1).
    READ-ONLY. Отдельный dataclass (доли, не micros → не Metrics). segments.date в WHERE (сверено v24)."""
    ensure_read_allowed(customer_id)
    channels = ", ".join(f"'{c}'" for c in _IS_CHANNELS)
    q = (
        "SELECT campaign.id, campaign.name, campaign.advertising_channel_type, "
        "metrics.search_impression_share, metrics.search_budget_lost_impression_share, "
        "metrics.search_rank_lost_impression_share FROM campaign "
        f"WHERE {_where(period, campaign_id)} AND campaign.advertising_channel_type IN ({channels})"
    )
    return [
        ImpressionShareRow(
            campaign_id=str(r.campaign.id),
            campaign_name=r.campaign.name,
            channel_type=_enum_name(r.campaign.advertising_channel_type),
            search_is=float(r.metrics.search_impression_share),
            budget_lost_is=float(r.metrics.search_budget_lost_impression_share),
            rank_lost_is=float(r.metrics.search_rank_lost_impression_share),
        )
        for r in _search(client, customer_id, q)
    ]


@dataclass
class OptimizationScore:
    score: float  # 0..1 (customer.optimization_score) — нативный балл Google
    uplift: float  # metrics.optimization_score_uplift — «+балл, если применить все рекомендации»


def fetch_optimization_score(client, customer_id: str) -> "OptimizationScore | None":
    """Нативный Google optimization_score (0..1) + суммарный uplift. READ-ONLY «второе мнение»
    рядом с нашим аудитом. None → недоступно (аудит не роняем). Балл считает Google, не мы."""
    ensure_read_allowed(customer_id)
    q = "SELECT customer.optimization_score, metrics.optimization_score_uplift FROM customer LIMIT 1"
    for r in _search(client, customer_id, q):
        return OptimizationScore(
            score=float(r.customer.optimization_score),
            uplift=float(r.metrics.optimization_score_uplift),
        )
    return None


@dataclass
class RecommendationRow:
    """Рекомендация Google + ЕЁ СОБСТВЕННАЯ оценка эффекта (`recommendation.impact`): что будет с
    метриками, если применить. Числа — прогноз Google, не наш; деньги уже поделены на micros (gr4).

    Нули в base/potential — «Google эффект не оценил» (impact заполнен не для всех типов), а не
    «эффекта нет»: показывать прирост можно только там, где potential > base."""

    type: str
    dismissed: bool
    campaign: str = ""  # имя кампании (пусто — рекомендация уровня аккаунта)
    resource_name: str = ""  # адрес для будущего ApplyRecommendation (через confirm-гейт)
    base_conversions: float = 0.0
    potential_conversions: float = 0.0
    base_cost: float = 0.0
    potential_cost: float = 0.0
    base_clicks: float = 0.0
    potential_clicks: float = 0.0

    @property
    def add_conversions(self) -> float:
        """Прогнозный прирост конверсий по оценке Google (≤0 → эффект не оценён / не в конверсиях)."""
        return round(self.potential_conversions - self.base_conversions, 1)

    @property
    def add_cost(self) -> float:
        """Во сколько Google оценивает прирост расхода (может быть 0 и отрицательным)."""
        return round(self.potential_cost - self.base_cost, 2)

    @property
    def add_clicks(self) -> float:
        return round(self.potential_clicks - self.base_clicks, 1)


def _recs_query(limit: int, with_impact: bool) -> str:
    fields = ["recommendation.type", "recommendation.dismissed", "recommendation.campaign"]
    if with_impact:
        # В GAQL v24 selectable — КОМПОЗИТ recommendation.impact (метаданные GoogleAdsFieldService);
        # leaf-пути impact.{base,potential}_metrics.* — «Unrecognized fields» (живая проба
        # 2026-07-17: молчаливая деградация в except выкидывала impact на КАЖДОМ прогоне).
        # Протобуф ответа несёт вложенные метрики целиком — парсер ниже читает их как раньше.
        fields += ["recommendation.impact", "campaign.name"]
    return f"SELECT {', '.join(fields)} FROM recommendation LIMIT {int(limit)}"


def fetch_recommendations(client, customer_id: str, limit: int = 50) -> list[RecommendationRow]:
    """Ф2: активные Google-рекомендации ВМЕСТЕ с их impact-метриками. READ-ONLY — только показ;
    применение (ApplyRecommendation) — отдельной мутацией через confirm-гейт, не отсюда.

    В SELECT идёт композит `recommendation.impact` (в GAQL v24 selectable именно он; leaf-пути
    impact.*_metrics.* сервер отвергает как Unrecognized — живая проба 2026-07-17), протобуф ответа
    несёт RecommendationImpact/RecommendationMetrics целиком. Деградация: если сервер отвергнет
    impact/джойн кампании, повторяем БЕЗ них — типы рекомендаций важнее и не должны падать заодно
    (прирост тогда 0.0 = «не оценено», gr8)."""
    ensure_read_allowed(customer_id)
    try:
        rows = list(_search(client, customer_id, _recs_query(limit, True)))
    except Exception:  # noqa: BLE001 — impact необязателен; типы рекомендаций читаем всё равно
        rows = list(_search(client, customer_id, _recs_query(limit, False)))
    out: list[RecommendationRow] = []
    for r in rows:
        rec = r.recommendation
        base = getattr(getattr(rec, "impact", None), "base_metrics", None)
        pot = getattr(getattr(rec, "impact", None), "potential_metrics", None)
        out.append(
            RecommendationRow(
                type=_enum_name(rec.type),
                dismissed=bool(rec.dismissed),
                campaign=getattr(getattr(r, "campaign", None), "name", "") or "",
                resource_name=str(getattr(rec, "resource_name", "") or ""),
                base_conversions=float(getattr(base, "conversions", 0.0) or 0.0),
                potential_conversions=float(getattr(pot, "conversions", 0.0) or 0.0),
                base_cost=int(getattr(base, "cost_micros", 0) or 0) / 1_000_000,
                potential_cost=int(getattr(pot, "cost_micros", 0) or 0) / 1_000_000,
                base_clicks=float(getattr(base, "clicks", 0) or 0),
                potential_clicks=float(getattr(pot, "clicks", 0) or 0),
            )
        )
    return out


@dataclass
class RecommendationSubscriptionRow:
    type: str  # тип авто-применяемой рекомендации (recommendation_subscription.type)
    status: str  # ENABLED = Google применяет сам, без подтверждения


def fetch_recommendation_subscriptions(
    client, customer_id: str
) -> list[RecommendationSubscriptionRow]:
    """Ф9: подписки авто-применения рекомендаций. ENABLED-подписка = Google меняет аккаунт САМ
    (ставки/ключи/тексты) — прямое противоречие философии бота «никаких изменений без подтверждения».
    Запрос сверен живой пробой на Draft (v24, 2026-07-17). READ-ONLY."""
    ensure_read_allowed(customer_id)
    q = (
        "SELECT recommendation_subscription.type, recommendation_subscription.status "
        "FROM recommendation_subscription"
    )
    return [
        RecommendationSubscriptionRow(
            type=_enum_name(r.recommendation_subscription.type_),
            status=_enum_name(r.recommendation_subscription.status),
        )
        for r in _search(client, customer_id, q)
    ]


@dataclass
class SearchTermRow:
    search_term: str
    campaign: str
    ad_group: str
    keyword: str  # ключ, по которому показался запрос (segments.keyword.info.text)
    match_type: str
    metrics: Metrics


def fetch_search_terms(
    client, customer_id: str, period, campaign_id: str | None = None, limit: int = 200
) -> list[SearchTermRow]:
    """Отчёт по поисковым запросам (search_term_view) за период — майнинг минус-слов/новых ключей.
    date ТОЛЬКО в WHERE (не в SELECT — иначе дробление по дням ломает агрегаты). READ-ONLY."""
    ensure_read_allowed(customer_id)
    q = (
        "SELECT search_term_view.search_term, campaign.name, ad_group.name, "
        "segments.keyword.info.text, segments.keyword.info.match_type, "
        f"{_METRICS_SELECT} FROM search_term_view WHERE {_where(period, campaign_id)} "
        f"ORDER BY metrics.cost_micros DESC LIMIT {int(limit)}"
    )
    return [
        SearchTermRow(
            search_term=r.search_term_view.search_term,
            campaign=r.campaign.name,
            ad_group=r.ad_group.name,
            keyword=r.segments.keyword.info.text,
            match_type=_enum_name(r.segments.keyword.info.match_type),
            metrics=_metrics(r.metrics),
        )
        for r in _search(client, customer_id, q)
    ]


@dataclass
class ConversionActionRow:
    name: str
    status: str
    type: str
    category: str
    primary_for_goal: bool
    # Ф9: модель атрибуции действия (GOOGLE_ADS_LAST_CLICK / _DATA_DRIVEN / …). Дефолт "" —
    # обратная совместимость со стабами в тестах (движок гейтит через getattr).
    attribution_model: str = ""


def fetch_conversion_health(client, customer_id: str) -> list[ConversionActionRow]:
    """Действия-конверсии аккаунта (статус/тип/категория/primary_for_goal + модель атрибуции).
    Пусто / нет ENABLED primary → трекинг под подозрением (решает движок аудита, крит-фикс #3).
    Поля сверены живой пробой на Draft (v24, 2026-07-17). READ-ONLY."""
    ensure_read_allowed(customer_id)
    q = (
        "SELECT conversion_action.name, conversion_action.status, conversion_action.type, "
        "conversion_action.category, conversion_action.primary_for_goal, "
        "conversion_action.attribution_model_settings.attribution_model FROM conversion_action"
    )
    out: list[ConversionActionRow] = []
    for r in _search(client, customer_id, q):
        ca = r.conversion_action
        out.append(
            ConversionActionRow(
                name=ca.name,
                status=_enum_name(ca.status),
                type=_enum_name(ca.type_),
                category=_enum_name(ca.category),
                primary_for_goal=bool(ca.primary_for_goal),
                attribution_model=_enum_name(ca.attribution_model_settings.attribution_model),
            )
        )
    return out


@dataclass
class CampaignBidding:
    campaign_id: str
    name: str
    strategy_type: str  # campaign.bidding_strategy_type (MAXIMIZE_CONVERSIONS / MANUAL_CPC / …)
    target_cpa: float | None  # в валюте аккаунта (из micros); None — цель не задана


def read_campaign_bidding(client, customer_id: str) -> list[CampaignBidding]:
    """Стратегия ставок + target CPA по кампаниям (для 3×-Kill цели и broad-safety). Цель берём из
    maximize_conversions.target_cpa_micros, иначе из legacy target_cpa. READ-ONLY."""
    ensure_read_allowed(customer_id)
    q = (
        "SELECT campaign.id, campaign.name, campaign.bidding_strategy_type, "
        "campaign.maximize_conversions.target_cpa_micros, campaign.target_cpa.target_cpa_micros "
        "FROM campaign WHERE campaign.status != 'REMOVED'"
    )
    out: list[CampaignBidding] = []
    for r in _search(client, customer_id, q):
        c = r.campaign
        tcpa_micros = int(
            getattr(c.maximize_conversions, "target_cpa_micros", 0)
            or getattr(c.target_cpa, "target_cpa_micros", 0)
            or 0
        )
        out.append(
            CampaignBidding(
                campaign_id=str(c.id),
                name=c.name,
                strategy_type=_enum_name(c.bidding_strategy_type),
                target_cpa=(tcpa_micros / 1_000_000) if tcpa_micros else None,
            )
        )
    return out


@dataclass
class AdPolicyRow:
    campaign: str
    ad_group: str
    ad_id: str
    approval_status: str  # DISAPPROVED / APPROVED_LIMITED / AREA_OF_INTEREST_ONLY / APPROVED / …


def fetch_ad_policy_health(client, customer_id: str) -> list[AdPolicyRow]:
    """Heartbeat: модерация ENABLED-объявлений (approval_status). Impaired-статусы (дизапрув/
    ограничение) → объявление молча не показывается — тихая потеря без расхода (флагует движок аудита).
    Серверный фильтр `!= 'APPROVED'` режет основную массу (одобренных); точный impaired-набор
    оставляет движок. Поля сверены по доке v24 (образец «Get all disapproved ads»). READ-ONLY."""
    ensure_read_allowed(customer_id)
    q = (
        "SELECT campaign.name, ad_group.name, ad_group_ad.ad.id, "
        "ad_group_ad.policy_summary.approval_status FROM ad_group_ad "
        "WHERE campaign.status = 'ENABLED' AND ad_group.status = 'ENABLED' "
        "AND ad_group_ad.status = 'ENABLED' "
        "AND ad_group_ad.policy_summary.approval_status != 'APPROVED' "
        "ORDER BY campaign.name LIMIT 1000"
    )
    out: list[AdPolicyRow] = []
    for r in _search(client, customer_id, q):
        out.append(
            AdPolicyRow(
                campaign=r.campaign.name,
                ad_group=r.ad_group.name,
                ad_id=str(r.ad_group_ad.ad.id),
                approval_status=_enum_name(r.ad_group_ad.policy_summary.approval_status),
            )
        )
    return out


def fetch_account_status(client, customer_id: str) -> str | None:
    """Heartbeat: customer.status аккаунта (ENABLED/SUSPENDED/CANCELED/CLOSED). SUSPENDED/CANCELED/
    CLOSED → показы остановлены (баннер-катастрофа в аудите). None при пустом ответе. READ-ONLY."""
    ensure_read_allowed(customer_id)
    q = "SELECT customer.status FROM customer LIMIT 1"
    for r in _search(client, customer_id, q):
        return _enum_name(r.customer.status)
    return None


@dataclass
class AdGroupStructureRow:
    campaign: str
    ad_group: str
    kw_count: int  # активных ключей в группе (структура: 5-15 норма, >20 «свалка», G03)
    rsa_count: int  # активных Responsive Search Ad в группе (≥2 норма, <2 — тонкое покрытие)


def normalize_keyword_text(text: str) -> str:
    """Ключ к дедупу текста ключевого слова (claude-ads G03 accuracy note): BROAD/PHRASE/EXACT одного
    и того же ключа — ОДИН ключ, а не три. Снимаем регистр и модификатор «+» legacy BMM, схлопываем
    пробелы: «+Купить  Обувь» == «купить обувь». НЕ путать с `_is_legacy_bmm` в audit/engine.py —
    там «+» как раз значимый сигнал, здесь он шум."""
    return " ".join(text.replace("+", " ").lower().split())


def fetch_adgroup_structure(client, customer_id: str, period) -> list[AdGroupStructureRow]:
    """Структура ENABLED ad group: число активных ключей + RSA (D, эвристики claude-ads G03/copilot).
    GAQL НЕ умеет COUNT/GROUP BY → тянем строки и считаем в КОДЕ по ad_group.id (два запроса:
    keyword_view для ключей, ad_group_ad для RSA). Поля публичны (сверить на TEST MCC). READ-ONLY.

    G03 accuracy note (Ф0, 2026-07-13): «свалку» считаем по ключам, которые РЕАЛЬНО КРУТИЛИСЬ —
    `metrics.impressions > 0` за период (потому и нужен period: у ad_group_criterion метрик нет,
    берём keyword_view) — и ДЕДУПИМ по тексту. Иначе спящий хвост и один ключ в трёх типах
    соответствия раздували счётчик → ложные срабатывания adgroup_bloat."""
    ensure_read_allowed(customer_id)
    names: dict[str, tuple[str, str]] = {}  # ad_group_id → (campaign, ad_group)
    kw_texts: dict[str, set[str]] = {}  # ad_group_id → уникальные нормализованные тексты ключей
    q_kw = (
        "SELECT ad_group.id, ad_group.name, campaign.name, ad_group_criterion.keyword.text "
        f"FROM keyword_view WHERE {period.gaql_between()} "
        "AND ad_group_criterion.status != 'REMOVED' AND ad_group.status = 'ENABLED' "
        "AND campaign.status = 'ENABLED' AND metrics.impressions > 0"
    )
    for r in _search(client, customer_id, q_kw):
        agid = str(r.ad_group.id)
        names.setdefault(agid, (r.campaign.name, r.ad_group.name))
        kw_texts.setdefault(agid, set()).add(
            normalize_keyword_text(r.ad_group_criterion.keyword.text)
        )
    kw = {agid: len(texts) for agid, texts in kw_texts.items()}
    rsa: dict[str, int] = {}
    q_ad = (
        "SELECT ad_group.id, ad_group.name, campaign.name, ad_group_ad.ad.type "
        "FROM ad_group_ad WHERE ad_group_ad.status = 'ENABLED' AND ad_group.status = 'ENABLED' "
        "AND campaign.status = 'ENABLED'"
    )
    for r in _search(client, customer_id, q_ad):
        agid = str(r.ad_group.id)
        names.setdefault(agid, (r.campaign.name, r.ad_group.name))
        if _enum_name(r.ad_group_ad.ad.type_) == "RESPONSIVE_SEARCH_AD":
            rsa[agid] = rsa.get(agid, 0) + 1
        else:
            rsa.setdefault(agid, 0)
    return [
        AdGroupStructureRow(
            campaign=names[agid][0],
            ad_group=names[agid][1],
            kw_count=kw.get(agid, 0),
            rsa_count=rsa.get(agid, 0),
        )
        for agid in names
    ]


@dataclass
class NegativeListsInfo:
    count: int  # число ENABLED shared_set типа NEGATIVE_KEYWORDS в аккаунте
    attached_campaigns: int  # сколько кампаний имеют привязанный негатив-СПИСОК
    campaign_level_count: int = 0  # G15: число минус-ключей, заданных ПРЯМО на кампании
    # Имена кампаний с ЛЮБЫМИ минусами (список ИЛИ прямые минус-ключи). Пустой frozenset ≠ None:
    # пустой = «прочитали, минусов нет»; None-поля тут не бывает (гейт на уровне ctx.negative_lists).
    campaigns_with_negatives: frozenset[str] = frozenset()


def fetch_negative_lists(client, customer_id: str) -> NegativeListsInfo:
    """Минус-слова аккаунта: shared-списки (G14) + минусы ПРЯМО на кампании (G15) + карта покрытия
    по кампаниям. Нет НИ ОДНОГО из двух при активном keyword-трафике → красный флаг гигиены.
    member_count НЕ селектим (в v24 сверить отдельно) — для сигнала достаточно факта наличия.

    G14/G15 accuracy note (Ф0, 2026-07-13): раньше смотрели ТОЛЬКО shared_set → аккаунт с минусами
    на уровне кампаний (нормальная практика для одиночных кампаний) получал ложное «гигиены нет».
    Карта `campaigns_with_negatives` нужна ещё и G17 (BROAD под Smart Bidding без минусов). READ-ONLY."""
    ensure_read_allowed(customer_id)
    q_sets = (
        "SELECT shared_set.id FROM shared_set "
        "WHERE shared_set.type = 'NEGATIVE_KEYWORDS' AND shared_set.status = 'ENABLED'"
    )
    count = sum(1 for _ in _search(client, customer_id, q_sets))
    # Привязка списков к кампаниям. Тип фильтруем В КОДЕ (не в WHERE): у campaign_shared_set
    # к кампании могут висеть и PLACEMENT_EXCLUSION-сеты — они минус-СЛОВАМИ не являются.
    q_att = (
        "SELECT campaign.name, shared_set.type FROM campaign_shared_set "
        "WHERE campaign_shared_set.status = 'ENABLED'"
    )
    attached = {
        r.campaign.name
        for r in _search(client, customer_id, q_att)
        if _enum_name(r.shared_set.type_) == "NEGATIVE_KEYWORDS"
    }
    # G15: минус-ключи прямо на кампании. `campaign_criterion.negative` фильтруем в КОДЕ — поле
    # селектится всегда, а его фильтруемость в WHERE зависит от ресурса (не гадаем, читаем и считаем).
    q_neg = (
        "SELECT campaign.name, campaign_criterion.negative FROM campaign_criterion "
        "WHERE campaign_criterion.type = 'KEYWORD' AND campaign_criterion.status != 'REMOVED'"
    )
    direct: dict[str, int] = {}
    for r in _search(client, customer_id, q_neg):
        if r.campaign_criterion.negative:
            direct[r.campaign.name] = direct.get(r.campaign.name, 0) + 1
    return NegativeListsInfo(
        count=count,
        attached_campaigns=len(attached),
        campaign_level_count=sum(direct.values()),
        campaigns_with_negatives=frozenset(attached | set(direct)),
    )


@dataclass
class NegativeKeywordRow:
    """Ф9: один минус-ключ С ТЕКСТОМ (в отличие от NegativeListsInfo, где только счётчики).
    scope: "campaign" (минус прямо на кампании) | "ad_group" | "shared" (в shared-списке;
    campaign тогда пуст, имя списка — в list_name)."""

    scope: str
    campaign: str
    ad_group: str
    text: str
    match_type: str
    list_name: str = ""


NEGATIVE_KEYWORDS_LIMIT = 5000
"""Потолок строк ПО КАЖДОМУ уровню минусов. Упор в него режет только полноту конфликт-чека
(пропущенный конфликт = пропущенная находка, не ложная — в отличие от keyword_inventory)."""


@dataclass
class NegativeKeywordsInfo:
    """Ф9: минус-слова трёх уровней + карта привязки shared-списков к кампаниям (имя списка →
    имена кампаний). Пустые списки ≠ None: «прочитали, минусов нет» — валидный факт."""

    rows: list[NegativeKeywordRow] = field(default_factory=list)
    shared_attachments: dict[str, frozenset] = field(default_factory=dict)


def fetch_negative_keywords(
    client, customer_id: str, limit: int = NEGATIVE_KEYWORDS_LIMIT
) -> NegativeKeywordsInfo:
    """Ф9 (конфликты минусов): тексты минус-ключей всех трёх уровней — прямо на кампании, на группе,
    в shared-списках (+ какая кампания к какому списку привязана). Запросы сверены живой пробой на
    Draft (v24, 2026-07-17): `ad_group_criterion.negative` фильтруем в WHERE (принят живым API —
    без него пришлось бы тянуть ВСЕ ключи аккаунта); `campaign_criterion.negative` — в КОДЕ
    (как в fetch_negative_lists). READ-ONLY."""
    ensure_read_allowed(customer_id)
    lim = int(limit)
    rows: list[NegativeKeywordRow] = []
    q_camp = (
        "SELECT campaign.name, campaign_criterion.negative, campaign_criterion.keyword.text, "
        "campaign_criterion.keyword.match_type FROM campaign_criterion "
        "WHERE campaign_criterion.type = 'KEYWORD' AND campaign_criterion.status != 'REMOVED' "
        f"LIMIT {lim}"
    )
    for r in _search(client, customer_id, q_camp):
        if r.campaign_criterion.negative:
            rows.append(
                NegativeKeywordRow(
                    scope="campaign",
                    campaign=r.campaign.name,
                    ad_group="",
                    text=r.campaign_criterion.keyword.text,
                    match_type=_enum_name(r.campaign_criterion.keyword.match_type),
                )
            )
    q_ag = (
        "SELECT campaign.name, ad_group.name, ad_group_criterion.keyword.text, "
        "ad_group_criterion.keyword.match_type FROM ad_group_criterion "
        "WHERE ad_group_criterion.type = 'KEYWORD' AND ad_group_criterion.status != 'REMOVED' "
        f"AND ad_group_criterion.negative = TRUE LIMIT {lim}"
    )
    for r in _search(client, customer_id, q_ag):
        rows.append(
            NegativeKeywordRow(
                scope="ad_group",
                campaign=r.campaign.name,
                ad_group=r.ad_group.name,
                text=r.ad_group_criterion.keyword.text,
                match_type=_enum_name(r.ad_group_criterion.keyword.match_type),
            )
        )
    q_shared = (
        "SELECT shared_set.name, shared_criterion.keyword.text, "
        "shared_criterion.keyword.match_type FROM shared_criterion "
        f"WHERE shared_criterion.type = 'KEYWORD' LIMIT {lim}"
    )
    for r in _search(client, customer_id, q_shared):
        rows.append(
            NegativeKeywordRow(
                scope="shared",
                campaign="",
                ad_group="",
                text=r.shared_criterion.keyword.text,
                match_type=_enum_name(r.shared_criterion.keyword.match_type),
                list_name=r.shared_set.name,
            )
        )
    # Привязка списков: тип фильтруем В КОДЕ (PLACEMENT_EXCLUSION-сеты — не минус-слова),
    # как в fetch_negative_lists.
    q_att = (
        "SELECT campaign.name, shared_set.name, shared_set.type FROM campaign_shared_set "
        "WHERE campaign_shared_set.status = 'ENABLED'"
    )
    attach: dict[str, set] = {}
    for r in _search(client, customer_id, q_att):
        if _enum_name(r.shared_set.type_) == "NEGATIVE_KEYWORDS":
            attach.setdefault(r.shared_set.name, set()).add(r.campaign.name)
    return NegativeKeywordsInfo(
        rows=rows,
        shared_attachments={k: frozenset(v) for k, v in attach.items()},
    )


@dataclass
class KeywordQualityRow:
    campaign: str
    ad_group: str
    keyword: str
    quality_score: int  # 1-10; 0 → нет данных (proto3-zero, гейтит движок аудита)
    expected_ctr: str  # BELOW_AVERAGE/AVERAGE/ABOVE_AVERAGE (search_predicted_ctr)
    ad_relevance: str  # creative_quality_score
    landing_page: str  # post_click_quality_score
    metrics: Metrics


def fetch_keyword_quality(
    client, customer_id: str, period, limit: int = 500
) -> list[KeywordQualityRow]:
    """Quality Score активных ключей + 3 компонента (Expected CTR / Ad Relevance / Landing Page) —
    B (claude-ads G20-G24 + Hassanelsisi). quality_score часто 0 (proto3, мало данных) → гейтит движок.
    date ТОЛЬКО в WHERE. Топ по расходу (анти-хвост). Поля публичны (сверить на TEST MCC). READ-ONLY."""
    ensure_read_allowed(customer_id)
    q = (
        "SELECT campaign.name, ad_group.name, ad_group_criterion.keyword.text, "
        "ad_group_criterion.quality_info.quality_score, "
        "ad_group_criterion.quality_info.search_predicted_ctr, "
        "ad_group_criterion.quality_info.creative_quality_score, "
        "ad_group_criterion.quality_info.post_click_quality_score, "
        f"{_METRICS_SELECT} FROM keyword_view WHERE {_where(period, None)} "
        "AND ad_group_criterion.status = 'ENABLED' AND ad_group.status = 'ENABLED' "
        f"AND campaign.status = 'ENABLED' ORDER BY metrics.cost_micros DESC LIMIT {int(limit)}"
    )
    out: list[KeywordQualityRow] = []
    for r in _search(client, customer_id, q):
        qi = r.ad_group_criterion.quality_info
        out.append(
            KeywordQualityRow(
                campaign=r.campaign.name,
                ad_group=r.ad_group.name,
                keyword=r.ad_group_criterion.keyword.text,
                quality_score=int(getattr(qi, "quality_score", 0) or 0),
                expected_ctr=_enum_name(qi.search_predicted_ctr),
                ad_relevance=_enum_name(qi.creative_quality_score),
                landing_page=_enum_name(qi.post_click_quality_score),
                metrics=_metrics(r.metrics),
            )
        )
    return out


@dataclass
class KeywordInventoryRow:
    """Ф4: активный ключ КАК ОН ЕСТЬ — вместе с нулями. Именно ради нулей отдельный фетчер:
    `fetch_keyword_quality` берёт топ по расходу, а ключ с 0 показов расхода не имеет by design —
    из такой выборки он выпадает первым, хотя он и есть предмет чека `zero_impression_keywords`."""

    campaign: str
    ad_group: str
    keyword: str
    match_type: str
    metrics: Metrics


KEYWORD_INVENTORY_LIMIT = 10000
"""Потолок строк инвентаря ключей. Достигнут ⇒ список УСЕЧЁН и как «свои ключи» непригоден
(см. докстринг `fetch_keyword_inventory`): вызывающий обязан пометить `keyword_inventory_truncated`."""


def fetch_keyword_inventory(
    client, customer_id: str, period, limit: int = KEYWORD_INVENTORY_LIMIT
) -> list[KeywordInventoryRow]:
    """Полный список ENABLED-ключей за период — БЕЗ `ORDER BY cost` (иначе нули отсекаются) и без
    метрик-фильтров. Нужен трём чекам: нулевые показы, каннибализация (один ключ в разных группах),
    и как «список своих ключей» для майнинга запросов (что уже есть — не собирать, что своё — не
    минусовать).

    ⚠️ Усечение по LIMIT НЕ безобидно (ревизия волны): в майнинге инвентарь — ОТРИЦАТЕЛЬНЫЙ фильтр
    («чего у меня нет»), поэтому недосчёт списка = ПЕРЕсчёт совета: `keyword_harvest` предложит
    собрать уже собранное, а `ngram_waste` — заминусовать слово, входящее в СВОЙ же ключ (применить =
    убить трафик). Поэтому при `len(rows) >= limit` вызывающий помечает `keyword_inventory_truncated`
    и эти два чека МОЛЧАТ (нет данных ≠ ноль, GR8). READ-ONLY."""
    ensure_read_allowed(customer_id)
    q = (
        "SELECT campaign.name, ad_group.name, ad_group_criterion.keyword.text, "
        "ad_group_criterion.keyword.match_type, "
        f"{_METRICS_SELECT} FROM keyword_view WHERE {_where(period, None)} "
        "AND ad_group_criterion.status = 'ENABLED' AND ad_group.status = 'ENABLED' "
        f"AND campaign.status = 'ENABLED' LIMIT {int(limit)}"
    )
    return [
        KeywordInventoryRow(
            campaign=r.campaign.name,
            ad_group=r.ad_group.name,
            keyword=r.ad_group_criterion.keyword.text,
            match_type=_enum_name(r.ad_group_criterion.keyword.match_type),
            metrics=_metrics(r.metrics),
        )
        for r in _search(client, customer_id, q)
    ]


@dataclass
class GeoWasteRow:
    campaign: str
    region: str  # человекочитаемое имя локации (canonical) или id (fallback)
    metrics: Metrics


def _resolve_geo_names(client, customer_id: str, resource_names: set) -> dict:
    """geo_target_constant.resource_name → canonical_name (best-effort). Сбой/пусто → {}. READ-ONLY."""
    if not resource_names:
        return {}
    ids = ", ".join(f"'{rn}'" for rn in resource_names if rn)
    if not ids:
        return {}
    q = (
        "SELECT geo_target_constant.resource_name, geo_target_constant.canonical_name "
        f"FROM geo_target_constant WHERE geo_target_constant.resource_name IN ({ids})"
    )
    out: dict = {}
    try:
        for r in _search(client, customer_id, q):
            out[r.geo_target_constant.resource_name] = r.geo_target_constant.canonical_name
    except Exception:  # noqa: BLE001 — имена не критичны, покажем id
        return {}
    return out


def fetch_geo_waste(client, customer_id: str, period, limit: int = 200) -> list[GeoWasteRow]:
    """Слив по ГЕО: geographic_view сегментированный ПО КАМПАНИИ и региону (A). Фильтр
    location_type='LOCATION_OF_PRESENCE' ОБЯЗАТЕЛЕН — иначе LOP+AOI задваивают расход региона. Имя
    региона резолвим best-effort. campaign.name → target_campaign (для дедупа: cost региона ⊂ cost
    кампании). Поля публичны (сверить на TEST MCC). READ-ONLY."""
    ensure_read_allowed(customer_id)
    # campaign.status в SELECT обязателен: v24 для geographic_view требует фильтруемый атрибут
    # кампании в SELECT («must be present in SELECT clause», живая проба 2026-07-17) — view-специфично
    # (ad_group_ad/keyword_view так не требуют).
    q = (
        "SELECT campaign.name, campaign.status, segments.geo_target_region, "
        f"{_METRICS_SELECT} FROM geographic_view "
        f"WHERE {_where(period, None)} AND geographic_view.location_type = 'LOCATION_OF_PRESENCE' "
        f"AND campaign.status = 'ENABLED' ORDER BY metrics.cost_micros DESC LIMIT {int(limit)}"
    )
    raw: list[tuple] = []
    region_res: set = set()
    for r in _search(client, customer_id, q):
        res = getattr(r.segments, "geo_target_region", "") or ""
        raw.append((r.campaign.name, res, _metrics(r.metrics)))
        if res:
            region_res.add(res)
    names = _resolve_geo_names(client, customer_id, region_res)
    return [
        GeoWasteRow(
            campaign=c,
            region=names.get(res, res.split("/")[-1] if res else "—"),
            metrics=m,
        )
        for c, res, m in raw
    ]


@dataclass
class ScheduleCell:
    campaign: str
    day_of_week: str
    hour: int
    metrics: Metrics


def fetch_schedule(client, customer_id: str, period, limit: int = 500) -> list[ScheduleCell]:
    """Расписание: campaign × segments.day_of_week × segments.hour (A, heatmap час×день) — временные
    ячейки со сливом. Поля публичны (сверить на TEST MCC). READ-ONLY."""
    ensure_read_allowed(customer_id)
    q = (
        "SELECT campaign.name, segments.day_of_week, segments.hour, "
        f"{_METRICS_SELECT} FROM campaign "
        f"WHERE {_where(period, None)} AND campaign.status = 'ENABLED' "
        f"ORDER BY metrics.cost_micros DESC LIMIT {int(limit)}"
    )
    return [
        ScheduleCell(
            campaign=r.campaign.name,
            day_of_week=_enum_name(r.segments.day_of_week),
            hour=int(getattr(r.segments, "hour", 0) or 0),
            metrics=_metrics(r.metrics),
        )
        for r in _search(client, customer_id, q)
    ]


@dataclass
class DevicePerformanceRow:
    campaign: str
    device: str  # MOBILE / DESKTOP / TABLET / CONNECTED_TV / OTHER (segments.device)
    metrics: Metrics


def fetch_device_performance(
    client, customer_id: str, period, limit: int = 500
) -> list[DevicePerformanceRow]:
    """Ф9: расход campaign × segments.device — разрыв CPA между устройствами (слив на mobile при
    конвертящем desktop и наоборот). Запрос сверен живой пробой на Draft (v24, 2026-07-17).
    READ-ONLY."""
    ensure_read_allowed(customer_id)
    q = (
        "SELECT campaign.name, campaign.status, segments.device, "
        f"{_METRICS_SELECT} FROM campaign "
        f"WHERE {_where(period, None)} AND campaign.status = 'ENABLED' "
        f"ORDER BY metrics.cost_micros DESC LIMIT {int(limit)}"
    )
    return [
        DevicePerformanceRow(
            campaign=r.campaign.name,
            device=_enum_name(r.segments.device),
            metrics=_metrics(r.metrics),
        )
        for r in _search(client, customer_id, q)
    ]


@dataclass
class AdRotationRow:
    campaign: str
    ad_group: str
    rotation_mode: str  # OPTIMIZE / ROTATE_FOREVER (ad_group.ad_rotation_mode)


def fetch_ad_rotation(client, customer_id: str) -> list[AdRotationRow]:
    """Ф9: режим ротации объявлений ENABLED-групп. ROTATE_FOREVER выключает оптимизацию показа
    Google (лучшее объявление не выигрывает чаще) — почти всегда наследие A/B-теста, который забыли
    закрыть. Запрос сверен живой пробой на Draft (v24, 2026-07-17). READ-ONLY."""
    ensure_read_allowed(customer_id)
    q = (
        "SELECT campaign.name, ad_group.name, ad_group.ad_rotation_mode FROM ad_group "
        "WHERE ad_group.status = 'ENABLED' AND campaign.status = 'ENABLED'"
    )
    return [
        AdRotationRow(
            campaign=r.campaign.name,
            ad_group=r.ad_group.name,
            rotation_mode=_enum_name(r.ad_group.ad_rotation_mode),
        )
        for r in _search(client, customer_id, q)
    ]


@dataclass
class BidLandscapeRow:
    """Ставка ключа + оценки позиций Google + доли ВЕРХНИХ позиций. Все деньги — в валюте аккаунта
    (micros делит КОД). 0.0 в любом поле = «нет данных» (proto3-zero неотличим от истинного нуля) —
    разбирают проверки движка, здесь не выдумываем."""

    campaign: str
    ad_group: str
    ad_group_id: str
    criterion_id: str
    keyword: str
    match_type: str
    strategy_type: (
        str  # campaign.bidding_strategy_type — «поднять ставку» осмысленно только на ручных
    )
    bid: float  # ad_group_criterion.effective_cpc_bid (фактическая ставка ключа: своя или группы)
    first_page_cpc: float  # оценка Google: ставка для попадания на первую страницу
    top_of_page_cpc: float  # …для верха страницы (над органикой)
    first_position_cpc: float  # …для первой позиции
    top_is: float  # 0..1 metrics.search_top_impression_share — доля показов НА ВЕРХУ
    abs_top_is: float  # 0..1 metrics.search_absolute_top_impression_share
    rank_lost_top_is: (
        float  # 0..1 metrics.search_rank_lost_top_impression_share — верх потерян по РАНГУ
    )
    metrics: Metrics


# Доли ВЕРХНИХ позиций на уровне ключа. Отдельной константой — фетчер деградирует без них (см. ниже).
_KW_TOP_IS_SELECT = (
    "metrics.search_top_impression_share, metrics.search_absolute_top_impression_share, "
    "metrics.search_rank_lost_top_impression_share"
)


def _bid_landscape_query(period, limit: int, with_top_is: bool) -> str:
    is_part = f"{_KW_TOP_IS_SELECT}, " if with_top_is else ""
    return (
        "SELECT campaign.name, campaign.bidding_strategy_type, ad_group.id, ad_group.name, "
        "ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, "
        "ad_group_criterion.keyword.match_type, ad_group_criterion.effective_cpc_bid_micros, "
        "ad_group_criterion.position_estimates.first_page_cpc_micros, "
        "ad_group_criterion.position_estimates.top_of_page_cpc_micros, "
        "ad_group_criterion.position_estimates.first_position_cpc_micros, "
        f"{is_part}{_METRICS_SELECT} FROM keyword_view WHERE {_where(period, None)} "
        "AND ad_group_criterion.status = 'ENABLED' AND ad_group.status = 'ENABLED' "
        f"AND campaign.status = 'ENABLED' ORDER BY metrics.cost_micros DESC LIMIT {int(limit)}"
    )


def fetch_bid_landscape(
    client, customer_id: str, period, limit: int = 200
) -> list[BidLandscapeRow]:
    """Ф1: ставка ключа + оценки позиций Google (`position_estimates`) + доли верхних позиций —
    основа ответа «какие слова поднять». READ-ONLY.

    Топ по расходу (анти-хвост): ключи с НУЛЕВЫМ расходом сюда не попадают by design — «ключ без
    показов» (G-KW1) требует отдельного запроса без ORDER BY cost (Ф4), не смешиваем.

    Деградация: доли верхних позиций на уровне ключа поддерживаются не всеми аккаунтами/каналами —
    если сервер отвергнет их, повторяем запрос БЕЗ них (ставки/оценки позиций важнее и не должны
    падать заодно). Доли тогда 0.0 → проверки по рангу молчат (нет данных ≠ «в норме», gr8)."""
    ensure_read_allowed(customer_id)
    try:
        rows = list(_search(client, customer_id, _bid_landscape_query(period, limit, True)))
    except Exception:  # noqa: BLE001 — top-IS необязателен; ставки/оценки позиций читаем всё равно
        rows = list(_search(client, customer_id, _bid_landscape_query(period, limit, False)))
    out: list[BidLandscapeRow] = []
    for r in rows:
        agc = r.ad_group_criterion
        pe = agc.position_estimates
        out.append(
            BidLandscapeRow(
                campaign=r.campaign.name,
                ad_group=r.ad_group.name,
                ad_group_id=str(r.ad_group.id),
                criterion_id=str(agc.criterion_id),
                keyword=agc.keyword.text,
                match_type=_enum_name(agc.keyword.match_type),
                strategy_type=_enum_name(r.campaign.bidding_strategy_type),
                bid=int(getattr(agc, "effective_cpc_bid_micros", 0) or 0) / 1_000_000,
                first_page_cpc=int(getattr(pe, "first_page_cpc_micros", 0) or 0) / 1_000_000,
                top_of_page_cpc=int(getattr(pe, "top_of_page_cpc_micros", 0) or 0) / 1_000_000,
                first_position_cpc=int(getattr(pe, "first_position_cpc_micros", 0) or 0)
                / 1_000_000,
                top_is=float(getattr(r.metrics, "search_top_impression_share", 0.0) or 0.0),
                abs_top_is=float(
                    getattr(r.metrics, "search_absolute_top_impression_share", 0.0) or 0.0
                ),
                rank_lost_top_is=float(
                    getattr(r.metrics, "search_rank_lost_top_impression_share", 0.0) or 0.0
                ),
                metrics=_metrics(r.metrics),
            )
        )
    return out


@dataclass
class SimPoint:
    """Одна точка симулятора Google: «если ставка/бюджет = amount, то будет столько-то». Прогноз
    САМОГО Google (не наша модель) на его недавних данных — деньги в валюте аккаунта."""

    amount: float  # cpc_bid (CPC_BID) или дневной бюджет (BUDGET)
    clicks: float
    cost: float
    impressions: float
    conversions: float  # biddable_conversions
    top_slot_impressions: float


@dataclass
class BidSimulation:
    """CPC_BID-симулятор КЛЮЧА. Google отдаёт его только там, где данных хватило, — пустой список
    это НОРМА (мало трафика), а не сбой чтения: в data_gaps не кладём."""

    ad_group_id: str
    criterion_id: str
    points: list[SimPoint]


@dataclass
class BudgetSimulation:
    """BUDGET-симулятор КАМПАНИИ + её текущий дневной бюджет (нужен как точка отсчёта: без него
    неизвестно, какая из точек «сейчас»)."""

    campaign_id: str
    campaign: str
    current_budget: float
    points: list[SimPoint]


def _sim_points(point_list, amount_field: str) -> list[SimPoint]:
    """Точки симулятора → SimPoint, отсортированные по amount. micros делит КОД (gr4)."""
    out = [
        SimPoint(
            amount=int(getattr(p, amount_field, 0) or 0) / 1_000_000,
            clicks=float(getattr(p, "clicks", 0) or 0),
            cost=int(getattr(p, "cost_micros", 0) or 0) / 1_000_000,
            impressions=float(getattr(p, "impressions", 0) or 0),
            conversions=float(getattr(p, "biddable_conversions", 0.0) or 0.0),
            top_slot_impressions=float(getattr(p, "top_slot_impressions", 0) or 0),
        )
        for p in getattr(point_list, "points", []) or []
    ]
    return sorted(out, key=lambda p: p.amount)


def fetch_bid_simulations(client, customer_id: str, limit: int = 200) -> list[BidSimulation]:
    """Ф1: CPC_BID-симуляторы на уровне КЛЮЧА — «+X% ставки → +N конверсий» цифрами Google. READ-ONLY.

    `modification_method = UNIFORM` → точки несут АБСОЛЮТНУЮ ставку (`cpc_bid_micros`); SCALING дал бы
    только множитель, из которого конкретную ставку не назовёшь. Симулятор ключа Google строит лишь
    для ручных стратегий и при достаточных данных — пустой ответ ожидаем (на TEST MCC он будет пуст).

    Периода тут нет by design: у ресурса своё окно (`start_date`/`end_date`, обычно последние 7 дней),
    сегментации по датам он не поддерживает."""
    ensure_read_allowed(customer_id)
    q = (
        "SELECT ad_group_criterion_simulation.ad_group_id, "
        "ad_group_criterion_simulation.criterion_id, "
        "ad_group_criterion_simulation.cpc_bid_point_list.points "
        "FROM ad_group_criterion_simulation "
        "WHERE ad_group_criterion_simulation.type = 'CPC_BID' "
        f"AND ad_group_criterion_simulation.modification_method = 'UNIFORM' LIMIT {int(limit)}"
    )
    out: list[BidSimulation] = []
    for r in _search(client, customer_id, q):
        s = r.ad_group_criterion_simulation
        points = _sim_points(s.cpc_bid_point_list, "cpc_bid_micros")
        if points:
            out.append(
                BidSimulation(
                    ad_group_id=str(s.ad_group_id),
                    criterion_id=str(s.criterion_id),
                    points=points,
                )
            )
    return out


@dataclass
class RsaAdRow:
    """Ф3: адаптивное поисковое объявление ЦЕЛИКОМ — тексты, пины, оценка Google, возраст — плюс
    ключи своей группы. Всё, чем «текст объявления» проверяется против «ключей, которые платят».

    Пустые значения означают «не прочитано», а не «ноль» (gr8): `ad_strength=""` — сервер не отдал
    поле, `age_days=-1` — нет даты старта показа (0 — это «запущено сегодня», совсем другое)."""

    campaign: str
    ad_group: str
    ad_group_id: str
    ad_id: str
    headlines: list[str]
    descriptions: list[str]
    pinned_headline_positions: frozenset[str]  # {"HEADLINE_1", …} — реально закреплённые позиции
    pinned_headlines: int  # сколько заголовков закреплено (пин убивает ротацию комбинаций)
    ad_strength: str  # POOR | AVERAGE | GOOD | EXCELLENT | PENDING | ""(не прочитано)
    age_days: int  # дней с начала показа; -1 = дата не прочитана
    metrics: Metrics
    keywords: list[str] = field(default_factory=list)  # ключи ГРУППЫ (второй запрос)


def _rsa_query(period, limit: int, with_dates: bool) -> str:
    dates = "ad_group_ad.start_date_time, " if with_dates else ""
    return (
        "SELECT campaign.name, ad_group.id, ad_group.name, ad_group_ad.ad.id, "
        f"ad_group_ad.ad_strength, {dates}"
        "ad_group_ad.ad.responsive_search_ad.headlines, "
        "ad_group_ad.ad.responsive_search_ad.descriptions, "
        f"{_METRICS_SELECT} FROM ad_group_ad WHERE {_where(period, None)} "
        "AND ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD' AND ad_group_ad.status = 'ENABLED' "
        "AND ad_group.status = 'ENABLED' AND campaign.status = 'ENABLED' "
        f"ORDER BY metrics.cost_micros DESC LIMIT {int(limit)}"
    )


def _age_days(started: str) -> int:
    """Дней с начала показа объявления. Пустая/нечитаемая дата → -1 («не знаем», не «сегодня»).
    Google отдаёт `start_date_time` как «YYYY-MM-DD HH:MM:SS» в таймзоне аккаунта — берём дату.

    ⚠️ У БОЛЬШИНСТВА объявлений это поле ПУСТО (см. `fetch_rsa_assets`) ⇒ -1 — норма, а не сбой."""
    head = (started or "")[:10]
    try:
        return max(0, (date.today() - date.fromisoformat(head)).days)
    except ValueError:
        return -1


def _rsa_keywords(client, customer_id: str, ad_group_ids: set[str]) -> dict[str, list[str]]:
    """Ключи ENABLED-групп (позитивные) → {ad_group_id: [текст, …]}. Нужны, чтобы проверить, ВХОДЯТ
    ли слова, за которые платим, в заголовки объявления (G35). Метрик тут нет by design: покрытие —
    свойство текста, а не трафика; отсев «платящих» групп делает движок по метрикам объявления.

    ⚠️ Сужаем ЗАПРОСОМ (`ad_group.id IN …`), а не пост-фильтром (ревизия волны): аккаунт-wide выборка
    под LIMIT на большом аккаунте вырезала бы ключи именно топ-спендовых групп (порядок не задан) —
    и `rsa_keyword_coverage_low` обвинял бы объявление в непокрытии ключей, которых просто не увидел.
    Батчами по 200 id: длина GAQL не безгранична."""
    out: dict[str, list[str]] = {}
    ids = sorted(str(a) for a in ad_group_ids if a)
    for i in range(0, len(ids), 200):
        batch = ", ".join(ids[i : i + 200])
        q = (
            "SELECT ad_group.id, ad_group_criterion.keyword.text FROM ad_group_criterion "
            "WHERE ad_group_criterion.type = 'KEYWORD' AND ad_group_criterion.negative = FALSE "
            "AND ad_group_criterion.status = 'ENABLED' AND ad_group.status = 'ENABLED' "
            f"AND campaign.status = 'ENABLED' AND ad_group.id IN ({batch})"
        )
        for r in _search(client, customer_id, q):
            out.setdefault(str(r.ad_group.id), []).append(r.ad_group_criterion.keyword.text)
    return out


def fetch_rsa_assets(client, customer_id: str, period, limit: int = 200) -> list[RsaAdRow]:
    """Ф3: тексты работающих RSA + пины + `ad_strength` + возраст + ключи их групп. READ-ONLY.

    Клон-флоу (`ads.read.read_campaign_config`) читает RSA по ИМЕНИ кампании и только первый
    в группе — для аудита не годится: нужен весь аккаунт, метрики (чтобы не флажить объявления
    без трафика) и пины. Топ по расходу — судим то, что реально жжёт деньги.

    Деградация: `ad_group_ad.start_date_time` поддерживается не везде — если сервер его отвергнет,
    повторяем запрос БЕЗ даты (тексты/пины/сила важнее). Тогда `age_days=-1` ⇒ чек «объявления не
    обновлялись» молчит: нет данных ≠ «в норме» (gr8).

    ⚠️ ЧЕСТНО О ВОЗРАСТЕ (ревизия волны, проверено по прото v24): `start_date_time` — это НЕ дата
    создания объявления, а ОГРАНИЧЕНИЕ РАСПИСАНИЯ («ad starts serving … on top of the campaign's
    start date. Only supported for some ad types») — у обычного RSA оно ПУСТОЕ. Значит `age_days`
    почти всегда -1, и `rsa_stale` на живом аккаунте молчит by design. Чтобы это молчание не читалось
    как «объявления свежие», сборщик (`audit/collect.py`) объявляет пробел `rsa_age`, когда RSA есть,
    а даты нет ни у одного. Настоящий возраст = `change_status` / `min(segments.date)` по объявлению —
    отдельное чтение, в эту волну НЕ входит."""
    ensure_read_allowed(customer_id)
    try:
        rows = list(_search(client, customer_id, _rsa_query(period, limit, True)))
    except Exception:  # noqa: BLE001 — дата опциональна; тексты/пины/ad_strength читаем всё равно
        rows = list(_search(client, customer_id, _rsa_query(period, limit, False)))
    out: list[RsaAdRow] = []
    for r in rows:
        rsa = r.ad_group_ad.ad.responsive_search_ad
        pins = [
            _enum_name(getattr(a, "pinned_field", ""))
            for a in rsa.headlines
            if _enum_name(getattr(a, "pinned_field", "")).startswith("HEADLINE_")
        ]
        strength = _enum_name(getattr(r.ad_group_ad, "ad_strength", ""))
        out.append(
            RsaAdRow(
                campaign=r.campaign.name,
                ad_group=r.ad_group.name,
                ad_group_id=str(r.ad_group.id),
                ad_id=str(r.ad_group_ad.ad.id),
                headlines=[a.text for a in rsa.headlines if getattr(a, "text", "")],
                descriptions=[a.text for a in rsa.descriptions if getattr(a, "text", "")],
                pinned_headline_positions=frozenset(pins),
                pinned_headlines=len(pins),
                ad_strength="" if strength in ("UNSPECIFIED", "UNKNOWN") else strength,
                age_days=_age_days(str(getattr(r.ad_group_ad, "start_date_time", "") or "")),
                metrics=_metrics(r.metrics),
            )
        )
    kw = _rsa_keywords(client, customer_id, {a.ad_group_id for a in out})
    for a in out:
        a.keywords = kw.get(a.ad_group_id, [])
    return out


def fetch_budget_simulations(client, customer_id: str, limit: int = 50) -> list[BudgetSimulation]:
    """Ф1: BUDGET-симуляторы кампаний — «+$B бюджета → +M конверсий» цифрами Google. READ-ONLY.

    Вторым запросом читаем ТЕКУЩИЙ дневной бюджет и имя кампании: в самом симуляторе есть только
    campaign_id и точки, а без «где мы сейчас» прирост считать не от чего. `modification_method`
    не фильтруем — точка бюджета несёт абсолютную сумму при любом методе."""
    ensure_read_allowed(customer_id)
    q = (
        "SELECT campaign_simulation.campaign_id, campaign_simulation.budget_point_list.points "
        f"FROM campaign_simulation WHERE campaign_simulation.type = 'BUDGET' LIMIT {int(limit)}"
    )
    sims: dict[str, list[SimPoint]] = {}
    for r in _search(client, customer_id, q):
        s = r.campaign_simulation
        points = _sim_points(s.budget_point_list, "budget_amount_micros")
        if points:
            sims[str(s.campaign_id)] = points
    if not sims:
        return []
    meta_q = (
        "SELECT campaign.id, campaign.name, campaign_budget.amount_micros "
        "FROM campaign WHERE campaign.status = 'ENABLED'"
    )
    out: list[BudgetSimulation] = []
    for r in _search(client, customer_id, meta_q):
        cid = str(r.campaign.id)
        points = sims.get(cid)
        if not points:
            continue
        out.append(
            BudgetSimulation(
                campaign_id=cid,
                campaign=r.campaign.name,
                current_budget=int(getattr(r.campaign_budget, "amount_micros", 0) or 0) / 1_000_000,
                points=points,
            )
        )
    return out


@dataclass
class CampaignBudgetRow:
    """Одна строка «дневной бюджет кампании» для MCP-обёртки get_budgets (Hermes-контур A).
    budget — amount_micros/1e6 в валюте аккаунта (делит КОД, не модель)."""

    campaign_id: str
    campaign: str
    channel_type: str
    status: str
    budget: float


def fetch_budgets(client, customer_id: str, limit: int = 500) -> list[CampaignBudgetRow]:
    """Дневные бюджеты кампаний аккаунта (campaign_budget.amount_micros → валюта). READ-ONLY.

    Лёгкий отдельный ридер (без симуляторов, в отличие от fetch_budget_simulations): просто «какой
    дневной бюджет задан по кампаниям», отсортировано по убыванию. REMOVED исключаем (мусор для
    оператора). Замок ЧТЕНИЯ — первой строкой (§8, как остальные fetch_*)."""
    ensure_read_allowed(customer_id)
    q = (
        "SELECT campaign.id, campaign.name, campaign.advertising_channel_type, "
        "campaign.status, campaign_budget.amount_micros FROM campaign "
        f"WHERE campaign.status != 'REMOVED' ORDER BY campaign_budget.amount_micros DESC "
        f"LIMIT {int(limit)}"
    )
    out: list[CampaignBudgetRow] = []
    for r in _search(client, customer_id, q):
        out.append(
            CampaignBudgetRow(
                campaign_id=str(r.campaign.id),
                campaign=r.campaign.name,
                channel_type=_enum_name(r.campaign.advertising_channel_type),
                status=_enum_name(r.campaign.status),
                budget=int(getattr(r.campaign_budget, "amount_micros", 0) or 0) / 1_000_000,
            )
        )
    return out


@dataclass
class CampaignAssetsRow:
    """Ф6 (G50/G51/G52): сколько расширений ДЕЙСТВУЕТ на кампании. Счётчик — сумма трёх уровней
    привязки (кампания + аккаунт + группы): ассет, привязанный на аккаунт, показывается во всех
    кампаниях, и не учесть его — значит написать «ситилинков нет» аккаунту, где они есть."""

    campaign_id: str
    campaign: str
    channel_type: str
    sitelinks: int
    callouts: int
    snippets: int


_ASSET_FIELDS = {"SITELINK": "sitelinks", "CALLOUT": "callouts", "STRUCTURED_SNIPPET": "snippets"}


def fetch_campaign_assets(client, customer_id: str) -> list[CampaignAssetsRow]:
    """Расширения ENABLED-кампаний: campaign_asset + customer_asset (аккаунт-уровень, действует на
    ВСЕ кампании) + ad_group_asset. Считаем ссылки, а не уникальные ассеты: один ассет, привязанный
    на двух уровнях, посчитается дважды — ошибка в сторону МОЛЧАНИЯ чека (порог «<4» не сработает),
    что честнее ложной находки «расширений нет». READ-ONLY."""
    ensure_read_allowed(customer_id)
    ft_filter = "('SITELINK', 'CALLOUT', 'STRUCTURED_SNIPPET')"

    # Аккаунт-уровень: действует на все кампании сразу.
    acct: dict[str, int] = {}
    cust_q = (
        "SELECT customer_asset.field_type FROM customer_asset "
        f"WHERE customer_asset.status = 'ENABLED' AND customer_asset.field_type IN {ft_filter}"
    )
    for r in _search(client, customer_id, cust_q):
        f = _ASSET_FIELDS.get(_enum_name(r.customer_asset.field_type))
        if f:
            acct[f] = acct.get(f, 0) + 1

    counts: dict[str, dict[str, int]] = {}
    for res, link in (("campaign_asset", "campaign_asset"), ("ad_group_asset", "ad_group_asset")):
        # campaign.status в SELECT — требование v24 для *_asset (живая проба 2026-07-17).
        q = (
            f"SELECT campaign.id, campaign.status, {link}.field_type FROM {res} "
            f"WHERE {link}.status = 'ENABLED' AND {link}.field_type IN {ft_filter} "
            "AND campaign.status = 'ENABLED'"
        )
        for r in _search(client, customer_id, q):
            f = _ASSET_FIELDS.get(_enum_name(getattr(r, link).field_type))
            if not f:
                continue
            c = counts.setdefault(str(r.campaign.id), {})
            c[f] = c.get(f, 0) + 1

    out: list[CampaignAssetsRow] = []
    meta_q = (
        "SELECT campaign.id, campaign.name, campaign.advertising_channel_type "
        "FROM campaign WHERE campaign.status = 'ENABLED'"
    )
    for r in _search(client, customer_id, meta_q):
        cid_ = str(r.campaign.id)
        c = counts.get(cid_, {})
        out.append(
            CampaignAssetsRow(
                campaign_id=cid_,
                campaign=r.campaign.name,
                channel_type=_enum_name(r.campaign.advertising_channel_type),
                sitelinks=c.get("sitelinks", 0) + acct.get("sitelinks", 0),
                callouts=c.get("callouts", 0) + acct.get("callouts", 0),
                snippets=c.get("snippets", 0) + acct.get("snippets", 0),
            )
        )
    return out


@dataclass
class CampaignSettingsRow:
    """Ф6.2 (G12/G11): настройки ENABLED-кампании, которые молча жгут бюджет, — И ДЕНЬГИ, которые они
    уже сожгли. Настройка без денег — это спор о галочке; настройка с деньгами — находка.

    content_* — расход/конверсии, пришедшие ИМЕННО с КМС внутри этой кампании
    (segments.ad_network_type = CONTENT).
    outside_* — расход/конверсии по кликам людей, физически находившихся ВНЕ таргетируемых регионов
    (user_location_view.targeting_location = FALSE). Ровно это и покупает гео-тип «присутствие ИЛИ
    интерес»: без такого замера чек был бы вкусовщиной («а вдруг бизнес национальный»).
    geo_target_type — «» когда Google не вернул значение ⇒ чек молчит (нет данных ≠ «настроено верно»).
    """

    campaign_id: str
    campaign: str
    channel_type: str
    search_network: bool
    content_network: bool
    geo_target_type: str
    content_cost: float = 0.0
    content_conversions: float = 0.0
    outside_cost: float = 0.0
    outside_conversions: float = 0.0


def _sum_by_campaign(client, customer_id: str, query: str, keep) -> dict[str, tuple[float, float]]:
    """(расход, конверсии) по campaign.id из сегментированного запроса; keep(row) решает, что считать."""
    acc: dict[str, tuple[float, float]] = {}
    for r in _search(client, customer_id, query):
        if not keep(r):
            continue
        cid_ = str(r.campaign.id)
        cost, conv = acc.get(cid_, (0.0, 0.0))
        acc[cid_] = (
            cost + int(r.metrics.cost_micros) / 1_000_000,
            conv + float(r.metrics.conversions),
        )
    return acc


def fetch_campaign_settings(client, customer_id: str, period) -> list[CampaignSettingsRow]:
    """Сети (поиск/партнёры/КМС) + тип гео-таргетинга ENABLED-кампаний + деньги, которые уже ушли
    в КМС и «в интерес».

    Обе сегментации (segments.ad_network_type, user_location_view) НЕ задваивают расход: сумма по
    сегментам = расход кампании. Значит content_cost/outside_cost ⊂ cost кампании, и дедуп в engine
    возьмёт по одному spend_segment (имя кампании) МАКСИМУМ, а не сумму. READ-ONLY."""
    ensure_read_allowed(customer_id)

    net = _sum_by_campaign(
        client,
        customer_id,
        "SELECT campaign.id, segments.ad_network_type, metrics.cost_micros, metrics.conversions "
        f"FROM campaign WHERE {_where(period, None)} AND campaign.status = 'ENABLED'",
        lambda r: _enum_name(r.segments.ad_network_type) == "CONTENT",
    )
    # Гео: клик засчитан кампании, а физическое место человека — вне таргета. targeting_location
    # False — это и есть «интерес», за который платят.
    outside = _sum_by_campaign(
        client,
        customer_id,
        # campaign.status в SELECT — требование v24 для user_location_view (живая проба 2026-07-17).
        "SELECT campaign.id, campaign.status, user_location_view.targeting_location, "
        f"metrics.cost_micros, metrics.conversions FROM user_location_view "
        f"WHERE {_where(period, None)} AND campaign.status = 'ENABLED'",
        lambda r: not bool(r.user_location_view.targeting_location),
    )

    out: list[CampaignSettingsRow] = []
    q = (
        "SELECT campaign.id, campaign.name, campaign.advertising_channel_type, "
        "campaign.network_settings.target_search_network, "
        "campaign.network_settings.target_content_network, "
        "campaign.geo_target_type_setting.positive_geo_target_type "
        "FROM campaign WHERE campaign.status = 'ENABLED'"
    )
    for r in _search(client, customer_id, q):
        c = r.campaign
        cid_ = str(c.id)
        cost, conv = net.get(cid_, (0.0, 0.0))
        out_cost, out_conv = outside.get(cid_, (0.0, 0.0))
        geo = _enum_name(getattr(c.geo_target_type_setting, "positive_geo_target_type", ""))
        out.append(
            CampaignSettingsRow(
                campaign_id=cid_,
                campaign=c.name,
                channel_type=_enum_name(c.advertising_channel_type),
                search_network=bool(c.network_settings.target_search_network),
                content_network=bool(c.network_settings.target_content_network),
                geo_target_type="" if geo in ("UNSPECIFIED", "UNKNOWN") else geo,
                content_cost=cost,
                content_conversions=conv,
                outside_cost=out_cost,
                outside_conversions=out_conv,
            )
        )
    return out


# ── Ф7: Performance Max ──────────────────────────────────────────────────────────────
# У PMax НЕТ ad_group / ad_group_ad / keyword_view: привычные срезы Search тут не существуют, а
# segments.keyword.* в SELECT молча ОТФИЛЬТРУЕТ все PMax-данные (дока по сегментации) — поэтому
# поисковые запросы берём из campaign_search_term_view, а не из search_term_view (fetch_search_terms
# PMax не видит вовсе). Метрики на asset_group — честная партиция кампании (сумма сходится); метрики
# на asset_group_asset НЕ суммируемы (один показ висит на КАЖДОМ ассете комбинации) — не читаем их.


@dataclass
class PmaxSearchTermRow:
    """Запрос, по которому показывалась PMax-кампания (campaign_search_term_view). Брендовость решает
    ДВИЖОК (токены бренда — из профиля клиента §20, а не из Google Ads).

    clicks — чтобы отличить «дорого и без результата» от «показы без кликов» (второе ставкой/минусом
    не лечится: денег на клик там не потрачено)."""

    search_term: str
    cost: float
    clicks: int
    conversions: float


@dataclass
class PmaxCampaignRow:
    """Ф7: PMax-кампания целиком — деньги за период + защита от мусорного трафика + брендовые запросы.

    negatives — ОБЪЕДИНЕНИЕ трёх уровней (минус-слова прямо на кампании + подключённые минус-списки +
    аккаунтные списки): аккаунтный список действует и на PMax, и не учесть его значит написать
    «минус-слов нет» аккаунту, где они есть.
    brand_exclusions — criterion типа BRAND/BRAND_LIST (SharedSet типа BRANDS). Механизм проверен по
    прото v24: бренд-исключения PMax живут в SharedSet, а НЕ в AssetSet (BRAND_LIST там нет).
    merchant_id — товарный фид (shopping_setting): для retail-PMax фид САМ является сигналом, поэтому
    «нет сигналов» без учёта фида было бы ложной находкой. "" ⇒ фида нет.
    days — длина периода: «мало конверсий» имеет смысл только в пересчёте на 30 дней.
    """

    campaign_id: str
    campaign: str
    cost: float
    clicks: int
    conversions: float
    days: int
    negatives: int
    brand_exclusions: int
    merchant_id: str = ""
    search_terms: list[PmaxSearchTermRow] = field(default_factory=list)


@dataclass
class PmaxAssetGroupRow:
    """Ф7: группа активов PMax + ЕЁ деньги (asset_group — честная партиция кампании).

    ad_strength — оценка САМОГО Google («» когда не вернул ⇒ чек молчит, GR8).
    action_items — [(тип ассета, сколько добавить)] из asset_coverage.ad_strength_action_items: Google
    прямым текстом говорит, чего не хватает. Это сильнее самодельной «плотности ассетов» — её и не
    считаем (порог «20 картинок» из головы дал бы ложные находки).
    videos / videos_auto — СВОИ видео и авто-сгенерированные Google (asset_group_asset.source =
    AUTOMATICALLY_CREATED). Считать их вместе — значит промолчать ровно там, где Google сам собрал
    видео из картинок вместо рекламодателя, то есть в самом плохом случае.
    """

    campaign_id: str
    campaign: str
    asset_group_id: str
    asset_group: str
    ad_strength: str
    cost: float
    clicks: int
    conversions: float
    headlines: int
    descriptions: int
    images: int
    logos: int
    videos: int
    videos_auto: int
    search_themes: int
    audiences: int
    action_items: list[tuple[str, int]] = field(default_factory=list)


_PMAX_CHANNEL = "campaign.advertising_channel_type = 'PERFORMANCE_MAX'"
# Ассеты PMax по смыслу, а не по одному enum: HEADLINE и LONG_HEADLINE — оба заголовки, логотип бывает
# трёх форм. Считаем группы, а не отдельные значения AssetFieldType.
_PMAX_ASSET_BUCKETS = {
    "HEADLINE": "headlines",
    "LONG_HEADLINE": "headlines",
    "DESCRIPTION": "descriptions",
    "MARKETING_IMAGE": "images",
    "SQUARE_MARKETING_IMAGE": "images",
    "PORTRAIT_MARKETING_IMAGE": "images",
    "TALL_PORTRAIT_MARKETING_IMAGE": "images",
    "LOGO": "logos",
    "LANDSCAPE_LOGO": "logos",
    "BUSINESS_LOGO": "logos",
    "YOUTUBE_VIDEO": "videos",
}
# Негативные criterion PMax: сами ключи, подключённый минус-список — и отдельно БРЕНД (другой смысл).
_NEG_KEYWORD_TYPES = ("KEYWORD", "NEGATIVE_KEYWORD_LIST")
_NEG_BRAND_TYPES = ("BRAND", "BRAND_LIST")


def fetch_pmax_campaigns(
    client, customer_id: str, period, limit: int = TOP_N
) -> list[PmaxCampaignRow]:
    """PMax-кампании: деньги за период + минус-слова (кампания ∪ списки ∪ аккаунт) + бренд-исключения
    + поисковые запросы (campaign_search_term_view). Пусто → PMax в аккаунте нет; чеки семьи молчат.

    Запросы к campaign_shared_set / customer_negative_criterion идут БЕЗ фильтра по каналу (матрица
    selectable_with для них не проверена вживую) — пересечение с PMax считает КОД. READ-ONLY."""
    ensure_read_allowed(customer_id)

    rows: dict[str, PmaxCampaignRow] = {}
    q = (
        "SELECT campaign.id, campaign.name, campaign.shopping_setting.merchant_id, "
        "metrics.cost_micros, metrics.clicks, metrics.conversions "
        f"FROM campaign WHERE {_where(period, None)} AND campaign.status = 'ENABLED' "
        f"AND {_PMAX_CHANNEL}"
    )
    for r in _search(client, customer_id, q):
        merchant = int(getattr(r.campaign.shopping_setting, "merchant_id", 0) or 0)
        rows[str(r.campaign.id)] = PmaxCampaignRow(
            campaign_id=str(r.campaign.id),
            campaign=r.campaign.name,
            cost=int(r.metrics.cost_micros) / 1_000_000,
            clicks=int(r.metrics.clicks),
            conversions=float(r.metrics.conversions),
            days=int(getattr(period, "days", 30) or 30),
            negatives=0,
            brand_exclusions=0,
            merchant_id=str(merchant) if merchant else "",
        )
    if not rows:
        return []

    # Минусы и бренд-исключения ПРЯМО на кампании.
    crit_q = (
        "SELECT campaign.id, campaign_criterion.type FROM campaign_criterion "
        f"WHERE {_PMAX_CHANNEL} AND campaign_criterion.negative = TRUE "
        "AND campaign_criterion.status != 'REMOVED'"
    )
    for r in _search(client, customer_id, crit_q):
        row = rows.get(str(r.campaign.id))
        if row is None:
            continue
        t = _enum_name(r.campaign_criterion.type_)
        if t in _NEG_KEYWORD_TYPES:
            row.negatives += 1
        elif t in _NEG_BRAND_TYPES:
            row.brand_exclusions += 1

    # Подключённые минус-списки (shared set) — по всем кампаниям, пересечение с PMax в коде.
    shared_q = (
        "SELECT campaign.id, shared_set.type FROM campaign_shared_set "
        "WHERE campaign_shared_set.status != 'REMOVED'"
    )
    for r in _search(client, customer_id, shared_q):
        row = rows.get(str(r.campaign.id))
        if row is not None and _enum_name(r.shared_set.type_) == "NEGATIVE_KEYWORDS":
            row.negatives += 1

    # Аккаунтные минус-списки действуют на ВСЕ кампании, включая PMax.
    acct_neg = sum(
        1
        for r in _search(
            client,
            customer_id,
            "SELECT customer_negative_criterion.type FROM customer_negative_criterion",
        )
        if _enum_name(r.customer_negative_criterion.type_) == "NEGATIVE_KEYWORD_LIST"
    )
    if acct_neg:
        for row in rows.values():
            row.negatives += acct_neg

    # Поисковые запросы PMax. segments.keyword.* сюда НЕ добавлять — он отфильтрует все PMax-строки.
    st_q = (
        "SELECT campaign.id, campaign_search_term_view.search_term, metrics.cost_micros, "
        f"metrics.clicks, metrics.conversions FROM campaign_search_term_view "
        f"WHERE {_where(period, None)} AND {_PMAX_CHANNEL} "
        f"ORDER BY metrics.cost_micros DESC LIMIT {int(limit)}"
    )
    for r in _search(client, customer_id, st_q):
        row = rows.get(str(r.campaign.id))
        if row is None:
            continue
        row.search_terms.append(
            PmaxSearchTermRow(
                search_term=r.campaign_search_term_view.search_term,
                cost=int(r.metrics.cost_micros) / 1_000_000,
                clicks=int(r.metrics.clicks),
                conversions=float(r.metrics.conversions),
            )
        )
    return list(rows.values())


def fetch_pmax_asset_groups(client, customer_id: str, period) -> list[PmaxAssetGroupRow]:
    """Группы активов PMax: сила объявлений от Google + что Google просит добавить + состав ассетов +
    поисковые темы + деньги группы. READ-ONLY."""
    ensure_read_allowed(customer_id)

    rows: dict[str, PmaxAssetGroupRow] = {}
    # Композит asset_group.asset_coverage в v24 не selectable («may not be used in SELECT clause»),
    # selectable — лист .ad_strength_action_items (метаданные GoogleAdsFieldService + живая проба
    # 2026-07-17); campaign.status в SELECT — то же view-требование, что у geographic_view.
    q = (
        "SELECT campaign.id, campaign.name, campaign.status, asset_group.id, asset_group.name, "
        "asset_group.ad_strength, asset_group.asset_coverage.ad_strength_action_items "
        f"FROM asset_group WHERE {_PMAX_CHANNEL} AND campaign.status = 'ENABLED' "
        "AND asset_group.status = 'ENABLED'"
    )
    for r in _search(client, customer_id, q):
        ag = r.asset_group
        strength = _enum_name(ag.ad_strength)
        items: list[tuple[str, int]] = []
        for ai in getattr(getattr(ag, "asset_coverage", None), "ad_strength_action_items", []):
            det = getattr(ai, "add_asset_details", None)
            ft = _enum_name(getattr(det, "asset_field_type", ""))
            n = int(getattr(det, "asset_count", 0) or 0)
            if ft and ft not in ("UNSPECIFIED", "UNKNOWN") and n > 0:
                items.append((ft, n))
        rows[str(ag.id)] = PmaxAssetGroupRow(
            campaign_id=str(r.campaign.id),
            campaign=r.campaign.name,
            asset_group_id=str(ag.id),
            asset_group=ag.name,
            ad_strength="" if strength in ("UNSPECIFIED", "UNKNOWN") else strength,
            cost=0.0,
            clicks=0,
            conversions=0.0,
            headlines=0,
            descriptions=0,
            images=0,
            logos=0,
            videos=0,
            videos_auto=0,
            search_themes=0,
            audiences=0,
            action_items=items,
        )
    if not rows:
        return []

    metrics_q = (
        "SELECT asset_group.id, metrics.cost_micros, metrics.clicks, metrics.conversions "
        f"FROM asset_group WHERE {_where(period, None)} AND {_PMAX_CHANNEL} "
        "AND asset_group.status = 'ENABLED'"
    )
    for r in _search(client, customer_id, metrics_q):
        row = rows.get(str(r.asset_group.id))
        if row is not None:
            row.cost += int(r.metrics.cost_micros) / 1_000_000
            row.clicks += int(r.metrics.clicks)
            row.conversions += float(r.metrics.conversions)

    assets_q = (
        "SELECT asset_group.id, asset_group_asset.field_type, asset_group_asset.source "
        "FROM asset_group_asset WHERE asset_group_asset.status = 'ENABLED'"
    )
    for r in _search(client, customer_id, assets_q):
        row = rows.get(str(r.asset_group.id))
        bucket = _PMAX_ASSET_BUCKETS.get(_enum_name(r.asset_group_asset.field_type))
        if row is None or not bucket:
            continue
        auto = _enum_name(getattr(r.asset_group_asset, "source", "")) == "AUTOMATICALLY_CREATED"
        if bucket == "videos" and auto:
            row.videos_auto += 1
        else:
            setattr(row, bucket, getattr(row, bucket) + 1)

    signals_q = (
        "SELECT asset_group.id, asset_group_signal.search_theme.text, "
        "asset_group_signal.audience.audience FROM asset_group_signal"
    )
    for r in _search(client, customer_id, signals_q):
        row = rows.get(str(r.asset_group.id))
        if row is None:
            continue
        # Строка сигнала — ЛИБО поисковая тема, ЛИБО аудитория (oneof): у аудиторий text пустой.
        if (r.asset_group_signal.search_theme.text or "").strip():
            row.search_themes += 1
        elif (getattr(r.asset_group_signal.audience, "audience", "") or "").strip():
            row.audiences += 1
    return list(rows.values())


# ── Р6: журнал ЧУЖИХ правок (change_event) ───────────────────────────────────────────
# Google не уведомляет «в вашем аккаунте кто-то поменял бюджет» — узнать можно только СПРОСИВ.
# Поэтому дыра Р6 (deploy/hermes/RISK_REGISTER.md) закрывается не «агент посмотрит, если
# догадается», а нашим ридером + плановой джобой: freshness-гейт ловит чужую правку лишь в
# момент исполнения нашего черновика, а правку, сделанную помимо нас, не видит никто.

CHANGE_EVENT_RETENTION_DAYS = 30  # ресурс живёт 30 дней: окно ≤30 И целиком внутри них (док Google)
# Разрешаем на СУТКИ меньше ретенции, и это не перестраховка. Верхняя граница окна — `today + 1`, а
# `today` — «сегодня аккаунта» best-effort: при недоступной TZ `reports.tz.account_today` берёт дату
# ХОСТА (:52-54). На аккаунте, чья дата на сутки впереди хостовой, окно ровно в 30 дней вылезает за
# ретенцию — сервер отвергает запрос ЦЕЛИКОМ (START_DATE_TOO_OLD), то есть алерт молчит каждый цикл
# и шумит `capture_exception`. Сутки запаса стоят одного дня истории.
CHANGE_EVENT_MAX_DAYS = 29
CHANGE_EVENT_MAX_LIMIT = 10_000  # LIMIT обязателен, верхняя граница задана Google


@dataclass
class ChangeEventRow:
    changed_at: str  # 'YYYY-MM-DD HH:MM:SS.ffffff' в ЧАСОВОМ ПОЯСЕ АККАУНТА (так отдаёт Google)
    resource_type: str  # CAMPAIGN | CAMPAIGN_BUDGET | AD_GROUP | AD_GROUP_CRITERION | AD | …
    operation: str  # CREATE | UPDATE | REMOVE
    client_type: str  # GOOGLE_ADS_WEB_CLIENT | GOOGLE_ADS_API | GOOGLE_ADS_EDITOR | …
    user_email: str  # кто правил. PII: наружу к модели уходит замаскированным (mcp_server)
    resource_name: str  # какой объект правили
    changed_fields: tuple[str, ...] = ()  # какие поля затронуты (FieldMask.paths)

    @property
    def via_api(self) -> bool:
        """Правка сделана через API — то есть, возможно, нами же (или другим API-клиентом)."""
        return self.client_type == "GOOGLE_ADS_API"


def check_change_event_days(days: int) -> int:
    """Валидация окна — ОТДЕЛЬНОЙ функцией, чтобы обёртки (MCP, джоба) могли отказать ДО сети:
    иначе на бессмысленном `days` наружу уедет код той аварии, которая случилась раньше (недоступный
    OAuth ⇒ `internal` вместо `invalid_argument`), и вызывающий чинит не свою ошибку.

    Отказ, а не тихое подрезание: молча укоротив окно, мы вернули бы «изменений нет» там, где их
    просто не искали, — а на этом ридере стоит алерт о чужих правках."""
    days = int(days)
    if not 1 <= days <= CHANGE_EVENT_MAX_DAYS:
        raise ValueError(f"окно change_event — 1..{CHANGE_EVENT_MAX_DAYS} дней, получено {days}")
    return days


def _change_events_query(start: date, end: date, limit: int) -> str:
    # old_resource/new_resource НЕ выбираем: это oneof по 15+ типам ресурсов, разбор каждого —
    # отдельная работа, а leaf-пути внутрь композитов сервер отвергает (шрам recommendation.impact,
    # :440). Имён изменённых полей хватает, чтобы сказать «правили бюджет кампании X», а текущее
    # значение читается обычным ридером.
    fields = (
        "change_event.change_date_time, change_event.change_resource_type, "
        "change_event.change_resource_name, change_event.resource_change_operation, "
        "change_event.client_type, change_event.user_email, change_event.changed_fields"
    )
    return (
        f"SELECT {fields} FROM change_event "
        f"WHERE change_event.change_date_time >= '{start.isoformat()}' "
        f"AND change_event.change_date_time <= '{end.isoformat()}' "
        f"ORDER BY change_event.change_date_time DESC LIMIT {int(limit)}"
    )


def fetch_change_events(
    client,
    customer_id: str,
    *,
    days: int = 7,
    today: date | None = None,
    limit: int = 200,
) -> list[ChangeEventRow]:
    """Р6: КТО, КОГДА, ЧЕРЕЗ ЧТО и КАКОЙ объект правил в аккаунте. READ-ONLY.

    Три требования ресурса, без которых сервер отвергает запрос целиком:
      • `LIMIT` обязателен, максимум 10000;
      • `WHERE change_event.change_date_time` обязателен;
      • окно ≤30 дней и целиком внутри последних 30 — глубже история через API недоступна
        (в веб-интерфейсе она за 2 года, это не одно и то же); мы разрешаем 29, см.
        `CHANGE_EVENT_MAX_DAYS`.

    `today` — «сегодня» В ЧАСОВОМ ПОЯСЕ АККАУНТА (в нём же Google отдаёт `change_date_time`);
    передаёт вызывающий — иначе на аккаунте в другом поясе край окна уедет на сутки. Верхняя
    граница — ЗАВТРА, а не сегодня: `change_date_time` — момент времени, сравнение с 'YYYY-MM-DD'
    берёт полночь, и сегодняшние правки — ровно те, ради которых всё делается, — не попали бы.

    Значений «было → стало» здесь НЕТ (см. `_change_events_query`) — только имена изменённых полей.
    """
    ensure_read_allowed(customer_id)
    days = check_change_event_days(days)
    limit = max(1, min(int(limit), CHANGE_EVENT_MAX_LIMIT))  # тут подрезание безвредно: усечение
    # хвоста, а не окна — самые свежие события идут первыми (ORDER BY … DESC).

    end = (today or date.today()) + timedelta(days=1)
    start = end - timedelta(days=days)  # ширина окна ровно `days` суток — влезает в лимит 30
    out: list[ChangeEventRow] = []
    for r in _search(client, customer_id, _change_events_query(start, end, limit)):
        ev = r.change_event
        paths = getattr(getattr(ev, "changed_fields", None), "paths", ()) or ()
        out.append(
            ChangeEventRow(
                changed_at=str(getattr(ev, "change_date_time", "") or ""),
                resource_type=_enum_name(getattr(ev, "change_resource_type", "")),
                operation=_enum_name(getattr(ev, "resource_change_operation", "")),
                client_type=_enum_name(getattr(ev, "client_type", "")),
                user_email=str(getattr(ev, "user_email", "") or ""),
                resource_name=str(getattr(ev, "change_resource_name", "") or ""),
                changed_fields=tuple(str(p) for p in paths),
            )
        )
    return out
