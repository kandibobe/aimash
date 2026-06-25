"""Проверка доступа к Google Ads (read-only) + обход иерархии MCC.

Запуск:  python scripts/check_access.py
Использует креды из .env. Ничего не меняет (только читает).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from google.ads.googleads.client import GoogleAdsClient  # noqa: E402
from google.ads.googleads.errors import GoogleAdsException  # noqa: E402

from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from core.config import settings  # noqa: E402

TARGET_CHILD = DRAFT_ACCOUNT_ID  # Aimash (Draft) — единственный разрешённый аккаунт


def build_client() -> GoogleAdsClient:
    cfg = {
        "developer_token": settings.google_ads_developer_token.get_secret_value(),
        "client_id": settings.google_ads_client_id,
        "client_secret": settings.google_ads_client_secret.get_secret_value(),
        "refresh_token": settings.google_ads_refresh_token.get_secret_value(),
        "use_proto_plus": True,
    }
    if settings.google_ads_login_customer_id:
        cfg["login_customer_id"] = settings.google_ads_login_customer_id
    return GoogleAdsClient.load_from_dict(cfg)


def read_account(ga, cid: str, label: str) -> None:
    query = (
        "SELECT customer.id, customer.descriptive_name, customer.currency_code, "
        "customer.time_zone, customer.manager FROM customer"
    )
    try:
        for row in ga.search(customer_id=cid, query=query):
            c = row.customer
            print(
                f"  ✅ {label}: id={c.id} '{c.descriptive_name}' {c.currency_code} "
                f"{c.time_zone} manager={c.manager}"
            )
    except GoogleAdsException as e:
        print(f"  ⚠️ {label} ({cid}) — ошибка:")
        for err in e.failure.errors:
            print("     -", err.error_code, err.message)


def main() -> None:
    if not settings.google_ads_refresh_token.get_secret_value():
        print("❌ нет refresh token")
        sys.exit(1)

    client = build_client()
    login = settings.google_ads_login_customer_id
    print(f"login_customer_id = {login}\n")

    cs = client.get_service("CustomerService")
    print("Доступные аккаунты (list_accessible_customers):")
    for rn in cs.list_accessible_customers().resource_names:
        print("  ", rn)

    ga = client.get_service("GoogleAdsService")
    print("\nЛогин-аккаунт:")
    read_account(ga, login, "login")

    print("\nИерархия (customer_client) под логин-аккаунтом — обход MCC:")
    cc_query = (
        "SELECT customer_client.id, customer_client.descriptive_name, customer_client.manager, "
        "customer_client.level, customer_client.currency_code, customer_client.status "
        "FROM customer_client"
    )
    try:
        for row in ga.search(customer_id=login, query=cc_query):
            cc = row.customer_client
            print(
                f"  • id={cc.id} '{cc.descriptive_name}' level={cc.level} "
                f"manager={cc.manager} {cc.currency_code} status={cc.status}"
            )
    except GoogleAdsException as e:
        print("  ⚠️ обход упал:")
        for err in e.failure.errors:
            print("     -", err.error_code, err.message)

    print(f"\nЦелевой аккаунт Aimash ({TARGET_CHILD}):")
    read_account(ga, TARGET_CHILD, "Aimash")


if __name__ == "__main__":
    main()
