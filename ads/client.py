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


def ensure_read_allowed(customer_id: str) -> None:
    """Замок ЧТЕНИЯ per-account (§8: сводный отчёт по дочерним аккаунтам MCC).

    Шире мутационного, но НЕ открытый. Множество разрешённого чтения =
    мутационный allow-list (`settings.allowed_customer_ids`) ∪ read-allow-list
    (`settings.read_customer_ids` из env `GOOGLE_ADS_READ_CUSTOMER_IDS`). Пустые оба ⇒ отказ
    (fail-closed). Нормализуем id (только цифры), '775-364-3025' ≡ '7753643025'.

    ⚠️ Мутации этим НЕ затрагиваются: у них свой узкий замок `ensure_allowed` с код-потолком
    `ALLOWED_CEILING`. Расширение read-allow-list (перечисление дочерних MCC) НЕ даёт права их
    менять — мутация на дочернем всё равно упрётся в `ensure_allowed`. read-allow-list по
    умолчанию ПУСТ ⇒ чтение, как и мутации, только на разрешённый аккаунт (поведение не меняется).
    """
    cid = normalize_customer_id(customer_id)
    mutate = {normalize_customer_id(x) for x in settings.allowed_customer_ids}
    read = {normalize_customer_id(x) for x in settings.read_customer_ids}
    allowed = mutate | read

    # fail-closed: без явного списка ничего не читаем per-account.
    if not allowed:
        raise PermissionError(
            "ни allowed_customer_ids, ни read_customer_ids не заданы — чтение запрещено "
            f"(fail-closed). Задай GOOGLE_ADS_ALLOWED_CUSTOMER_IDS={DRAFT_ACCOUNT_ID} в .env"
        )
    if cid not in allowed:
        raise PermissionError(
            f"customer_id {cid} не разрешён для чтения (read allow-list {sorted(allowed)}) — "
            "операция запрещена"
        )


def ensure_manager_allowed(manager_id: str) -> None:
    """Замок для ОБХОДА MCC (чтение customer_client от имени менеджерского аккаунта).

    Отдельный чокпойнт, потому что manager_id (= login_customer_id) — это менеджер, он НЕ входит
    в ALLOWED_CEILING (тот — потолок per-account операций над дочерним Aimash Draft). Разрешён
    ТОЛЬКО настроенный login_customer_id из .env; пустой ⇒ fail-closed (обход запрещён).
    Нормализуем id, поэтому '775-364-3025' и '7753643025' эквивалентны.
    """
    mid = normalize_customer_id(manager_id)
    configured = normalize_customer_id(settings.google_ads_login_customer_id)
    if not configured:
        raise PermissionError(
            "login_customer_id не задан — обход MCC запрещён (fail-closed). "
            "Задай GOOGLE_ADS_LOGIN_CUSTOMER_ID в .env."
        )
    if mid != configured:
        raise PermissionError(f"manager_id {mid} ≠ настроенного MCC {configured} — обход запрещён")
