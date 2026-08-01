"""Тесты планировщика (Фаза 3, ТЗ §14): аномалии (чистая логика), очистка просроченных
черновиков (на временном SQLite), и КОД-ГАРД golden rule #3 — планировщик не меняет аккаунт.
"""

from __future__ import annotations

import pathlib
import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from ads.client import DRAFT_ACCOUNT_ID
from scheduler.anomaly import detect_anomalies


def _m(cost: float, conv: float):
    return SimpleNamespace(cost=cost, conversions=conv)


def _traffic(cost: float, conv: float, *, impressions: int, clicks: int):
    return SimpleNamespace(
        cost=cost,
        conversions=conv,
        impressions=impressions,
        clicks=clicks,
    )


async def _fake_client_async(*a, **k):
    # scheduler зовёт await build_client_async(acct) (холодная сборка вне loop) — фейк-заглушка.
    return None


def _arecipients(value):
    """D9: _recipients теперь корутина (env ∪ БД whitelist). Фейк, возвращающий готовый set."""

    async def _f():
        return set(value)

    return _f


@pytest.fixture(autouse=True)
def _legacy_access(monkeypatch):
    """C2: джобы стали enforcement-aware; тесты этого модуля проверяют АГРЕГАЦИЮ дайджестов, не
    доступ — фиксируем legacy-проход. Иначе гранты, оставленные другими тест-файлами в общей
    SQLite (auto-режим ⇒ enforcement включён), заставляли бы C2-фильтр съедать аккаунты.
    Enforced-семантика покрыта отдельным тестом ниже (он переопределяет режим сам)."""
    import core.access as acc
    from core.config import settings as _cfg

    monkeypatch.setattr(_cfg, "account_access_mode", "legacy")
    acc._invalidate_enforcement_cache()
    yield
    acc._invalidate_enforcement_cache()


# ── P1-J: per-account оверлей порогов ─────────────────────────────────────────────
def test_effective_thresholds_overlay():
    from scheduler.jobs import _effective_thresholds

    thr = {"spend_spike_pct": 50.0, "per_account": {"111": {"min_spend": 100.0}}}
    # для аккаунта 111 — оверлей поверх chat-дефолтов; ключ per_account не течёт в пороги
    eff = _effective_thresholds(thr, "111")
    assert eff == {"spend_spike_pct": 50.0, "min_spend": 100.0}
    # для другого аккаунта — только базовые (без per_account)
    assert _effective_thresholds(thr, "999") == {"spend_spike_pct": 50.0}
    # None/пусто — как есть
    assert _effective_thresholds(None, "111") is None


def test_per_account_min_spend_overlay_changes_detection():
    from scheduler.jobs import _effective_thresholds

    thr = {"min_spend": 1.0, "per_account": {"777": {"min_spend": 1000.0}}}
    # аккаунт 777: расход 500 при высоком min_spend=1000 → шум подавлен (нет алертов)
    eff777 = _effective_thresholds(thr, "777")
    assert detect_anomalies(_m(500, 0), _m(0, 0), eff777) == []
    # другой аккаунт: базовый min_spend=1.0 → spend_no_conv срабатывает
    eff_other = _effective_thresholds(thr, "555")
    assert any(
        a.kind == "spend_no_conv" for a in detect_anomalies(_m(500, 0), _m(10, 5), eff_other)
    )


# ── Аномалии (чистая логика, без SDK) ────────────────────────────────────────────
def test_anomaly_spend_spike():
    alerts = detect_anomalies(_m(200, 5), _m(100, 5))  # +100% расход
    kinds = {a.kind for a in alerts}
    assert "spend_spike" in kinds
    assert "conv_drop" not in kinds


def test_anomaly_conv_drop():
    alerts = detect_anomalies(_m(100, 4), _m(100, 10))  # -60% конверсий
    assert any(a.kind == "conv_drop" for a in alerts)


def test_anomaly_none_when_stable():
    assert detect_anomalies(_m(100, 10), _m(95, 10)) == []  # +5% расход, 0% конв.


def test_anomaly_ignores_tiny_spend():
    assert detect_anomalies(_m(0.5, 0), _m(0.1, 0)) == []  # оба < min_spend → тишина


def test_anomaly_prev_zero_no_divzero():
    alerts = detect_anomalies(_m(50, 0), _m(0, 0))  # база расхода 0 → не делим на ноль
    assert all(a.kind != "spend_spike" for a in alerts)


def test_anomaly_spend_no_conv():
    alerts = detect_anomalies(_m(50, 0), _m(40, 8))  # были конверсии, стало 0 при расходе
    kinds = {a.kind for a in alerts}
    assert "conv_drop" in kinds and "spend_no_conv" in kinds


def test_anomaly_custom_thresholds():
    # порог 200% — рост на 100% НЕ должен триггерить
    assert detect_anomalies(_m(200, 5), _m(100, 5), {"spend_spike_pct": 200.0}) == []


def test_anomaly_detects_stopped_delivery():
    alerts = detect_anomalies(
        _traffic(0, 0, impressions=0, clicks=0),
        _traffic(0, 2, impressions=1000, clicks=100),
    )
    assert "delivery_stopped" in {a.kind for a in alerts}


def test_anomaly_detects_impressions_and_ctr_drop():
    alerts = detect_anomalies(
        _traffic(20, 2, impressions=200, clicks=4),
        _traffic(20, 2, impressions=1000, clicks=100),
    )
    kinds = {a.kind for a in alerts}
    assert {"impressions_drop", "ctr_drop"} <= kinds


def test_anomaly_ignores_delivery_noise_below_previous_impressions_floor():
    alerts = detect_anomalies(
        _traffic(0, 0, impressions=0, clicks=0),
        _traffic(0, 0, impressions=10, clicks=1),
    )
    assert not {"delivery_stopped", "impressions_drop", "ctr_drop"} & {a.kind for a in alerts}


def test_scheduler_account_health_includes_status_heartbeats(monkeypatch):
    from audit import engine
    from scheduler import jobs

    captured = {}

    def fake_build(report, **kwargs):
        captured["report"] = report
        captured.update(kwargs)
        return "health"

    monkeypatch.setattr(engine, "build_audit", fake_build)
    report = object()
    policy = [SimpleNamespace(approval_status="DISAPPROVED")]

    assert jobs._account_health(report, ad_policy=policy, account_status="SUSPENDED") == "health"
    assert captured == {
        "report": report,
        "ad_policy": policy,
        "account_status": "SUSPENDED",
    }


# ── Очистка просроченных черновиков (reject + audit), на временном SQLite ─────────
async def test_cleanup_stale_rejects_old_pending_only():
    from confirm.store import ConfirmStore
    from db.models import AuditLog, Proposal
    from db.session import Session, init_db
    from scheduler.jobs import cleanup_stale_proposals

    await init_db()
    old_cid, fresh_cid = uuid.uuid4().hex, uuid.uuid4().hex
    async with Session() as s:
        s.add(
            Proposal(
                confirmation_id=old_cid,
                operation="update_budget",
                customer_id=DRAFT_ACCOUNT_ID,
                summary="старый",
                params={},
                chat_id=1,
                user_initiated=True,
                status="pending",
                created_at=datetime(2020, 1, 1),  # заведомо просрочен
            )
        )
        s.add(
            Proposal(
                confirmation_id=fresh_cid,
                operation="update_budget",
                customer_id=DRAFT_ACCOUNT_ID,
                summary="свежий",
                params={},
                chat_id=1,
                user_initiated=True,
                status="pending",  # created_at = сейчас (server_default)
            )
        )
        await s.commit()

    n = await cleanup_stale_proposals(ttl_hours=24)
    assert n >= 1

    store = ConfirmStore()
    assert (await store.get_confirmed(old_cid)).status == "rejected"  # просроченный отклонён
    assert (await store.get_confirmed(fresh_cid)).status == "pending"  # свежий не тронут

    async with Session() as s:
        audit = (
            (await s.execute(select(AuditLog).where(AuditLog.confirmation_id == old_cid)))
            .scalars()
            .all()
        )
    assert any(r.status == "rejected" for r in audit)  # отклонение записано в audit


# ── Пороги аномалий per-chat из UserSettings (ТЗ §14, БЛОК F) ─────────────────────
async def test_anomaly_thresholds_read_from_user_settings(monkeypatch):
    """run_anomaly_check читает UserSettings.alert_thresholds per-chat: чат с высоким личным
    порогом НЕ получает алерт, а чат на дефолте — получает. Метрики аккаунта общие, пороги — нет.
    READ-ONLY (golden rule #3): SDK не зовётся (fetch_totals замокан)."""
    from sqlalchemy import delete

    from db.models import UserSettings
    from db.session import Session, init_db
    from scheduler import jobs

    await init_db()
    async with Session() as s:
        # Идемпотентность: временный SQLite переживает между прогонами → чистим фикс. ключи.
        await s.execute(delete(UserSettings).where(UserSettings.chat_id.in_([1, 2])))
        # chat 1 — личный высокий порог (200%): рост на 100% его НЕ пробивает.
        s.add(UserSettings(chat_id=1, alert_thresholds={"spend_spike_pct": 200.0}))
        await s.commit()

    # Расход +100% (100 → 200), конверсии без изменений. fetch_totals: 1-й вызов cur, 2-й prev.
    seq = iter([_m(200, 5), _m(100, 5)])
    # build_client_async теперь per-account (принимает customer_id) → async-фейк глотает аргументы.
    monkeypatch.setattr(jobs, "build_client_async", _fake_client_async)
    monkeypatch.setattr(jobs, "fetch_totals", lambda *a, **k: next(seq))
    monkeypatch.setattr(jobs, "_recipients", _arecipients({1, 2}))
    # Один аккаунт (метрики аккаунта общие, пороги — per-chat): seq из 2 значений = cur+prev.
    monkeypatch.setattr(jobs, "_scheduled_accounts", lambda: [DRAFT_ACCOUNT_ID])

    class FakeBot:
        def __init__(self):
            self.sent: list[int] = []

        async def send_message(self, chat_id, text, **kw):
            self.sent.append(chat_id)

    bot = FakeBot()
    await jobs.run_anomaly_check(bot)

    assert 2 in bot.sent  # дефолтный порог 50% < 100% → алерт
    assert 1 not in bot.sent  # личный порог 200% > 100% → тишина


# ── Мультиаккаунт (§8): ОДИН дайджест/сообщение на оператора по всем аккаунтам ─────
async def test_scheduled_report_multi_account_one_digest(monkeypatch):
    """run_scheduled_report обходит все разрешённые аккаунты и шлёт ОДИН дайджест на оператора
    (анти-спам), а не по сообщению на аккаунт."""
    from scheduler import jobs

    A, B = "1112223334", "2223334445"
    monkeypatch.setattr(jobs, "_scheduled_accounts", lambda: [A, B])
    monkeypatch.setattr(jobs, "build_client_async", _fake_client_async)

    async def fake_report(_client, acct, _period, **k):
        return f"R:{acct}"

    monkeypatch.setattr(jobs, "build_account_report_async", fake_report)
    monkeypatch.setattr(jobs, "summary_text", lambda r, lang=None: r)  # r=="R:<acct>" (3H: +lang)
    monkeypatch.setattr(jobs, "_recipients", _arecipients({1}))

    sent: list[tuple[int, str]] = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text))

    await jobs.run_scheduled_report(FakeBot())
    assert len(sent) == 1  # ОДНО сообщение оператору, не два
    assert f"R:{A}" in sent[0][1] and f"R:{B}" in sent[0][1]  # оба аккаунта в дайджесте


async def test_scheduled_report_prefixes_health_and_survives_broken_report(monkeypatch):
    """Плановый отчёт открывается тем же «что горит», что и /report: балл/грейд/под-риском НАД
    цифрами (engine-only, 0 доп. чтений). Здоровье — КОСМЕТИКА: отчёт, по которому движок не смог
    посчитать (кривой/пустой), обязан дойти БЕЗ строки, а не упасть — иначе новая фича роняет
    доставку старой (её и проверяет соседний тест: там report — строка, движок давится и молчит)."""
    from types import SimpleNamespace

    from reports.queries import Breakdown, Metrics
    from scheduler import jobs

    A = "1112223334"
    m = Metrics(impressions=1000, clicks=100, cost_micros=int(500 * 1e6), conversions=0.0)
    camp = Breakdown("campaign", "Кампании", ["Кампания", "Статус"], [(("Поиск", "ENABLED"), m)])
    rep = SimpleNamespace(
        customer_id=A,
        totals=m,
        prev_totals=None,
        period=SimpleNamespace(date_from="2026-06-29", date_to="2026-07-05"),
        currency="USD",
        breakdowns=[camp],
    )

    async def fake_report(_client, _acct, _period, **k):
        return rep

    monkeypatch.setattr(jobs, "_scheduled_accounts", lambda: [A])
    monkeypatch.setattr(jobs, "build_client_async", _fake_client_async)
    monkeypatch.setattr(jobs, "build_account_report_async", fake_report)
    monkeypatch.setattr(jobs, "summary_text", lambda r, lang=None: "SUMMARY")
    monkeypatch.setattr(jobs, "_recipients", _arecipients({1}))

    sent: list[tuple[int, str]] = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text))

    await jobs.run_scheduled_report(FakeBot())

    from audit.engine import build_audit
    from audit.render import audit_headline

    expected = audit_headline(build_audit(rep), "ru")
    assert expected, "движку есть что сказать (иначе тест зелен вхолостую)"
    body = sent[0][1]
    assert expected in body
    assert body.index(expected) < body.index("SUMMARY"), "здоровье идёт НАД цифрами, как в /report"


async def test_anomaly_multi_account_one_message(monkeypatch):
    """run_anomaly_check собирает аномалии по нескольким аккаунтам в ОДНО сообщение на оператора."""
    from scheduler import jobs

    A, B = "1112223334", "2223334445"
    monkeypatch.setattr(jobs, "_scheduled_accounts", lambda: [A, B])
    monkeypatch.setattr(jobs, "build_client_async", _fake_client_async)
    # Оба аккаунта со всплеском расхода (+100%): curA,prevA, curB,prevB.
    seq = iter([_m(200, 5), _m(100, 5), _m(300, 5), _m(150, 5)])
    monkeypatch.setattr(jobs, "fetch_totals", lambda *a, **k: next(seq))
    monkeypatch.setattr(jobs, "_recipients", _arecipients({2}))  # дефолтный порог → алерт

    sent: list[tuple[int, str]] = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text))

    await jobs.run_anomaly_check(FakeBot())
    assert len(sent) == 1  # одно сообщение оператору
    assert f"Аккаунт {A}" in sent[0][1] and f"Аккаунт {B}" in sent[0][1]


async def test_scheduled_report_enforced_filters_recipient_accounts(monkeypatch):
    """C2 (аудит 2026-07): enforced-режим — оператор БЕЗ гранта не получает метрики чужого
    аккаунта в плановом дайджесте (раньше рассылка игнорировала пер-юзер гранты — утечка)."""
    import core.access as acc
    from core.access import grant_account_access, revoke_account_access
    from core.config import settings as cfg
    from db.session import init_db
    from scheduler import jobs

    A, B = "1112223334", "2223334445"
    monkeypatch.setattr(jobs, "_scheduled_accounts", lambda: [A, B])
    monkeypatch.setattr(jobs, "build_client_async", _fake_client_async)

    async def fake_report(_client, acct, _period, **k):
        return f"R:{acct}"

    monkeypatch.setattr(jobs, "build_account_report_async", fake_report)
    monkeypatch.setattr(jobs, "summary_text", lambda r, lang=None: r)
    monkeypatch.setattr(jobs, "_recipients", _arecipients({3}))
    monkeypatch.setattr(cfg, "account_access_mode", "enforced")  # поверх legacy-фикстуры модуля
    acc._invalidate_enforcement_cache()

    await init_db()
    await grant_account_access(3, A)  # грант ТОЛЬКО на A
    sent: list[tuple[int, str]] = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text))

    try:
        await jobs.run_scheduled_report(FakeBot())
    finally:
        await revoke_account_access(3, A)
        acc._invalidate_enforcement_cache()
    assert len(sent) == 1
    assert f"R:{A}" in sent[0][1]  # свой аккаунт — в дайджесте
    assert f"R:{B}" not in sent[0][1]  # чужой (без гранта) — отфильтрован


# ── 3.3 (замечание 7): дайджест — дельты CPA/ROAS, «что горит», тихий режим, кнопка ─
def _mm(cost: float, conv: float, value: float = 0.0, clicks: int = 100):
    """Полные Metrics для отчётов дайджеста (в отличие от _m — там только cost/conversions)."""
    from reports.queries import Metrics

    return Metrics(
        impressions=clicks * 10,
        clicks=clicks,
        cost_micros=int(cost * 1_000_000),
        conversions=conv,
        conv_value=value,
    )


def _rep(acct: str, m, prev=None, currency: str = "USD"):
    """Минимальный отчёт под run_scheduled_report: totals/prev_totals/период/валюта/брейкдаун."""
    from reports.queries import Breakdown

    camp = Breakdown("campaign", "Кампании", ["Кампания", "Статус"], [(("Поиск", "ENABLED"), m)])
    return SimpleNamespace(
        customer_id=acct,
        totals=m,
        prev_totals=prev,
        period=SimpleNamespace(date_from="2026-07-09", date_to="2026-07-15"),
        currency=currency,
        breakdowns=[camp],
    )


class _SentBot:
    def __init__(self):
        self.sent: list[tuple[int, str, object]] = []

    async def send_message(self, chat_id, text="", **kw):
        self.sent.append((chat_id, text, kw.get("reply_markup")))


def _wire_sched(monkeypatch, jobs, reports: dict, recipients: set[int]):
    """Общая обвязка run_scheduled_report: фейк-клиент, отчёты по словарю, стаб summary_text."""
    monkeypatch.setattr(jobs, "_scheduled_accounts", lambda: list(reports))
    monkeypatch.setattr(jobs, "build_client_async", _fake_client_async)

    async def fake_report(_client, acct, _period, **k):
        return reports[acct]

    monkeypatch.setattr(jobs, "build_account_report_async", fake_report)
    monkeypatch.setattr(jobs, "summary_text", lambda r, lang=None: f"R:{r.customer_id}")
    monkeypatch.setattr(jobs, "_recipients", _arecipients(recipients))

    async def no_status_heartbeats(_client, _acct):
        return None, None

    monkeypatch.setattr(jobs, "_account_status_heartbeats", no_status_heartbeats)


def test_digest_delta_line_unit():
    """3.3: дельту CPA/ROAS считает КОД (golden rule #4); нет сравнения/конверсий → '' (не мусор),
    строковый фейк-отчёт соседних тестов переживается молча."""
    from scheduler.jobs import _report_delta_line

    t, p = _mm(500, 5, value=1000.0), _mm(400, 4, value=600.0)
    line = _report_delta_line(SimpleNamespace(totals=t, prev_totals=p), "USD", "ru")
    assert "CPA 100.00 → 100.00 USD" in line
    assert "ROAS 1.50 → 2.00" in line
    assert "к пред. периоду" in line
    assert _report_delta_line(SimpleNamespace(totals=t, prev_totals=None), "USD", "ru") == ""
    z = _mm(500, 0)
    assert _report_delta_line(SimpleNamespace(totals=z, prev_totals=z), "USD", "ru") == ""
    assert _report_delta_line("R:строка-не-отчёт", "USD", "ru") == ""


async def test_digest_delta_and_findings_in_body(monkeypatch):
    """3.3: в теле дайджеста — строка дельты CPA/ROAS (prev_totals уже в отчёте) и топ-находки
    аудита из уже посчитанного _account_health (0 доп. чтений Google Ads)."""
    from scheduler import jobs

    A, B = "1112223334", "2223334445"
    reports = {
        A: _rep(A, _mm(500, 5, value=1000.0), prev=_mm(400, 4, value=600.0)),
        B: _rep(B, _mm(500, 0)),  # расход без конверсий → у аудита есть что сказать
    }
    _wire_sched(monkeypatch, jobs, reports, {91001})

    bot = _SentBot()
    await jobs.run_scheduled_report(bot)
    assert len(bot.sent) == 1
    body = bot.sent[0][1]
    # валюта в дайджесте дочитывается живым account_currency (здесь фейк-клиент → без кода)
    assert "CPA 100.00 → 100.00" in body and "ROAS 1.50 → 2.00" in body
    assert "Главное из аудита:" in body  # топ-находки под цифрами
    assert "Что горит" not in body  # +25% расхода ниже дефолтного порога — не аномалия


async def test_digest_hot_anomalies_and_dedup(monkeypatch):
    """3.3: «🔥 Что горит» по detect_anomalies прямо в дайджесте; повтор той же аномалии НЕ шлётся —
    дедуп через тот же блоб _ANOMALY_SEEN_KEY, что у 6-часовой run_anomaly_check."""
    from sqlalchemy import delete

    from db.models import UserSettings
    from db.session import Session, init_db
    from scheduler import jobs

    await init_db()
    chat = 91003
    async with Session() as s:  # временный SQLite переживает прогоны → чистим блоб дедупа
        await s.execute(delete(UserSettings).where(UserSettings.chat_id == chat))
        await s.commit()

    A = "1112223334"
    reports = {A: _rep(A, _mm(300, 3), prev=_mm(100, 3))}  # +200% расход > дефолтных 50%
    _wire_sched(monkeypatch, jobs, reports, {chat})

    bot1 = _SentBot()
    await jobs.run_scheduled_report(bot1)
    assert "Что горит" in bot1.sent[0][1]

    bot2 = _SentBot()
    await jobs.run_scheduled_report(bot2)
    body2 = bot2.sent[0][1]
    assert "Что горит" not in body2  # аномалия уже доставлена — второй раз не шумим
    assert f"R:{A}" in body2  # сам блок цифр никуда не делся


async def test_digest_quiet_accounts_collapse(monkeypatch):
    """3.3 (решение владельца 2026-07-17): аккаунты без расходов/кликов/аномалий/наших мутаций и
    находок схлопываются в строку «Без событий: N» — их блоки цифр в дайджест не попадают."""
    from scheduler import jobs

    A, B = "8881112220", "8882223330"
    reports = {A: _rep(A, _mm(100, 1)), B: _rep(B, _mm(0, 0, clicks=0))}
    _wire_sched(monkeypatch, jobs, reports, {91004})
    monkeypatch.setattr(
        jobs, "_account_health", lambda r, **_kwargs: None
    )  # детерминизм: аудит молчит

    async def _no_applied(_days):
        return {}

    monkeypatch.setattr("confirm.store.audit_applied_by_account_since", _no_applied)

    bot = _SentBot()
    await jobs.run_scheduled_report(bot)
    assert len(bot.sent) == 1
    body = bot.sent[0][1]
    assert f"R:{A}" in body
    assert f"R:{B}" not in body  # тихий аккаунт — без блока цифр
    assert "Без событий: 1" in body


async def test_digest_applied_line_unquiets_and_counts_only_visible(monkeypatch):
    """3.3: применённые нами мутации будят «тихий» аккаунт и дают строку «применено: N»;
    C2 — счётчик суммируется ТОЛЬКО по видимым в дайджесте аккаунтам (чужие 100 не текут)."""
    from scheduler import jobs

    A, B = "8881112220", "8882223330"
    reports = {A: _rep(A, _mm(100, 1)), B: _rep(B, _mm(0, 0, clicks=0))}
    _wire_sched(monkeypatch, jobs, reports, {91005})
    monkeypatch.setattr(jobs, "_account_health", lambda r, **_kwargs: None)

    async def _applied(_days):
        return {B: 2, "9998887776": 100}  # 100 — аккаунт вне дайджеста, утечь не должен

    monkeypatch.setattr("confirm.store.audit_applied_by_account_since", _applied)

    bot = _SentBot()
    await jobs.run_scheduled_report(bot)
    body = bot.sent[0][1]
    assert f"R:{B}" in body  # мутации были → аккаунт НЕ тихий
    assert "применено изменений: 2" in body
    assert "102" not in body  # C2: чужой счёт не суммирован


async def test_digest_auction_stale_nudge(monkeypatch):
    """3.4: срез /competitors старше 30 дн. → нудж «обнови» в блоке аккаунта. Свежий срез и
    отсутствие импортов — тишины не нарушаем (кто фичей не пользуется, того не спамим);
    тихий аккаунт нудж НЕ будит — это не событие."""
    from datetime import date, timedelta

    from scheduler import jobs

    A, B = "5551112223", "5552223334"
    reports = {A: _rep(A, _mm(100, 1)), B: _rep(B, _mm(0, 0, clicks=0))}
    _wire_sched(monkeypatch, jobs, reports, {91011})
    monkeypatch.setattr(jobs, "_account_health", lambda r, **_kwargs: None)

    async def _no_applied(_days):
        return {}

    monkeypatch.setattr("confirm.store.audit_applied_by_account_since", _no_applied)

    stale = (date.today() - timedelta(days=45)).isoformat()

    async def _stale_snap(cid, **k):
        return SimpleNamespace(snapshot_date=stale)

    monkeypatch.setattr("db.competitors.latest_snapshot", _stale_snap)
    bot = _SentBot()
    await jobs.run_scheduled_report(bot)
    body = bot.sent[0][1]
    assert "Срезу аукционов уже 45 дн." in body and "/competitors" in body
    assert body.count("Срезу аукционов") == 1  # тихий B нудж не разбудил (его блока нет)
    assert "Без событий: 1" in body

    fresh = (date.today() - timedelta(days=5)).isoformat()

    async def _fresh_snap(cid, **k):
        return SimpleNamespace(snapshot_date=fresh)

    monkeypatch.setattr("db.competitors.latest_snapshot", _fresh_snap)
    bot2 = _SentBot()
    await jobs.run_scheduled_report(bot2)
    assert "Срезу аукционов" not in bot2.sent[0][1]

    async def _no_snap(cid, **k):
        return None

    monkeypatch.setattr("db.competitors.latest_snapshot", _no_snap)
    bot3 = _SentBot()
    await jobs.run_scheduled_report(bot3)
    assert "Срезу аукционов" not in bot3.sent[0][1]


async def test_digest_all_quiet_single_short_message(monkeypatch):
    """3.3: тишина везде — одно короткое сообщение вместо простыни блоков."""
    from scheduler import jobs

    B = "8882223330"
    reports = {B: _rep(B, _mm(0, 0, clicks=0))}
    _wire_sched(monkeypatch, jobs, reports, {91006})
    monkeypatch.setattr(jobs, "_account_health", lambda r, **_kwargs: None)

    async def _no_applied(_days):
        return {}

    monkeypatch.setattr("confirm.store.audit_applied_by_account_since", _no_applied)

    bot = _SentBot()
    await jobs.run_scheduled_report(bot)
    assert len(bot.sent) == 1
    body = bot.sent[0][1]
    assert "Тишина" in body
    assert f"R:{B}" not in body and "———" not in body


async def test_digest_blocks_sorted_by_at_risk(monkeypatch):
    """3.3: блоки отсортированы по деньгам-под-риском, не по порядку обхода и не по расходу:
    A тратит больше (1000 > 200), но под риском только B — B выше."""
    from scheduler import jobs

    A, B = "1112223334", "2223334445"
    reports = {A: _rep(A, _mm(1000, 5)), B: _rep(B, _mm(200, 0))}
    _wire_sched(monkeypatch, jobs, reports, {91007})

    def fake_health(report, **_kwargs):
        return SimpleNamespace(
            at_risk=200.0 if report.customer_id == B else 0.0, findings=[], currency="USD"
        )

    monkeypatch.setattr(jobs, "_account_health", fake_health)

    bot = _SentBot()
    await jobs.run_scheduled_report(bot)
    body = bot.sent[0][1]
    assert body.index(f"R:{B}") < body.index(f"R:{A}")


async def test_digest_action_button_and_proactive_antidup(monkeypatch):
    """3.3: одна actionable-секция — топ-совет с ИСПОЛНИМОЙ неденежной операцией (двойной гейт
    /advise: allow-list + params); кнопка лишь СТАРТУЕТ confirm-гейт по тапу — из scheduler
    proposal не создаётся. Операторам с advise_proactive кнопку не кладём (анти-дубль:
    им карточки шлёт run_recommendations_digest). Персистнется ТОЛЬКО показанная рекомендация."""
    from sqlalchemy import delete

    def advise_feedback_kb(*_args, **_kwargs):
        return object()

    from db.models import UserSettings
    from db.session import Session, init_db
    from scheduler import delivery, jobs

    # Кнопку планировщик берёт у порта (развязка C4), заполняет его bot-процесс. Без заполнения
    # дайджест уходит ТЕКСТОМ — это штатная деградация, а не сбой, но тест здесь как раз про кнопку.
    monkeypatch.setitem(delivery._BUILDERS, delivery.ADVISE_FEEDBACK, advise_feedback_kb)

    await init_db()
    chat_btn, chat_pro = 91009, 91010
    async with Session() as s:
        await s.execute(delete(UserSettings).where(UserSettings.chat_id.in_([chat_btn, chat_pro])))
        s.add(UserSettings(chat_id=chat_pro, ui_prefs={"advise_proactive": "on"}))
        await s.commit()

    A = "1112223334"
    reports = {A: _rep(A, _mm(100, 1))}
    _wire_sched(monkeypatch, jobs, reports, {chat_btn, chat_pro})

    rec = SimpleNamespace(
        suggested_operation="pause_campaign",  # в _ADVISE_APPLY_OPS, params собираются
        target_campaign="Поиск",
        evidence={},
        at_risk=100.0,
        severity="warn",
        priority=1.0,
        kind="spend_no_conv",
        body="Кампания «Поиск»: расход без конверсий — поставить на паузу",
        rec_uid="RUTEST331",
    )

    async def fake_build(_chat, _acct, **_kw):
        return SimpleNamespace(recs=[rec])

    recorded: list[tuple[int, str]] = []

    async def fake_record(chat_id, acct, recs, source=""):
        recorded.append((chat_id, acct))
        return recs

    monkeypatch.setattr("advisor.service.build_recommendations", fake_build)
    monkeypatch.setattr("advisor.store.record_recommendations", fake_record)

    bot = _SentBot()
    await jobs.run_scheduled_report(bot)
    by_chat = {cid: (text, markup) for cid, text, markup in bot.sent}
    text_btn, markup_btn = by_chat[chat_btn]
    assert "Можно применить" in text_btn and markup_btn is not None
    assert rec.body[:40] in text_btn  # текст совета в секции
    text_pro, markup_pro = by_chat[chat_pro]
    assert "Можно применить" not in text_pro and markup_pro is None
    assert recorded == [(chat_btn, A)]  # только показанная, только кнопочному чату


async def test_audit_applied_by_account_since_counts_fresh_applied_only():
    """3.3: audit_applied_by_account_since — только status='applied' и только внутри окна
    (rejected и applied 3-дневной давности не считаются); GROUP BY per customer_id."""
    from datetime import timedelta, timezone

    from sqlalchemy import delete

    from confirm.store import audit_applied_by_account_since
    from db.models import AuditLog
    from db.session import Session, init_db

    await init_db()
    X = "9911223344"
    now = datetime.now(timezone.utc)
    async with Session() as s:
        await s.execute(delete(AuditLog).where(AuditLog.customer_id == X))  # идемпотентность
        for status, dt in (
            ("applied", now),
            ("applied", now),
            ("rejected", now),
            ("applied", now - timedelta(days=3)),
        ):
            s.add(
                AuditLog(
                    confirmation_id=uuid.uuid4().hex,
                    operation="update_status",
                    customer_id=X,
                    chat_id=1,
                    status=status,
                    created_at=dt,
                )
            )
        await s.commit()

    res = await audit_applied_by_account_since(1)
    assert res.get(X) == 2


# ── КОД-ГАРД (golden rule #3): планировщик НЕ может менять аккаунт ────────────────
def test_scheduler_never_imports_mutations():
    """Структурный гард по AST (не по тексту — докстринги/комментарии не считаются): scheduler
    не импортирует слой мутаций/исполнения и не вызывает apply_*/mutate_/execute_confirmed.
    Если нельзя импортировать — нельзя и вызвать → аккаунт не изменить из планировщика."""
    import ast

    pkg = pathlib.Path(__file__).resolve().parents[1] / "scheduler"
    files = list(pkg.glob("*.py"))
    assert files, "не найдены файлы scheduler/*.py"
    forbidden_modules = {"ads.mutations", "ads.service"}  # слои записи/исполнения
    for py in files:
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=py.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert mod not in forbidden_modules, f"{py.name}: import из {mod}"
                if mod == "ads":
                    for a in node.names:
                        assert a.name not in {
                            "mutations",
                            "service",
                        }, f"{py.name}: from ads import {a.name}"
            elif isinstance(node, ast.Import):
                for a in node.names:
                    assert a.name not in forbidden_modules, f"{py.name}: import {a.name}"
            elif isinstance(node, ast.Call):  # вызовы в КОДЕ (AST игнорирует докстринги)
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                assert name != "execute_confirmed", f"{py.name}: вызов execute_confirmed"
                assert not name.startswith(("apply_", "mutate_")), f"{py.name}: вызов {name}"


# ── КОД-ГАРД (квота): здоровье в дайджестах — ТОЛЬКО engine-only ──────────────────
def test_scheduler_audit_is_engine_only():
    """Планировщик ходит ВЕЕРОМ по всем аккаунтам и на КАЖДОМ прогоне. Полный сбор аудита
    (audit.collect.gather_audit) — 23 параллельных чтения Google Ads на аккаунт: N аккаунтов × 4
    прогона в сутки съедят квоту, ради строки, которую никто не просил. Здоровье в дайджесте строится
    ТОЛЬКО по уже собранному отчёту (build_audit — чистая функция, 0 доп. чтений). Гард структурный:
    нельзя импортировать — нельзя случайно позвать (правило «дорогое чтение — только по прямой
    просьбе человека»: /audit, /export, досье)."""
    import ast

    pkg = pathlib.Path(__file__).resolve().parents[1] / "scheduler"
    for py in pkg.glob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=py.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "") != "audit.collect", f"{py.name}: import из audit.collect"
                if (node.module or "") == "audit":
                    for a in node.names:
                        assert a.name != "collect", f"{py.name}: from audit import collect"
            elif isinstance(node, ast.Import):
                for a in node.names:
                    assert a.name != "audit.collect", f"{py.name}: import audit.collect"
            elif isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                assert name != "gather_audit", f"{py.name}: вызов gather_audit (23 чтения/аккаунт)"
                # Снимок тренда пишет ТОЛЬКО /audit (полный ctx). Upsert из дайджеста (engine-only,
                # тот же score_model_version, тот же ключ) молча затёр бы честную базу — и клиент
                # увидел бы «▲ +15 за неделю», которого не было.
                assert name != "record_snapshot", (
                    f"{py.name}: вызов record_snapshot (отравит тренд)"
                )


# ── 1.3: еженедельный дайджест — текст + файл админам, no-op без админов ───────────
class _DigestBot:
    def __init__(self):
        self.messages: list = []
        self.documents: list = []

    async def send_message(self, chat_id, text="", **kw):
        self.messages.append((chat_id, text))

    async def send_document(self, chat_id, document, **kw):
        self.documents.append((chat_id, getattr(document, "filename", "")))


async def _seed_digest_rows():
    from datetime import timezone

    from db.models import AuditLog, ErrorEvent
    from db.session import Session

    from core import bugs

    now = datetime.now(timezone.utc)
    async with Session() as s:
        s.add(
            ErrorEvent(
                request_id="rid1",
                chat_id=1,
                where="handler",
                exc_type="ValueError",
                message="boom",
                traceback="tb",
                created_at=now,
            )
        )
        s.add(
            AuditLog(
                confirmation_id="c1",
                operation="create_search_campaign",
                customer_id=DRAFT_ACCOUNT_ID,
                chat_id=1,
                status="applied",
                created_at=now,
            )
        )
        await s.commit()
    await bugs.add_bug_report(1, "кнопка не работает", username="op")


async def test_weekly_digest_sends_text_and_file_to_admins(monkeypatch):
    """1.3: дайджест шлёт админу текст + прикреплённый файл; активность/ошибки/баг-репорты собраны."""
    from core.config import settings
    from db.session import init_db
    from scheduler import jobs

    await init_db()
    await _seed_digest_rows()
    monkeypatch.setattr(settings, "admin_chat_ids", "5", raising=False)
    bot = _DigestBot()
    served = await jobs.run_weekly_digest(bot)
    assert served == 1
    assert bot.messages and bot.messages[0][0] == 5  # текст админу
    assert bot.documents and bot.documents[0][0] == 5  # файл админу
    assert bot.documents[0][1] == "weekly_digest.txt"
    body = bot.messages[0][1]
    assert "🗓" in body  # заголовок дайджеста


async def test_weekly_digest_no_admins_is_noop(monkeypatch):
    from core.config import settings
    from db.session import init_db
    from scheduler import jobs

    await init_db()
    monkeypatch.setattr(settings, "admin_chat_ids", "", raising=False)
    bot = _DigestBot()
    assert await jobs.run_weekly_digest(bot) == 0
    assert not bot.messages and not bot.documents
