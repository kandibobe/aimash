"""§19 Этапы 2/5/6/7: проводка визарда без минтинга proposal до финала; финал собирает один
composite proposal create_search_campaign из накопленного черновика.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ads.client as ads_client  # noqa: E402
import bot.main as bm  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from ads.read import ReusableAsset  # noqa: E402
from bot.callbacks import CcCB  # noqa: E402
from db.models import Proposal  # noqa: E402
from db.session import Session, init_db  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


class FakeMessage:
    def __init__(self, text="", chat_id=100):
        self.text = text
        self.chat = type("C", (), {"id": chat_id})()
        self.answers: list = []
        self.edits: list = []

    async def answer(self, text="", **kw):
        self.answers.append((text, kw))
        return self

    async def edit_text(self, text="", **kw):
        self.edits.append((text, kw))
        return self


class FakeCallbackQuery:
    def __init__(self, message, uid=100):
        self.message = message
        self.from_user = type("U", (), {"id": uid})()
        self.answers: list = []

    async def answer(self, text="", show_alert=False, **kw):
        self.answers.append((text, show_alert))


class FakeFSM:
    def __init__(self, data=None):
        self._d = dict(data or {})

    async def get_data(self):
        return dict(self._d)

    async def update_data(self, **kw):
        self._d.update(kw)

    async def set_state(self, *a, **k):
        pass

    async def clear(self):
        self._d = {}


async def _muts(chat_id: int) -> int:
    async with Session() as s:
        return int(
            (
                await s.execute(
                    select(func.count())
                    .select_from(Proposal)
                    .where(Proposal.chat_id == chat_id, Proposal.operation != "rsa_curation")
                )
            ).scalar_one()
        )


async def _full_draft(chat: int) -> str:
    """Черновик, дошедший до финала: настройки + объявление + ключи."""
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)

    def _fill(st):
        st["settings"] = {
            "campaign_name": "Кения · Авто · Search",
            "geo_locations": ["Кения"],
            "languages": ["English"],
            "budget_daily_micros": 40_000_000,
            "cpc_bid_micros": 180_000,
            "bidding_strategy": "maximize_conversions",
            "match_type": "phrase",
            "by_analogy": [],
        }
        st["ad"] = {
            "final_url": "https://shop.example/used",
            "path1": "Used-Cars",
            "path2": "Кения",
            "headlines": ["Поддержанные авто", "Проверенные б/у", "Авто с гарантией"],
            "descriptions": ["Большой выбор авто с пробегом.", "Гарантия и проверка."],
        }
        st["keywords"] = {"list": ["used cars nairobi"], "match_type": "phrase"}

    await bm.CDRAFTS.patch(sid, _fill)
    return sid


@pytest.mark.asyncio
async def test_stage2_provide_keywords_advances():
    await init_db()
    chat = 7700201
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    await bm.CDRAFTS.set_step(sid, 2)
    fsm = FakeFSM({"cc_session": sid})
    await bm.cc_keywords_text(FakeMessage("[used cars], cheap cars kenya", chat_id=chat), fsm)
    snap = await bm.CDRAFTS.get(sid)
    assert snap.wizard_state["keywords"]["list"] == ["used cars", "cheap cars kenya"]
    assert snap.current_step == 3  # ушли к объявлению
    assert await _muts(chat) == 0


@pytest.mark.asyncio
async def test_stage5_use_assets_reuses_and_advances():
    await init_db()
    chat = 7700202
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    await bm.CDRAFTS.set_step(sid, 5)
    fsm = FakeFSM({"cc_session": sid})

    async def fake_read(fn, client, cid, **kw):
        return [
            ReusableAsset("customers/x/assets/1", "SITELINK", "Каталог"),
            ReusableAsset("customers/x/assets/2", "CALLOUT", "Гарантия"),
        ]

    cq = FakeCallbackQuery(FakeMessage(chat_id=chat))
    with (
        patched(ads_client, "build_client", lambda *a, **k: object()),
        patched(bm, "run_ads_read_call", fake_read),
    ):
        await bm.cc_use_assets(cq, CcCB(action="use_assets"), fsm)
    snap = await bm.CDRAFTS.get(sid)
    assert len(snap.wizard_state["assets"]["reuse_links"]) == 2
    assert (
        snap.current_step == 5
    )  # вернулись в меню ассетов (можно добавить ещё; «Готово» → этап 6)
    assert await _muts(chat) == 0

    # «Готово» (skip на этапе 5) → этап 6
    cq2 = FakeCallbackQuery(FakeMessage(chat_id=chat))
    await bm.cc_skip(cq2, CcCB(action="skip"), fsm)
    snap2 = await bm.CDRAFTS.get(sid)
    assert snap2.current_step == 6


@pytest.mark.asyncio
async def test_stage6_url_options_saved_and_advances():
    await init_db()
    chat = 7700203
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    await bm.CDRAFTS.set_step(sid, 6)
    fsm = FakeFSM({"cc_session": sid})
    await bm.cc_url_text(
        FakeMessage("{lpurl}?utm_source=google | utm_medium=cpc", chat_id=chat), fsm
    )
    snap = await bm.CDRAFTS.get(sid)
    assert snap.wizard_state["url_options"]["tracking_url_template"] == "{lpurl}?utm_source=google"
    assert snap.wizard_state["url_options"]["final_url_suffix"] == "utm_medium=cpc"
    assert snap.current_step == 7  # ушли к финалу
    assert await _muts(chat) == 0


@pytest.mark.asyncio
async def test_stage7_create_builds_composite_proposal():
    await init_db()
    chat = 7700204
    sid = await _full_draft(chat)
    await bm.CDRAFTS.set_step(sid, 7)
    fsm = FakeFSM({"cc_session": sid})
    captured = {}

    async def fake_present(message, *, chat_id, operation, params, summary, cid):
        captured.update(operation=operation, params=params)

    cq = FakeCallbackQuery(FakeMessage(chat_id=chat))
    with patched(bm, "_present_proposal", fake_present):
        await bm.cc_create(cq, CcCB(action="create"), fsm)

    assert captured["operation"] == "create_search_campaign"
    p = captured["params"]
    assert p["campaign_name"].startswith("Кения")
    assert p["geo_locations"] == ["Кения"]
    assert p["languages"] == ["English"]
    assert p["bidding"]["strategy"] == "maximize_conversions"
    assert p["path1"] == "Used-Cars"
    assert p["url_options"] is None  # не задавали
    # черновик визарда помечен done
    snap = await bm.CDRAFTS.get(sid)
    assert snap.status == "done"


@pytest.mark.asyncio
async def test_launch_button_mints_resume_proposal():
    """§19.8: «🚀 Запустить» после создания → resume_campaign proposal (confirm-гейт), не прямой запуск.
    Кэш имени одноразовый (не запустить дважды по старой кнопке)."""
    await init_db()
    chat = 7700206
    bm._CC_LAUNCH_CACHE[chat] = "Кения · Авто · Search"
    captured = {}

    async def fake_present(message, *, chat_id, operation, params, summary, cid):
        captured.update(operation=operation, params=params)

    cq = FakeCallbackQuery(FakeMessage(chat_id=chat))
    with patched(bm, "_present_proposal", fake_present):
        await bm.cc_launch(cq, CcCB(action="launch"))
    assert captured["operation"] == "resume_campaign"
    assert captured["params"]["campaign"] == "Кения · Авто · Search"
    assert chat not in bm._CC_LAUNCH_CACHE  # одноразово

    # повторный клик по старой кнопке → нечего запускать (show_alert), proposal не минтится
    captured.clear()
    cq2 = FakeCallbackQuery(FakeMessage(chat_id=chat))
    with patched(bm, "_present_proposal", fake_present):
        await bm.cc_launch(cq2, CcCB(action="launch"))
    assert not captured
    assert cq2.answers and cq2.answers[-1][1] is True  # show_alert


@pytest.mark.asyncio
async def test_stage7_create_blocks_without_ad():
    await init_db()
    chat = 7700205
    sid = await bm.CDRAFTS.create(chat_id=chat, customer_id=DRAFT_ACCOUNT_ID)
    await bm.CDRAFTS.set_step(sid, 7)
    fsm = FakeFSM({"cc_session": sid})
    cq = FakeCallbackQuery(FakeMessage(chat_id=chat))
    called = {"present": False}

    async def fake_present(*a, **k):
        called["present"] = True

    with patched(bm, "_present_proposal", fake_present):
        await bm.cc_create(cq, CcCB(action="create"), fsm)
    assert called["present"] is False  # без объявления — нет proposal
    assert cq.answers and cq.answers[-1][1] is True  # show_alert
