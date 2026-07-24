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

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _legacy_access(monkeypatch):
    """C2: run_business_digest стал enforcement-aware; тесты модуля проверяют СБОРКУ дайджеста,
    не доступ — фиксируем legacy-проход (гранты других тест-файлов в общей SQLite включали бы
    auto-enforcement, и фильтр съедал бы аккаунты)."""
    import core.access as acc
    from core.config import settings as _cfg

    monkeypatch.setattr(_cfg, "account_access_mode", "legacy")
    acc._invalidate_enforcement_cache()
    yield
    acc._invalidate_enforcement_cache()


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
    # C3: Alert структурный (kind+params), текст рендерится i18n-ключом anomaly_<kind> на доставке.
    monkeypatch.setattr(
        jobs,
        "detect_anomalies",
        lambda cur, prev, thr=None, *, currency="": [
            SimpleNamespace(
                kind="spend_spike",
                params={"pct": "+100", "prev": "50.00", "now": "100.00", "cur": " USD"},
            )
        ],
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
    assert "Расход вырос на +100%" in text  # аномалии недели (C3: рендер i18n по kind+params)
    assert f"совет-{B}" not in text  # упавший B пропущен, но дайджест дошёл

    async with Session() as s:
        p1 = (await s.execute(select(func.count()).select_from(Proposal))).scalar()
    assert p0 == p1  # READ-ONLY: proposal не создаются


async def test_bizdigest_health_line_is_engine_only_and_writes_no_snapshot(monkeypatch):
    """Здоровье в дайджесте: та же строка, что у /audit и /report (один движок — расхождению текстов
    взяться неоткуда), но БЕЗ единого доп. чтения Google Ads.

    Два инварианта, каждый — про честность, а не про красоту:
    • gather_audit (23 чтения/аккаунт) в планировщике не зовётся: квота (структурный гард —
      tests/test_scheduler.py::test_scheduler_audit_is_engine_only);
    • снимок тренда НЕ пишется. Ключ account_health_snapshot — (customer_id, snapshot_date,
      period_days) без «режима сбора», а score_model_version у engine-only и полного /audit одинаков ⇒
      upsert дайджеста затёр бы честную базу, и клиент прочитал бы выдуманное «▲ +15 за неделю»."""
    from sqlalchemy import func, select

    import advisor.service as advisor_service
    from db.models import AccountHealthSnapshot
    from db.session import Session, init_db
    from reports.queries import Breakdown, Metrics
    from scheduler import jobs

    await init_db()
    chat, A = 6205, "3334445556"

    def _live_report():
        """Аккаунт с расходом и БЕЗ конверсий — движку есть что сказать (балл < 100)."""
        m = Metrics(impressions=1000, clicks=100, cost_micros=int(500 * 1e6), conversions=0.0)
        camp = Breakdown(
            "campaign", "Кампании", ["Кампания", "Статус"], [(("Поиск", "ENABLED"), m)]
        )
        return SimpleNamespace(
            customer_id=A,
            totals=m,
            prev_totals=m,
            period=SimpleNamespace(date_from="2026-06-29", date_to="2026-07-05"),
            currency="USD",
            breakdowns=[camp],
        )

    async def _arecipients():
        return {chat}

    async def _achats(_recipients):
        return {chat}

    async def _fake_client(cid=None):
        return object()

    async def _fake_period(_client, _acct, _n):
        return SimpleNamespace(date_from="2026-06-29", date_to="2026-07-05")

    async def _fake_read(fn, client, acct, *a, **kw):
        return "USD"

    async def _fake_report(_client, _acct, _period, **kw):
        return _live_report()

    async def _fake_build(chat_id, acct, **kw):
        return SimpleNamespace(recs=[])

    monkeypatch.setattr(jobs, "_recipients", _arecipients)
    monkeypatch.setattr(jobs, "_business_digest_chats", _achats)
    monkeypatch.setattr(jobs, "_scheduled_accounts", lambda: [A])
    monkeypatch.setattr(jobs, "build_client_async", _fake_client)
    monkeypatch.setattr(jobs, "_account_period", _fake_period)
    monkeypatch.setattr(jobs, "run_ads_read_call", _fake_read)
    monkeypatch.setattr(jobs, "build_account_report_async", _fake_report)
    monkeypatch.setattr(jobs, "summary_text", lambda r, lang=None: "SUMMARY")
    monkeypatch.setattr(jobs, "detect_anomalies", lambda *a, **kw: [])
    monkeypatch.setattr(advisor_service, "build_recommendations", _fake_build)

    sent: list[tuple[int, str]] = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text))

    async with Session() as s:
        snap0 = (await s.execute(select(func.count()).select_from(AccountHealthSnapshot))).scalar()

    await jobs.run_business_digest(FakeBot())

    assert len(sent) == 1
    text = sent[0][1]
    # Ровно та строка, что рисует движок для /audit и /report — не переписанная в планировщике.
    from audit.engine import build_audit
    from audit.render import audit_headline

    expected = audit_headline(build_audit(_live_report()), "ru")
    assert expected, "движку есть что сказать про этот аккаунт (иначе тест зелен вхолостую)"
    assert expected in text

    async with Session() as s:
        snap1 = (await s.execute(select(func.count()).select_from(AccountHealthSnapshot))).scalar()
    assert snap0 == snap1, "дайджест ПИШЕТ снимок здоровья — он отравит baseline тренда /audit"


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
