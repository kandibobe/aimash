"""Пер-пользовательский доступ к аккаунтам + активный аккаунт чата (core.access, §8/§12).

Проверяют: Draft доступен без гранта (обратная совместимость), не-Draft — только по гранту
(fail-closed); активный аккаунт по умолчанию Draft, переключение персистится, а отзыв доступа/выход
аккаунта из read-list ⇒ тихий откат на Draft (НИКОГДА не чужой аккаунт). Реальная БД на temp SQLite.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.access import (  # noqa: E402
    ensure_account_allowed_for_user,
    get_active_account,
    grant_account_access,
    list_user_account_ids,
    revoke_account_access,
    set_active_account,
)
from core.config import settings  # noqa: E402
from db.session import init_db  # noqa: E402

DRAFT = "7753643025"
ACCT = "1112223334"
CHAT = 555


@pytest.fixture(autouse=True)
def _enforced_mode(monkeypatch):
    """Тесты этого модуля описывают СТРОГУЮ пер-юзер семантику — фиксируем режим enforced.
    В дефолтном auto пустая таблица грантов даёт legacy-проход (обратная совместимость
    одно-операторного режима) и смазала бы ассерты «без гранта — отказ». Режимы auto/legacy
    покрыты отдельными тестами ниже."""
    monkeypatch.setattr(settings, "account_access_mode", "enforced")


@contextmanager
def _read(read_ids: str, allowed: str = DRAFT):
    pa, pr = settings.google_ads_allowed_customer_ids, settings.google_ads_read_customer_ids
    settings.google_ads_allowed_customer_ids = allowed
    settings.google_ads_read_customer_ids = read_ids
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = pa
        settings.google_ads_read_customer_ids = pr


async def test_draft_allowed_without_grant_others_denied():
    await init_db()
    await ensure_account_allowed_for_user(CHAT, DRAFT)  # Draft — без гранта, не бросает
    with pytest.raises(PermissionError):
        await ensure_account_allowed_for_user(CHAT, ACCT)  # чужой без гранта — отказ


async def test_grant_makes_allowed_and_is_idempotent():
    await init_db()
    await revoke_account_access(CHAT, ACCT)  # чистый старт (temp БД переживает между тестами)
    await grant_account_access(CHAT, ACCT)
    await grant_account_access(CHAT, ACCT)  # повтор не должен падать/дублировать
    await ensure_account_allowed_for_user(CHAT, ACCT)  # теперь разрешён
    assert set(await list_user_account_ids(CHAT)) == {DRAFT, ACCT}
    await revoke_account_access(CHAT, ACCT)
    with pytest.raises(PermissionError):
        await ensure_account_allowed_for_user(CHAT, ACCT)


async def test_active_account_default_and_switch_and_revoke_fallback():
    await init_db()
    await revoke_account_access(CHAT, ACCT)
    await set_active_account(CHAT, DRAFT)
    assert await get_active_account(CHAT) == DRAFT  # дефолт/Draft

    # переключение на не-Draft требует И read-allowed, И гранта
    await grant_account_access(CHAT, ACCT)
    await set_active_account(CHAT, ACCT)
    with _read(read_ids=ACCT):
        assert await get_active_account(CHAT) == ACCT  # доступ есть → активен ACCT
        # отозвали грант → тихий откат на Draft (НЕ чужой аккаунт)
        await revoke_account_access(CHAT, ACCT)
        assert await get_active_account(CHAT) == DRAFT

    # даже с грантом, но БЕЗ read-list (вышел из доступа) → откат на Draft
    await grant_account_access(CHAT, ACCT)
    await set_active_account(CHAT, ACCT)
    with _read(read_ids=""):  # ACCT не в read-list → ensure_read_allowed бросит
        assert await get_active_account(CHAT) == DRAFT
    await revoke_account_access(CHAT, ACCT)


async def test_multi_chat_operator_isolation():
    """§12: грант ОДНОГО оператора не даёт доступ ДРУГОМУ — изоляция строго по chat_id."""
    await init_db()
    chat_a, chat_b = 701, 702
    await revoke_account_access(chat_a, ACCT)
    await revoke_account_access(chat_b, ACCT)
    await grant_account_access(chat_a, ACCT)  # доступ выдан ТОЛЬКО chat_a

    await ensure_account_allowed_for_user(chat_a, ACCT)  # chat_a — ок
    with pytest.raises(PermissionError):
        await ensure_account_allowed_for_user(chat_b, ACCT)  # chat_b — отказ (чужой грант не виден)
    assert set(await list_user_account_ids(chat_a)) == {DRAFT, ACCT}
    assert await list_user_account_ids(chat_b) == [DRAFT]  # у chat_b только Draft
    await revoke_account_access(chat_a, ACCT)


async def test_grant_and_check_normalize_dashed_ids():
    """normalize_customer_id: дефис-форма ≡ только цифры — и при гранте, и при проверке."""
    await init_db()
    chat = 703
    await revoke_account_access(chat, ACCT)
    await grant_account_access(chat, "111-222-3334")  # грант в человекочитаемой дефис-форме
    await ensure_account_allowed_for_user(chat, ACCT)  # проверка в цифрах — проходит
    await ensure_account_allowed_for_user(chat, "111-222-3334")  # и в дефисах — тоже
    await ensure_account_allowed_for_user(chat, "775-364-3025")  # Draft (дефис) без гранта
    await revoke_account_access(chat, ACCT)


async def test_list_user_account_ids_always_draft_sorted_dedup():
    """Draft всегда в списке (даже без грантов); выдача отсортирована, без дублей, нормализована."""
    await init_db()
    chat = 704
    await revoke_account_access(chat, ACCT)
    assert await list_user_account_ids(chat) == [DRAFT]  # ноль грантов → только Draft

    await grant_account_access(chat, "111-222-3334")  # дефис-форма
    await grant_account_access(chat, ACCT)  # тот же аккаунт после нормализации (не дубль)
    ids = await list_user_account_ids(chat)
    assert ids == sorted({DRAFT, ACCT})  # отсортировано + дедуп + нормализовано
    assert ids.count(ACCT) == 1
    await revoke_account_access(chat, ACCT)


async def test_set_active_account_upsert_branches():
    """Обе ветки upsert: первый set создаёт строку UserSettings, второй обновляет (без дубля)."""
    from sqlalchemy import func, select

    from db.models import UserSettings
    from db.session import Session

    await init_db()
    chat = 705
    await revoke_account_access(chat, ACCT)
    await set_active_account(chat, DRAFT)  # insert-ветка (строки ещё не было)
    assert await get_active_account(chat) == DRAFT

    await grant_account_access(chat, ACCT)
    await set_active_account(chat, ACCT)  # update-ветка (строка уже есть)
    with _read(read_ids=ACCT):
        assert await get_active_account(chat) == ACCT  # значение сменилось, доступ есть

    async with Session() as s:
        n = (
            await s.execute(
                select(func.count()).select_from(UserSettings).where(UserSettings.chat_id == chat)
            )
        ).scalar_one()
    assert n == 1  # ровно одна строка — update не сделал дубль
    await revoke_account_access(chat, ACCT)


# ── 2B: режимы пер-юзер изоляции (auto / enforced / legacy) ───────────────────────
async def test_auto_mode_empty_table_is_legacy_pass(monkeypatch):
    """auto + ни одного гранта в таблице → legacy-проход: не-Draft НЕ требует гранта
    (текущее одно-операторное поведение сохраняется до первого /grant)."""
    from core import access as acc
    from db.models import AccountAccess
    from db.session import Session

    await init_db()
    monkeypatch.setattr(settings, "account_access_mode", "auto")
    # гарантируем пустую таблицу (общая temp-БД сессии) + сброс кэша enforcement
    from sqlalchemy import delete

    async with Session() as s:
        await s.execute(delete(AccountAccess))
        await s.commit()
    acc._invalidate_enforcement_cache()

    assert await acc.per_user_enforcement_active() is False
    await ensure_account_allowed_for_user(9001, ACCT)  # НЕ бросает (legacy-проход)


async def test_first_grant_activates_enforcement(monkeypatch):
    """auto: ПЕРВЫЙ грант включает enforcement — другой оператор теряет legacy-проход."""
    from core import access as acc
    from db.models import AccountAccess
    from db.session import Session

    await init_db()
    monkeypatch.setattr(settings, "account_access_mode", "auto")
    from sqlalchemy import delete

    async with Session() as s:
        await s.execute(delete(AccountAccess))
        await s.commit()
    acc._invalidate_enforcement_cache()

    await grant_account_access(9002, ACCT)  # первый грант (инвалидирует кэш сам)
    try:
        assert await acc.per_user_enforcement_active() is True
        await ensure_account_allowed_for_user(9002, ACCT)  # грантованный — ок
        with pytest.raises(PermissionError):
            await ensure_account_allowed_for_user(9003, ACCT)  # другой — отказ
    finally:
        await revoke_account_access(9002, ACCT)


async def test_env_enforced_denies_non_draft_with_empty_table(monkeypatch):
    """enforced: строгий режим даже с пустой таблицей (не-Draft без гранта — отказ)."""
    monkeypatch.setattr(settings, "account_access_mode", "enforced")
    await init_db()
    with pytest.raises(PermissionError):
        await ensure_account_allowed_for_user(9004, ACCT)
    await ensure_account_allowed_for_user(9004, DRAFT)  # Draft — всегда


async def test_legacy_mode_disables_per_user_lock(monkeypatch):
    """legacy: пер-юзер замок осознанно выключен (только глобальный read-замок на вызывающем)."""
    monkeypatch.setattr(settings, "account_access_mode", "legacy")
    await init_db()
    await ensure_account_allowed_for_user(9005, ACCT)  # не бросает


async def test_admin_bypasses_per_user_read_grant(monkeypatch):
    """F: админ (ADMIN_CHAT_IDS) читает любой read-allowed аккаунт БЕЗ гранта (владелец видит все
    активные дочерние в пикерах); не-админ без гранта — отказ. Мутации админ-байпас НЕ открывает."""
    from ads.client import ensure_allowed

    await init_db()
    admin_chat, plain_chat = 8801, 8802
    monkeypatch.setattr(settings, "admin_chat_ids", str(admin_chat))  # режим enforced (autouse)
    await ensure_account_allowed_for_user(admin_chat, ACCT)  # админ — без гранта, не бросает
    await ensure_account_allowed_for_user(admin_chat, DRAFT)  # Draft — тоже ок
    with pytest.raises(PermissionError):
        await ensure_account_allowed_for_user(plain_chat, ACCT)  # не-админ без гранта — отказ
    with pytest.raises(PermissionError):
        ensure_allowed(ACCT)  # админ-байпас ЧТЕНИЯ не открывает МУТАЦИИ (потолок в коде)


async def test_grant_does_not_open_mutations():
    """Зеркало ключевого инварианта (golden rule 9): грант ЧТЕНИЯ на дочерний НЕ открывает
    мутации — ensure_allowed (потолок ALLOWED_CEILING=Draft) всё равно отказывает."""
    from ads.client import ensure_allowed

    await init_db()
    await grant_account_access(9006, ACCT)
    try:
        with _read(read_ids=ACCT):
            await ensure_account_allowed_for_user(9006, ACCT)  # чтение — ок
            with pytest.raises(PermissionError):
                ensure_allowed(ACCT)  # мутация — отказ (потолок в коде)
    finally:
        await revoke_account_access(9006, ACCT)


async def test_resolve_read_account_paths(monkeypatch):
    """2D-бэкенд: пусто → активный аккаунт; id → композитный замок; имя → уникальный матч по meta;
    неоднозначно → LookupError; запрещён → PermissionError (не молчаливая подмена)."""
    from ads.client import set_discovered_read_children, set_discovered_read_children_meta
    from ads.read import ChildAccount
    from core.access import resolve_read_account

    await init_db()
    monkeypatch.setattr(settings, "account_access_mode", "legacy")  # изолируем от грантов
    chat = 9007
    ch_a = ChildAccount(
        id=ACCT, name="Kasi Motors", currency="USD", manager=False, level=1, status="ENABLED"
    )
    ch_b = ChildAccount(
        id="9998887776", name="Kasi Parts", currency="USD", manager=False, level=1, status="ENABLED"
    )
    set_discovered_read_children([ACCT, "9998887776"])
    set_discovered_read_children_meta([ch_a, ch_b])
    try:
        with _read(read_ids=""):
            assert await resolve_read_account(chat, None) == DRAFT  # пусто → активный (Draft)
            assert await resolve_read_account(chat, "111-222-3334") == ACCT  # id нормализован
            assert await resolve_read_account(chat, "Kasi Motors") == ACCT  # точное имя
            assert await resolve_read_account(chat, "motors") == ACCT  # уникальная подстрока
            with pytest.raises(LookupError):
                await resolve_read_account(chat, "Kasi")  # неоднозначно (2 кандидата)
        with _read(read_ids="", allowed=""):  # всё пусто → read-замок отказывает
            set_discovered_read_children([])
            with pytest.raises(PermissionError):
                await resolve_read_account(chat, ACCT)
    finally:
        set_discovered_read_children([])
        set_discovered_read_children_meta([])


# ── §8 «читать РЕАЛЬНЫЙ аккаунт целиком»: авто-дефолт на единственный живой read-аккаунт ──
async def test_no_selection_defaults_to_single_live_account():
    """Нет явного выбора + РОВНО ОДИН живой read-аккаунт (доступный оператору) → он, а не Draft."""
    from ads.client import set_discovered_read_children

    await init_db()
    chat = 7101
    set_discovered_read_children([])  # детерминизм: живой набор только из read_ids ниже
    await revoke_account_access(chat, ACCT)
    await grant_account_access(chat, ACCT)  # оператор допущен к живому аккаунту (enforced-autouse)
    try:
        with _read(read_ids=ACCT):
            assert await get_active_account(chat) == ACCT  # авто на единственный живой
    finally:
        await revoke_account_access(chat, ACCT)


async def test_no_selection_multiple_live_stays_draft():
    """Нет выбора + НЕСКОЛЬКО живых read-аккаунтов → Draft (не угадываем; выбор — пикер /account)."""
    from ads.client import set_discovered_read_children

    await init_db()
    chat, acct2 = 7102, "9998887776"
    set_discovered_read_children([])
    await grant_account_access(chat, ACCT)
    await grant_account_access(chat, acct2)
    try:
        with _read(read_ids=f"{ACCT},{acct2}"):
            assert await get_active_account(chat) == DRAFT  # >1 живого → не угадываем
    finally:
        await revoke_account_access(chat, ACCT)
        await revoke_account_access(chat, acct2)


async def test_no_selection_single_live_not_granted_stays_draft():
    """Нет выбора + один живой, но оператору НЕ гранован (enforced) → Draft (fail-closed, не утечка)."""
    from ads.client import set_discovered_read_children

    await init_db()
    chat = 7103
    set_discovered_read_children([])
    await revoke_account_access(chat, ACCT)  # гранта нет
    with _read(read_ids=ACCT):
        assert await get_active_account(chat) == DRAFT  # нет гранта → авто-дефолт не откроет чужой


async def test_live_read_default_does_not_open_mutation():
    """Инвариант §8 × golden rule 9: авто-дефолт ЧТЕНИЯ на живой аккаунт НЕ открывает МУТАЦИИ на
    нём — ensure_allowed (потолок ALLOWED_CEILING=Draft) отказывает, пока аккаунт не в
    allowed_customer_ids. Смена read-дефолта делает отказ на не-Draft строже, а не слабее."""
    from ads.client import ensure_allowed, set_discovered_read_children

    await init_db()
    chat = 7106
    set_discovered_read_children([])
    await grant_account_access(chat, ACCT)  # оператор может ЧИТАТЬ живой аккаунт
    try:
        with _read(read_ids=ACCT, allowed=DRAFT):  # виден на чтение, НЕ включён на мутации
            assert await get_active_account(chat) == ACCT  # чтение авто-дефолтит на живой
            with pytest.raises(PermissionError):
                ensure_allowed(ACCT)  # но мутация на него — ОТКАЗ (замок держит, fail-closed)
    finally:
        await revoke_account_access(chat, ACCT)


async def test_account_choice_pending_matrix():
    """pending ⇔ NULL-выбор И >1 живого ДОСТУПНОГО аккаунта. Пин Draft / один живой / ноль
    живых → False (прежнее поведение, Draft/авто-дефолт). Грант-фильтр: из двух живых гранован
    один ⇒ выбор не нужен (авто-дефолт на него)."""
    from ads.client import set_discovered_read_children
    from core.access import account_choice_pending

    await init_db()
    chat, acct2 = 7105, "9998887776"
    set_discovered_read_children([])
    await grant_account_access(chat, ACCT)
    await grant_account_access(chat, acct2)
    try:
        with _read(read_ids=f"{ACCT},{acct2}"):
            assert await account_choice_pending(chat) is True  # NULL + 2 живых → ждём выбор
            await set_active_account(chat, DRAFT)  # осознанный пин Draft — выбора не ждём
            assert await account_choice_pending(chat) is False
            await set_active_account(chat, None)  # сброс на NULL → снова ждём
            assert await account_choice_pending(chat) is True
        with _read(read_ids=ACCT):  # один живой → авто-дефолт (WIP), пикер не нужен
            assert await account_choice_pending(chat) is False
        with _read(read_ids=""):  # ноль живых → Draft-песочница без шума
            assert await account_choice_pending(chat) is False
        await revoke_account_access(chat, acct2)  # грантован лишь один из двух живых
        with _read(read_ids=f"{ACCT},{acct2}"):
            assert await account_choice_pending(chat) is False  # доступен один → авто-дефолт
            assert await get_active_account(chat) == ACCT
    finally:
        await revoke_account_access(chat, ACCT)
        await revoke_account_access(chat, acct2)
        await set_active_account(chat, None)


async def test_pinned_draft_not_overridden_by_single_live():
    """ЯВНЫЙ пин Draft (setacct/heal) НЕ перебивается авто-дефолтом на живой аккаунт."""
    from ads.client import set_discovered_read_children

    await init_db()
    chat = 7104
    set_discovered_read_children([])
    await grant_account_access(chat, ACCT)
    await set_active_account(chat, DRAFT)  # оператор осознанно пинит Draft (песочница)
    try:
        with _read(read_ids=ACCT):  # живой аккаунт есть, но пин Draft держится
            assert await get_active_account(chat) == DRAFT
    finally:
        await revoke_account_access(chat, ACCT)


async def test_get_active_account_corner_cases():
    """Углы резолва: нет строки → Draft; явный Draft → Draft; дефис-форма в БД → нормализованный возврат."""
    from sqlalchemy import select

    from db.models import UserSettings
    from db.session import Session

    await init_db()
    assert await get_active_account(70600) == DRAFT  # свежий чат без UserSettings → Draft

    chat = 706
    await revoke_account_access(chat, ACCT)
    await set_active_account(chat, DRAFT)
    assert await get_active_account(chat) == DRAFT  # явно сохранён Draft → Draft

    # Защитная нормализация на ЧТЕНИИ: если в БД лежит дефис-форма (легаси/миграция),
    # get_active_account всё равно вернёт цифры (строка 98 ads.client.normalize в core.access).
    await grant_account_access(chat, ACCT)
    async with Session() as s:
        row = (
            await s.execute(select(UserSettings).where(UserSettings.chat_id == chat))
        ).scalar_one()
        row.selected_customer_id = "111-222-3334"  # пишем дефис-форму напрямую (минуя set_active)
        await s.commit()
    with _read(read_ids=ACCT):
        assert await get_active_account(chat) == ACCT  # вернулось нормализованным
    await revoke_account_access(chat, ACCT)


# ── C2 (аудит 2026-07): фильтр аккаунтов под получателя scheduler-рассылки ────────
async def test_accessible_accounts_enforced_filters_to_grants():
    """enforced: получатель видит Draft + свои гранты; чужой аккаунт из рассылки отсечён
    (метрики чужого клиента не утекают в плановый отчёт/аномалии/дайджест)."""
    from core.access import accessible_accounts_for_user, grant_account_access

    await init_db()
    chat = 556001
    await grant_account_access(chat, ACCT)
    try:
        out = await accessible_accounts_for_user(chat, [DRAFT, ACCT, "9998887776"])
        assert out == [DRAFT, ACCT]  # порядок кандидатов сохранён, не-грантованный отсечён
    finally:
        await revoke_account_access(chat, ACCT)


async def test_accessible_accounts_admin_sees_all(monkeypatch):
    """Админ — все кандидаты без грантов (он и управляет грантами; чтение ≠ деньги)."""
    from core.access import accessible_accounts_for_user

    await init_db()
    chat = 556002
    monkeypatch.setattr(settings, "admin_chat_ids", str(chat))
    out = await accessible_accounts_for_user(chat, [DRAFT, ACCT, "9998887776"])
    assert out == [DRAFT, ACCT, "9998887776"]


async def test_accessible_accounts_legacy_pass_unchanged(monkeypatch):
    """legacy-режим (enforcement выключен): поведение рассылок НЕ меняется — все кандидаты."""
    import core.access as acc
    from core.access import accessible_accounts_for_user

    await init_db()
    monkeypatch.setattr(settings, "account_access_mode", "legacy")
    acc._invalidate_enforcement_cache()
    try:
        out = await accessible_accounts_for_user(556003, [DRAFT, ACCT])
        assert out == [DRAFT, ACCT]
    finally:
        acc._invalidate_enforcement_cache()


async def test_accessible_accounts_fail_closed_on_db_error(monkeypatch):
    """Сбой чтения грантов ⇒ только Draft (fail-closed), не «всё» и не исключение."""
    import core.access as acc
    from core.access import accessible_accounts_for_user

    await init_db()

    class _BoomSession:
        def __call__(self, *a, **k):
            raise RuntimeError("db down")

    monkeypatch.setattr(acc, "Session", _BoomSession())
    out = await accessible_accounts_for_user(556004, [DRAFT, ACCT])
    assert out == [DRAFT]
