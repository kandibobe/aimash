"""Создать тестовую кампанию (Search, НА ПАУЗЕ) в тестовом аккаунте Aimash.

Нужна, чтобы было что менять в Фазе 1 (сейчас 0 кампаний). Пауза + тест-аккаунт = денег не тратит.
Запуск:  python scripts/create_test_campaign.py
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from google.ads.googleads.errors import GoogleAdsException  # noqa: E402

from ads.client import build_client, ensure_allowed  # noqa: E402

TEST = "7753643025"


def main() -> None:
    ensure_allowed(TEST)
    client = build_client()
    suffix = uuid.uuid4().hex[:6]

    # 1) Бюджет
    budget_service = client.get_service("CampaignBudgetService")
    budget_op = client.get_type("CampaignBudgetOperation")
    b = budget_op.create
    b.name = f"Aimash test budget {suffix}"
    b.amount_micros = 1_000_000  # 1 ед. валюты аккаунта в день (AUD); кампания на паузе
    b.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    b.explicitly_shared = False

    try:
        budget_res = budget_service.mutate_campaign_budgets(
            customer_id=TEST, operations=[budget_op]
        )
        budget_rn = budget_res.results[0].resource_name
        print("✅ бюджет создан:", budget_rn)
    except GoogleAdsException as e:
        print("⚠️ создание бюджета упало:")
        for err in e.failure.errors:
            print("   -", err.error_code, err.message)
        return

    # 2) Кампания (Search, PAUSED, manual CPC)
    campaign_service = client.get_service("CampaignService")
    camp_op = client.get_type("CampaignOperation")
    c = camp_op.create
    c.name = f"Aimash Test Search {suffix}"
    c.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SEARCH
    c.status = client.enums.CampaignStatusEnum.PAUSED
    c.campaign_budget = budget_rn
    # ставка: пустой ManualCpc (enhanced_cpc устарел в v24)
    client.copy_from(c.manual_cpc, client.get_type("ManualCpc"))
    # обязательно: декларация EU political advertising (enum EuPoliticalAdvertisingStatus; 3 = DOES_NOT_CONTAIN)
    c.contains_eu_political_advertising = 3
    c.network_settings.target_google_search = True
    c.network_settings.target_search_network = False
    c.network_settings.target_content_network = False

    try:
        camp_res = campaign_service.mutate_campaigns(customer_id=TEST, operations=[camp_op])
        print("✅ кампания создана (PAUSED):", camp_res.results[0].resource_name)
        print("   имя:", c.name)
    except GoogleAdsException as e:
        print("⚠️ создание кампании упало:")
        for err in e.failure.errors:
            print("   -", err.error_code, err.message)


if __name__ == "__main__":
    main()
