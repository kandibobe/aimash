"""core.observe (#10 Наблюдаемость): per-run учёт.

• run_scope пишет ОДНУ строку agent_runs с суммами + упорядоченные agent_run_events;
• record_event вне scope — тихий no-op (нечему сшивать);
• исключение из блока → status='error', строка ВСЁ РАВНО записана, ошибка проброшена;
• import_hermes_activity идемпотентен по (date, model) — повтор обновляет, не двоит;
• cost_report группирует/суммирует по окну и сортирует по стоимости убыв.

SQLite (init_db), без сети: Hermes-активность мокаем через or_activity.fetch_activity seam."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, func, select

from core import context, observe, or_activity
from db.models import AgentRun, AgentRunEvent
from db.session import Session, init_db


async def _get_run(run_id: str) -> AgentRun | None:
    async with Session() as s:
        return (
            await s.execute(select(AgentRun).where(AgentRun.run_id == run_id))
        ).scalar_one_or_none()


async def _events(run_id: str) -> list[AgentRunEvent]:
    async with Session() as s:
        rows = (
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
        return list(rows)


async def _clear() -> None:
    async with Session() as s:
        # Денежные события НЕ удаляем: их держит триггер (пол хранения, Волна 3) — и они здесь ни
        # при чём, детерминизм нужен `cost_report`, а он считает по agent_runs.
        await s.execute(delete(AgentRunEvent).where(AgentRunEvent.kind.not_in(observe.MONEY_KINDS)))
        await s.execute(delete(AgentRun))
        await s.commit()


async def test_run_scope_persists_totals_and_events() -> None:
    await init_db()
    async with observe.run_scope("analysis") as acc:
        observe.note_model("openai/gpt-x")
        observe.record_event(
            "llm",
            tool_name="analyst",
            cost_usd=0.02,
            prompt_tokens=100,
            completion_tokens=50,
            cached_tokens=10,
        )
        observe.record_event(
            "ads_read", tool_name="get_stats", latency_ms=42, rows_returned=7, args={"limit": 10}
        )
        rid = acc.run_id

    run = await _get_run(rid)
    assert run is not None
    assert run.status == "ok"
    assert run.model == "openai/gpt-x"
    assert run.iterations_used == 1  # считаются только LLM-шаги
    assert run.prompt_tokens == 100 and run.completion_tokens == 50 and run.cached_tokens == 10
    assert abs(run.cost_usd - 0.02) < 1e-9
    assert run.finished_at is not None

    evs = await _events(rid)
    assert [e.kind for e in evs] == ["llm", "ads_read"]  # порядок seq сохранён
    assert evs[1].latency_ms == 42 and evs[1].rows_returned == 7
    assert evs[1].args_redacted is not None and "limit" in evs[1].args_redacted


async def test_record_event_noop_without_scope() -> None:
    await init_db()
    assert observe.current_run() is None
    observe.record_event("llm", cost_usd=1.0)  # вне scope — не бросает, ничего не пишет
    assert observe.current_run() is None


async def test_run_scope_error_status_and_reraise() -> None:
    await init_db()
    rid_box: dict[str, str] = {}
    with pytest.raises(ValueError):
        async with observe.run_scope("boom") as acc:
            rid_box["rid"] = acc.run_id
            observe.record_event("tool", tool_name="x")
            raise ValueError("kaboom")
    run = await _get_run(rid_box["rid"])
    assert run is not None and run.status == "error"  # строка записана даже при исключении


async def test_import_hermes_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    await init_db()
    rows = [
        or_activity.ActivityRow(
            date="2026-07-23",
            model="openai/gpt-5.6-terra",
            provider_name="OpenAI",
            usage=1.5,
            requests=10,
            prompt_tokens=1000,
            completion_tokens=500,
            reasoning_tokens=0,
            byok_usage_inference=None,
        )
    ]

    async def fake_fetch(date: str | None = None, *, client=None):  # noqa: ANN202
        return rows

    monkeypatch.setattr(or_activity, "fetch_activity", fake_fetch)

    n1 = await observe.import_hermes_activity("2026-07-23")
    n2 = await observe.import_hermes_activity("2026-07-23")  # повтор — обновляет, не двоит
    assert n1 == 1 and n2 == 1

    rid = observe._hermes_run_id("2026-07-23", "openai/gpt-5.6-terra")
    async with Session() as s:
        cnt = (
            await s.execute(select(func.count(AgentRun.id)).where(AgentRun.run_id == rid))
        ).scalar_one()
    assert cnt == 1  # один и тот же (date, model) → одна строка

    run = await _get_run(rid)
    assert run is not None and run.origin == "hermes"
    assert abs(run.cost_usd - 1.5) < 1e-9 and run.iterations_used == 10


async def test_cost_report_groups_and_sorts() -> None:
    await init_db()
    await _clear()  # детерминизм: отчёт не должен зависеть от строк других тестов в общем окне

    async def _run(customer: str, cost: float) -> None:
        tok = context.set_context(request_id=f"rep-{customer}", customer_id=customer)
        try:
            async with observe.run_scope("analysis"):
                observe.record_event("llm", cost_usd=cost)
        finally:
            context.reset_context(tok)

    await _run("9111", 0.10)
    await _run("9222", 0.30)

    since = datetime.now(timezone.utc) - timedelta(days=1)
    rows = await observe.cost_report(since=since, group_by="customer")
    by = {r["group"]: r for r in rows}
    assert abs(by["9111"]["cost_usd"] - 0.10) < 1e-9
    assert abs(by["9222"]["cost_usd"] - 0.30) < 1e-9
    assert [r["group"] for r in rows] == ["9222", "9111"]  # дороже — первым (сортировка убыв.)
    assert by["9222"]["runs"] == 1 and by["9222"]["iterations"] == 1
