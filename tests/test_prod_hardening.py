"""Тесты предпродакшн-харденинга (2026-07): наблюдаемость/устойчивость/сохранность.

Покрывает: A1 (проактивный алерт админам о новых error_events), A3 (кнопки /diag + формат),
B2 (single-instance advisory-lock — no-op на SQLite), C2 (ретеншн-purge, audit_log НЕ трогаем),
C3 (пер-юзер дневной потолок LLM, fail-closed). Всё офлайн: SQLite / чистые функции, без сети/SDK.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


# ── A1: проактивный алерт админам о новых error_events ─────────────────────────────
async def test_error_alerts_baseline_then_new(monkeypatch):
    """Первый прогон только ставит базлайн (не спамит историей); затем НОВЫЕ инциденты уходят
    ВСЕМ админам одним дедуплённым дайджестом; повторный прогон без новых — тишина."""
    from core.config import settings
    from db.models import ErrorEvent
    from db.session import Session, init_db
    from scheduler import jobs

    await init_db()
    monkeypatch.setattr(settings, "admin_chat_ids", "1,2")
    jobs._error_alert_last_id = None

    bot = FakeBot()  # первый прогон — базлайн, без рассылки
    assert await jobs.run_error_alerts(bot) == 0
    assert bot.sent == []

    async with Session() as s:  # 3 новых: 2 одинаковых по (exc_type, where) + 1 иной
        for _ in range(2):
            s.add(
                ErrorEvent(request_id="r1", where="cmd:status", exc_type="ValueError", message="x")
            )
        s.add(ErrorEvent(request_id="r2", where="cmd:balance", exc_type="KeyError", message="y"))
        await s.commit()

    bot2 = FakeBot()
    assert await jobs.run_error_alerts(bot2) == 3
    assert {c for c, _ in bot2.sent} == {1, 2}  # оба админа получили
    assert "3" in bot2.sent[0][1]  # число новых инцидентов
    assert "×2" in bot2.sent[0][1]  # дедуп: ValueError дважды

    bot3 = FakeBot()  # новых нет — тишина (чек-поинт продвинут)
    assert await jobs.run_error_alerts(bot3) == 0
    assert bot3.sent == []


async def test_error_alerts_no_admins_noop(monkeypatch):
    """Нет ADMIN_CHAT_IDS ⇒ фича opt-in, джоба ничего не шлёт (fail-closed к спаму)."""
    from core.config import settings
    from scheduler import jobs

    monkeypatch.setattr(settings, "admin_chat_ids", "")
    jobs._error_alert_last_id = None
    bot = FakeBot()
    assert await jobs.run_error_alerts(bot) == 0
    assert bot.sent == []


# ── C2: ретеншн-purge растущих таблиц (audit_log НЕ трогаем) ───────────────────────
async def test_purge_deletes_old_keeps_running_and_audit(monkeypatch):
    from core.config import settings
    from db.models import AuditLog, CrawlJob, ErrorEvent
    from db.session import Session, init_db
    from scheduler import jobs

    await init_db()
    monkeypatch.setattr(settings, "error_events_retain_days", 90)
    monkeypatch.setattr(settings, "crawl_jobs_retain_days", 30)
    old = datetime(2020, 1, 1)  # заведомо за порогом
    async with Session() as s:
        await s.execute(delete(ErrorEvent))  # детерминизм (temp SQLite переживает между тестами)
        await s.execute(delete(CrawlJob))
        s.add(ErrorEvent(request_id="old", where="w", exc_type="E", message="m", created_at=old))
        s.add(ErrorEvent(request_id="new", where="w", exc_type="E", message="m"))  # created_at=now
        s.add(
            CrawlJob(
                job_id="cj_old_done",
                customer_id="1",
                chat_id=1,
                domain="x",
                status="done",
                created_at=old,
            )
        )
        s.add(
            CrawlJob(  # running НЕ трогаем (его закрывает reconcile_stale_crawls, не purge)
                job_id="cj_old_run",
                customer_id="1",
                chat_id=1,
                domain="x",
                status="running",
                created_at=old,
            )
        )
        s.add(
            AuditLog(  # денежный реестр — purge НЕ должен его касаться никогда
                confirmation_id="c",
                operation="update_budget",
                customer_id="1",
                chat_id=1,
                status="applied",
                created_at=old,
            )
        )
        await s.commit()

    res = await jobs.purge_stale_rows()
    assert res["error_events"] == 1  # удалён только old
    assert res["crawl_jobs"] == 1  # удалён только терминальный old_done
    async with Session() as s:
        ee = set((await s.execute(select(ErrorEvent.request_id))).scalars().all())
        cj = set((await s.execute(select(CrawlJob.job_id))).scalars().all())
        au = (await s.execute(select(func.count()).select_from(AuditLog))).scalar()
    assert "new" in ee and "old" not in ee
    assert "cj_old_run" in cj and "cj_old_done" not in cj
    assert au >= 1  # audit_log цел


async def test_purge_nulls_stale_page_text_but_keeps_row(monkeypatch):
    """§20: протухает ТЕКСТ страницы, а не строка — карта sitelinks (top_site_pages) должна выжить."""
    from core.config import settings
    from db.models import ClientSitePage
    from db.session import Session, init_db
    from scheduler import jobs

    await init_db()
    monkeypatch.setattr(settings, "error_events_retain_days", 0)
    monkeypatch.setattr(settings, "crawl_jobs_retain_days", 0)
    monkeypatch.setattr(settings, "account_health_retain_days", 0)
    monkeypatch.setattr(settings, "site_page_text_retain_days", 90)
    async with Session() as s:
        await s.execute(delete(ClientSitePage))
        s.add(
            ClientSitePage(
                profile_id=9901,
                url="https://x.test/old",
                title="old",
                text="старый текст",
                crawled_at=datetime(2020, 1, 1),
            )
        )
        s.add(ClientSitePage(profile_id=9901, url="https://x.test/new", title="new", text="свежий"))
        await s.commit()

    res = await jobs.purge_stale_rows()
    assert res["site_page_text"] == 1
    async with Session() as s:
        rows = {
            r.url: r.text
            for r in (
                await s.execute(select(ClientSitePage).where(ClientSitePage.profile_id == 9901))
            ).scalars()
        }
    assert rows == {"https://x.test/old": None, "https://x.test/new": "свежий"}  # строки обе целы


async def test_purge_disabled_when_retain_zero(monkeypatch):
    from core.config import settings
    from db.models import ErrorEvent
    from db.session import Session, init_db
    from scheduler import jobs

    await init_db()
    monkeypatch.setattr(settings, "error_events_retain_days", 0)  # 0 ⇒ не чистить
    monkeypatch.setattr(settings, "crawl_jobs_retain_days", 0)
    monkeypatch.setattr(settings, "account_health_retain_days", 0)  # N1.1: та же семантика
    monkeypatch.setattr(settings, "site_page_text_retain_days", 0)  # §20: тексты краула
    async with Session() as s:
        await s.execute(delete(ErrorEvent))
        s.add(
            ErrorEvent(
                request_id="anc",
                where="w",
                exc_type="E",
                message="m",
                created_at=datetime(2019, 1, 1),
            )
        )
        await s.commit()
    assert await jobs.purge_stale_rows() == {
        "error_events": 0,
        "crawl_jobs": 0,
        "account_health_snapshot": 0,
        "site_page_text": 0,
    }
    async with Session() as s:
        cnt = (await s.execute(select(func.count()).select_from(ErrorEvent))).scalar()
    assert cnt >= 1  # ничего не удалено


# ── C3: пер-юзер дневной потолок LLM (fail-closed) ─────────────────────────────────
def test_llm_budget_blocks_at_limit(monkeypatch):
    from core import llm_budget
    from core.config import settings

    monkeypatch.setattr(settings, "llm_daily_calls_per_user", 3)
    llm_budget.reset()
    for _ in range(3):
        llm_budget.consume(999)  # в пределах — ок
    with pytest.raises(llm_budget.LLMBudgetExceededError) as ei:
        llm_budget.consume(999)  # 4-й — отказ
    assert ei.value.used == 3 and ei.value.limit == 3
    llm_budget.consume(1000)  # другой чат не затронут


def test_llm_budget_disabled_when_zero(monkeypatch):
    from core import llm_budget
    from core.config import settings

    monkeypatch.setattr(settings, "llm_daily_calls_per_user", 0)
    llm_budget.reset()
    for _ in range(50):
        llm_budget.consume(1)  # 0 ⇒ гард выключен, никогда не бросает


def test_llm_budget_none_chat_noop(monkeypatch):
    from core import llm_budget
    from core.config import settings

    monkeypatch.setattr(settings, "llm_daily_calls_per_user", 1)
    llm_budget.reset()
    llm_budget.consume(None)
    llm_budget.consume(None)  # chat_id None → без гейта (не бросает)


# ── A3: клавиатура /diag + форматтеры ──────────────────────────────────────────────
def test_diag_kb_detail_only_for_admin():
    from bot.keyboards import diag_kb

    rows = [SimpleNamespace(request_id="abc123", where="w", exc_type="E", created_at=None)]
    cbs_admin = [
        b.callback_data
        for r in diag_kb(rows, today=False, is_admin=True).inline_keyboard
        for b in r
    ]
    assert any(c.startswith("diag:") for c in cbs_admin)
    assert any("detail" in c for c in cbs_admin)  # detail-кнопка у админа есть
    cbs_user = [
        b.callback_data
        for r in diag_kb(rows, today=False, is_admin=False).inline_keyboard
        for b in r
    ]
    assert not any("detail" in c for c in cbs_user)  # у не-админа detail НЕТ


def test_diag_kb_today_toggle():
    from bot.keyboards import diag_kb

    t_off = [
        b.text.lower() for r in diag_kb([], today=False, is_admin=False).inline_keyboard for b in r
    ]
    assert any("сегодня" in t or "today" in t for t in t_off)
    t_on = [
        b.text.lower() for r in diag_kb([], today=True, is_admin=False).inline_keyboard for b in r
    ]
    assert any("все" in t or "all" in t for t in t_on)


def test_fmt_error_alert_dedup_counts():
    from bot import texts

    rows = [
        SimpleNamespace(
            request_id="r1", where="cmd:status", exc_type="ValueError", created_at=None
        ),
        SimpleNamespace(
            request_id="r1", where="cmd:status", exc_type="ValueError", created_at=None
        ),
        SimpleNamespace(request_id="r2", where="cmd:balance", exc_type="KeyError", created_at=None),
    ]
    out = texts.fmt_error_alert(rows)
    assert "3" in out and "×2" in out and "KeyError" in out


def test_fmt_error_detail_escapes_and_truncates():
    from bot import texts

    row = SimpleNamespace(
        request_id="rid1",
        where="w",
        exc_type="E",
        created_at=None,
        traceback="A" * 5000,
        message="m",
    )
    out = texts.fmt_error_detail(row)
    assert "rid1" in out and "<pre>" in out
    assert len(out) < 4096  # влезает в лимит Telegram


# ── B2: single-instance advisory-lock (no-op на SQLite) ────────────────────────────
async def test_single_instance_lock_noop_on_sqlite():
    from db.session import acquire_single_instance_lock, release_single_instance_lock

    assert await acquire_single_instance_lock() is True  # SQLite (dev/test) → no-op True
    await release_single_instance_lock()
    await release_single_instance_lock()  # идемпотентно
