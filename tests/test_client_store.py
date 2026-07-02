"""§20 (Фаза A): хранилище профилей клиентов (clients.store.ClientProfileStore).

Проверяем: upsert (create + merge §20.5), контакты/услуги, рендер контекста для генераторов +
кап, отметку аккаунтов с профилем, clear (удаляет профиль, но история «до» переживает clear),
ключ по customer_id (один аккаунт — один профиль).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clients.store import ClientProfileStore  # noqa: E402
from db.models import (  # noqa: E402
    ClientContact,
    ClientProfile,
    ClientProfileHistory,
    ClientService,
)
from db.session import Session, init_db  # noqa: E402

# БД одна на сессию тестов (conftest чистит файл лишь на импорте) → у каждого теста свой customer_id,
# чтобы не ловить чужие данные (изоляция по ключу, как в остальной суйте).
CID = "1000000001"


def _kasi_patch() -> dict:
    """Пример из §20.3 (Kasi Motors) в виде patch модели."""
    return {
        "brand": "Kasi Motors",
        "business_desc": "автодилер, поддержанные авто, Найроби, Кения",
        "geo": "Кения, Найроби",
        "language": "English, Swahili",
        "website": "kasimotors.co.ke",
        "socials": {"instagram": "@kasimotors"},
        "notes": "упор на гарантию 12 мес",
        "contacts": [{"kind": "phone", "value": "+254 712 345 678"}],
        "services": [
            {"name": "Седаны", "price": "от $4000", "category": "авто"},
            {"name": "Внедорожники", "price": "от $7000", "category": "авто"},
        ],
    }


@pytest.mark.asyncio
async def test_upsert_creates_profile_with_details():
    await init_db()
    CID = "1000000002"
    store = ClientProfileStore()
    res = await store.apply_upsert(CID, _kasi_patch(), operation="profile_save")
    assert res["created"] is True
    assert res["customer_id"] == CID

    prof = await store.get_by_account(CID)
    assert prof is not None
    assert prof["brand"] == "Kasi Motors"
    assert prof["socials"]["instagram"] == "@kasimotors"
    assert len(prof["contacts"]) == 1
    assert len(prof["services"]) == 2


@pytest.mark.asyncio
async def test_upsert_merge_keeps_untouched_fields():
    """§20.3 «ничего не теряется»: новый контакт ДОБАВЛЯЕТСЯ (старый остаётся), notes ДОПИСЫВАЮТСЯ.
    (Раньше непустой список заменял категорию целиком — data-loss footgun, закрыт merge-семантикой.)"""
    await init_db()
    CID = "1000000003"
    store = ClientProfileStore()
    await store.apply_upsert(CID, _kasi_patch(), operation="profile_save")
    # обновляем только телефон и заметку — бренд/услуги/старый телефон должны остаться
    await store.apply_upsert(
        CID,
        {"notes": "новая заметка", "contacts": [{"kind": "phone", "value": "+254 700 000 000"}]},
        operation="profile_update",
    )
    prof = await store.get_by_account(CID)
    assert prof["brand"] == "Kasi Motors"  # не затёрт пустым
    assert prof["notes"] == "упор на гарантию 12 мес\n— новая заметка"  # append, не replace
    values = {c["value"] for c in prof["contacts"]}
    assert values == {"+254 712 345 678", "+254 700 000 000"}  # union: старый НЕ потерян
    assert len(prof["services"]) == 2  # услуги не тронуты (в patch их не было)


@pytest.mark.asyncio
async def test_services_merge_updates_price_and_appends():
    """Мердж услуг по имени: совпавшая обновляет цену, новая добавляется, остальные сохраняются."""
    await init_db()
    CID = "1000000013"
    store = ClientProfileStore()
    await store.apply_upsert(CID, _kasi_patch(), operation="profile_save")
    await store.apply_upsert(
        CID,
        {
            "services": [
                {"name": "седаны", "price": "от $4500"},  # casefold-совпадение → апдейт цены
                {"name": "Пикапы", "price": "от $9000"},  # новая → добавится
            ]
        },
        operation="profile_update",
    )
    prof = await store.get_by_account(CID)
    by_name = {s["name"]: s for s in prof["services"]}
    assert set(by_name) == {"Седаны", "Внедорожники", "Пикапы"}  # ничего не пропало
    assert by_name["Седаны"]["price"] == "от $4500"  # цена обновлена
    assert by_name["Седаны"]["category"] == "авто"  # непереданное поле сохранено
    assert by_name["Внедорожники"]["price"] == "от $7000"  # не тронута


@pytest.mark.asyncio
async def test_replace_services_flag_replaces_category():
    """Явный replace_services=True (менеджер прямо попросил «замени») — категория заменяется."""
    await init_db()
    CID = "1000000014"
    store = ClientProfileStore()
    await store.apply_upsert(CID, _kasi_patch(), operation="profile_save")
    await store.apply_upsert(
        CID,
        {"services": [{"name": "Только мотоциклы"}], "replace_services": True},
        operation="profile_update",
    )
    prof = await store.get_by_account(CID)
    assert [s["name"] for s in prof["services"]] == ["Только мотоциклы"]


@pytest.mark.asyncio
async def test_replace_flag_with_empty_list_is_ignored():
    """Fail-safe: replace-флаг с ПУСТЫМ списком игнорируется — массовое удаление только через
    «🗑 Очистить профиль» (случайный флаг от LLM не сотрёт данные)."""
    await init_db()
    CID = "1000000015"
    store = ClientProfileStore()
    await store.apply_upsert(CID, _kasi_patch(), operation="profile_save")
    await store.apply_upsert(
        CID, {"services": [], "replace_services": True}, operation="profile_update"
    )
    prof = await store.get_by_account(CID)
    assert len(prof["services"]) == 2  # ничего не удалено


@pytest.mark.asyncio
async def test_notes_append_idempotent_and_capped():
    from clients.store import merge_notes

    assert merge_notes(None, "a") == "a"
    assert merge_notes("a", None) == "a"
    assert merge_notes("тон дружелюбный", "тон дружелюбный") == "тон дружелюбный"  # идемпотент
    assert merge_notes("a", "b") == "a\n— b"
    capped = merge_notes("x" * 4000, "новое", max_chars=4000)
    assert len(capped) <= 4000 and capped.endswith("новое")  # хвост (новейшее) выживает


@pytest.mark.asyncio
async def test_preview_merge_matches_apply_upsert():
    """preview_merge — единый источник истины: предсказание == фактический результат апдейта."""
    from clients.store import preview_merge

    await init_db()
    CID = "1000000016"
    store = ClientProfileStore()
    await store.apply_upsert(CID, _kasi_patch(), operation="profile_save")
    before = await store.get_by_account(CID)
    patch = {
        "notes": "акция августа",
        "services": [{"name": "Седаны", "price": "от $5000"}, {"name": "Автобусы"}],
        "contacts": [{"kind": "email", "value": "sales@kasi.example"}],
        "brand": "Kasi Motors Ltd",
    }
    predicted = preview_merge(before, patch)
    await store.apply_upsert(CID, patch, operation="profile_update")
    actual = await store.get_by_account(CID)
    assert actual["brand"] == predicted["brand"]
    assert actual["notes"] == predicted["notes"]
    assert {s["name"] for s in actual["services"]} == {s["name"] for s in predicted["services"]}
    assert {c["value"] for c in actual["contacts"]} == {c["value"] for c in predicted["contacts"]}
    assert {s["name"]: s["price"] for s in actual["services"]} == {
        s["name"]: s.get("price") for s in predicted["services"]
    }


@pytest.mark.asyncio
async def test_one_profile_per_customer_id():
    await init_db()
    CID = "1000000004"
    store = ClientProfileStore()
    await store.apply_upsert(CID, {"brand": "A"}, operation="profile_save")
    await store.apply_upsert(CID, {"brand": "B"}, operation="profile_update")
    async with Session() as s:
        n = (
            await s.execute(
                select(func.count())
                .select_from(ClientProfile)
                .where(ClientProfile.customer_id == CID)
            )
        ).scalar_one()
    assert n == 1  # один аккаунт — один профиль (§20.2)


@pytest.mark.asyncio
async def test_context_text_renders_and_caps():
    await init_db()
    CID = "1000000005"
    store = ClientProfileStore()
    assert await store.profile_context_text(CID) == ""  # нет профиля → пусто
    await store.apply_upsert(CID, _kasi_patch(), operation="profile_save")
    ctx = await store.profile_context_text(CID)
    assert "Kasi Motors" in ctx
    assert "Седаны" in ctx
    assert "+254" not in ctx  # телефон (PII) в контекст генерации не попадает
    short = await store.profile_context_text(CID, max_chars=20)
    assert len(short) <= 20


@pytest.mark.asyncio
async def test_accounts_with_profile_flags():
    await init_db()
    CID = "1000000006"
    store = ClientProfileStore()
    await store.apply_upsert(CID, {"brand": "A"}, operation="profile_save")
    flags = await store.accounts_with_profile([CID, "9999999999"])
    assert flags == {CID}


@pytest.mark.asyncio
async def test_clear_deletes_profile_but_history_survives():
    await init_db()
    CID = "1000000007"
    store = ClientProfileStore()
    await store.apply_upsert(CID, _kasi_patch(), operation="profile_save")
    async with Session() as s:  # запоминаем profile_id ДО удаления (детали проверяем по нему)
        pid = (
            await s.execute(select(ClientProfile.id).where(ClientProfile.customer_id == CID))
        ).scalar_one()
    res = await store.apply_clear(CID, confirmation_id="deadbeef")
    assert res["cleared"] is True
    assert await store.get_by_account(CID) is None

    async with Session() as s:
        contacts = (
            await s.execute(
                select(func.count())
                .select_from(ClientContact)
                .where(ClientContact.profile_id == pid)
            )
        ).scalar_one()
        services = (
            await s.execute(
                select(func.count())
                .select_from(ClientService)
                .where(ClientService.profile_id == pid)
            )
        ).scalar_one()
        hist = (
            await s.execute(
                select(func.count())
                .select_from(ClientProfileHistory)
                .where(ClientProfileHistory.customer_id == CID)
            )
        ).scalar_one()
    assert contacts == 0 and services == 0  # детали удалены
    assert hist >= 2  # save + clear записали историю (переживает clear — ключ customer_id, не FK)


@pytest.mark.asyncio
async def test_top_site_pages_orders_by_usefulness():
    """§20.6: страницы краула для sitelinks — services/catalog/price раньше, home последней."""
    from clients.store import ClientProfileStore as _CPS

    await init_db()
    CID = "1000000017"
    store = _CPS()
    await store.apply_upsert(
        CID,
        {"brand": "X"},
        operation="profile_save",
        crawl_extra={
            "website": "https://x.example",
            "site_pages": [
                {"url": "https://x.example/", "title": "Главная", "page_type": "home"},
                {"url": "https://x.example/services", "title": "Услуги", "page_type": "services"},
                {"url": "https://x.example/prices", "title": "Цены", "page_type": "price"},
                {"url": "https://x.example/blog", "title": "Блог", "page_type": "blog"},
            ],
        },
    )
    pages = await store.top_site_pages(CID)
    types = [p["page_type"] for p in pages]
    assert types[0] == "services"  # полезное для sitelinks — первым
    assert types[-1] == "home"  # главная — последней (обычно уже final_url)
    assert (await store.top_site_pages(CID, limit=2)).__len__() == 2
    assert await store.top_site_pages("9999999998") == []  # нет профиля → []
