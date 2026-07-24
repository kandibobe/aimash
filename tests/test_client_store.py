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


# ── 3.6б: кодовый дедуп RU/EN-алиасов и подстрок-дублей (merge_services) ──────────────────
def test_services_merge_collapses_en_ru_aliases_and_substring_dupes():
    """Двуязычный сайт даёт «Sedans» и «Седаны» — casefold-ключ их НЕ схлопнул бы (разные
    алфавиты). Алиас-карта _SVC_ALIASES сводит их в одну запись (имя существующей выживает,
    непустые поля обновляются); «ремонт квартир под ключ» дописывается в «Ремонт квартир»
    (подстрока по границе слов)."""
    from clients.store import merge_services

    existing = [
        {"name": "Седаны", "price": "от $4000", "category": "авто"},
        {"name": "Ремонт квартир", "price": None, "category": None},
    ]
    incoming = [
        {"name": "Sedans", "price": "from $4500"},  # EN-алиас → апдейт «Седаны», не дубль
        {"name": "Ремонт квартир под ключ", "price": "от 10 000"},  # подстрока → апдейт
        {"name": "Пикапы"},  # новое → добавится
    ]
    got = merge_services(existing, incoming)
    assert [s["name"] for s in got] == ["Седаны", "Ремонт квартир", "Пикапы"]
    by = {s["name"]: s for s in got}
    assert by["Седаны"]["price"] == "from $4500"  # цена обновлена
    assert by["Седаны"]["category"] == "авто"  # непереданное поле сохранено
    assert by["Ремонт квартир"]["price"] == "от 10 000"


def test_services_merge_selfheals_preexisting_bilingual_dupes():
    """Дубли, накопленные в БД ДО фикса («Sedans» и «Седаны» уже лежат рядом), схлопываются при
    следующем апдейте: поля сливаются, а не теряются (existing идёт через тот же фолд)."""
    from clients.store import merge_services

    existing = [
        {"name": "Седаны", "price": "от $4000", "category": "авто"},
        {"name": "Sedans", "price": None, "description": "japan imports", "category": None},
    ]
    got = merge_services(existing, [])
    assert [s["name"] for s in got] == ["Седаны"]
    assert got[0]["description"] == "japan imports"  # поле дубля не потеряно
    assert got[0]["price"] == "от $4000"  # пустое поле дубля НЕ затёрло цену

    # конфликт двух НЕпустых полей: побеждает более поздняя строка (порядок детерминирован —
    # _to_dict читает с ORDER BY id; до фикса поздний дубль затирал ранний ЦЕЛИКОМ, теперь по полям)
    got2 = merge_services(
        [{"name": "Седаны", "price": "от $4000"}, {"name": "Sedans", "price": "от $4500"}], []
    )
    assert got2 == [{"name": "Седаны", "price": "от $4500"}]


def test_services_merge_distinct_services_never_collapse():
    """Fail-safe guard'ы подстроки: «голое слово + уточнённое» — это РАЗНЫЕ услуги, не дубль
    («Запчасти» ≠ «Запчасти Toyota»); схлоп разрешён только когда короткий ключ сам ≥2 слов
    И ≥5 симв. («СТО» не втягивается в «СТО двигателей»). Лучше дубль, чем ложный мердж."""
    from clients.store import merge_services

    existing = [{"name": "Запчасти", "description": "оригинал"}, {"name": "СТО двигателей"}]
    got = merge_services(existing, [{"name": "Запчасти Toyota"}, {"name": "СТО"}])
    assert [s["name"] for s in got] == ["Запчасти", "СТО двигателей", "Запчасти Toyota", "СТО"]
    assert got[0]["description"] == "оригинал"  # ничего не затёрто соседней услугой


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


def _overlong_patch() -> dict:
    """Скаляры длиннее своих колонок: LLM пишет в geo/language фразу, а не список (живой кейс —
    «Based in Kobe, Japan; serves buyers in 100+ countries…»)."""
    return {
        "brand": "Б" * 400,
        "geo": "Г" * 300,
        "language": "English, Japanese, Russian, Swahili and Arabic depending on the market",
        "website": "https://example.com/" + "a" * 3000,
        "business_desc": "О" * 5000,  # Text-колонка — НЕ режется
    }


def test_scalar_max_mirrors_columns():
    """Дрейф-гард: _SCALAR_MAX обязан совпадать с длинами VARCHAR-колонок ClientProfile. Правка
    модели/миграции без правки карты лимитов ⇒ Postgres снова роняет upsert (SQLite смолчит)."""
    from clients.store import _SCALAR_MAX

    cols = ClientProfile.__table__.columns
    for field, limit in _SCALAR_MAX.items():
        assert cols[field].type.length == limit, f"{field}: колонка разошлась с _SCALAR_MAX"


def test_preview_merge_caps_scalars_to_column_limits():
    """Скаляры режутся ДО вставки (иначе Postgres: StringDataRightTruncation). Text-поля — нет."""
    from clients.store import _SCALAR_MAX, preview_merge

    out = preview_merge(None, _overlong_patch())
    for field, limit in _SCALAR_MAX.items():
        assert len(out[field]) == limit, f"{field} не обрезан под лимит колонки"
    assert len(out["business_desc"]) == 5000  # Text — обрезать нечего


@pytest.mark.asyncio
async def test_overlong_scalars_write_and_match_preview():
    """Инвариант «diff = запись» держится и на переполнении: что показали в «станет», то и в БД."""
    from clients.store import _SCALAR_MAX, preview_merge

    await init_db()
    CID = "1000000017"
    store = ClientProfileStore()
    patch = _overlong_patch()
    predicted = preview_merge(None, patch)
    await store.apply_upsert(CID, patch, operation="profile_save")  # не должен падать
    actual = await store.get_by_account(CID)
    for field in _SCALAR_MAX:
        assert actual[field] == predicted[field]


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


@pytest.mark.asyncio
async def test_site_page_title_capped_and_text_persisted():
    """title пишется в String(512): без обрезки длинный <title> роняет ВЕСЬ upsert на Postgres
    (22001 StringDataRightTruncation; SQLite длину не enforce'ит — на тестах баг был невидим).
    text страницы хранится: досье пересобирается без повторного обхода сайта."""
    from db.models import ClientSitePage

    await init_db()
    cid = "1000000019"
    store = ClientProfileStore()
    await store.apply_upsert(
        cid,
        {"brand": "X"},
        operation="profile_save",
        crawl_extra={
            "site_pages": [
                {
                    "url": "https://x.example/about",
                    "title": "Т" * 900,  # реальные <title> с SEO-хвостом бывают и длиннее
                    "page_type": "about",
                    "text": "Компания основана в 2016 году.",
                    "content_hash": "h1",
                }
            ]
        },
    )
    async with Session() as s:
        prof_id = (
            await s.execute(select(ClientProfile.id).where(ClientProfile.customer_id == cid))
        ).scalar()
        row = (
            await s.execute(select(ClientSitePage).where(ClientSitePage.profile_id == prof_id))
        ).scalar_one()
    assert len(row.title) == 500 and row.title.startswith("Т")
    assert row.text == "Компания основана в 2016 году."
