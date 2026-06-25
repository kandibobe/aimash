"""Чтение Google Ads через GAQL/SearchStream. READ-ONLY — ничего не меняет.

Предпочитаем SearchStream (страница до 10k строк = 1 операция против дневной квоты).
"""
from __future__ import annotations

from dataclasses import dataclass

from google.ads.googleads.client import GoogleAdsClient

from ads.client import ensure_allowed


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


def list_child_accounts(client: GoogleAdsClient, manager_id: str) -> list[ChildAccount]:
    """Обход иерархии MCC через customer_client."""
    ga = client.get_service("GoogleAdsService")
    q = (
        "SELECT customer_client.id, customer_client.descriptive_name, "
        "customer_client.currency_code, customer_client.manager, customer_client.level, "
        "customer_client.status FROM customer_client"
    )
    out: list[ChildAccount] = []
    for row in ga.search(customer_id=str(manager_id), query=q):
        cc = row.customer_client
        out.append(ChildAccount(
            id=str(cc.id), name=cc.descriptive_name, currency=cc.currency_code,
            manager=cc.manager, level=cc.level, status=cc.status.name,
        ))
    return out


def account_stats(client: GoogleAdsClient, customer_id: str, days: int = 30) -> AccountStats:
    """Сводная статистика по аккаунту за период. Только для аккаунтов из белого списка."""
    ensure_allowed(customer_id)
    days = {7: 7, 14: 14, 30: 30}.get(days, 30)  # предустановленные диапазоны GAQL
    ga = client.get_service("GoogleAdsService")
    q = (
        "SELECT metrics.impressions, metrics.clicks, metrics.cost_micros, "
        "metrics.conversions, metrics.conversions_value "
        f"FROM customer WHERE segments.date DURING LAST_{days}_DAYS"
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


def list_campaigns(client: GoogleAdsClient, customer_id: str) -> list[dict]:
    """Список кампаний аккаунта (id, имя, статус). Только для белого списка."""
    ensure_allowed(customer_id)
    ga = client.get_service("GoogleAdsService")
    q = "SELECT campaign.id, campaign.name, campaign.status FROM campaign ORDER BY campaign.id"
    return [
        {"id": str(r.campaign.id), "name": r.campaign.name, "status": r.campaign.status.name}
        for r in ga.search(customer_id=str(customer_id), query=q)
    ]
