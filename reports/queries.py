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
    type: str
    dismissed: bool


def fetch_recommendations(client, customer_id: str, limit: int = 50) -> list[RecommendationRow]:
    """Активные Google-рекомендации (тип + dismissed). READ-ONLY — только показ; применение вне объёма
    (при надобности ApplyRecommendation через confirm-гейт). Per-rec impact-метрики недоступны нест.
    путём в v24 (P0) — берём типы + агрегатный uplift из fetch_optimization_score."""
    ensure_read_allowed(customer_id)
    q = f"SELECT recommendation.type, recommendation.dismissed FROM recommendation LIMIT {int(limit)}"
    return [
        RecommendationRow(_enum_name(r.recommendation.type), bool(r.recommendation.dismissed))
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


def fetch_conversion_health(client, customer_id: str) -> list[ConversionActionRow]:
    """Действия-конверсии аккаунта (статус/тип/категория/primary_for_goal). Пусто / нет ENABLED
    primary → трекинг под подозрением (решает движок аудита, крит-фикс #3). READ-ONLY."""
    ensure_read_allowed(customer_id)
    q = (
        "SELECT conversion_action.name, conversion_action.status, conversion_action.type, "
        "conversion_action.category, conversion_action.primary_for_goal FROM conversion_action"
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
    q = (
        "SELECT campaign.name, segments.geo_target_region, "
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
