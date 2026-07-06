"""1.6 (аудит 2026-07-06): недельный БИЗНЕС-дайджест менеджерам (/bizdigest, run_business_digest).

Инварианты: opt-in per-chat (по умолчанию тишина, fail-closed к анти-спаму); ОДНО сообщение на
оператора с блоками per-account (WoW-сводка + CPA + топ-3 совета + аномалии); READ-ONLY — Proposal
не создаются; сбой одного аккаунта не валит рассылку.
"""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def _report(cost=100.0, prev_cost=50.0, conv=4.0, prev_conv=2.0):
    return SimpleNamespace(
        totals=SimpleNamespace(cost_micros=int(cost * 1e6), conversions=conv),
        prev_totals=SimpleNamespace(cost_micros=int(prev_cost * 1e6), conversions=prev_conv),
        period=SimpleNamespace(date_from="2026-06-29", date_to="2026-07-05"),
        currency="USD",
        breakdowns=[],
    )


async def test_bizdigest_optin_reads_ui_prefs():
    from sqlalchemy import delete

    from db.models import UserSettings
    from db.session import Session, init_db
    from scheduler import jobs

    await init_db()
    on_chat, off_chat = 6201, 6202
    async with Session() as s:
        await s.execute(delete(UserSettings).where(UserSettings.chat_id.in_([on_chat, off_chat])))
        s.add(UserSettings(chat_id=on_chat, ui_prefs={"business_digest": "1"}))
        s.add(UserSettings(chat_id=off_chat, ui_prefs={"business_digest": "0"}))
        await s.commit()
    got = await jobs._business_digest_chats({on_chat, off_chat, 999999})
    assert got == {on_chat}  # только явный opt-in; неизвестный чат — тишина


async def test_bizdigest_one_message_with_blocks_no_proposal(monkeypatch):
    from sqlalchemy import func, select

    import advisor.service as advisor_service
    from db.models import Proposal
    from db.session import Session, init_db
    from scheduler import jobs

    await init_db()
    chat = 6203
    A, B = "1112223334", "2223334445"

    async def _arecipients():
        return {chat}

    async def _achats(_recipients):
        return {chat}

    monkeypatch.setattr(jobs, "_recipients", _arecipients)
    monkeypatch.setattr(jobs, "_business_digest_chats", _achats)
    monkeypatch.setattr(jobs, "_scheduled_accounts", lambda: [A, B])

    async def _fake_client(cid=None):
        return object()

    async def _fake_period(_client, _acct, _n):
        return SimpleNamespace(date_from="2026-06-29", date_to="2026-07-05")

    async def _fake_read(fn, client, acct, *a, **kw):
        return "USD"  # account_currency

    async def _fake_report(_client, acct, _period, **kw):
        if acct == B:
            raise RuntimeError("SDK down")  # сбой одного аккаунта не валит рассылку
        return _report()

    monkeypatch.setattr(jobs, "build_client_async", _fake_client)
    monkeypatch.setattr(jobs, "_account_period", _fake_period)
    monkeypatch.setattr(jobs, "run_ads_read_call", _fake_read)
    monkeypatch.setattr(jobs, "build_account_report_async", _fake_report)
    monkeypatch.setattr(jobs, "summary_text", lambda r, lang=None: f"SUMMARY[{lang}]")
    monkeypatch.setattr(
        jobs,
        "detect_anomalies",
        lambda cur, prev, thr=None, *, currency="": [SimpleNamespace(message="всплеск расхода")],
    )

    async def _fake_build(chat_id, acct, **kw):
        assert kw.get("persist") is False and kw.get("use_llm") is False
        return SimpleNamespace(recs=[SimpleNamespace(body=f"совет-{acct}")])

    monkeypatch.setattr(advisor_service, "build_recommendations", _fake_build)

    async def _acapture(exc, *, where):
        return "code"

    monkeypatch.setattr(jobs, "capture_exception", _acapture)

    sent: list[tuple[int, str]] = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text))

    async with Session() as s:
        p0 = (await s.execute(select(func.count()).select_from(Proposal))).scalar()

    await jobs.run_business_digest(FakeBot())

    assert len(sent) == 1  # одно сообщение на оператора
    text = sent[0][1]
    assert "SUMMARY" in text and "CPA" in text  # сводка + CPA-строка
    assert f"совет-{A}" in text  # топ-советы аккаунта A
    assert "всплеск расхода" in text  # аномалии недели
    assert f"совет-{B}" not in text  # упавший B пропущен, но дайджест дошёл

    async with Session() as s:
        p1 = (await s.execute(select(func.count()).select_from(Proposal))).scalar()
    assert p0 == p1  # READ-ONLY: proposal не создаются


async def test_bizdigest_silent_without_optin(monkeypatch):
    from scheduler import jobs

    async def _arecipients():
        return {6204}

    async def _achats(_recipients):
        return set()

    monkeypatch.setattr(jobs, "_recipients", _arecipients)
    monkeypatch.setattr(jobs, "_business_digest_chats", _achats)

    sent: list = []

    class FakeBot:
        async def send_message(self, *a, **kw):
            sent.append(a)

    await jobs.run_business_digest(FakeBot())
    assert sent == []
