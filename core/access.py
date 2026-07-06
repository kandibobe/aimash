"""Пер-пользовательский доступ к аккаунтам + активный аккаунт чата (§8/§12, мультиоператор).

Бэкенд (БД), который UI бота (/account, пикеры, /grant) зовёт, чтобы:
  • узнать/сменить активный аккаунт чата (get/set_active_account → UserSettings.selected_customer_id);
  • проверить, имеет ли оператор право на аккаунт (ensure_account_allowed_for_user → account_access);
  • выдать/снять грант и перечислить доступные аккаунты;
  • зарезолвить аккаунт из свободного ввода/аргумента агента (resolve_read_account).

Замки КОМПОЗИТНЫЕ и fail-closed:
  • глобальный read-замок ads.client.ensure_read_allowed (что боту вообще разрешено читать);
  • пер-пользовательский грант (этот модуль) — кто из операторов какой аккаунт ведёт.
Draft (DRAFT_ACCOUNT_ID) доступен всем whitelisted без отдельного гранта в ЛЮБОМ режиме.

Режимы enforcement (env ACCOUNT_ACCESS_MODE, см. core.config):
  auto (дефолт) — пустая таблица account_access ⇒ legacy-проход (все whitelisted видят весь
  read-list — текущее одно-операторное поведение); ПЕРВЫЙ грант включает enforcement для всех.
  enforced — строгий даже с пустой таблицей. legacy — пер-юзер замок выключен осознанно.
"""

from __future__ import annotations

import time

from sqlalchemy import delete, select

from ads.client import DRAFT_ACCOUNT_ID, ensure_read_allowed
from core.config import normalize_customer_id, settings
from core.logging import log
from db.models import AccountAccess, UserSettings, Whitelist
from db.session import Session

# Кэш «есть ли хоть один грант» (режим auto): (deadline_monotonic, value). TTL короткий — граница
# между «пусто» и «первый грант» сдвигается редко; grant/revoke инвалидируют кэш немедленно.
_ENF_TTL_S = 60.0
_enf_cache: tuple[float, bool] | None = None


def _invalidate_enforcement_cache() -> None:
    global _enf_cache
    _enf_cache = None


async def per_user_enforcement_active() -> bool:
    """Активна ли пер-пользовательская изоляция. enforced → True; legacy → False; auto → есть ли
    хоть один грант в account_access (кэш _ENF_TTL_S; grant/revoke инвалидируют). Невалидный режим
    трактуем как auto (warning один раз на процесс не делаем — значение читается часто)."""
    global _enf_cache
    mode = (settings.account_access_mode or "auto").strip().lower()
    if mode == "enforced":
        return True
    if mode == "legacy":
        return False
    now = time.monotonic()
    if _enf_cache is not None and _enf_cache[0] > now:
        return _enf_cache[1]
    async with Session() as s:
        row = (await s.execute(select(AccountAccess.id).limit(1))).first()
    active = row is not None
    _enf_cache = (now + _ENF_TTL_S, active)
    return active


# ── Рантайм-whitelist (§6/§12): env ∪ БД, fail-closed ─────────────────────────────────────
# Кэш DB-набора chat_id (короткий TTL — граница меняется редко; add/remove инвалидируют сразу).
# Как _enf_cache: избегаем удара в БД на КАЖДЫЙ апдейт Telegram (WhitelistMiddleware — самый горячий).
_WL_TTL_S = 30.0
_wl_cache: tuple[float, frozenset[int]] | None = None
_wl_db_warned = False  # один WARNING на процесс, если таблица недоступна (миграция не применена)


def _invalidate_whitelist_cache() -> None:
    global _wl_cache
    _wl_cache = None


async def _db_whitelist_ids() -> frozenset[int]:
    """chat_id из таблицы whitelist (кэш _WL_TTL_S). Пусто ⇒ frozenset() (fail-closed сохраняется:
    объединение с env; пустое объединение блокирует всех в middleware).

    Устойчивость: любой сбой БД (таблица не создана — миграция 0017 не применена; обрыв соединения)
    трактуем как ПУСТОЙ набор, НЕ роняя обработку апдейта. Это fail-closed в безопасную сторону:
    env-whitelist проверяется РАНЬШЕ (бутстрап-админ всегда проходит), а неизвестные chat_id при
    недоступной БД получают отказ, а не доступ. Один WARNING на процесс (не шумим на каждый апдейт)."""
    global _wl_cache, _wl_db_warned
    now = time.monotonic()
    if _wl_cache is not None and _wl_cache[0] > now:
        return _wl_cache[1]
    try:
        async with Session() as s:
            rows = (await s.execute(select(Whitelist.chat_id))).scalars().all()
    except Exception as e:  # noqa: BLE001 — БД недоступна/таблицы нет → fail-closed (пустой набор)
        if not _wl_db_warned:
            _wl_db_warned = True
            log.warning(
                "whitelist: таблица недоступна (%s) — рантайм-whitelist пуст, действует только env. "
                "Применить миграцию 0017 (alembic upgrade head).",
                type(e).__name__,
            )
        return frozenset()
    ids = frozenset(int(x) for x in rows)
    _wl_cache = (now + _WL_TTL_S, ids)
    return ids


async def is_whitelisted(chat_id: int | None) -> bool:
    """Пущен ли chat_id в бота: env TELEGRAM_WHITELIST_CHAT_IDS ∪ таблица whitelist (fail-closed —
    None/отсутствие в обоих ⇒ False). Env — бутстрап первого админа; БД — рантайм /adduser."""
    if chat_id is None:
        return False
    if chat_id in settings.whitelist:
        return True
    return chat_id in await _db_whitelist_ids()


async def whitelisted_ids() -> set[int]:
    """ВЕСЬ набор доверенных операторов: env TELEGRAM_WHITELIST_CHAT_IDS (бутстрап) ∪ таблица
    whitelist (рантайм /adduser). Fail-closed (сбой БД ⇒ только env — см. _db_whitelist_ids).
    D9: для не-hot-path потребителей, которым нужен весь список операторов (планировщик —
    плановые отчёты/аномалии/advise), а не проверка одного id (is_whitelisted). Раньше планировщик
    брал только env и рантайм-добавленные (/adduser) операторы не получали рассылки до рестарта."""
    return set(settings.whitelist) | set(await _db_whitelist_ids())


async def add_whitelisted_user(
    chat_id: int, *, added_by: int | None = None, note: str | None = None
) -> bool:
    """Добавить оператора в рантайм-whitelist (идемпотентно). True — добавлен, False — уже был.
    Инвалидирует кэш немедленно (новый оператор пишет боту сразу, без рестарта и без ожидания TTL)."""
    async with Session() as s:
        exists = (await s.execute(select(Whitelist.id).where(Whitelist.chat_id == chat_id))).first()
        if exists is not None:
            return False
        s.add(Whitelist(chat_id=chat_id, added_by=added_by, note=note))
        await s.commit()
    _invalidate_whitelist_cache()
    return True


async def remove_whitelisted_user(chat_id: int) -> None:
    """Убрать оператора из рантайм-whitelist (no-op, если его не было). Env-whitelist этим НЕ
    затрагивается (env — бутстрап; убрать env-оператора можно только правкой .env)."""
    async with Session() as s:
        await s.execute(delete(Whitelist).where(Whitelist.chat_id == chat_id))
        await s.commit()
    _invalidate_whitelist_cache()


async def list_whitelisted_users() -> list[Whitelist]:
    """Операторы рантайм-whitelist (БД), отсортированы по дате добавления. Env-операторы здесь НЕ
    показываются (они не в таблице) — /users помечает их отдельно из settings.whitelist."""
    async with Session() as s:
        return list(
            (await s.execute(select(Whitelist).order_by(Whitelist.created_at))).scalars().all()
        )


async def ensure_account_allowed_for_user(chat_id: int, customer_id: str) -> None:
    """Пер-пользовательский замок. Draft — всем whitelisted (без записи). Прочие — по явному
    гранту в account_access, КОГДА enforcement активен (см. режимы в докстринге модуля); в
    legacy-проходе (auto + пустая таблица) не-Draft пропускается — глобальный read-замок
    ensure_read_allowed остаётся на вызывающем. Нет гранта при активном enforcement ⇒
    PermissionError (fail-closed)."""
    cid = normalize_customer_id(customer_id)
    if cid == DRAFT_ACCOUNT_ID:
        return
    # F: админ бота (ADMIN_CHAT_IDS) читает ВСЁ read-allowed без пер-юзер гранта — он и так управляет
    # грантами (/grant), чтение ≠ деньги. Владелец-одиночка видит все активные дочерние во всех пикерах
    # (раньше первый же /grant включал enforcement и прятал не-гранованные аккаунты). Не-админ операторы
    # сохраняют изоляцию. Мутационный замок этим НЕ затрагивается (ensure_allowed — отдельный).
    if chat_id in settings.admin_ids:
        return
    if not await per_user_enforcement_active():
        return  # legacy-проход: пер-юзер изоляция не включена (авторитетен глобальный замок)
    async with Session() as s:
        row = (
            await s.execute(
                select(AccountAccess.id).where(
                    AccountAccess.chat_id == chat_id, AccountAccess.customer_id == cid
                )
            )
        ).first()
    if row is None:
        raise PermissionError(f"chat {chat_id} не имеет доступа к аккаунту {cid}")


async def grant_account_access(chat_id: int, customer_id: str) -> None:
    """Выдать оператору доступ к аккаунту (идемпотентно — повтор не создаёт дубль).
    ⚠️ В режиме auto ПЕРВЫЙ грант включает enforcement для всех операторов."""
    cid = normalize_customer_id(customer_id)
    async with Session() as s:
        exists = (
            await s.execute(
                select(AccountAccess.id).where(
                    AccountAccess.chat_id == chat_id, AccountAccess.customer_id == cid
                )
            )
        ).first()
        if exists is None:
            s.add(AccountAccess(chat_id=chat_id, customer_id=cid))
            await s.commit()
    _invalidate_enforcement_cache()


async def revoke_account_access(chat_id: int, customer_id: str) -> None:
    """Снять доступ оператора к аккаунту (no-op, если гранта не было)."""
    cid = normalize_customer_id(customer_id)
    async with Session() as s:
        await s.execute(
            delete(AccountAccess).where(
                AccountAccess.chat_id == chat_id, AccountAccess.customer_id == cid
            )
        )
        await s.commit()
    _invalidate_enforcement_cache()


async def list_user_account_ids(chat_id: int) -> list[str]:
    """Аккаунты, доступные оператору: Draft (всегда) + явно выданные гранты. Отсортированы, без
    дублей. UI /account показывает их (дополнительно фильтруя глобальным ensure_read_allowed)."""
    async with Session() as s:
        rows = (
            (
                await s.execute(
                    select(AccountAccess.customer_id).where(AccountAccess.chat_id == chat_id)
                )
            )
            .scalars()
            .all()
        )
    return sorted({DRAFT_ACCOUNT_ID, *(normalize_customer_id(c) for c in rows)})


async def _default_live_account(chat_id: int) -> str:
    """§8 «читать РЕАЛЬНЫЙ аккаунт целиком»: дефолт аккаунта ЧТЕНИЯ, когда оператор не выбрал явно.
    Если у бота РОВНО ОДИН живой read-аккаунт (обход MCC ∪ env read-list, КРОМЕ Draft), доступный
    этому оператору (глобальный read-замок × пер-юзер грант) → он. Иначе '' (⇒ вызывающий берёт
    Draft): ноль живых → пустая песочница; несколько → НЕ угадываем (решает пикер /account).

    Почему безопасно для мутаций: таргет мутации = тот же активный аккаунт (read/write согласованы,
    чтобы «пауза X» целилась в ТУ кампанию, что видит оператор), но замок ads.client.ensure_allowed
    режет не-allow-listed аккаунт (fail-closed) ДО показа кнопок — т.е. смена дефолта чтения делает
    отказ на не-Draft мутации строже, а не слабее (golden rule 9 сохранён)."""
    from ads.client import discovered_read_children

    live = {
        n
        for c in (set(discovered_read_children()) | set(settings.read_customer_ids))
        if (n := normalize_customer_id(c)) and n != DRAFT_ACCOUNT_ID
    }
    if len(live) != 1:  # 0 → Draft (песочница); >1 → пикер (не угадываем активный)
        return ""
    cid = next(iter(live))
    try:
        ensure_read_allowed(cid)  # глобальный read-замок (fail-closed)
        await ensure_account_allowed_for_user(chat_id, cid)  # пер-пользователь
    except PermissionError:
        return ""  # оператору этот аккаунт не гранован → Draft (fail-closed)
    return cid


async def get_active_account(chat_id: int) -> str:
    """Активный аккаунт ЧТЕНИЯ чата. Явный выбор (/account, пикер) ПЕРЕПРОВЕРЯЕТСЯ (глобальный
    read-замок + пер-пользовательский грант): доступ отозвали ⇒ тихо откатываемся на Draft
    (fail-closed: НИКОГДА не на чужой аккаунт). Нет явного выбора (или Draft) → «реальный аккаунт
    целиком»: единственный живой read-аккаунт (см. _default_live_account), иначе Draft. Единственная
    точка резолва «активного» в боте — смена дефолта каскадно включает живой аккаунт во ВСЕХ чтениях."""
    async with Session() as s:
        sel = (
            await s.execute(
                select(UserSettings.selected_customer_id).where(UserSettings.chat_id == chat_id)
            )
        ).scalar_one_or_none()
    cid = normalize_customer_id(sel) if sel else ""
    if not cid:
        # НЕТ явного выбора (NULL) → авто «реальный аккаунт целиком»: единственный живой read-аккаунт.
        return await _default_live_account(chat_id) or DRAFT_ACCOUNT_ID
    if cid == DRAFT_ACCOUNT_ID:
        # ЯВНЫЙ пин Draft (setacct-пикер выбрал Draft / _heal_if_stuck_global): НЕ авто-прыжок на
        # живой аккаунт — оператор осознанно смотрит песочницу. «Авто» доступно через /account reset.
        return DRAFT_ACCOUNT_ID
    try:
        ensure_read_allowed(cid)  # глобальный read-замок (fail-closed)
        await ensure_account_allowed_for_user(chat_id, cid)  # пер-пользователь
    except PermissionError:
        log.info("active-account: доступ к %s для chat %s отозван → откат на Draft", cid, chat_id)
        return DRAFT_ACCOUNT_ID  # явный выбор отозван → fail-closed на Draft (не прыгаем на другой)
    return cid


async def set_active_account(chat_id: int, customer_id: str | None) -> None:
    """Установить активный аккаунт чата (upsert UserSettings.selected_customer_id). None/пусто —
    сброс на Draft (хранится NULL). Валидацию права делает вызывающий (UI) до вызова — здесь
    только персист (переживает рестарт)."""
    cid = normalize_customer_id(customer_id) if customer_id else None
    async with Session() as s:
        row = (
            await s.execute(select(UserSettings).where(UserSettings.chat_id == chat_id))
        ).scalar_one_or_none()
        if row is None:
            s.add(UserSettings(chat_id=chat_id, selected_customer_id=cid))
        else:
            row.selected_customer_id = cid
        await s.commit()


async def resolve_read_account(chat_id: int, raw: str | None) -> str:
    """Резолв аккаунта ЧТЕНИЯ из свободного ввода (аргумент агента get_stats / текст команды).

    • пусто → активный аккаунт чата (get_active_account — тот же резолв, что /report);
    • цифры/id с разделителями → normalize + КОМПОЗИТНЫЙ замок (read + пер-юзер), иначе
      PermissionError (fail-closed — НЕ подменяем молча другим аккаунтом);
    • текст без цифр → матч по имени среди обнаруженных дочерних (discovered_read_children_meta):
      точное совпадение (casefold), затем УНИКАЛЬНАЯ подстрока; неоднозначно/не найдено →
      LookupError со списком кандидатов (вызывающий покажет уточнение)."""
    text = str(raw or "").strip()
    if not text:
        return await get_active_account(chat_id)
    cid = normalize_customer_id(text)
    if cid:  # похоже на id
        ensure_read_allowed(cid)
        await ensure_account_allowed_for_user(chat_id, cid)
        return cid
    # матч по имени (только читаемые дочерние из meta — не открывает ничего сверх замков)
    from ads.client import discovered_read_children_meta

    needle = text.casefold()
    meta = discovered_read_children_meta()
    exact = [c for c, ch in meta.items() if (ch.name or "").casefold() == needle]
    candidates = exact or [c for c, ch in meta.items() if needle in (ch.name or "").casefold()]
    if len(candidates) != 1:
        names = sorted((meta[c].name or c) for c in candidates)[:5]
        raise LookupError(
            f"аккаунт по имени {text!r} не найден однозначно"
            + (f" (кандидаты: {', '.join(names)})" if names else "")
        )
    cid = candidates[0]
    ensure_read_allowed(cid)
    await ensure_account_allowed_for_user(chat_id, cid)
    return cid
