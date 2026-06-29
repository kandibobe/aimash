"""Чтение Google Ads через GAQL/SearchStream. READ-ONLY — ничего не меняет.

Предпочитаем SearchStream (страница до 10k строк = 1 операция против дневной квоты).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from google.ads.googleads.client import GoogleAdsClient

from ads.client import ensure_manager_allowed, ensure_read_allowed


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
