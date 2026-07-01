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

    async def fake_seeds(*, topic, profile, language):
        captured["profile"] = profile
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
