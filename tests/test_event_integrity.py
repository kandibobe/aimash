"""Волна 3 (event sourcing): событие денежного пути + неизменяемость журнала.

Три независимых свойства, и ни одно не заменяет другие:

1. **Событие есть у КАЖДОЙ мутации, и оно пишется fail-closed ДО вызова SDK.** Точка эмиссии одна —
   `ads.mutations._require_confirmation`, тот же структурный чокпойнт, что несёт confirm-гейт и
   freshness (инвариант `test_all_apply_functions_call_require_confirmation`): новая мутация обязана
   через него пройти, значит забыть событие негде.
2. **СУБД не даёт переписать событие и не даёт удалить денежное, пока оно моложе пола хранения.**
   Проверяется живьём на SQLite (триггеры ставит `db.session.init_db` из того же
   `db.models.event_immutability_ddl`, что накатывает Alembic на Postgres) — а не «только на проде».
3. **Хэш-цепочка делает вырезанное звено видимым.** Триггер запрещает, цепочка ПОКАЗЫВАЕТ — включая
   случай, когда правивший имел права на таблицу и триггер снял. Отрицательный контроль ниже удаляет
   разрешённое (не-денежное) событие из середины прогона и требует, чтобы `verify_chain` покраснел.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import delete, select, text, update

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ads.mutations as mut  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from conftest import FakeConfirmStore, FakeProposal  # noqa: E402
from core import observe  # noqa: E402
from db.models import AgentRunEvent  # noqa: E402
from db.session import Session, engine  # noqa: E402
from test_write_layer import _FakeClient, allowed_ids, patched  # noqa: E402


async def _events(run_id: str) -> list[AgentRunEvent]:
    async with Session() as s:
        return list(
            (
                await s.execute(
                    select(AgentRunEvent)
                    .where(AgentRunEvent.run_id == run_id)
                    .order_by(AgentRunEvent.seq)
                )
            )
            .scalars()
            .all()
        )


async def _write(run_id: str, events: list[dict]) -> None:
    """Записать события прогона подряд, продолжая уже лежащую цепочку (как это делает код)."""
    async with Session() as s:
        next_seq, prev = await observe._chain_tail(s, run_id)
        for row in observe.chain_events(run_id, events, start_seq=next_seq, prev=prev):
            s.add(AgentRunEvent(run_id=run_id, **row))
        await s.commit()


def _ev(kind: str = "tool", **kw) -> dict:
    base = {
        "kind": kind,
        "tool_name": None,
        "latency_ms": None,
        "cost_usd": 0.0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "rows_returned": None,
        "args_redacted": None,
        "result_digest": None,
        "ok": True,
    }
    base.update(kw)
    return base


# ── 1. Событие денежного пути ────────────────────────────────────────────────────────────────
async def test_mutation_emits_money_event_through_the_chokepoint():
    """Настоящий `apply_*` (не `_require_confirmation` напрямую) — доказываем, что событие лежит на
    ДЕНЕЖНОМ пути, а не в отдельно вызванной функции."""
    from core import context

    store = FakeConfirmStore(FakeProposal("pause_campaign", "confirmed", user_initiated=True))
    run_id = f"chk{uuid.uuid4().hex[:8]}"
    tok = context.set_context(request_id=run_id)
    try:
        with (
            patched(mut, "_set_campaign_status_via_sdk", lambda *a, **k: {"applied": True}),
            allowed_ids(DRAFT_ACCOUNT_ID),
        ):
            await mut.apply_pause_campaign(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="23",
                confirmation_id="ok",
                confirm_store=store,
                ads_client=_FakeClient(),
            )
    finally:
        context.reset_context(tok)
    evs = await _events(run_id)
    assert len(evs) == 1
    assert evs[0].kind == "ads_mutate" and evs[0].tool_name == "pause_campaign"
    assert evs[0].payload_digest  # подписано: строка в цепочке, а не вне её


async def test_money_event_failure_blocks_mutation_before_claim(monkeypatch: pytest.MonkeyPatch):
    """Fail-closed И порядок: журнал не записался ⇒ SDK не тронут И одноразовый claim не съеден.

    Второе не менее важно первого. Пиши мы событие ПОСЛЕ claim, отказ журнала оставлял бы человека
    и без мутации, и без карточки — подтверждение сгорело бы на служебном сбое."""
    import db.session as dbs

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(dbs, "Session", boom)
    store = FakeConfirmStore(FakeProposal("pause_campaign", "confirmed", user_initiated=True))
    sdk_called = {"n": 0}

    def fake_sdk(*a, **k):
        sdk_called["n"] += 1
        return {"applied": True}

    with (
        patched(mut, "_set_campaign_status_via_sdk", fake_sdk),
        allowed_ids(DRAFT_ACCOUNT_ID),
        pytest.raises(observe.EventWriteError),
    ):
        await mut.apply_pause_campaign(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="23",
            confirmation_id="ok",
            confirm_store=store,
            ads_client=_FakeClient(),
        )
    assert sdk_called["n"] == 0  # Google Ads не тронут
    assert store._claimed is False  # подтверждение не сожжено — человек повторит ту же команду


async def test_money_event_error_carries_no_raw_exception_text(monkeypatch: pytest.MonkeyPatch):
    """Правило 5: наружу тип, не `str(e)` — в DSN пароль."""
    import db.session as dbs

    def boom():
        raise RuntimeError("postgresql://user:SUPERSECRET@host/db unreachable")

    monkeypatch.setattr(dbs, "Session", boom)
    with pytest.raises(observe.EventWriteError) as ei:
        await observe.record_money_event("ads_mutate", operation="update_budget")
    assert "SUPERSECRET" not in str(ei.value) and "RuntimeError" in str(ei.value)


# ── 2. Неизменяемость обеспечивает СУБД ──────────────────────────────────────────────────────
_sqlite_only = pytest.mark.skipif(
    engine.dialect.name != "sqlite", reason="триггеры проверяются на диалекте тестовой БД"
)


@_sqlite_only
async def test_update_of_any_event_is_refused_by_db():
    run_id = f"upd{uuid.uuid4().hex[:8]}"
    await _write(run_id, [_ev("tool", tool_name="x")])
    with pytest.raises(Exception) as ei:  # noqa: PT011 — тип зависит от драйвера, важен сам отказ
        async with Session() as s:
            await s.execute(
                update(AgentRunEvent).where(AgentRunEvent.run_id == run_id).values(ok=False)
            )
            await s.commit()
    assert "UPDATE forbidden" in str(ei.value)


@_sqlite_only
async def test_delete_of_fresh_money_event_is_refused_by_db():
    run_id = f"del{uuid.uuid4().hex[:8]}"
    await _write(run_id, [_ev("ads_mutate", tool_name="update_budget")])
    with pytest.raises(Exception) as ei:  # noqa: PT011
        async with Session() as s:
            await s.execute(delete(AgentRunEvent).where(AgentRunEvent.run_id == run_id))
            await s.commit()
    assert "retention floor" in str(ei.value)
    assert len(await _events(run_id)) == 1  # строка на месте


@_sqlite_only
async def test_delete_of_non_money_event_is_allowed():
    """Пол хранения — не запрет уборки: таблица растёт монотонно, ретеншн обязан работать."""
    run_id = f"ok{uuid.uuid4().hex[:8]}"
    await _write(run_id, [_ev("llm"), _ev("ads_read")])
    async with Session() as s:
        await s.execute(delete(AgentRunEvent).where(AgentRunEvent.run_id == run_id))
        await s.commit()
    assert await _events(run_id) == []


@_sqlite_only
async def test_delete_of_aged_money_event_is_allowed():
    """Событие СТАРШЕ пола удаляется. Пишем created_at в обход ORM (UPDATE запрещён триггером —
    поэтому именно INSERT с явной датой, а не «состарить существующее»)."""
    run_id = f"old{uuid.uuid4().hex[:8]}"
    async with Session() as s:
        await s.execute(
            text(
                "INSERT INTO agent_run_events (run_id, seq, kind, cost_usd, prompt_tokens, "
                "completion_tokens, ok, created_at) VALUES (:r, 0, 'ads_mutate', 0, 0, 0, 1, "
                f"datetime('now', '-{observe.MONEY_RETENTION_DAYS + 1} days'))"
            ),
            {"r": run_id},
        )
        await s.commit()
    async with Session() as s:
        await s.execute(delete(AgentRunEvent).where(AgentRunEvent.run_id == run_id))
        await s.commit()
    assert await _events(run_id) == []


# ── 3. Хэш-цепочка ───────────────────────────────────────────────────────────────────────────
def test_canonical_json_is_order_and_precision_stable():
    """Дайджест обязан воспроизводиться другим процессом: порядок ключей и представление float на
    него влиять не должны, значение — должно."""
    a = observe.canonical_json({"b": 1, "a": 0.1 + 0.2})
    b = observe.canonical_json({"a": 0.30000000000000004, "b": 1})
    assert a == b
    assert observe.canonical_json({"a": 0.3000005}) != observe.canonical_json({"a": 0.31})


def test_genesis_is_derived_from_run_id():
    """Иначе первое событие переносится в другой прогон, не порвав ни одной ссылки."""
    assert observe.genesis_digest("r1") != observe.genesis_digest("r2")
    assert observe.chain_events("r1", [_ev()])[0]["prev_digest"] == observe.genesis_digest("r1")


async def test_verify_chain_ok_on_intact_run():
    run_id = f"int{uuid.uuid4().hex[:8]}"
    await _write(run_id, [_ev("llm"), _ev("tool", tool_name="a"), _ev("ads_read", rows_returned=3)])
    v = await observe.verify_chain(run_id)
    assert v == {"ok": True, "events": 3, "unchained": 0, "broken_at": None, "reason": None}


@_sqlite_only
async def test_verify_chain_sees_a_deleted_link():
    """Отрицательный контроль: удаление РАЗРЕШЁННОГО события триггер пропускает — цепочка обязана
    его показать. Два механизма дополняют друг друга, а не дублируют."""
    run_id = f"cut{uuid.uuid4().hex[:8]}"
    await _write(run_id, [_ev("llm"), _ev("tool", tool_name="middle"), _ev("ads_read")])
    async with Session() as s:
        await s.execute(
            delete(AgentRunEvent).where(AgentRunEvent.run_id == run_id, AgentRunEvent.seq == 1)
        )
        await s.commit()
    v = await observe.verify_chain(run_id)
    assert v["ok"] is False and v["broken_at"] == 2 and v["reason"] == "seq_gap"


async def test_verify_chain_sees_a_forged_payload():
    """Строка, чьё содержимое не сходится со своей подписью, — даже если вставлена напрямую."""
    run_id = f"frg{uuid.uuid4().hex[:8]}"
    await _write(run_id, [_ev("tool", tool_name="honest")])
    # Ссылка на предыдущее звено ЧЕСТНАЯ (иначе первым сработал бы prev_mismatch и подмену
    # содержимого мы бы не проверили вовсе) — врёт только содержимое.
    prev = (await _events(run_id))[0].payload_digest
    lying = observe.chain_events(run_id, [_ev("tool", tool_name="honest")], start_seq=1, prev=prev)[
        0
    ]
    lying["tool_name"] = "forged"  # подпись считалась по 'honest'
    async with Session() as s:
        s.add(AgentRunEvent(run_id=run_id, **lying))
        await s.commit()
    v = await observe.verify_chain(run_id)
    assert v["ok"] is False and v["broken_at"] == 1 and v["reason"] == "payload_mismatch"


async def test_legacy_rows_are_counted_not_claimed_verified():
    """Строки без дайджеста (записаны до 0035) — `unchained`, а не «сошлось». Врать про
    непроверяемый хвост нельзя."""
    run_id = f"leg{uuid.uuid4().hex[:8]}"
    async with Session() as s:
        s.add(AgentRunEvent(run_id=run_id, seq=0, **_ev("llm")))  # без prev/payload_digest
        await s.commit()
    await _write(run_id, [_ev("tool", tool_name="new")])
    v = await observe.verify_chain(run_id)
    assert v["ok"] is True and v["unchained"] == 1 and v["events"] == 2


async def test_run_scope_batch_continues_the_money_chain():
    """Батч на закрытии scope НЕ начинает нумерацию с нуля: денежное событие уже лежит под seq 0,
    и enumerate(0..N) развилил бы цепочку на два звена с одним prev_digest."""
    from core import context

    rid = f"mix{uuid.uuid4().hex[:8]}"
    tok = context.set_context(request_id=rid)
    try:
        await observe.record_money_event("ads_mutate", operation="update_budget")
        async with observe.run_scope("apply"):
            observe.record_event("llm", cost_usd=0.01)
            observe.record_event("tool", tool_name="after")
    finally:
        context.reset_context(tok)
    evs = await _events(rid)
    assert [e.seq for e in evs] == [0, 1, 2]
    assert [e.kind for e in evs] == ["ads_mutate", "llm", "tool"]
    assert (await observe.verify_chain(rid))["ok"] is True


async def test_nested_run_scope_reuses_the_outer_run():
    """Прогон = один ассистентский ход, и ход не содержит внутри себя ходов. Волна 3 открывает scope
    в трёх местах сразу (агентский цикл, `execute_confirmed`, тул-слой MCP), и вложенный вызов
    обязан лечь во ВНЕШНИЙ прогон: иначе `agent_runs` получила бы две строки с одним run_id, а
    `cost_report` посчитал бы один ход за два."""
    from sqlalchemy import func

    from core import context
    from db.models import AgentRun

    rid = f"nst{uuid.uuid4().hex[:8]}"
    tok = context.set_context(request_id=rid)
    try:
        async with observe.run_scope("outer") as outer:
            observe.record_event("llm", cost_usd=0.01)
            async with observe.run_scope("inner") as inner:
                assert inner is outer  # тот же аккумулятор, а не второй прогон
                observe.record_event("tool", tool_name="inner-step")
    finally:
        context.reset_context(tok)

    async with Session() as s:
        headers = (
            await s.execute(select(func.count(AgentRun.id)).where(AgentRun.run_id == rid))
        ).scalar_one()
    assert headers == 1  # ОДНА строка прогона
    evs = await _events(rid)
    assert [e.kind for e in evs] == ["llm", "tool"]  # события вложенного — в том же прогоне
    assert (await observe.verify_chain(rid))["ok"] is True


async def test_purge_removes_whole_runs_and_never_money_ones(monkeypatch):
    """Уборка журнала идёт ЦЕЛЫМИ прогонами и обходит денежные.

    Два независимых требования в одном месте. (а) Вырезать звено из хэш-цепочки нельзя даже штатной
    джобе: `verify_chain` увидел бы `seq_gap` и объявил подделкой собственную уборку. (б) Прогон, где
    есть хоть одно денежное событие, не убирается вовсе — его пол хранения держит триггер СУБД, и
    построчное удаление там просто упало бы, уронив всю уборку."""
    from datetime import datetime, timedelta, timezone

    from core.config import settings
    from db.models import AgentRun
    from scheduler import jobs

    old = datetime.now(timezone.utc) - timedelta(days=400)
    plain, money = f"pg1{uuid.uuid4().hex[:6]}", f"pg2{uuid.uuid4().hex[:6]}"
    async with Session() as s:
        for rid in (plain, money):
            s.add(AgentRun(run_id=rid, origin="machine", operation="t", started_at=old))
        await s.commit()
    await _write(plain, [_ev("llm"), _ev("tool")])
    await _write(money, [_ev("ads_mutate", tool_name="update_budget"), _ev("tool")])

    for key in (
        "error_events",
        "crawl_jobs",
        "account_health",
        "site_page_text",
        "ads_quota_ops",
    ):
        monkeypatch.setattr(settings, f"{key}_retain_days", 0)  # чистим ТОЛЬКО журнал прогонов
    monkeypatch.setattr(settings, "agent_runs_retain_days", 30)

    res = await jobs.purge_stale_rows()
    assert res["agent_runs"] >= 1
    assert await _events(plain) == []  # прогон ушёл целиком
    assert [e.kind for e in await _events(money)] == ["ads_mutate", "tool"]  # денежный цел
    async with Session() as s:
        left = set(
            (await s.execute(select(AgentRun.run_id).where(AgentRun.run_id.in_([plain, money]))))
            .scalars()
            .all()
        )
    assert left == {money}  # заголовок денежного прогона тоже остался


async def test_replay_script_reconstructs_the_run_and_matches_audit(capsys):
    """`scripts/replay_run.py` — читающая сторона контура: восстановить ход, проверить цепочку и
    сверить ЗАЯВКУ денежного пути с audit-row.

    Сверка здесь не украшение: событие `ads_mutate` пишется ДО SDK-вызова, а исход живёт в
    `audit_log` (правило 15). Заявка без audit-row — не «мутации не было», а «исход неизвестен», и
    скрипт обязан отличать одно от другого, а не рапортовать зелёным."""
    import importlib.util
    import json

    from db.models import AuditLog

    spec = importlib.util.spec_from_file_location(
        "replay_run", Path(__file__).resolve().parents[1] / "scripts" / "replay_run.py"
    )
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)

    from core import context
    from db.models import AgentRun

    rid, cid = f"rp{uuid.uuid4().hex[:8]}", f"cid-{uuid.uuid4().hex[:8]}"
    tok = context.set_context(request_id=rid)
    try:
        await observe.record_money_event(
            "ads_mutate", operation="update_budget", confirmation_id=cid
        )
        async with observe.run_scope("execute_confirmed"):
            observe.record_event("ads_read", tool_name="get_stats", rows_returned=3)
    finally:
        context.reset_context(tok)

    # Заявка ещё не подкреплена исходом — скрипт обязан покраснеть (код 1), а не смолчать.
    assert await replay._replay(rid, as_json=True) == 1
    assert cid in json.loads(capsys.readouterr().out)["unmatched_claims"]

    async with Session() as s:
        s.add(
            AuditLog(
                confirmation_id=cid,
                operation="update_budget",
                customer_id=DRAFT_ACCOUNT_ID,
                chat_id=1,
                status="applied",
            )
        )
        await s.commit()

    assert await replay._replay(rid, as_json=True) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["chain"]["ok"] is True
    assert [e["kind"] for e in out["events"]] == ["ads_mutate", "ads_read"]
    assert out["money_claims"][cid] == [["applied", "update_budget"]]
    assert out["unmatched_claims"] == []

    # Человекочитаемый режим не должен падать и обязан нести тот же вердикт. Заголовок прогона уже
    # записал сам `run_scope` — руками добавлять второй нельзя, это и была бы та развилка run_id.
    async with Session() as s:
        headers = (await s.execute(select(AgentRun.run_id).where(AgentRun.run_id == rid))).all()
    assert len(headers) == 1
    assert await replay._replay(rid, as_json=False) == 0
    text_out = capsys.readouterr().out
    assert "цепочка" in text_out and cid in text_out
