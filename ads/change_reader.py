"""Чтение change_event ресурса Google Ads — детектор внешних правок.

GAQL-запрос к change_event: кто, когда, что и как изменил в аккаунте.
Используется MCP-инструментом detect_external_edits."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from google.ads.googleads.client import GoogleAdsClient

from ads.client import ensure_read_allowed


def fetch_change_events(
    client: GoogleAdsClient,
    customer_id: str,
    hours_back: int = 24,
    limit: int = 200,
) -> list[dict[str, str]]:
    """Выборка change_event-строк из Google Ads за последние N часов.

    Возвращает список dict'ов с полями:
      resource, resource_id, change_type, campaign, changed_at, user_agent.

    GAQL:
      SELECT change_event.resource_name, change_event.change_type,
             change_event.user_agent, change_event.change_date_time,
             campaign.name
      WHERE change_event.change_date_time > timestamp

    Args:
        client: GoogleAdsClient для аккаунта.
        customer_id: Номер клиента (нормализованный).
        hours_back: За сколько часов назад смотреть (по умолчанию 24).
        limit: MAX-строк из GAQL (дефолт 200).

    Returns:
        Список dict'ов с данными изменений.

    Raises:
        PermissionError: если customer_id не в read-allow-list.
    """
    ensure_read_allowed(customer_id)

    since = (datetime.now(timezone.utc) - timedelta(hours=int(hours_back))).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )

    ga = client.get_service("GoogleAdsService")
    query = (
        "SELECT change_event.resource_name, change_event.change_type, "
        "change_event.user_agent, change_event.change_date_time, "
        "campaign.name "
        "FROM change_event "
        "WHERE change_event.change_date_time > '%s' "
        "ORDER BY change_event.change_date_time DESC "
        "LIMIT %d" % (since, int(limit))
    )

    results: list[dict[str, str]] = []
    for row in ga.search(customer_id=str(customer_id), query=query):
        ce = row.change_event
        campaign_name = ""
        try:
            campaign_name = row.campaign.name
        except Exception:  # noqa: BLE001 — кампания могла быть удалена
            pass

        results.append(
            {
                "resource": ce.resource_name or "",
                "resource_id": ce.resource_name.rsplit("/", 1)[-1] if ce.resource_name else "",
                "change_type": ce.change_type.name if ce.change_type else "",
                "campaign": campaign_name,
                "changed_at": ce.change_date_time or "",
                "user_agent": ce.user_agent or "",
            }
        )

    return results