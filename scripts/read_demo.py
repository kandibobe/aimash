"""Демо чтения Google Ads (Фаза 0): дочерние аккаунты + статистика + кампании тестового аккаунта.

Запуск:  python scripts/read_demo.py    (read-only, ничего не меняет)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.client import build_client  # noqa: E402
from ads.read import account_stats, list_campaigns, list_child_accounts  # noqa: E402
from core.config import settings  # noqa: E402

TEST_ACCOUNT = "7753643025"  # Aimash (Draft account)


def main() -> None:
    client = build_client()
    manager = settings.google_ads_login_customer_id

    print(f"Менеджер (MCC): {manager}")
    children = list_child_accounts(client, manager)
    enabled = [c for c in children if c.status == "ENABLED" and not c.manager]
    print(f"Дочерних аккаунтов: {len(children)} (активных не-менеджеров: {len(enabled)})")
    print(f"Белый список (боту можно трогать): {sorted(settings.allowed_customer_ids)}\n")

    print(f"=== Тестовый аккаунт Aimash {TEST_ACCOUNT} ===")
    st = account_stats(client, TEST_ACCOUNT, days=30)
    print(f"  Статистика 30 дней: показы={st.impressions} клики={st.clicks} "
          f"расход={st.cost:.2f} конверсии={st.conversions} ценность={st.conv_value:.2f}")
    camps = list_campaigns(client, TEST_ACCOUNT)
    print(f"  Кампаний: {len(camps)}")
    for c in camps[:10]:
        print(f"    - [{c['status']}] {c['id']} {c['name']}")

    # Защита: попытка тронуть НЕ-разрешённый аккаунт должна упасть
    print("\n=== Проверка белого списка (должно запретить) ===")
    try:
        account_stats(client, "1469059209")  # DARIAL — реальный клиент, НЕ в списке
        print("  ❌ ОШИБКА: доступ к чужому аккаунту НЕ заблокирован!")
    except PermissionError as e:
        print(f"  ✅ заблокировано: {e}")


if __name__ == "__main__":
    main()
