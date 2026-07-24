"""Построение Google Ads клиента из .env + замок аккаунтов (раздельно ЧТЕНИЕ и МУТАЦИИ).

⚠️ Менеджерский аккаунт (MCC) содержит реальные клиентские аккаунты. Решение владельца 2026-07:
Draft-only доктрина СНЯТА — бот готов менять ВСЕ аккаунты, ВИДИМЫЕ ему под его MCC. ТОЧНЫЙ
контракт замка МУТАЦИЙ (ensure_allowed), сверенный с кодом ниже:
  1) КОД-минимум `ALLOWED_CEILING = {Draft}` — env не может его ПОНИЗИТЬ/убрать (Draft всегда в
     потолке). Это МИНИМУМ, а не «только Draft»;
  2) ЭФФЕКТИВНЫЙ потолок `allowed_ceiling()` = минимум ∪ аккаунты, ВИДИМЫЕ боту (env read-list +
     дочерние обхода MCC). Мутировать можно ТОЛЬКО видимый аккаунт (опечатка в чужой боевой id
     отсекается — его нет среди видимых). Это ГЛАВНАЯ несменяемая страховка;
  3) fail-closed на мисконфиг — пустой мутационный набор ⇒ отказ (а не «разрешено всё»);
  4) членство — `customer_id` обязан быть в мутационном наборе ⊆ потолок.
Мутационный набор:
  • сентинел `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS=all` (или `*`, `settings.allow_all_visible`) ⇒ набор =
    ВЕСЬ эффективный потолок `allowed_ceiling()` (все видимые). Это ПРОД-ДЕФОЛТ (см.
    core.config._default_mutations_all_in_prod) — прод готов из коробки;
  • иначе — явный список id (`settings.allowed_customer_ids`), чтобы СУЗИТЬ набор;
  • в dev/test пусто ⇒ мутаций нет (fail-closed).
Две несменяемые страховки поверх набора: confirm-гейт (мутация только после «да» + confirmation_id,
ensure_allowed перепроверяется на исполнении, tests/test_execute_account_binding.py) и потолок
видимости (аккаунт вне MCC немутируем). Бюджет из scheduler остаётся заблокирован всегда
(user_initiated). ЧТЕНИЕ — отдельный, более широкий замок (`ensure_read_allowed`, §8). Детали — у
`ALLOWED_CEILING`/`allowed_ceiling`/`ensure_allowed` ниже.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING

from core.config import normalize_customer_id, settings

if TYPE_CHECKING:
    from google.ads.googleads.client import GoogleAdsClient

    from ads.read import ChildAccount

# Aimash (Draft account), 775-364-3025 — базовый аккаунт МУТАЦИЙ (всегда в потолке).
DRAFT_ACCOUNT_ID = "7753643025"
# Базовый КОД-минимум потолка мутаций. Эффективный потолок считает `allowed_ceiling()` = этот минимум
# ∪ ВИДИМЫЕ боту аккаунты (env read-list + дочерние настроенного MCC). Мутационный набор
# (settings.allowed_customer_ids) ⊆ эффективного потолка (см. ensure_allowed).
#
# ✅ МУТАЦИИ на всех видимых аккаунтах (решение владельца 2026-07, Draft-only доктрина снята):
#   • ПРОД-ДЕФОЛТ — `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS=all` (settings.allow_all_visible) ⇒ мутационный
#     набор = ВЕСЬ allowed_ceiling() (Draft + read-list + дочерние обхода MCC). Аккаунт под ДРУГИМ
#     MCC требует per-account OAuth-токена (scripts/register_account.py → oauth_tokens), иначе
#     build_client его не аутентифицирует (видим для замка, но не подключён).
#   • СУЗИТЬ — явный список id в `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS` (каждый обязан быть ВИДИМ боту,
#     иначе allowed_ceiling() отсекает — защита от опечатки в чужой боевой id).
#   • dev/test пусто ⇒ мутаций нет (fail-closed).
# Исполнение привязано к proposal.customer_id и ЗАНОВО ensure_allowed(cid) на исполнении
# (tests/test_execute_account_binding.py); _present_proposal штампует АКТИВНЫЙ аккаунт (G2).
# Бюджет из scheduler остаётся заблокирован всегда (user_initiated).
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

# §8 (полный мульти-аккаунт, ЧТЕНИЕ): дочерние аккаунты, ОБНАРУЖЕННЫЕ обходом разрешённого MCC
# (`discover_read_children` под замком `ensure_manager_allowed`) на старте бота. Это эффективный
# read-allow-list, ПРОИЗВОДНЫЙ ОТ КОДА, а не из env-строки: наполняется только листами настроенных
# MCC (`settings.login_customer_id_set`). Пусто ⇒ ничего не добавляет (fail-closed сохраняется:
# при отсутствии обхода читаем только мутационный аккаунт + env read-list). ⚠️ МУТАЦИИ этим НЕ
# затрагиваются — у них свой узкий замок с код-потолком `ALLOWED_CEILING`; чтение дочернего НЕ даёт
# права его менять (инвариант test_mutation_lock_unchanged_by_read_allowlist).
_READ_DISCOVERED: set[str] = set()

# §8 (полный мульти-аккаунт, UI-пикер): МЕТА обнаруженных дочерних (id → ChildAccount с именем/
# валютой/статусом) — ТОЛЬКО для ОТОБРАЖЕНИЯ в пикере аккаунтов (/report /export /sheets /account),
# чтобы не крутить обход MCC повторно на каждый /report. ⚠️ НЕ авторизация: доступ по-прежнему решает
# `ensure_read_allowed` (id-набор `_READ_DISCOVERED` + env + мутационный). Пустой meta ⇒ пикер падает
# на id-метки, доступ НЕ открывается. Наполняется вместе с `_READ_DISCOVERED` в `discover_read_children`.
_READ_CHILDREN_META: dict[str, "ChildAccount"] = {}

# 2.3 (аудит 2026-07-06): МЕТА НЕАКТИВНЫХ дочерних (CANCELED/SUSPENDED/CLOSED) из обхода MCC.
# НЕ авторизация по умолчанию: в `_READ_DISCOVERED` их нет ⇒ авто-пикеры/scheduler/allowed_ceiling
# не затронуты. Разрешает ТОЛЬКО ЯВНОЕ чтение (`ensure_read_allowed(..., explicit=True)`) — история
# приостановленного аккаунта по прямому запросу id/имени; Google, вероятно, всё равно откажет
# (CUSTOMER_NOT_ENABLED) — бот покажет честную причину, а не generic-ошибку.
_READ_INACTIVE_META: dict[str, "ChildAccount"] = {}


def set_discovered_inactive_children_meta(children: Iterable["ChildAccount"]) -> int:
    """Заменить мета неактивных дочерних (зеркало set_discovered_read_children_meta). Идемпотентно."""
    _READ_INACTIVE_META.clear()
    for ch in children:
        cid = normalize_customer_id(ch.id)
        if cid:
            _READ_INACTIVE_META[cid] = ch
    return len(_READ_INACTIVE_META)


def discovered_inactive_children_meta() -> dict[str, "ChildAccount"]:
    """Копия мета неактивных дочерних (id → ChildAccount) — для явного резолва по id/имени и
    честных сообщений «аккаунт в статусе X». Пусто, пока не прошёл обход MCC."""
    return dict(_READ_INACTIVE_META)


def set_discovered_read_children(ids: Iterable[str]) -> int:
    """Заменить набор обнаруженных дочерних (read-only §8) на нормализованные `ids`. Возвращает
    размер набора. Пустой вход очищает набор (вернёт к чтению только мутационного + env read-list).

    ⚠️ id-only контракт СОХРАНЁН (тесты зовут с plain-id/пустым списком). Meta-набор для пикера —
    ОТДЕЛЬНЫЙ сеттер `set_discovered_read_children_meta` (наполняет `discover_read_children`)."""
    _READ_DISCOVERED.clear()
    for x in ids:
        cid = normalize_customer_id(x)
        if cid:
            _READ_DISCOVERED.add(cid)
    return len(_READ_DISCOVERED)


def set_discovered_read_children_meta(children: Iterable["ChildAccount"]) -> int:
    """Заменить МЕТА обнаруженных дочерних (только для отображения в пикере). Ключ — нормализованный
    id, значение — ChildAccount (имя/валюта/статус). Идемпотентно (полная пересборка). НЕ влияет на
    авторизацию (её держит `_READ_DISCOVERED`/`ensure_read_allowed`)."""
    _READ_CHILDREN_META.clear()
    for ch in children:
        cid = normalize_customer_id(ch.id)
        if cid:
            _READ_CHILDREN_META[cid] = ch
    return len(_READ_CHILDREN_META)


def discovered_read_children() -> set[str]:
    """Копия набора обнаруженных обходом MCC дочерних (§8) — для планировщика (кому слать плановый
    отчёт/аномалии по всем дочерним). Копия, чтобы вызывающий не мутировал внутренний набор."""
    return set(_READ_DISCOVERED)


def discovered_read_children_meta() -> dict[str, "ChildAccount"]:
    """Копия МЕТА обнаруженных дочерних (id → ChildAccount) — для пикера аккаунтов в боте. Копия
    словаря (значения-датаклассы разделяемы — их не мутируем). Пусто, пока не прошёл обход MCC."""
    return dict(_READ_CHILDREN_META)


async def discover_read_children() -> int:
    """§8: обойти КАЖДЫЙ настроенный MCC (`settings.login_customer_id_set`) через
    `ads.read.list_child_accounts` (замок обхода `ensure_manager_allowed`) и запомнить лист-аккаунты
    (не-менеджерские) как эффективный read-allow-list (`_READ_DISCOVERED`). READ-ONLY.

    Вызывать на СТАРТЕ бота (после `load_oauth_cache`, чтобы для не-основных MCC были per-account
    креды). Сбой обхода одного MCC логируем и ПРОПУСКАЕМ — не роняем старт (тот MCC просто не
    попадёт в read-набор; сводка честно покажет пропуски). Возвращает число обнаруженных дочерних.
    Идемпотентна: каждый вызов пересобирает набор заново (перезапуск/перекраул дочерних)."""
    from core.logging import log
    from core.resilience import run_ads_read_call

    managers = settings.login_customer_id_set
    if not managers:
        return 0  # нет настроенных MCC ⇒ обход невозможен (fail-closed, набор остаётся пустым)
    found: set[str] = set()
    found_meta: dict[str, "ChildAccount"] = {}  # id → ChildAccount (для пикера; не авторизация)
    total_leaves = 0  # P1-10: сколько НЕ-менеджерских дочерних всего (для диагностики «почему N»)
    inactive: list[str] = []  # id + статус пропущенных не-ENABLED (для лога)
    inactive_meta: dict[str, "ChildAccount"] = {}  # 2.3: мета неактивных (явное чтение по запросу)
    for mid in sorted(managers):
        if not mid:
            continue  # defense-in-depth: пустой id (мусор из конфига) — не делаем ga.search('')
        try:
            from ads.read import list_child_accounts  # ленивый импорт: избегаем цикла с ads.read

            client = await build_client_async(mid)  # холодная сборка (после /refresh) — вне loop
            children = await run_ads_read_call(
                list_child_accounts, client, mid, label="mcc_discover"
            )
        except Exception as e:  # noqa: BLE001 — обход одного MCC не критичен для старта
            log.warning("mcc discover: MCC %s не обойдён (%s)", mid, type(e).__name__)
            continue
        for ch in children:
            if ch.manager:
                continue
            total_leaves += 1
            # §8/A3: не-ENABLED дочерние (CANCELED/SUSPENDED/CLOSED/…) НЕ кладём в read-набор —
            # их запрос всё равно упрётся в PERMISSION_DENIED / CUSTOMER_NOT_ENABLED (это флудило
            # scheduler-аномалии и /diag каждый цикл) и они не нужны в пикерах отчётов/экспорта.
            # /mcc-сводка их всё равно покажет отдельной секцией «неактивные» (reports.mcc._is_active),
            # т.к. заново обходит MCC. Мутационный замок (ensure_allowed) — отдельный, не затронут.
            if (ch.status or "").upper() != "ENABLED":
                _icid = normalize_customer_id(ch.id)
                inactive.append(f"{_icid}:{(ch.status or '?')}")
                if _icid:
                    inactive_meta[_icid] = ch  # 2.3: явное чтение истории по прямому запросу
                continue
            cid = normalize_customer_id(ch.id)
            if cid:
                found.add(cid)
                found_meta[cid] = ch  # имя/валюта/статус для UI-пикера
    n = set_discovered_read_children(found)
    set_discovered_read_children_meta(found_meta.values())  # meta для пикера (не влияет на доступ)
    set_discovered_inactive_children_meta(inactive_meta.values())  # 2.3: НЕ в _READ_DISCOVERED
    # P1-10: явная диагностика «почему в пикере N аккаунтов» — всего дочерних / ENABLED показано /
    # неактивных скрыто (со статусами). Отвечает на «почему только 3»: остальные не-ENABLED.
    log.info(
        "mcc discover (§8): дочерних всего=%d, ENABLED (в пикере/read)=%d, скрыто неактивных=%d%s",
        total_leaves,
        n,
        len(inactive),
        (" [" + ", ".join(inactive[:20]) + "]") if inactive else "",
    )
    return n


# ── Ленивая само-починка обхода MCC перед показом пикера (2026-07) ────────────────────────
# Если обход на СТАРТЕ не прошёл (транзиентный сбой/таймаут холодного старта SDK/OAuth), набор
# `_READ_DISCOVERED` пуст и ВСЕ пикеры аккаунтов деградируют на Draft + env read-list — до суточного
# re-discovery (`scheduler.jobs.run_mcc_rediscovery`) или ручного /refresh: окно «часть аккаунтов
# пропала везде» может тянуться до `settings.mcc_rediscovery_hours`. Чтобы СЛЕДУЮЩИЙ тап сам починил
# список, перечислитель пикеров (`bot.main._read_account_rows`) зовёт `ensure_read_children_discovered`.
# Лок (без стампеда параллельных пикеров) создаём лениво — на первом await есть running loop.
_discover_lock: asyncio.Lock | None = None
_discover_last_attempt: float = 0.0
_DISCOVER_RETRY_COOLDOWN_S = 60.0  # не бьём Google Ads на КАЖДЫЙ пикер, если обход стабильно падает


def _discovery_lock() -> asyncio.Lock:
    """Лениво создать лок обхода (module-level asyncio.Lock() до старта loop хрупок между версиями)."""
    global _discover_lock
    if _discover_lock is None:
        _discover_lock = asyncio.Lock()
    return _discover_lock


async def ensure_read_children_discovered() -> int:
    """Гарантировать, что обход MCC ВЫПОЛНЕН (ленивая само-починка перед перечислением аккаунтов).

    Быстрый путь (нулевая латентность здорового старта): набор дочерних НЕПУСТ ⇒ no-op. Пуст И
    настроен MCC ⇒ пробуем обойти ЕЩЁ раз: под локом (double-checked — параллельные пикеры не
    запустят N обходов) и с кулдауном `_DISCOVER_RETRY_COOLDOWN_S` (обход стабильно падает — не
    долбим API на каждый пикер). Возвращает размер набора после попытки.

    READ-ONLY: замок обхода `ensure_manager_allowed` — внутри `discover_read_children`; исключение
    НЕ роняет вызывающий пикер (best-effort). ⚠️ Мутации/потолок этим НЕ расширяются: наполняется
    тот же `_READ_DISCOVERED` (ENABLED-листы), что и на старте — чинится лишь fail-quiet «обход не
    прошёл» без рестарта, набор мутаций (`ensure_allowed`) не затрагивается."""
    global _discover_last_attempt
    if _READ_DISCOVERED:
        return len(_READ_DISCOVERED)  # обход уже дал детей — быстрый путь (не трогаем API)
    if not settings.login_customer_id_set:
        return 0  # нет настроенного MCC ⇒ обход невозможен (fail-closed; набор остаётся пуст)
    now = time.monotonic()
    if (now - _discover_last_attempt) < _DISCOVER_RETRY_COOLDOWN_S:
        return len(_READ_DISCOVERED)  # недавно пробовали и не вышло — ждём кулдаун (без спама API)
    async with _discovery_lock():
        if _READ_DISCOVERED:  # другой корутин успел обойти, пока ждали лок (double-check)
            return len(_READ_DISCOVERED)
        _discover_last_attempt = time.monotonic()
        try:
            return await discover_read_children()
        except Exception as e:  # noqa: BLE001 — само-починка best-effort, не роняем пикер
            from core.logging import log

            log.warning("mcc discover (lazy): повторный обход не выполнен: %s", type(e).__name__)
            return len(_READ_DISCOVERED)


def has_oauth_runtime(account: str) -> bool:
    """2.5: есть ли в рантайм-кэше per-account OAuth-креды для аккаунта (для /mutready-чек-листа).
    ТОЛЬКО bool — сам секрет наружу не выносим (golden rule 5)."""
    return normalize_customer_id(account) in _OAUTH_RUNTIME


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


# Дедлайн gRPC на ЧТЕНИЯХ. google-ads v24 по умолчанию НЕ ставит дедлайн (`timeout` у search =
# _MethodDefault) → зависший RPC живёт вечно. `asyncio.timeout` в core.resilience отменяет только
# КОРУТИНУ ожидания: поток `asyncio.to_thread` с висящим gRPC продолжает жить, а run_ads_read_call
# ретраит до ADS_MAX_ATTEMPTS раз → до 4 мёртвых потоков на один /report; пул потоков забивается,
# бот перестаёт читать вообще. Поэтому дедлайн ставим в САМ SDK-вызов, в единственной точке сборки
# клиента (call-site'ов ga.search ~40 — их не обойти).
# МУТАЦИИ НЕ ТРОГАЕМ ОСОЗНАННО: DEADLINE_EXCEEDED на mutate не означает, что операция не
# применилась (запрос мог доехать) → дедлайн там превратил бы висящий поток в риск задвоения.
_READ_DEADLINE_METHODS: dict[str, tuple[str, ...]] = {
    "GoogleAdsService": ("search", "search_stream"),
    "KeywordPlanIdeaService": ("generate_keyword_ideas",),
}


def _read_deadline_s() -> float:
    """Чуть РАНЬШЕ asyncio-таймаута (core.resilience.ADS_TIMEOUT_S = settings.ads_timeout_s): хотим
    DeadlineExceeded ОТ SDK (поток освободится сам), а не отмену корутины поверх живого RPC."""
    return max(5.0, float(settings.ads_timeout_s) - 5.0)


class _DeadlineService:
    """Прокси read-сервиса SDK: подставляет `timeout=` в чтения, если call-site не задал свой.
    Остальные методы (включая mutate) проксируются без изменений."""

    __slots__ = ("_svc", "_methods", "_timeout")

    def __init__(self, svc: object, methods: tuple[str, ...], timeout: float) -> None:
        self._svc = svc
        self._methods = methods
        self._timeout = timeout

    def __getattr__(self, name: str):
        attr = getattr(self._svc, name)
        if name not in self._methods:
            return attr

        def _with_deadline(*args, **kwargs):
            kwargs.setdefault("timeout", self._timeout)
            return attr(*args, **kwargs)

        return _with_deadline


class _DeadlineClient:
    """Прокси GoogleAdsClient: get_service отдаёт read-сервисы обёрнутыми (см. _READ_DEADLINE_METHODS).
    Всё прочее (get_type, enums, copy_from, мутационные сервисы) — как у настоящего клиента."""

    __slots__ = ("_client",)

    def __init__(self, client: object) -> None:
        self._client = client

    def get_service(self, name: str, **kwargs):
        svc = self._client.get_service(name, **kwargs)  # type: ignore[attr-defined]
        methods = _READ_DEADLINE_METHODS.get(name)
        return _DeadlineService(svc, methods, _read_deadline_s()) if methods else svc

    def __getattr__(self, name: str):
        return getattr(self._client, name)


def build_client(customer_id: str | None = None) -> "GoogleAdsClient":
    """SDK-клиент для аккаунта. Без аргумента (или Draft) — из .env; кэш по нормализованному id.
    Импорт SDK ленивый: ensure_allowed/константы доступны без google-ads.

    §8 (активно): для не-Draft с зарегистрированным per-account токеном (oauth_tokens → _OAUTH_RUNTIME
    через load_oauth_cache на старте) конфиг берётся из БД (свой refresh-токен + свой login_customer_id,
    расшифровка core.secrets); иначе — .env (один тест-MCC единым токеном покрывает тест-дочерних).
    Выбор кредов вынесен в `_cfg_for`. МУТАЦИИ это НЕ расширяет — замок `ensure_allowed`=Draft отдельно."""
    from google.ads.googleads.client import GoogleAdsClient

    cid = normalize_customer_id(customer_id) if customer_id else DRAFT_ACCOUNT_ID
    cached = _CLIENT_CACHE.get(cid)
    if cached is not None:
        return cached
    # version= делает `settings.google_ads_api_version` АВТОРИТЕТНЫМ (иначе SDK молча берёт свой
    # дефолт — сейчас совпадает с пином google-ads>=31.1,<32=v24, но не гарантированно при апгрейде
    # библиотеки). Рассинхрон версии теперь = явный отказ get_service, а не тихий дрейф. Пин lib и
    # эту строку перепроверять скилом gads-version (v24 сансет ~май 2027).
    raw = GoogleAdsClient.load_from_dict(_cfg_for(cid), version=settings.google_ads_api_version)
    # Прокси с gRPC-дедлайном на чтениях (см. _DeadlineClient): без него зависший RPC держит поток
    # to_thread вечно, а ретраи read-пути множат такие потоки. Мутационные сервисы не затронуты.
    client: GoogleAdsClient = _DeadlineClient(raw)  # type: ignore[assignment]
    _CLIENT_CACHE[cid] = client
    return client


async def build_client_async(customer_id: str | None = None) -> "GoogleAdsClient":
    """`build_client` для async-хендлеров: кэш-хит — мгновенно, ХОЛОДНЫЙ путь (сборка SDK-клиента
    ~0.5–2 c: первый вызов по аккаунту / после `clear_client_cache` в /refresh) уходит в поток,
    чтобы не замораживать event loop (латентность — §15). Семантика идентична `build_client`."""
    import asyncio

    cid = normalize_customer_id(customer_id) if customer_id else DRAFT_ACCOUNT_ID
    cached = _CLIENT_CACHE.get(cid)
    if cached is not None:
        return cached
    return await asyncio.to_thread(build_client, customer_id)


def _cfg_for(cid: str) -> dict:
    """Конфиг google-ads для нормализованного cid. §8 (активно): не-Draft с зарегистрированным
    per-account токеном (oauth_tokens → _OAUTH_RUNTIME через load_oauth_cache) → свой refresh-токен +
    свой login_customer_id (MCC). Иначе (Draft или нет записи) — .env (один тест-MCC единым токеном
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


def allowed_ceiling() -> frozenset[str]:
    """G1: ЭФФЕКТИВНЫЙ потолок мутаций = базовый код-минимум `ALLOWED_CEILING` ({DRAFT}) ∪ аккаунты,
    которые бот ВИДИТ (env `GOOGLE_ADS_READ_CUSTOMER_IDS` ∪ дочерние, обнаруженные обходом настроенного
    MCC `_READ_DISCOVERED`). Мутационный набор (`settings.allowed_customer_ids`) обязан быть ⊆ этого
    потолка (см. `ensure_allowed`) — env НЕ может включить мутации на аккаунте, которого бот не видит
    (не под нашим MCC / не в read-list). Так владелец включает мутации УПРАВЛЯЕМЫМ списком, но только
    среди видимых аккаунтов (не может опечаткой открыть чужой боевой id).

    ⚠️ Чтение дочернего само по себе НЕ делает его мутируемым: он попадает лишь в ПОТОЛОК, а мутация
    требует ещё и членства в `settings.allowed_customer_ids` (явный opt-in владельца) — инвариант
    `test_grant_does_not_open_mutations`/`test_mutation_lock_unchanged_by_read_allowlist`."""
    visible = {normalize_customer_id(x) for x in settings.read_customer_ids} | set(_READ_DISCOVERED)
    return ALLOWED_CEILING | frozenset(i for i in visible if i)


def ensure_allowed(customer_id: str) -> None:
    """Замок аккаунта МУТАЦИЙ. Бросает PermissionError на любой запрет.

    Это единственная точка, через которую ВСЕ мутации проверяют customer_id. Нормализуем id (только
    цифры), поэтому '775-364-3025' и '7753643025' эквивалентны.

    Мутационный набор (решение владельца 2026-07, Draft-only доктрина снята):
      • `settings.allow_all_visible` (сентинел `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS=all`, прод-дефолт)
        ⇒ набор = ВЕСЬ `allowed_ceiling()` (все видимые: Draft ∪ read-list ∪ дочерние обхода MCC);
      • иначе — явный `settings.allowed_customer_ids`, ограниченный тем же потолком (способ СУЗИТЬ).
    Потолок видимости и confirm-гейт остаются несменяемыми страховками: аккаунт вне MCC немутируем,
    «да» + confirmation_id обязательны (перепроверка на исполнении). В dev/test пусто ⇒ fail-closed.
    """
    cid = normalize_customer_id(customer_id)
    ceiling = allowed_ceiling()
    # Сентинел «all» ⇒ мутационный набор = весь видимый потолок (динамически ограничен фактически
    # обнаруженным набором: сбой discovery ⇒ мутабелен лишь пол потолка {Draft}, а не больше).
    if settings.allow_all_visible:
        allowed = set(ceiling)
    else:
        allowed = {normalize_customer_id(x) for x in settings.allowed_customer_ids}

    # (2) fail-closed: без явного allow-list (и без сентинела «all») ничего не разрешаем.
    if not allowed:
        raise PermissionError(
            "allowed_customer_ids пуст — операции запрещены (fail-closed). "
            f"Задай GOOGLE_ADS_ALLOWED_CUSTOMER_IDS=all (все видимые) или ={DRAFT_ACCOUNT_ID} в .env"
        )
    # (1) потолок: мутировать можно только аккаунт, который бот ВИДИТ (Draft/read/discovered) —
    # чужой/боевой id вне видимости не пройдёт (при «all» набор == потолок, проверка тривиальна).
    if not allowed <= ceiling:
        raise PermissionError(
            f"allowed_customer_ids {sorted(allowed)} выходит за потолок мутаций "
            f"{sorted(ceiling)} — аккаунт должен быть виден боту (Draft, GOOGLE_ADS_READ_CUSTOMER_IDS "
            "или дочерний настроенного MCC)"
        )
    # (3) членство.
    if cid not in allowed:
        raise PermissionError(
            f"customer_id {cid} не разрешён (allow-list {sorted(allowed)}) — операция запрещена"
        )


def ensure_read_allowed(customer_id: str, *, explicit: bool = False) -> None:
    """Замок ЧТЕНИЯ per-account (§8: сводный отчёт по дочерним аккаунтам MCC).

    Шире мутационного, но НЕ открытый. Множество разрешённого чтения =
    мутационный allow-list (`settings.allowed_customer_ids`) ∪ read-allow-list
    (`settings.read_customer_ids` из env `GOOGLE_ADS_READ_CUSTOMER_IDS`) ∪ ОБНАРУЖЕННЫЕ обходом MCC
    дочерние (`_READ_DISCOVERED`, §8-полный-мульти-аккаунт — заполняется `discover_read_children`
    на старте под замком `ensure_manager_allowed`). Все три пусты ⇒ отказ (fail-closed). Нормализуем
    id (только цифры), '775-364-3025' ≡ '7753643025'.

    explicit=True (2.3) — ЯВНЫЙ запрос оператора по id/имени (`/account <id>`, NL): множество
    дополняется НЕАКТИВНЫМИ дочерними наших MCC (`_READ_INACTIVE_META`) — чтение истории
    CANCELED/SUSPENDED аккаунта по прямому запросу. Дефолт False = байт-в-байт старое поведение;
    авто-пикеры/scheduler/дефолты explicit НЕ ставят. Неактивные НЕ попадают в `allowed_ceiling`
    (он строится от `_READ_DISCOVERED`) — потолок мутаций не расширяется (golden rule 9).

    ⚠️ Мутации этим НЕ затрагиваются: у них свой узкий замок `ensure_allowed` с код-потолком
    `ALLOWED_CEILING`. Расширение read-allow-list (перечисление дочерних MCC) НЕ даёт права их
    менять — мутация на дочернем всё равно упрётся в `ensure_allowed`. read-allow-list по
    умолчанию ПУСТ ⇒ чтение, как и мутации, только на разрешённый аккаунт (поведение не меняется).
    """
    cid = normalize_customer_id(customer_id)
    mutate = {normalize_customer_id(x) for x in settings.allowed_customer_ids}
    read = {normalize_customer_id(x) for x in settings.read_customer_ids}
    allowed = mutate | read | _READ_DISCOVERED  # ∪ дочерние, обнаруженные обходом MCC (§8)
    if explicit:  # 2.3: только по прямому запросу оператора — неактивные дочерние наших MCC
        allowed = allowed | set(_READ_INACTIVE_META)

    # fail-closed: без явного списка И без обнаруженных дочерних ничего не читаем per-account.
    if not allowed:
        raise PermissionError(
            "ни allowed_customer_ids, ни read_customer_ids, ни обход MCC не дали аккаунтов — "
            f"чтение запрещено (fail-closed). Задай GOOGLE_ADS_ALLOWED_CUSTOMER_IDS={DRAFT_ACCOUNT_ID} в .env"
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
    if not mid:
        # Defense-in-depth (golden rule #10, fail-closed): пустой/ненормализуемый manager_id —
        # это НЕ валидный MCC. Раньше '' мог оказаться членом множества и пройти проверку ниже
        # (fail-open) → ga.search(customer_id='') падал в проде. Явно отказываем ДО членства.
        raise PermissionError("manager_id пуст/не нормализуется — обход MCC запрещён (fail-closed)")
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
