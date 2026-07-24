"""§20→§19/§10 (Фаза E): профиль клиента подаётся в генераторы.

Проверяем: _merge_usp (профиль первым, кап, пусто→None); _cc_profile_ctx_account (текст профиля
или '' без профиля); и что cc_kw_generate прокидывает непустой profile в generate_seed_keywords,
когда у preview-аккаунта визарда есть профиль (при отсутствии — '' , прежнее поведение).
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402
import keywords.seeds as KS  # noqa: E402
from bot.callbacks import CcCB  # noqa: E402
from clients.store import ClientProfileStore  # noqa: E402
from db.session import init_db  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


class FakeMessage:
    def __init__(self, chat_id: int = 100):
        self.chat = type("C", (), {"id": chat_id})()
        self.answers: list = []

    async def answer(self, text: str = "", **kw):
        self.answers.append((text, kw))
        return self


class FakeCB:
    def __init__(self, chat_id: int = 100):
        self.message = FakeMessage(chat_id)
        self.from_user = type("U", (), {"id": chat_id})()
        self.answers: list = []

    async def answer(self, text: str = "", show_alert: bool = False, **kw):
        self.answers.append((text, show_alert))


class FakeState:
    def __init__(self, **data):
        self._data = dict(data)

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kw):
        self._data.update(kw)

    async def set_state(self, s):
        pass

    async def clear(self):
        self._data = {}


def test_copybrief_profile_is_emitted_in_prompt():
    from adcopy.generate import CopyBrief, _user_prompt

    # профиль — первоклассное поле CopyBrief (§20), выводится отдельно от usp (посадочная)
    brief = CopyBrief(topic="Авто", profile="Бренд: Kasi Motors", usp="УТП со страницы")
    prompt = _user_prompt(brief, 15, 4)
    assert "О клиенте: Бренд: Kasi Motors" in prompt
    assert "УТП: УТП со страницы" in prompt
    # без профиля — строка «О клиенте» не появляется (аддитивно, прежнее поведение)
    assert "О клиенте" not in _user_prompt(CopyBrief(topic="Авто"), 15, 4)


@pytest.mark.asyncio
async def test_profile_ctx_account_returns_text_or_empty():
    await init_db()
    cust = "7000000001"
    assert await bm._cc_profile_ctx_account(cust) == ""  # нет профиля → пусто
    await ClientProfileStore().apply_upsert(
        cust, {"brand": "Kasi Motors"}, operation="profile_save"
    )
    ctx = await bm._cc_profile_ctx_account(cust)
    assert "Kasi Motors" in ctx


@pytest.mark.asyncio
async def test_cc_kw_generate_passes_profile_to_seeds():
    await init_db()
    chat_id = 720
    preview = "7000000002"
    await ClientProfileStore().apply_upsert(
        preview, {"brand": "Kasi Motors", "business_desc": "автодилер"}, operation="profile_save"
    )
    session = await bm.CDRAFTS.create(
        chat_id=chat_id, customer_id=bm.DRAFT_ACCOUNT_ID, preview_customer_id=preview
    )
    await bm.CDRAFTS.patch(
        session,
        lambda s: s.__setitem__("settings", {"campaign_name": "Авто Кения"}),
        expected_chat_id=chat_id,
    )

    captured: dict = {}

    async def fake_seeds(*, topic, profile, language, url=None):
        captured["profile"] = profile
        captured["url"] = url  # §19.4.2: сайт из §20-профиля (здесь профиль без сайта → None)
        raise RuntimeError("stop-before-network")  # short-circuit до build_client/Sheets

    state = FakeState(cc_session=session)
    with patched(KS, "generate_seed_keywords", fake_seeds):
        await bm.cc_kw_generate(FakeCB(chat_id), CcCB(action="kw_generate"), state)

    assert "Kasi Motors" in captured["profile"]  # профиль preview-аккаунта дошёл до генератора


@pytest.mark.asyncio
async def test_cc_ad_url_passes_profile_to_rsa_brief():
    await init_db()
    chat_id = 721
    preview = "7000000003"
    await ClientProfileStore().apply_upsert(
        preview, {"brand": "Kasi Motors", "business_desc": "автодилер"}, operation="profile_save"
    )
    session = await bm.CDRAFTS.create(
        chat_id=chat_id, customer_id=bm.DRAFT_ACCOUNT_ID, preview_customer_id=preview
    )
    await bm.CDRAFTS.patch(
        session,
        lambda s: s.__setitem__("settings", {"campaign_name": "Авто Кения"}),
        expected_chat_id=chat_id,
    )

    captured: dict = {}

    async def fake_gen(brief):
        captured["brief"] = brief
        raise RuntimeError("stop-before-session")  # short-circuit курации/сессии

    async def fake_fetch(url):
        return "текст посадочной страницы"

    class M:
        def __init__(self):
            self.chat = type("C", (), {"id": chat_id})()
            self.text = "https://kasimotors.co.ke/used-cars"
            self.answers: list = []

        async def answer(self, text="", **kw):
            self.answers.append((text, kw))
            return self

    state = FakeState(cc_session=session)
    with patched(bm, "_generate_rsa", fake_gen), patched(bm.ingest, "fetch_url_text", fake_fetch):
        await bm.cc_ad_url(M(), state)

    brief = captured["brief"]
    assert "Kasi Motors" in (brief.profile or "")  # профиль → первоклассное поле CopyBrief.profile
    assert "посадочной" in (brief.usp or "")  # усп — с посадочной, отдельно от профиля


@pytest.mark.asyncio
async def test_cc_asset_type_sitelinks_gets_crawled_pages():
    """§20.6: cc_asset_type(family=sitelinks) прокидывает карту страниц краула preview-аккаунта
    в generate_asset (site_pages) — ссылки предлагаются из РЕАЛЬНЫХ url сайта."""
    await init_db()
    chat_id = 721
    preview = "7000000003"
    await ClientProfileStore().apply_upsert(
        preview,
        {"brand": "Kasi Motors"},
        operation="profile_save",
        crawl_extra={
            "website": "https://kasi.example",
            "site_pages": [
                {"url": "https://kasi.example/catalog", "title": "Каталог", "page_type": "catalog"},
                {"url": "https://kasi.example/", "title": "Главная", "page_type": "home"},
            ],
        },
    )
    session = await bm.CDRAFTS.create(
        chat_id=chat_id, customer_id=bm.DRAFT_ACCOUNT_ID, preview_customer_id=preview
    )
    await bm.CDRAFTS.patch(
        session,
        lambda s: (
            s.__setitem__("settings", {"campaign_name": "Авто Кения", "product": "авто"}),
            s["ad"].__setitem__("final_url", "https://kasi.example"),
        ),
        expected_chat_id=chat_id,
    )
    captured: dict = {}

    async def fake_generate_asset(family, **kw):
        captured.update(family=family, **kw)
        return {
            "family": "sitelinks",
            "params": {
                "sitelinks": [{"link_text": "Каталог", "final_url": "https://kasi.example/catalog"}]
            },
        }

    import adcopy.assets_gen as AG

    cq = FakeCB(chat_id)
    with patched(AG, "generate_asset", fake_generate_asset):
        await bm.cc_asset_type(
            cq, CcCB(action="asset_type", sub="sitelinks"), FakeState(cc_session=session)
        )
    assert captured.get("family") == "sitelinks"
    pages = captured.get("site_pages") or []
    assert any(p["url"] == "https://kasi.example/catalog" for p in pages)  # реальные url из краула
    # сообщение-пометка «из реальной карты сайта» отправлено
    assert any("карты сайта" in (t or "") for t, _ in cq.message.answers)


@pytest.mark.asyncio
async def test_cc_asset_type_gated_shows_requirement_not_silent():
    """§19.7.1: выбор типа, недоступного в v24/вне объёма (location/affiliate/app), НЕ молчит и НЕ
    падает — бот объясняет причину и возвращает в пикер (раньше эти типы просто отсутствовали)."""
    await init_db()
    chat_id = 724
    session = await bm.CDRAFTS.create(chat_id=chat_id, customer_id=bm.DRAFT_ACCOUNT_ID)
    for family, needle in (
        ("location", "Business Profile"),
        ("affiliate_location", "v24"),
        ("app", "вне объёма"),
    ):
        cq = FakeCB(chat_id)
        await bm.cc_asset_type(
            cq, CcCB(action="asset_type", sub=family), FakeState(cc_session=session)
        )
        texts_sent = " ".join(t or "" for t, _ in cq.message.answers)
        assert needle in texts_sent, f"{family}: причина не объяснена"
    # ни один недоступный тип не накопил ассет в черновике (assets.new пуст)
    draft = await bm.CDRAFTS.get(session, expected_chat_id=chat_id)
    assert not (draft.wizard_state.get("assets") or {}).get("new")


@pytest.mark.asyncio
async def test_cc_asset_type_lead_form_prompts_for_privacy_url():
    """§19.7.1: лид-форма РЕАЛИЗОВАНА — выбор запрашивает URL политики конфиденциальности (ассет
    строится за confirm-гейтом после ввода), а не отклоняется как «нужна настройка»."""
    await init_db()
    chat_id = 725
    session = await bm.CDRAFTS.create(chat_id=chat_id, customer_id=bm.DRAFT_ACCOUNT_ID)
    cq = FakeCB(chat_id)
    await bm.cc_asset_type(
        cq, CcCB(action="asset_type", sub="lead_form"), FakeState(cc_session=session)
    )
    texts_sent = " ".join(t or "" for t, _ in cq.message.answers)
    assert "конфиденциальн" in texts_sent  # просим privacy-URL, не отказываем
