"""Построение Google Ads клиента из .env + защита по белому списку аккаунтов.

⚠️ Менеджерский аккаунт содержит реальные клиентские аккаунты — операции разрешены
ТОЛЬКО для customer_id из белого списка (settings.allowed_customer_ids).
"""
from __future__ import annotations

from functools import lru_cache

from google.ads.googleads.client import GoogleAdsClient

from core.config import settings


@lru_cache(maxsize=1)
def build_client() -> GoogleAdsClient:
    cfg = {
        "developer_token": settings.google_ads_developer_token,
        "client_id": settings.google_ads_client_id,
        "client_secret": settings.google_ads_client_secret,
        "refresh_token": settings.google_ads_refresh_token,
        "use_proto_plus": True,
    }
    if settings.google_ads_login_customer_id:
        cfg["login_customer_id"] = settings.google_ads_login_customer_id
    return GoogleAdsClient.load_from_dict(cfg)


def ensure_allowed(customer_id: str) -> None:
    """Разрешаем работать только с аккаунтами из белого списка (защита боевых клиентов)."""
    allowed = settings.allowed_customer_ids
    if allowed and str(customer_id) not in allowed:
        raise PermissionError(
            f"customer_id {customer_id} не в белом списке {sorted(allowed)} — операция запрещена"
        )
