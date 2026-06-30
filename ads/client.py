"""Построение Google Ads клиента из .env + замок единственного аккаунта.

⚠️ Менеджерский аккаунт (MCC) содержит реальные клиентские аккаунты. Бот имеет право
читать/менять ТОЛЬКО один аккаунт — Aimash (Draft). Замок трёхслойный:
  1) потолок в КОДЕ (ALLOWED_CEILING) — env не может его расширить;
  2) fail-closed — пустой allow-list => отказ (а не «разрешено всё»);
  3) членство — customer_id обязан быть в allow-list ⊆ потолок.
Любое расширение круга аккаунтов = ОСОЗНАННАЯ правка этого файла, не строки в .env.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.config import normalize_customer_id, settings

if TYPE_CHECKING:
    from google.ads.googleads.client import GoogleAdsClient

# Aimash (Draft account), 775-364-3025 — ЕДИНСТВЕННЫЙ аккаунт, на котором разрешены МУТАЦИИ.
DRAFT_ACCOUNT_ID = "7753643025"
# Жёсткий потолок МУТАЦИЙ: env (allowed_customer_ids) не может выйти за этот набор.
# Расширение круга мутаций = ОСОЗНАННАЯ правка этого файла (см. docstring модуля).
ALLOWED_CEILING = frozenset({DRAFT_ACCOUNT_ID})

# Кэш SDK-клиентов по нормализованному customer_id. Раньше был @lru_cache(maxsize=1) — один клиент
# на процесс. Под мультиаккаунт (§8): у разных аккаунтов будут разные refresh-токен/login_customer_id
# (Фаза 3, oauth_tokens), поэтому кэшируем ПО id. В тест-фазе все тест-дочерние под одним MCC
# покрываются единым env-токеном → конфиг пока общий (см. _env_cfg).
_CLIENT_CACHE: dict[str, "GoogleAdsClient"] = {}

# Рантайм-кэш РАСШИФРОВАННЫХ per-account OAuth-кредов (§8/Фаза 3): account → (refresh_token,
# login_customer_id). Заполняется load_oauth_cache() из таблицы oauth_tokens (decrypt) на старте.
# ⚠️ Секрет В ПАМЯТИ (не логируем, не в repr) — golden rule #5. Пусто ⇒ build_client падает на
# .env (обратная совместимость: тест-дочерние под одним MCC покрыты единым токеном).
_OAUTH_RUNTIME: dict[str, tuple[str, str | None]] = {}


def set_oauth_runtime(account: str, refresh_token: str, login_customer_id: str | None) -> None:
    """Положить расшифрованные креды аккаунта в рантайм-кэш + сбросить SDK-клиент этого id
    (чтобы пересобрался на новом токене при ротации). Секрет не логируем."""
    cid = normalize_customer_id(account)
    _OAUTH_RUNTIME[cid] = (refresh_token, normalize_customer_id(login_customer_id) or None)
    _CLIENT_CACHE.pop(cid, None)


async def load_oauth_cache() -> int:
    """Загрузить per-account OAuth-токены из БД (oauth_tokens) в рантайм-кэш: расшифровать
    refresh_token_enc (core.secrets.decrypt) и положить в _OAUTH_RUNTIME. Вызывать на СТАРТЕ бота
    (после init_db). Возвращает число загруженных аккаунтов.

    Сбой расшифровки ОДНОГО аккаунта (порча ключа/строки) логируем и ПРОПУСКАЕМ — не роняем старт
    из-за одного аккаунта; этот аккаунт просто не будет доступен (build_client упрётся в отсутствие
    кредов или откатится на .env). Секреты не логируем (только id и тип ошибки)."""
    from core.logging import log
    from core.secrets import decrypt
    from db.models import OAuthToken
    from db.session import Session
    from sqlalchemy import select

    loaded = 0
    async with Session() as s:
        rows = (await s.execute(select(OAuthToken))).scalars().all()
    for r in rows:
        try:
            token = decrypt(r.refresh_token_enc)
        except Exception as e:  # noqa: BLE001 — порча ключа/строки: пропускаем аккаунт, не старт
            log.warning("oauth: не расшифрован токен аккаунта %s (%s)", r.account, type(e).__name__)
            continue
        set_oauth_runtime(r.account, token, r.login_customer_id)
        loaded += 1
    if loaded:
        log.info("oauth: загружено per-account токенов: %d", loaded)
    return loaded


def _env_cfg() -> dict:
    """Конфиг google-ads из .env (единственный refresh-токен + MCC). Покрывает Draft и любой
    тест-дочерний под тем же login_customer_id. SecretStr раскрываем в точке использования."""
    cfg = {
        "developer_token": settings.google_ads_developer_token.get_secret_value(),
        "client_id": settings.google_ads_client_id,
        "client_secret": settings.google_ads_client_secret.get_secret_value(),
        "refresh_token": settings.google_ads_refresh_token.get_secret_value(),
        "use_proto_plus": True,
    }
    if settings.google_ads_login_customer_id:
        cfg["login_customer_id"] = settings.google_ads_login_customer_id
    return cfg


def build_client(customer_id: str | None = None) -> "GoogleAdsClient":
    """SDK-клиент для аккаунта. Без аргумента (или Draft) — из .env (байт-в-байт как раньше),
    кэш по нормализованному id. Импорт SDK ленивый: ensure_allowed/константы доступны без google-ads.

    Фаза 1 (сейчас): конфиг всегда из .env — один тест-MCC единым токеном покрывает всех тест-
    дочерних. Фаза 3 (разные MCC): для не-Draft будет ветка oauth_tokens (per-account refresh-токен
    + login_customer_id из БД, расшифровка core.secrets). Сигнатура уже принимает customer_id, чтобы
    16 вызывающих перешли на per-account вызов без новой правки контракта."""
    from google.ads.googleads.client import GoogleAdsClient

    cid = normalize_customer_id(customer_id) if customer_id else DRAFT_ACCOUNT_ID
    cached = _CLIENT_CACHE.get(cid)
    if cached is not None:
        return cached
    client = GoogleAdsClient.load_from_dict(_cfg_for(cid))
    _CLIENT_CACHE[cid] = client
    return client


def _cfg_for(cid: str) -> dict:
    """Конфиг google-ads для нормализованного cid. Фаза 3: не-Draft с зарегистрированным per-account
    токеном (oauth_tokens → _OAUTH_RUNTIME через load_oauth_cache) → свой refresh-токен + свой
    login_customer_id (MCC). Иначе (Draft или нет записи) — .env (один тест-MCC единым токеном
    покрывает тест-дочерних). Вынесен из build_client, чтобы тестировать выбор кредов без SDK."""
    creds = _OAUTH_RUNTIME.get(cid) if cid != DRAFT_ACCOUNT_ID else None
    if creds is None:
        return _env_cfg()
    refresh_token, login_cid = creds
    cfg = {
        "developer_token": settings.google_ads_developer_token.get_secret_value(),
        "client_id": settings.google_ads_client_id,
        "client_secret": settings.google_ads_client_secret.get_secret_value(),
        "refresh_token": refresh_token,
        "use_proto_plus": True,
    }
    if login_cid:
        cfg["login_customer_id"] = login_cid
    return cfg


def clear_client_cache(customer_id: str | None = None) -> None:
    """Сбросить кэш клиента(ов). Без аргумента — весь кэш; с id — только его (нужно при ротации
    refresh-токена в Фазе 3, чтобы не отдать устаревший клиент)."""
    if customer_id is None:
        _CLIENT_CACHE.clear()
    else:
        _CLIENT_CACHE.pop(normalize_customer_id(customer_id), None)


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
    в ALLOWED_CEILING (тот — потолок per-account мутаций над дочерним Aimash Draft). Разрешены
    ТОЛЬКО настроенные MCC (settings.login_customer_id_set = основной login_customer_id ∪ доп.
    список); пустое множество ⇒ fail-closed (обход запрещён). Нормализуем id, поэтому
    '775-364-3025' и '7753643025' эквивалентны. Под мультиаккаунт (§8/Фаза 3) аккаунты могут жить
    под РАЗНЫМИ MCC — поэтому множество, а не один скаляр (легаси-скаляр в него вложен).
    """
    mid = normalize_customer_id(manager_id)
    configured = settings.login_customer_id_set
    if not configured:
        raise PermissionError(
            "login_customer_id не задан — обход MCC запрещён (fail-closed). "
            "Задай GOOGLE_ADS_LOGIN_CUSTOMER_ID в .env."
        )
    if mid not in configured:
        raise PermissionError(
            f"manager_id {mid} не среди настроенных MCC {sorted(configured)} — обход запрещён"
        )
