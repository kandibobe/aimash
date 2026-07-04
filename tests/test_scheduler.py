"""Тесты планировщика (Фаза 3, ТЗ §14): аномалии (чистая логика), очистка просроченных
черновиков (на временном SQLite), и КОД-ГАРД golden rule #3 — планировщик не меняет аккаунт.
"""

from __future__ import annotations

import pathlib
import uuid
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import select

from ads.client import DRAFT_ACCOUNT_ID
from scheduler.anomaly import detect_anomalies


def _m(cost: float, conv: float):
    return SimpleNamespace(cost=cost, conversions=conv)


async def _fake_client_async(*a, **k):
    # scheduler зовёт await build_client_async(acct) (холодная сборка вне loop) — фейк-заглушка.
    return None


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
    monkeypatch.setattr(jobs, "_recipients", lambda: {1, 2})
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
    monkeypatch.setattr(jobs, "_recipients", lambda: {1})

    sent: list[tuple[int, str]] = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text))

    await jobs.run_scheduled_report(FakeBot())
    assert len(sent) == 1  # ОДНО сообщение оператору, не два
    assert f"R:{A}" in sent[0][1] and f"R:{B}" in sent[0][1]  # оба аккаунта в дайджесте


async def test_anomaly_multi_account_one_message(monkeypatch):
    """run_anomaly_check собирает аномалии по нескольким аккаунтам в ОДНО сообщение на оператора."""
    from scheduler import jobs

    A, B = "1112223334", "2223334445"
    monkeypatch.setattr(jobs, "_scheduled_accounts", lambda: [A, B])
    monkeypatch.setattr(jobs, "build_client_async", _fake_client_async)
    # Оба аккаунта со всплеском расхода (+100%): curA,prevA, curB,prevB.
    seq = iter([_m(200, 5), _m(100, 5), _m(300, 5), _m(150, 5)])
    monkeypatch.setattr(jobs, "fetch_totals", lambda *a, **k: next(seq))
    monkeypatch.setattr(jobs, "_recipients", lambda: {2})  # дефолтный порог → алерт

    sent: list[tuple[int, str]] = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text))

    await jobs.run_anomaly_check(FakeBot())
    assert len(sent) == 1  # одно сообщение оператору
    assert f"Аккаунт {A}" in sent[0][1] and f"Аккаунт {B}" in sent[0][1]


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
