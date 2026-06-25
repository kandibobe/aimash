"""Создать группу объявлений в тест-кампании (аккаунт 7753643025), если её ещё нет.

Нужна для live write-теста: update_bid и add_keywords живут на уровне ad group, а
scripts/create_test_campaign.py создаёт кампанию БЕЗ группы. Идемпотентно: если группа уже
есть — ничего не делает. Безопасно: тест-аккаунт, кампания на паузе → денег не тратит.
Запуск: python scripts/create_test_ad_group.py
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Windows cp1251-консоль падает на эмодзи (✅⚠️) в выводе — принудительно UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:
    pass

from google.ads.googleads.errors import GoogleAdsException  # noqa: E402

from ads.client import DRAFT_ACCOUNT_ID, build_client, ensure_allowed  # noqa: E402
from ads.read import list_campaigns  # noqa: E402
from ads.resolve import find_ad_groups, find_campaign_by_name  # noqa: E402


def main() -> None:
    ensure_allowed(DRAFT_ACCOUNT_ID)  # замок: только Aimash Draft
    client = build_client()

    camps = list_campaigns(client, DRAFT_ACCOUNT_ID)
    if not camps:
        print("нет кампаний — сначала: python scripts/create_test_campaign.py")
        return
    name = camps[0]["name"]
    ref = find_campaign_by_name(client, DRAFT_ACCOUNT_ID, name)
    if ref is None:
        print(f"кампания '{name}' не зарезолвилась — неожиданно")
        return
    print(f"Кампания: '{name}' (id={ref.id}, status={ref.status})")

    existing = find_ad_groups(client, DRAFT_ACCOUNT_ID, name)
    if existing:
        g = existing[0]
        print(
            f"✅ группа уже есть: '{g.name}' (id={g.id}); ставка {g.cpc_bid_micros / 1e6:.2f}. "
            "Ничего не делаю (идемпотентно)."
        )
        return

    suffix = uuid.uuid4().hex[:6]
    ad_group_service = client.get_service("AdGroupService")
    op = client.get_type("AdGroupOperation")
    new_ag = op.create
    new_ag.name = f"Aimash Test AdGroup {suffix}"
    new_ag.campaign = ref.resource_name
    new_ag.status = client.enums.AdGroupStatusEnum.ENABLED
    new_ag.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
    new_ag.cpc_bid_micros = 500_000  # 0.50 в валюте аккаунта (AUD); кампания на паузе — не тратит

    try:
        res = ad_group_service.mutate_ad_groups(customer_id=DRAFT_ACCOUNT_ID, operations=[op])
        print("✅ группа создана:", res.results[0].resource_name)
        print("   имя:", new_ag.name, "| ставка: 0.50")
    except GoogleAdsException as e:
        print("⚠️ создание группы упало:")
        for err in e.failure.errors:
            print("   -", err.error_code, err.message)


if __name__ == "__main__":
    main()
