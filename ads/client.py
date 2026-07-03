"""Построение Google Ads клиента из .env + замок единственного аккаунта.

⚠️ Менеджерский аккаунт (MCC) содержит реальные клиентские аккаунты. Бот имеет право
читать/менять ТОЛЬКО один аккаунт — Aimash (Draft). Замок трёхслойный:
  1) потолок в КОДЕ (ALLOWED_CEILING) — env не может его расширить;
  2) fail-closed — пустой allow-list => отказ (а не «разрешено всё»);
  3) членство — customer_id обязан быть в allow-list ⊆ потолок.
Любое расширение круга аккаунтов = ОСОЗНАННАЯ правка этого файла, не строки в .env.
"""

from __future__ import annotations

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
# ✅ ВКЛЮЧЕНИЕ МУТАЦИЙ на конкретном аккаунте (G, «управляемый список», решение владельца 2026-07) —
# УПРАВЛЯЕМО КОНФИГОМ, без правки кода на каждый аккаунт:
#   1) добавить его id в `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS` (env) — но он обязан быть ВИДИМ боту
#      (Draft / GOOGLE_ADS_READ_CUSTOMER_IDS / дочерний настроенного MCC), иначе allowed_ceiling()
#      его не пропустит (защита от опечатки в чужой боевой id);
#   2) если аккаунт под ДРУГИМ MCC — зарегистрировать его per-account OAuth-токен
#      (scripts/register_account.py → oauth_tokens), иначе build_client не аутентифицирует его.
# Исполнение уже привязано к proposal.customer_id и ЗАНОВО ensure_allowed(cid) на исполнении
# (tests/test_execute_account_binding.py); _present_proposal штампует АКТИВНЫЙ аккаунт (G2).
# По умолчанию allowed_customer_ids={Draft} ⇒ поведение НЕ меняется, пока владелец не включит аккаунт.
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
            # §8/A3: не-ENABLED дочерние (CANCELED/SUSPENDED/CLOSED/…) НЕ кладём в read-набор —
            # их запрос всё равно упрётся в PERMISSION_DENIED / CUSTOMER_NOT_ENABLED (это флудило
            # scheduler-аномалии и /diag каждый цикл) и они не нужны в пикерах отчётов/экспорта.
            # /mcc-сводка их всё равно покажет отдельной секцией «неактивные» (reports.mcc._is_active),
            # т.к. заново обходит MCC. Мутационный замок (ensure_allowed) — отдельный, не затронут.
            if (ch.status or "").upper() != "ENABLED":
                continue
            cid = normalize_customer_id(ch.id)
            if cid:
                found.add(cid)
                found_meta[cid] = ch  # имя/валюта/статус для UI-пикера
    n = set_discovered_read_children(found)
    set_discovered_read_children_meta(found_meta.values())  # meta для пикера (не влияет на доступ)
    if n:
        log.info("mcc discover: дочерних аккаунтов доступно на чтение (§8): %d", n)
    return n


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
    client = GoogleAdsClient.load_from_dict(_cfg_for(cid), version=settings.google_ads_api_version)
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

    G1: мутационный набор = `settings.allowed_customer_ids` (env, opt-in владельца), ограниченный
    ЭФФЕКТИВНЫМ потолком `allowed_ceiling()` (Draft ∪ видимые аккаунты). По умолчанию allow-list =
    {Draft} ⇒ поведение как раньше (мутации только на Draft), пока владелец не добавит видимый аккаунт.
    """
    cid = normalize_customer_id(customer_id)
    allowed = {normalize_customer_id(x) for x in settings.allowed_customer_ids}
    ceiling = allowed_ceiling()

    # (2) fail-closed: без явного allow-list ничего не разрешаем.
    if not allowed:
        raise PermissionError(
            "allowed_customer_ids пуст — операции запрещены (fail-closed). "
            f"Задай GOOGLE_ADS_ALLOWED_CUSTOMER_IDS={DRAFT_ACCOUNT_ID} в .env"
        )
    # (1) потолок: env может включить мутации только на аккаунте, который бот ВИДИТ (Draft/read/
    # discovered) — чужой/боевой id вне видимости не пройдёт.
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


def ensure_read_allowed(customer_id: str) -> None:
    """Замок ЧТЕНИЯ per-account (§8: сводный отчёт по дочерним аккаунтам MCC).

    Шире мутационного, но НЕ открытый. Множество разрешённого чтения =
    мутационный allow-list (`settings.allowed_customer_ids`) ∪ read-allow-list
    (`settings.read_customer_ids` из env `GOOGLE_ADS_READ_CUSTOMER_IDS`) ∪ ОБНАРУЖЕННЫЕ обходом MCC
    дочерние (`_READ_DISCOVERED`, §8-полный-мульти-аккаунт — заполняется `discover_read_children`
    на старте под замком `ensure_manager_allowed`). Все три пусты ⇒ отказ (fail-closed). Нормализуем
    id (только цифры), '775-364-3025' ≡ '7753643025'.

    ⚠️ Мутации этим НЕ затрагиваются: у них свой узкий замок `ensure_allowed` с код-потолком
    `ALLOWED_CEILING`. Расширение read-allow-list (перечисление дочерних MCC) НЕ даёт права их
    менять — мутация на дочернем всё равно упрётся в `ensure_allowed`. read-allow-list по
    умолчанию ПУСТ ⇒ чтение, как и мутации, только на разрешённый аккаунт (поведение не меняется).
    """
    cid = normalize_customer_id(customer_id)
    mutate = {normalize_customer_id(x) for x in settings.allowed_customer_ids}
    read = {normalize_customer_id(x) for x in settings.read_customer_ids}
    allowed = mutate | read | _READ_DISCOVERED  # ∪ дочерние, обнаруженные обходом MCC (§8)

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
