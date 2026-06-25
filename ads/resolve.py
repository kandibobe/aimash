"""Резолв кампании по имени → id/budget + пересчёт суммы бюджета. READ-ONLY (для оркестрации).

Модель даёт ИМЯ кампании, а SDK-мутации нужен id (и для процентов — текущий бюджет).
Здесь только чтение; запись — в ads/mutations за confirm-гейтом.
"""

from __future__ import annotations

from dataclasses import dataclass

from google.ads.googleads.client import GoogleAdsClient

from ads.client import ensure_allowed


@dataclass
class CampaignRef:
    id: str
    resource_name: str
    name: str
    status: str
    budget_resource: str
    budget_micros: int


@dataclass
class AdGroupRef:
    id: str
    resource_name: str
    name: str
    status: str
    cpc_bid_micros: int
    campaign_id: str


def _gaql_escape(value: str) -> str:
    """Экранирование строкового литерала для GAQL (предотвращает инъекцию в WHERE name = '...')."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def find_campaign_by_name(
    client: GoogleAdsClient, customer_id: str, name: str
) -> CampaignRef | None:
    ensure_allowed(customer_id)
    ga = client.get_service("GoogleAdsService")
    safe = _gaql_escape(name)
    q = (
        "SELECT campaign.id, campaign.name, campaign.status, campaign.campaign_budget, "
        "campaign_budget.amount_micros FROM campaign "
        f"WHERE campaign.name = '{safe}' LIMIT 1"
    )
    for row in ga.search(customer_id=str(customer_id), query=q):
        return CampaignRef(
            id=str(row.campaign.id),
            resource_name=row.campaign.resource_name,
            name=row.campaign.name,
            status=row.campaign.status.name,
            budget_resource=row.campaign.campaign_budget,
            budget_micros=row.campaign_budget.amount_micros,
        )
    return None


def find_ad_groups(
    client: GoogleAdsClient, customer_id: str, campaign_name: str
) -> list[AdGroupRef]:
    """Группы объявлений кампании (по имени кампании). Для bid (ставка) и add_keywords —
    обе операции живут на уровне ad group. READ-ONLY. Пустой список = у кампании нет групп
    (или кампания не найдена) — вызывающий код должен это обработать ДО любой записи."""
    ensure_allowed(customer_id)
    ga = client.get_service("GoogleAdsService")
    safe = _gaql_escape(campaign_name)
    q = (
        "SELECT ad_group.id, ad_group.name, ad_group.status, ad_group.cpc_bid_micros, "
        "ad_group.resource_name, campaign.id FROM ad_group "
        f"WHERE campaign.name = '{safe}' ORDER BY ad_group.id"
    )
    out: list[AdGroupRef] = []
    for row in ga.search(customer_id=str(customer_id), query=q):
        out.append(
            AdGroupRef(
                id=str(row.ad_group.id),
                resource_name=row.ad_group.resource_name,
                name=row.ad_group.name,
                status=row.ad_group.status.name,
                cpc_bid_micros=row.ad_group.cpc_bid_micros,
                campaign_id=str(row.campaign.id),
            )
        )
    return out


def compute_new_micros(current_micros: int, mode: str, value: float) -> int:
    """Пересчёт бюджета в micros по режиму команды. Валюта не конвертируется (значение в валюте аккаунта)."""
    if mode == "increase_by_percent":
        return int(round(current_micros * (1 + value / 100)))
    if mode == "increase_by_amount":
        return int(round(current_micros + value * 1_000_000))
    if mode == "set_to":
        return int(round(value * 1_000_000))
    raise ValueError(f"неизвестный mode бюджета: {mode}")
