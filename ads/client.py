"""Построение Google Ads клиента из .env + замок единственного аккаунта.

⚠️ Менеджерский аккаунт (MCC) содержит реальные клиентские аккаунты. Бот имеет право
читать/менять ТОЛЬКО один аккаунт — Aimash (Draft). Замок трёхслойный:
  1) потолок в КОДЕ (ALLOWED_CEILING) — env не может его расширить;
  2) fail-closed — пустой allow-list => отказ (а не «разрешено всё»);
  3) членство — customer_id обязан быть в allow-list ⊆ потолок.
Любое расширение круга аккаунтов = ОСОЗНАННАЯ правка этого файла, не строки в .env.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from core.config import normalize_customer_id, settings

if TYPE_CHECKING:
    from google.ads.googleads.client import GoogleAdsClient

# Aimash (Draft account), 775-364-3025 — ЕДИНСТВЕННЫЙ разрешённый аккаунт.
DRAFT_ACCOUNT_ID = "7753643025"
# Жёсткий потолок: env (allowed_customer_ids) не может выйти за этот набор.
ALLOWED_CEILING = frozenset({DRAFT_ACCOUNT_ID})


@lru_cache(maxsize=1)
def build_client() -> "GoogleAdsClient":
    # Импорт SDK ленивый: ensure_allowed/константы остаются доступны без google-ads.
    from google.ads.googleads.client import GoogleAdsClient

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


def ensure_allowed(customer_id: str) -> None:
    """Замок единственного аккаунта. Бросает PermissionError на любой запрет.

    Это единственная точка, через которую и чтение per-account, и ВСЕ мутации
    проверяют customer_id. Нормализуем id (только цифры), поэтому '775-364-3025'
    и '7753643025' эквивалентны.
    """
    cid = normalize_customer_id(customer_id)
    allowed = {normalize_customer_id(x) for x in settings.allowed_customer_ids}

    # (2) fail-closed: без явного allow-list ничего не разрешаем.
    if not allowed:
        raise PermissionError(
            "allowed_customer_ids пуст — операции запрещены (fail-closed). "
            f"Задай GOOGLE_ADS_ALLOWED_CUSTOMER_IDS={DRAFT_ACCOUNT_ID} в .env"
        )
    # (1) потолок в коде: env не может добавить чужой/боевой аккаунт.
    if not allowed <= ALLOWED_CEILING:
        raise PermissionError(
            f"allowed_customer_ids {sorted(allowed)} выходит за код-потолок "
            f"{sorted(ALLOWED_CEILING)} — расширение требует правки ads/client.py"
        )
    # (3) членство.
    if cid not in allowed:
        raise PermissionError(
            f"customer_id {cid} не разрешён (allow-list {sorted(allowed)}) — операция запрещена"
        )
