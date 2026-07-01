"""§20.4 (Фаза D): оркестрация фонового краула (bot.main._run_client_crawl) без сети.

Патчим crawler.* и structure_crawl (LLM) → фейковый результат. Проверяем: свежий профиль →
auto-save (сайт + карта страниц сохранены); существующий профиль → черновик profile_update
(«было→станет», pending), а исполнение по ✅ применяет crawl_extra (website/site_pages).
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402
from clients.crawler import CrawlPage, CrawlResult  # noqa: E402
from clients.execute import execute_confirmed_memory  # noqa: E402
from clients.profile_extract import ClientProfileExtract  # noqa: E402
from clients.store import ClientProfileStore  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
from db.session import init_db  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


class FakeBot:
    def __init__(self):
        self.sent: list = []

    async def send_message(self, chat_id, text="", **kw):
        self.sent.append((chat_id, text, kw))


def _fake_result() -> CrawlResult:
    return CrawlResult(
        domain="ex.com",
        pages=[CrawlPage(url="https://ex.com/", title="Home", page_type="home", text="dealer")],
        socials={"instagram": "https://instagram.com/kasi"},
        phones=["+254 712 345 678"],
        emails=[],
    )


async def _noop_robots(url):
    return lambda u: True


async def _noop_sitemap(url):
    return None


def _fake_crawl(result):
    async def _c(start_url, **kwargs):
        return result

    return _c


def _fake_structure(extract):
    async def _s(**kwargs):
        return extract

    return _s


@pytest.mark.asyncio
async def test_fresh_profile_autosaves():
    await init_db()
    cust = "6000000001"
    await ClientProfileStore().apply_clear(cust)  # гарантируем «свежесть»
    extract = ClientProfileExtract(
        brand="Kasi Motors",
        business_desc="автодилер",
        services=[{"name": "Поддержанные авто", "price": "от $4000"}],
    )
    bot = FakeBot()
    with (
        patched(bm.crawler, "load_robots", _noop_robots),
        patched(bm.crawler, "fetch_sitemap", _noop_sitemap),
        patched(bm.crawler, "crawl_site", _fake_crawl(_fake_result())),
        patched(bm, "structure_crawl", _fake_structure(extract)),
    ):
        await bm._run_client_crawl(bot, 700, cust, "https://ex.com/")

    prof = await ClientProfileStore().get_by_account(cust)
    assert prof is not None
    assert prof["brand"] == "Kasi Motors"
    assert prof["website"] == "https://ex.com/"
    assert prof["site_pages_count"] >= 1  # карта страниц сохранена
    assert any("+254" in c["value"] for c in prof["contacts"])  # телефон краулера влит

    # §20.4: богатая сводка «что нашли» — разделы/услуги/цены/контакты/соцсети
    msg = bot.sent[-1][1]
    assert "Обойдено страниц" in msg
    assert "Поддержанные авто" in msg  # услуга
    assert "$4000" in msg  # цена
    assert "instagram" in msg  # соцсеть
    assert "Home" in msg  # раздел (заголовок страницы)


@pytest.mark.asyncio
async def test_existing_profile_creates_update_proposal_and_applies_crawl_extra():
    await init_db()
    cust = "6000000002"
    chat_id = 701
    store = ClientProfileStore()
    await store.apply_clear(cust)
    await store.apply_upsert(cust, {"brand": "Old"}, operation="profile_save")  # уже есть профиль
    bm._LAST_PENDING.pop(chat_id, None)
    extract = ClientProfileExtract(business_desc="обновлённый бизнес")
    with (
        patched(bm.crawler, "load_robots", _noop_robots),
        patched(bm.crawler, "fetch_sitemap", _noop_sitemap),
        patched(bm.crawler, "crawl_site", _fake_crawl(_fake_result())),
        patched(bm, "structure_crawl", _fake_structure(extract)),
    ):
        await bm._run_client_crawl(FakeBot(), chat_id, cust, "https://ex.com/")

    cid = bm._LAST_PENDING[chat_id]
    snap = await ConfirmStore().get_confirmed(cid)
    assert snap.operation == "profile_update"
    assert snap.status == "pending"  # НЕ применено молча — ждёт ✅
    assert snap.params["source"] == "crawl"
    assert "site_pages" in snap.params["crawl_extra"]

    # ✅ (confirm + execute) → crawl_extra применён: сайт и карта страниц сохранены
    await ConfirmStore().confirm(cid, chat_id=chat_id)
    await execute_confirmed_memory(ConfirmStore(), cid)
    prof = await store.get_by_account(cust)
    assert prof["brand"] == "Old"  # старое не затёрто
    assert prof["website"] == "https://ex.com/"
    assert prof["site_pages_count"] >= 1
