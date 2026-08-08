"""Read-only Google Ads spend input for deterministic budget pacing."""

from __future__ import annotations

from ads.client import ensure_read_allowed
from reports.period import Period


def fetch_period_spend_micros(
    client: object,
    customer_id: str,
    period: Period,
    *,
    campaign_id: str | None = None,
) -> int:
    """Return complete campaign spend for the period via SearchStream, without a row cap."""
    ensure_read_allowed(customer_id)
    campaign_filter = ""
    if campaign_id is not None:
        campaign_filter = f" AND campaign.id = {int(campaign_id)}"
    query = (
        "SELECT campaign.id, metrics.cost_micros FROM campaign WHERE "
        f"{period.gaql_between()} AND campaign.status != 'REMOVED'{campaign_filter}"
    )
    service = client.get_service("GoogleAdsService")
    total = 0
    for batch in service.search_stream(customer_id=str(customer_id), query=query):
        for row in batch.results:
            total += int(row.metrics.cost_micros or 0)
    return total
