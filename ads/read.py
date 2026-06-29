"""Чтение Google Ads через GAQL/SearchStream. READ-ONLY — ничего не меняет.

Предпочитаем SearchStream (страница до 10k строк = 1 операция против дневной квоты).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from google.ads.googleads.client import GoogleAdsClient

from ads.client import ensure_manager_allowed, ensure_read_allowed
from ads.resolve import gaql_escape


@dataclass
class ChildAccount:
    id: str
    name: str
    currency: str
    manager: bool
    level: int
    status: str


@dataclass
class AccountStats:
    impressions: int
    clicks: int
    cost: float  # в валюте аккаунта (из micros)
    conversions: float
    conv_value: float


@dataclass
class Audience:
    resource_name: str
    name: str
    size: int  # размер аудитории для показа (user_list.size_for_display), 0 если неизвестно


def list_child_accounts(client: GoogleAdsClient, manager_id: str) -> list[ChildAccount]:
    """Обход иерархии MCC через customer_client. Чокпойнт: только настроенный MCC (fail-closed) —
    перечисление дочерних аккаунтов чужого менеджера запрещено (golden rule #9)."""
    ensure_manager_allowed(manager_id)
    ga = client.get_service("GoogleAdsService")
    q = (
        "SELECT customer_client.id, customer_client.descriptive_name, "
        "customer_client.currency_code, customer_client.manager, customer_client.level, "
        "customer_client.status FROM customer_client"
    )
    out: list[ChildAccount] = []
    for row in ga.search(customer_id=str(manager_id), query=q):
        cc = row.customer_client
        out.append(
            ChildAccount(
                id=str(cc.id),
                name=cc.descriptive_name,
                currency=cc.currency_code,
                manager=cc.manager,
                level=cc.level,
                status=cc.status.name,
            )
        )
    return out


def account_stats(client: GoogleAdsClient, customer_id: str, days: int = 30) -> AccountStats:
    """Сводная статистика по аккаунту за РЕАЛЬНЫЙ период N дней. Только для аккаунтов из белого списка.

    Окно строим из фактического N через `segments.date BETWEEN` (а не из пресета `LAST_N_DAYS`):
    раньше любой N кроме 7/14/30 молча схлопывался в 30, и подпись «N дн.» врала про объём данных
    (а это денежные метрики). end = вчера (как LAST_N_DAYS — без неполного «сегодня»); нормализация
    таймзон по дочерним аккаунтам — §8, отложена (один аккаунт → host-дата ок)."""
    ensure_read_allowed(customer_id)
    n = max(1, int(days))
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=n - 1)
    ga = client.get_service("GoogleAdsService")
    q = (
        "SELECT metrics.impressions, metrics.clicks, metrics.cost_micros, "
        "metrics.conversions, metrics.conversions_value "
        f"FROM customer WHERE segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"
    )
    imp = clk = cost = 0
    conv = cval = 0.0
    for row in ga.search(customer_id=str(customer_id), query=q):
        m = row.metrics
        imp += m.impressions
        clk += m.clicks
        cost += m.cost_micros
        conv += m.conversions
        cval += m.conversions_value
    return AccountStats(imp, clk, cost / 1_000_000, conv, cval)


_CURRENCY_CACHE: dict[
    str, str
] = {}  # customer_id → currency_code (валюта не меняется в рамках сессии)


def account_currency(client: GoogleAdsClient, customer_id: str) -> str:
    """Код валюты аккаунта (§9), напр. 'USD'/'UAH'. Один GAQL FROM customer, кэш по customer_id.
    Только для белого списка (замок аккаунта). '' если не удалось прочитать (вызывающий покажет
    метрики без явной валюты)."""
    ensure_read_allowed(customer_id)
    cid = str(customer_id)
    if cid in _CURRENCY_CACHE:
        return _CURRENCY_CACHE[cid]
    ga = client.get_service("GoogleAdsService")
    code = ""
    for row in ga.search(
        customer_id=cid, query="SELECT customer.currency_code FROM customer LIMIT 1"
    ):
        code = row.customer.currency_code
        break
    if code:
        _CURRENCY_CACHE[cid] = code  # кэшируем только успешное чтение
    return code


def list_audiences(client: GoogleAdsClient, customer_id: str) -> list[Audience]:
    """Доступные аудитории аккаунта (user_list) для прикрепления к кампании (§3). Только белый
    список (замок аккаунта). Берём открытые для членства списки ремаркетинга/аудиторий."""
    ensure_read_allowed(customer_id)
    ga = client.get_service("GoogleAdsService")
    q = (
        "SELECT user_list.resource_name, user_list.name, user_list.size_for_display "
        "FROM user_list WHERE user_list.membership_status = 'OPEN' ORDER BY user_list.name"
    )
    out: list[Audience] = []
    for row in ga.search(customer_id=str(customer_id), query=q):
        ul = row.user_list
        out.append(
            Audience(
                resource_name=ul.resource_name,
                name=ul.name,
                size=int(getattr(ul, "size_for_display", 0) or 0),
            )
        )
    return out


def list_campaigns(client: GoogleAdsClient, customer_id: str) -> list[dict]:
    """Список кампаний аккаунта (id, имя, статус). Только для белого списка."""
    ensure_read_allowed(customer_id)
    ga = client.get_service("GoogleAdsService")
    q = "SELECT campaign.id, campaign.name, campaign.status FROM campaign ORDER BY campaign.id"
    return [
        {"id": str(r.campaign.id), "name": r.campaign.name, "status": r.campaign.status.name}
        for r in ga.search(customer_id=str(customer_id), query=q)
    ]


# ── §2A: чтение полного конфига кампании для клонирования («как в кампании X») ────────
@dataclass
class KeywordSeed:
    text: str
    match_type: str  # broad | phrase | exact (lower-case для схем create_search_campaign)


@dataclass
class AdGroupConfig:
    id: str
    name: str
    cpc_bid_micros: int
    keywords: list[KeywordSeed]
    headlines: list[str]
    descriptions: list[str]
    final_url: str
    path1: str
    path2: str


@dataclass
class CampaignConfig:
    id: str
    name: str
    status: str
    channel_type: str  # SEARCH | DISPLAY | … (клон-в-create_search_campaign только для SEARCH)
    budget_micros: int
    ad_groups: list[AdGroupConfig]


_MATCH_TYPE_MAP = {"BROAD": "broad", "PHRASE": "phrase", "EXACT": "exact"}


def read_campaign_config(
    client: GoogleAdsClient, customer_id: str, campaign_name: str
) -> CampaignConfig | None:
    """Полный (клонируемый) конфиг кампании по ИМЕНИ. READ-ONLY, замок чтения (ensure_read_allowed).
    None — кампания не найдена. Google не отдаёт всё одной строкой → GAQL-веер: кампания+бюджет+
    канал; группы (имя/cpc); позитивные ключи по группам (текст+тип); RSA-тексты по группам
    (headlines/descriptions/final_url/path). Гео/минус-слова/стратегия/аудитории НЕ читаются в v1 —
    клон в create_search_campaign их не переносит (вызывающий честно сообщает об этом)."""
    ensure_read_allowed(customer_id)
    cid = str(customer_id)
    ga = client.get_service("GoogleAdsService")
    safe = gaql_escape(campaign_name)

    # 1) база кампании + бюджет + тип канала
    base = None
    q1 = (
        "SELECT campaign.id, campaign.name, campaign.status, "
        "campaign.advertising_channel_type, campaign.campaign_budget, "
        "campaign_budget.amount_micros FROM campaign "
        f"WHERE campaign.name = '{safe}' LIMIT 1"
    )
    for row in ga.search(customer_id=cid, query=q1):
        base = row
        break
    if base is None:
        return None
    camp_id = int(base.campaign.id)

    # 2) группы объявлений (имя + cpc), без REMOVED
    groups: dict[str, AdGroupConfig] = {}
    order: list[str] = []
    q2 = (
        "SELECT ad_group.id, ad_group.name, ad_group.cpc_bid_micros FROM ad_group "
        f"WHERE campaign.id = {camp_id} AND ad_group.status != 'REMOVED' ORDER BY ad_group.id"
    )
    for row in ga.search(customer_id=cid, query=q2):
        ag_id = str(row.ad_group.id)
        groups[ag_id] = AdGroupConfig(
            id=ag_id,
            name=row.ad_group.name,
            cpc_bid_micros=int(row.ad_group.cpc_bid_micros or 0),
            keywords=[],
            headlines=[],
            descriptions=[],
            final_url="",
            path1="",
            path2="",
        )
        order.append(ag_id)

    # 3) позитивные ключи по группам (текст + тип соответствия)
    q3 = (
        "SELECT ad_group.id, ad_group_criterion.keyword.text, "
        "ad_group_criterion.keyword.match_type FROM ad_group_criterion "
        f"WHERE campaign.id = {camp_id} AND ad_group_criterion.type = 'KEYWORD' "
        "AND ad_group_criterion.negative = FALSE AND ad_group_criterion.status != 'REMOVED'"
    )
    for row in ga.search(customer_id=cid, query=q3):
        ag = groups.get(str(row.ad_group.id))
        if ag is None:
            continue
        mt = _MATCH_TYPE_MAP.get(row.ad_group_criterion.keyword.match_type.name, "phrase")
        ag.keywords.append(KeywordSeed(text=row.ad_group_criterion.keyword.text, match_type=mt))

    # 4) RSA-тексты по группам (первое RSA на группу — источник заголовков/описаний/URL)
    q4 = (
        "SELECT ad_group.id, ad_group_ad.ad.final_urls, "
        "ad_group_ad.ad.responsive_search_ad.headlines, "
        "ad_group_ad.ad.responsive_search_ad.descriptions, "
        "ad_group_ad.ad.responsive_search_ad.path1, "
        "ad_group_ad.ad.responsive_search_ad.path2 FROM ad_group_ad "
        f"WHERE campaign.id = {camp_id} AND ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD' "
        "AND ad_group_ad.status != 'REMOVED'"
    )
    for row in ga.search(customer_id=cid, query=q4):
        ag = groups.get(str(row.ad_group.id))
        if ag is None or ag.headlines:  # первый RSA на группу — остальные пропускаем
            continue
        rsa = row.ad_group_ad.ad.responsive_search_ad
        ag.headlines = [a.text for a in rsa.headlines if getattr(a, "text", "")]
        ag.descriptions = [a.text for a in rsa.descriptions if getattr(a, "text", "")]
        ag.path1 = getattr(rsa, "path1", "") or ""
        ag.path2 = getattr(rsa, "path2", "") or ""
        urls = list(row.ad_group_ad.ad.final_urls or [])
        ag.final_url = urls[0] if urls else ""

    return CampaignConfig(
        id=str(camp_id),
        name=base.campaign.name,
        status=base.campaign.status.name,
        channel_type=base.campaign.advertising_channel_type.name,
        budget_micros=int(base.campaign_budget.amount_micros or 0),
        ad_groups=[groups[a] for a in order],
    )


# ── §3-assets: список ассетов-расширений кампании (для показа/открепления) ────────────
@dataclass
class CampaignAssetRow:
    link_resource_name: str  # campaign_asset.resource_name — его и удаляют при откреплении
    field_type: str  # SITELINK | CALLOUT | STRUCTURED_SNIPPET | …
    label: str  # человекочитаемая подпись (link_text / callout_text / header+values / имя)


def list_campaign_assets(
    client: GoogleAdsClient, customer_id: str, campaign_id: str
) -> list[CampaignAssetRow]:
    """Ассеты-расширения кампании (campaign_asset + поля связанного asset). READ-ONLY, замок чтения.
    label собираем по field_type (sitelink link_text / callout_text / snippet header+values / имя)."""
    ensure_read_allowed(customer_id)
    cid = str(customer_id)
    ga = client.get_service("GoogleAdsService")
    q = (
        "SELECT campaign_asset.resource_name, campaign_asset.field_type, campaign_asset.asset, "
        "asset.type, asset.name, asset.sitelink_asset.link_text, asset.callout_asset.callout_text, "
        "asset.structured_snippet_asset.header, asset.structured_snippet_asset.values "
        f"FROM campaign_asset WHERE campaign.id = {int(campaign_id)} "
        "AND campaign_asset.status != 'REMOVED'"
    )
    out: list[CampaignAssetRow] = []
    for row in ga.search(customer_id=cid, query=q):
        ca = row.campaign_asset
        a = row.asset
        ft = ca.field_type.name
        if ft == "SITELINK":
            label = getattr(a.sitelink_asset, "link_text", "") or a.name
        elif ft == "CALLOUT":
            label = getattr(a.callout_asset, "callout_text", "") or a.name
        elif ft == "STRUCTURED_SNIPPET":
            ss = a.structured_snippet_asset
            vals = ", ".join(list(ss.values)[:5])
            label = f"{ss.header}: {vals}" if getattr(ss, "header", "") else (a.name or "")
        else:
            label = a.name or ft
        out.append(
            CampaignAssetRow(link_resource_name=ca.resource_name, field_type=ft, label=label)
        )
    return out
