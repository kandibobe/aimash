"""§8/мультиаккаунт: меню /campaigns читает И мутирует ОДИН аккаунт-якорь (не хардкод Draft).

Инварианты: (a) дефолт аккаунта-якоря = Draft (fail-closed, поведение как раньше); (b) _send_campaigns
запоминает АКТИВНЫЙ аккаунт чтения; (c) кнопки меню минтят proposal на тот же аккаунт (customer_id
пробрасывается в _present_proposal → там ensure_allowed заново, не-Draft вне allow-list отсекается).
Замок мутаций при этом не ослабляется (тесты замка — tests/test_safety_core.py).
"""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import ads.client as ads_client  # noqa: E402
import bot.main as bm  # noqa: E402

_ACCT = "1112223334"


class _FakeMsg:
    def __init__(self):
        self.answers: list = []
        self.chat = SimpleNamespace(id=777001)

    async def answer(self, text="", **kw):
        self.answers.append((text, kw))


def test_camp_account_defaults_to_draft():
    # нет записи в _CAMP_ACCT → Draft (fail-closed, меню как раньше)
    assert bm._camp_account(999999) == bm.DRAFT_ACCOUNT_ID


async def test_send_campaigns_anchors_active_account(monkeypatch):
    chat = 777001

    async def _fake_active(_chat):
        return _ACCT

    async def _fake_client(cid=None):
        return object()

    async def _fake_read(fn, client, customer_id, *a, **kw):
        # проверяем, что читаем ИМЕННО активный аккаунт, не Draft
        assert customer_id == _ACCT
        return [{"name": "Camp A", "id": "1", "status": "ENABLED"}]

    monkeypatch.setattr(bm, "_active_read_account", _fake_active)
    monkeypatch.setattr(ads_client, "build_client_async", _fake_client)
    monkeypatch.setattr(bm, "run_ads_read_call", _fake_read)

    msg = _FakeMsg()
    await bm._send_campaigns(msg, chat)
    assert bm._CAMP_ACCT.get(chat) == _ACCT  # аккаунт-якорь запомнен
    assert bm._CAMP_CACHE.get(chat)  # список кампаний закэширован


async def test_camp_mutate_targets_anchor_account(monkeypatch):
    chat = 777002
    bm._CAMP_CACHE[chat] = [{"name": "Camp A", "id": "1", "status": "ENABLED"}]
    bm._CAMP_ACCT[chat] = _ACCT

    captured: dict = {}

    async def _fake_present(message, *, chat_id, operation, params, summary, cid, **kw):
        captured["customer_id"] = kw.get("customer_id")
        captured["operation"] = operation
        captured["campaign"] = params.get("campaign")

    monkeypatch.setattr(bm, "_present_proposal", _fake_present)

    answered: list = []

    async def _answer(*a, **kw):
        answered.append((a, kw))

    cq = SimpleNamespace(
        message=SimpleNamespace(chat=SimpleNamespace(id=chat), answer=_answer),
        from_user=SimpleNamespace(id=chat),
        answer=_answer,
    )
    await bm._camp_mutate(cq, 0, "pause_campaign")
    # мутация нацелена на аккаунт-якорь меню, не на Draft
    assert captured["customer_id"] == _ACCT
    assert captured["operation"] == "pause_campaign"
    assert captured["campaign"] == "Camp A"
