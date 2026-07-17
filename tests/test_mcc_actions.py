"""3.5: действия под сводкой /mcc — bot-слой (обогащение скорами + кнопки + фоновый прогон).

Инварианты (безопасность/честность, не красота):
• _augment_mcc_health — best-effort: сбой БД НЕ роняет сводку; stale только при чужой
  score_model_version (сверка с ЖИВЫМ audit.engine.SCORE_MODEL_VERSION, не с копией);
• _mcc_audit_all — READ-ONLY score-прогон: замок чтения перепроверяется per-account в момент
  прогона (TOCTOU), сбой одного аккаунта не валит остальные, снапшот пишется той же семантикой,
  что /audit (день аккаунта, окно периода);
• on_mcc_audit_all — один прогон на чат (двойной тап → алерт), потерянный кэш (рестарт) → stale;
• on_mcc_account — тап закрепляет аккаунт ТОЛЬКО через fail-closed замок: read-замок × пер-юзер
  грант; отказ ⇒ выбор НЕ сохраняется. Мутационный замок не затрагивается вовсе.
"""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import bot.handlers.reports as reports  # noqa: E402
import bot.main as bm  # noqa: E402

CID_A = "9999000333"
CID_B = "9999000444"


class _Msg:
    """Утиный Message: answer() возвращает дочерний _Msg (прогресс правится edit_text)."""

    def __init__(self, chat_id: int):
        self.chat = SimpleNamespace(id=chat_id)
        self.bot = None
        self.answers: list[tuple[str, dict]] = []
        self.edits: list[str] = []
        self.children: list[_Msg] = []

    async def answer(self, text, **kw):
        self.answers.append((text, kw))
        child = _Msg(self.chat.id)
        self.children.append(child)
        return child

    async def edit_text(self, text, **kw):
        self.edits.append(text)


class _Cq:
    def __init__(self, msg: _Msg | None, chat_id: int):
        self.message = msg
        self.from_user = SimpleNamespace(id=chat_id)
        self.answer_calls: list[tuple[tuple, dict]] = []

    async def answer(self, *a, **kw):
        self.answer_calls.append((a, kw))


def _child_report(cid: str):
    """Утиный ChildReport с дефолтами health-полей (как в reports.mcc.ChildReport)."""
    return SimpleNamespace(
        account=SimpleNamespace(id=cid),
        health_score=None,
        health_grade="",
        health_at_risk=None,
        health_date="",
        health_stale=False,
    )


# ── _augment_mcc_health: скоры из кэша, stale по живой версии, сбой БД не роняет ────────
async def test_augment_mcc_health_marks_scores_and_stale(monkeypatch):
    from audit.engine import SCORE_MODEL_VERSION

    rows = {
        CID_A: SimpleNamespace(
            score=72,
            grade="C",
            at_risk=40.0,
            snapshot_date="2026-07-15",
            score_model_version=SCORE_MODEL_VERSION,  # текущая эпоха → НЕ stale
        ),
        CID_B: SimpleNamespace(
            score=88,
            grade="A",
            at_risk=0.0,
            snapshot_date="2026-06-01",
            score_model_version="e1:dead",  # старая эпоха → stale
        ),
    }

    async def fake_latest(cids, *, period_days=30):
        assert set(cids) == {CID_A, CID_B, "0000000001"}
        return rows

    monkeypatch.setattr("audit.snapshot.latest_snapshots", fake_latest)
    summary = SimpleNamespace(
        children=[_child_report(CID_A), _child_report(CID_B), _child_report("0000000001")]
    )
    await bm._augment_mcc_health(summary)

    a, b, c = summary.children
    assert (a.health_score, a.health_grade, a.health_at_risk) == (72, "C", 40.0)
    assert a.health_stale is False
    assert b.health_stale is True and b.health_score == 88
    assert c.health_score is None  # снапшота нет — честно «аудит не прогонялся»


async def test_augment_mcc_health_survives_db_failure(monkeypatch):
    async def boom(cids, *, period_days=30):
        raise RuntimeError("db down")

    monkeypatch.setattr("audit.snapshot.latest_snapshots", boom)
    summary = SimpleNamespace(children=[_child_report(CID_A)])
    await bm._augment_mcc_health(summary)  # не бросает
    assert summary.children[0].health_score is None


# ── _mcc_audit_all: снапшоты пишутся, сбой одного аккаунта не валит прогон ──────────────
async def test_mcc_audit_all_records_snapshots_and_survives_failures(monkeypatch):
    chat = 7101
    read_checked: list[str] = []
    recorded: list[tuple[str, int, str, int]] = []

    monkeypatch.setattr(bm, "ensure_read_allowed", lambda cid: read_checked.append(cid))

    async def fake_client(cid):
        return object()

    async def fake_period(client, cid, default, *, label=""):
        return SimpleNamespace(days=30)

    async def fake_gather(client, cid, period, *, target_cpa=None):
        if cid == CID_B:
            raise RuntimeError("SDK boom")  # сбой одного аккаунта
        return SimpleNamespace(has_activity=True, score=71, customer_id=cid)

    async def fake_record(result, *, snapshot_date, period_days):
        recorded.append((result.customer_id, result.score, snapshot_date, period_days))
        return True

    async def fake_local_date(client, cid):
        return "2026-07-17"

    async def fake_target_cpa(chat_id, cid):
        return None

    monkeypatch.setattr("ads.client.build_client_async", fake_client)
    monkeypatch.setattr("reports.tz.account_period", fake_period)
    monkeypatch.setattr("audit.collect.gather_audit", fake_gather)
    monkeypatch.setattr("audit.render.score_affecting_gaps", lambda result: [])
    monkeypatch.setattr("audit.snapshot.record_snapshot", fake_record)
    monkeypatch.setattr(bm, "_account_local_date", fake_local_date)
    monkeypatch.setattr(bm, "_load_target_cpa", fake_target_cpa)

    m = _Msg(chat)
    await bm._mcc_audit_all(m, chat, [CID_A, CID_B])

    assert read_checked == [CID_A, CID_B]  # TOCTOU: замок на момент прогона, per-account
    assert recorded == [(CID_A, 71, "2026-07-17", 30)]  # сбойный B не записан
    progress = m.children[0]  # первое answer — прогресс-сообщение
    assert progress.edits, "прогресс должен редактироваться"
    final = progress.edits[-1]
    assert "1/2" in final  # ok/total
    assert ("сбоев: 1" in final) or ("failed: 1" in final)


async def test_mcc_audit_all_caps_account_list(monkeypatch):
    """Кап _MCC_AUDIT_MAX: один тап не съедает квоту — прогоняется только worst-first голова."""
    chat = 7102
    seen: list[str] = []

    monkeypatch.setattr(bm, "ensure_read_allowed", lambda cid: None)

    async def fake_client(cid):
        seen.append(cid)
        raise RuntimeError("stop early")  # дальше идти не нужно — считаем только охват

    monkeypatch.setattr("ads.client.build_client_async", fake_client)
    cids = [f"90000000{i:02d}" for i in range(bm._MCC_AUDIT_MAX + 10)]
    await bm._mcc_audit_all(_Msg(chat), chat, cids)
    assert len(seen) == bm._MCC_AUDIT_MAX


# ── on_mcc_audit_all: stale-кэш, повторный тап, RUNNING-гард держится и снимается ───────
async def test_on_mcc_audit_all_guard_and_stale(monkeypatch):
    chat = 7103
    calls: list[list[str]] = []

    async def fake_run(m, chat_id, cids):
        assert chat_id in bm._MCC_AUDIT_RUNNING  # гард держится ВО ВРЕМЯ прогона
        calls.append(cids)

    monkeypatch.setattr(bm, "_mcc_audit_all", fake_run)

    # 1) кэш пуст (рестарт) → stale-алерт, прогона нет
    bm._MCC_AUDIT_CACHE.pop(chat, None)
    cq = _Cq(_Msg(chat), chat)
    await reports.on_mcc_audit_all(cq, bm.MccAuditCB(action="run"))
    assert calls == []
    assert cq.answer_calls and cq.answer_calls[0][1].get("show_alert") is True

    # 2) прогон уже идёт → алерт «уже идёт», второй не стартует
    bm._MCC_AUDIT_CACHE[chat] = [CID_A]
    bm._MCC_AUDIT_RUNNING.add(chat)
    try:
        cq2 = _Cq(_Msg(chat), chat)
        await reports.on_mcc_audit_all(cq2, bm.MccAuditCB(action="run"))
        assert calls == []
        assert cq2.answer_calls[0][1].get("show_alert") is True
    finally:
        bm._MCC_AUDIT_RUNNING.discard(chat)

    # 3) happy: прогон запущен списком из кэша, RUNNING снят после
    cq3 = _Cq(_Msg(chat), chat)
    await reports.on_mcc_audit_all(cq3, bm.MccAuditCB(action="run"))
    assert calls == [[CID_A]]
    assert chat not in bm._MCC_AUDIT_RUNNING
    bm._MCC_AUDIT_CACHE.pop(chat, None)


# ── on_mcc_account: закрепление аккаунта ТОЛЬКО через fail-closed замок ─────────────────
async def test_on_mcc_account_sets_active_fail_closed(monkeypatch):
    chat = 7104
    saved: list[tuple[int, str]] = []

    async def fake_save(chat_id, acct):
        saved.append((chat_id, acct))

    async def grant_ok(chat_id, acct):
        return None

    monkeypatch.setattr(bm, "_save_selected_account", fake_save)
    monkeypatch.setattr(bm, "ensure_read_allowed", lambda cid: None)
    monkeypatch.setattr("core.access.ensure_account_allowed_for_user", grant_ok)

    msg = _Msg(chat)
    await reports.on_mcc_account(_Cq(msg, chat), bm.MccAcctCB(cid=CID_A))
    assert saved == [(chat, CID_A)]
    assert msg.answers, "пользователь должен получить подтверждение выбора"

    # отказ пер-юзер гранта (fail-closed) → выбор НЕ сохранён, отказ показан
    saved.clear()

    async def grant_deny(chat_id, acct):
        raise PermissionError("no grant")

    monkeypatch.setattr("core.access.ensure_account_allowed_for_user", grant_deny)
    msg2 = _Msg(chat)
    await reports.on_mcc_account(_Cq(msg2, chat), bm.MccAcctCB(cid=CID_A))
    assert saved == []
    assert msg2.answers, "отказ должен быть виден пользователю"

    # отказ read-замка (аккаунт вне видимости) → тоже не сохранён
    def read_deny(cid):
        raise PermissionError("outside read allow-list")

    monkeypatch.setattr(bm, "ensure_read_allowed", read_deny)
    monkeypatch.setattr("core.access.ensure_account_allowed_for_user", grant_ok)
    msg3 = _Msg(chat)
    await reports.on_mcc_account(_Cq(msg3, chat), bm.MccAcctCB(cid=CID_A))
    assert saved == []
