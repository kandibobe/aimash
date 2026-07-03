"""Задачи планировщика: плановые отчёты, проверка аномалий, очистка просроченных черновиков.

READ-ONLY + уведомления. ⛔ НИКОГДА не импортирует ads.mutations и не вызывает
execute_confirmed / apply_* — планировщик не меняет аккаунт (golden rule #3). Очистка лишь
ОТКЛОНЯЕТ (reject, с аудитом) старые pending-черновики: они не подтверждались → SDK не звался →
деньги не тратились, отклонить безопасно. Реконсиляция зависших executing (needs_review) — тоже
только запись в ЛОКАЛЬНУЮ БД + уведомление (НЕ авто-ретрай: мутации не идемпотентны).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from ads.client import DRAFT_ACCOUNT_ID, build_client_async
from confirm.store import ConfirmStore
from core.ads_errors import is_account_access_error
from core.config import settings
from core.context import request_scope, reset_context, set_context
from core.errors import capture_exception
from core.logging import log
from core.resilience import run_ads_read_call
from db.models import CampaignDraft, CrawlJob, Proposal, UserSettings
from db.session import Session
from reports.period import last_n_days
from reports.queries import fetch_totals
from reports.service import build_account_report_async, summary_text
from scheduler.anomaly import detect_anomalies

REPORT_WINDOW_DAYS = 7  # окно планового отчёта
ANOMALY_WINDOW_DAYS = 7  # окно сравнения для аномалий (текущие N дн. vs предыдущие N дн.)
PROPOSAL_TTL_HOURS = 24  # сколько живёт неподтверждённый черновик до авто-отклонения
_DIGEST_MAX = 3800  # потолок длины дайджеста для Telegram (лимит 4096, оставляем запас)


def _recipients() -> set[int]:
    """Кому слать: доверенные whitelisted-пользователи (операторы бота)."""
    return set(settings.whitelist)


def _scheduled_accounts() -> list[str]:
    """Аккаунты для плановых отчётов/аномалий (§8): мутационный allow-list ∪ env read-list ∪
    дочерние, ОБНАРУЖЕННЫЕ обходом MCC на старте (discover_read_children — полный мульти-аккаунт).
    Пусто ⇒ дефолт [Draft] (поведение как раньше — единственный аккаунт). READ-ONLY: scheduler
    только читает (golden rule #3)."""
    from ads.client import discovered_read_children

    accts = settings.allowed_customer_ids | settings.read_customer_ids | discovered_read_children()
    return sorted(accts) if accts else [DRAFT_ACCOUNT_ID]


async def _broadcast(bot, text: str, **kw) -> None:
    for chat_id in _recipients():
        try:
            await bot.send_message(chat_id, text, **kw)
        except Exception as e:  # один недоступный чат не должен ронять рассылку
            log.warning("scheduler: не доставлено в %s: %s: %s", chat_id, type(e).__name__, e)


async def run_scheduled_report(bot) -> None:
    """Плановый отчёт (последние N дн.) по ВСЕМ разрешённым на чтение аккаунтам (§8) — ОДИН дайджест
    на оператора (анти-спам, не N сообщений). READ-ONLY. Сбой одного аккаунта не валит остальные
    (capture_exception per-account) и не топит рассылку."""
    with request_scope("scheduler:report"):  # §15: корреляция логов джобы по request_id
        if not _recipients():
            log.info("scheduler: получателей нет (whitelist пуст) — пропуск планового отчёта")
            return
        accounts = _scheduled_accounts()
        if not accounts:
            log.info("scheduler: нет аккаунтов для отчёта (allow/read-list пусты) — пропуск")
            return
        period = last_n_days(REPORT_WINDOW_DAYS)
        # 3H: блоки собираем per-lang (у операторов может быть RU и EN): summary_text локализуется,
        # а валюта аккаунта дочитывается per-account (раньше суммы шли голыми числами без кода).
        from bot import i18n

        langs = {i18n.get_lang(chat_id) for chat_id in _recipients()} or {"ru"}
        blocks: dict[str, list[str]] = {lang: [] for lang in langs}
        for acct in accounts:
            tok = set_context(customer_id=acct)  # §8: ошибки/логи этого аккаунта атрибутируются
            try:
                # per-account (Фаза 3: свой токен/MCC из oauth_tokens); холодная сборка вне loop
                client = await build_client_async(acct)
                currency = ""
                try:  # валюта best-effort: без неё показываем числа без кода (как раньше)
                    from ads.read import account_currency

                    currency = await run_ads_read_call(
                        account_currency, client, acct, label=f"sched_cur_{acct}"
                    )
                except Exception:  # noqa: BLE001
                    currency = ""
                report = await build_account_report_async(client, acct, period, currency=currency)
                for lang in langs:
                    blocks[lang].append(summary_text(report, lang))
            except Exception as e:  # сеть/доступ/SDK — фиксируем (§15), остальные аккаунты живут
                if is_account_access_error(e):
                    # A3: аккаунт деактивирован/нет прав — ОЖИДАЕМО (не дефект). Не пишем в /diag
                    # каждый цикл и не пугаем оператора блоком; тихо пропускаем из дайджеста.
                    log.info(
                        "scheduler report: аккаунт %s недоступен на чтение (ожидаемо, пропуск): %s",
                        acct,
                        type(e).__name__,
                    )
                else:
                    await capture_exception(e, where=f"scheduler:report:{acct}")
                    for lang in langs:
                        blocks[lang].append(f"⚠️ Аккаунт {acct}: отчёт недоступен (см. /diag)")
            finally:
                reset_context(tok)
        if not any(blocks.values()):
            return
        digests: dict[str, str] = {}
        for lang, parts in blocks.items():
            header = "🗓 Scheduled report" if lang == "en" else "🗓 Плановый отчёт"
            digest = header + "\n\n" + "\n\n———\n\n".join(parts)
            if len(digest) > _DIGEST_MAX:  # анти-спам: не дробим на N сообщений, усечём с пометкой
                digest = digest[:_DIGEST_MAX] + "\n\n…(усечено — полный отчёт по аккаунту: /report)"
            digests[lang] = digest
        for chat_id in _recipients():
            try:
                await bot.send_message(chat_id, digests[i18n.get_lang(chat_id)])
            except Exception as e:  # один недоступный чат не должен ронять рассылку
                log.warning("scheduler: не доставлено в %s: %s: %s", chat_id, type(e).__name__, e)


async def _thresholds_by_chat(chat_ids: set[int]) -> dict[int, dict | None]:
    """Пороги аномалий per-chat из UserSettings.alert_thresholds (JSON). Чата нет в таблице →
    None (detect_anomalies возьмёт DEFAULT_THRESHOLDS). Один запрос на всех получателей.
    READ-ONLY: только чтение настроек, аккаунт не трогается (golden rule #3)."""
    if not chat_ids:
        return {}
    async with Session() as s:
        rows = (
            await s.execute(
                select(UserSettings.chat_id, UserSettings.alert_thresholds).where(
                    UserSettings.chat_id.in_(chat_ids)
                )
            )
        ).all()
    return {cid: thr for cid, thr in rows}


def _format_alerts_multi(account_alerts: list[tuple[str, list]], lang: str = "ru") -> str:
    """Единое сообщение об аномалиях по НЕСКОЛЬКИМ аккаунтам (§8, анти-спам: один месседж на
    оператора, а не по сообщению на аккаунт). Каждый блок помечен аккаунтом. 3H: заголовок/
    подпись локализованы per-recipient (тексты алертов — RU-детектор, суммы несут код валюты)."""
    if lang == "en":
        parts = [f"🔔 <b>Anomalies</b> (last {ANOMALY_WINDOW_DAYS}d vs previous period):"]
    else:
        parts = [f"🔔 <b>Аномалии</b> (за {ANOMALY_WINDOW_DAYS} дн. к предыдущему периоду):"]
    for acct, alerts in account_alerts:
        parts.append(f"\n<b>{'Account' if lang == 'en' else 'Аккаунт'} {acct}</b>:")
        parts.extend("• " + a.message for a in alerts)
    if lang == "en":
        parts.append("\n<i>This is only a signal — I never change anything myself.</i>")
    else:
        parts.append("\n<i>Это только сигнал — сам я ничего не меняю. Реши и дай команду.</i>")
    return "\n".join(parts)


async def run_anomaly_check(bot) -> None:
    """Сравнение последних N дн. с предыдущими ПО ВСЕМ разрешённым аккаунтам (§8); алерт при росте
    расхода/падении конверсий. Пороги — per-chat из UserSettings.alert_thresholds (иначе дефолтные).
    Анти-спам: ОДИН месседж на оператора со всеми его аккаунтами. READ-ONLY (golden rule #3):
    fetch_totals через run_ads_read_call (ретрай TimeoutError/транзиентных), без мутаций."""
    with request_scope("scheduler:anomaly"):  # §15: корреляция логов джобы по request_id
        recipients = _recipients()
        if not recipients:
            return
        accounts = _scheduled_accounts()
        if not accounts:
            return
        period = last_n_days(ANOMALY_WINDOW_DAYS)
        # cur/prev (+валюта, 3H) на каждый аккаунт; сбой одного фиксируем и пропускаем.
        metrics: dict[str, tuple] = {}
        for acct in accounts:
            tok = set_context(customer_id=acct)  # §8: per-account атрибуция ошибок/логов
            try:
                client = await build_client_async(acct)
                cur = await run_ads_read_call(
                    fetch_totals, client, acct, period, label=f"anom_{acct}"
                )
                prev = await run_ads_read_call(
                    fetch_totals, client, acct, period.previous(), label=f"anom_prev_{acct}"
                )
                currency = ""
                try:  # 3H: код валюты в суммах алерта (best-effort)
                    from ads.read import account_currency

                    currency = await run_ads_read_call(
                        account_currency, client, acct, label=f"anom_cur_{acct}"
                    )
                except Exception:  # noqa: BLE001
                    currency = ""
                metrics[acct] = (cur, prev, currency)
            except Exception as e:  # сеть/доступ/SDK — фиксируем (§15), остальные аккаунты живут
                if is_account_access_error(e):
                    # A3: деактивирован/нет прав — ОЖИДАЕМО. Раньше это писалось в /diag+Sentry как
                    # error на КАЖДЫЙ такой аккаунт КАЖДЫЙ цикл (журнал в скринах был им завален).
                    log.info(
                        "scheduler anomaly: аккаунт %s недоступен на чтение (ожидаемо, пропуск): %s",
                        acct,
                        type(e).__name__,
                    )
                else:
                    await capture_exception(e, where=f"scheduler:anomaly:{acct}")
            finally:
                reset_context(tok)
        if not metrics:
            return
        from bot import i18n

        thresholds = await _thresholds_by_chat(recipients)
        # Пороги per-chat → для каждого получателя собираем алерты по всем аккаунтам в ОДНО сообщение.
        for chat_id in recipients:
            thr = thresholds.get(chat_id)
            acct_alerts: list[tuple[str, list]] = []
            for acct, (cur, prev, currency) in metrics.items():
                alerts = detect_anomalies(cur, prev, thr, currency=currency)
                if alerts:
                    acct_alerts.append((acct, alerts))
            if not acct_alerts:
                continue
            try:
                await bot.send_message(
                    chat_id,
                    _format_alerts_multi(acct_alerts, lang=i18n.get_lang(chat_id)),
                    parse_mode="HTML",
                )
            except Exception as e:  # один недоступный чат не должен ронять остальные
                log.warning(
                    "scheduler: алерт не доставлен в %s: %s: %s", chat_id, type(e).__name__, e
                )


async def cleanup_stale_proposals(
    *, now: datetime | None = None, ttl_hours: int = PROPOSAL_TTL_HOURS
) -> int:
    """Просроченные pending-черновики → reject (с аудитом). Возвращает число отклонённых.

    Сравнение возраста — в Python (а не в SQL), чтобы корректно работать и на SQLite (наивный
    UTC), и на Postgres (tz-aware): наивный created_at трактуем как UTC."""
    with request_scope("scheduler:cleanup"):  # §15: корреляция логов джобы по request_id
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=ttl_hours)
        store = ConfirmStore()
        async with Session() as s:
            rows = (
                (await s.execute(select(Proposal).where(Proposal.status == "pending")))
                .scalars()
                .all()
            )
            stale: list[tuple[str, int]] = []
            for p in rows:
                created = p.created_at
                if created is None:
                    continue
                if created.tzinfo is None:  # SQLite хранит наивный UTC
                    created = created.replace(tzinfo=timezone.utc)
                if created < cutoff:
                    stale.append((p.confirmation_id, p.chat_id))
        for cid, chat_id in stale:
            # §19/§11: TTL-просроченные create_search/gdn/demand_gen_campaign несут временные медиа
            # по media_id — чистим их перед reject (иначе осиротеют на диске).
            snap = await store.get_confirmed(cid)
            if snap is not None:
                from ads.assets import clear_pending_media_ids, collect_search_campaign_media_ids

                p = snap.params or {}
                if snap.operation == "create_search_campaign":
                    # изображения Этапа 4 + логотипы business_logo (§19.7.1) — единый сборщик
                    clear_pending_media_ids(collect_search_campaign_media_ids(p))
                elif snap.operation == "create_gdn_campaign" and p.get("media_id"):
                    clear_pending_media_ids([p["media_id"]])
                elif snap.operation == "create_demand_gen_campaign" and p.get("logo_media_id"):
                    clear_pending_media_ids([p["logo_media_id"]])
            await store.reject(cid, chat_id=chat_id)  # pending→rejected + audit (одноразово)
        if stale:
            log.info("scheduler: отклонено просроченных черновиков: %d", len(stale))
        return len(stale)


async def cleanup_stale_campaign_drafts(
    *, now: datetime | None = None, ttl_hours: int | None = None
) -> int:
    """§19: брошенные активные черновики визарда «Создание кампании» → status='abandoned'.

    Не proposal и не мутация — просто гасим залежавшиеся active-черновики (SDK не звался, деньги
    не тратились). Возраст считаем в Python (наивный created/updated трактуем как UTC) — корректно
    и на SQLite, и на Postgres. TTL щедрый (settings.campaign_draft_ttl_hours, дефолт 72ч): Этап-2
    round-trip с Google Sheets может занять день."""
    ttl = settings.campaign_draft_ttl_hours if ttl_hours is None else ttl_hours
    with request_scope("scheduler:cleanup-drafts"):
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=ttl)
        n = 0
        orphan_media: list[str] = []
        async with Session() as s:
            rows = (
                (await s.execute(select(CampaignDraft).where(CampaignDraft.status == "active")))
                .scalars()
                .all()
            )
            for d in rows:
                updated = d.updated_at or d.created_at
                if updated is None:
                    continue
                if updated.tzinfo is None:  # SQLite хранит наивный UTC
                    updated = updated.replace(tzinfo=timezone.utc)
                if updated < cutoff:
                    d.status = "abandoned"
                    n += 1
                    ws = d.wizard_state or {}
                    orphan_media += ws.get("images", {}).get("media_ids", []) or []
                    # §19.7.1: логотипы business_logo из набора ассетов брошенного черновика
                    for spec in (ws.get("assets") or {}).get("new") or []:
                        if str(spec.get("family") or "") == "business_logo":
                            mid = (spec.get("params") or {}).get("media_id")
                            if mid:
                                orphan_media.append(str(mid))
            if n:
                await s.commit()
        if orphan_media:  # §19: чистим временные изображения брошенных черновиков (вне транзакции)
            from ads.assets import clear_pending_media_ids

            clear_pending_media_ids(orphan_media)
        if n:
            log.info("scheduler: брошено просроченных черновиков визарда: %d", n)
        return n


async def reconcile_stale_executing(
    bot=None, *, now: datetime | None = None, stale_minutes: int | None = None
) -> int:
    """Черновики, зависшие в 'executing' дольше N мин: процесс упал ПОСЛЕ claim, ПОСРЕДИ мутации —
    исход НЕИЗВЕСТЕН (SDK мог применить изменение в Google Ads). НЕ авто-ретраим (мутации не
    идемпотентны, golden rule) — помечаем needs_review (mark_needs_review, атомарный CAS) +
    audit-строка (§12: полнота журнала, событие видно в /journal) + error_event (/diag) +
    уведомление владельца в чат. Пометка — запись в ЛОКАЛЬНУЮ БД, не мутация Ads (golden rule #3).

    Возраст — от decided_at (момент claim; fallback created_at), наивный datetime трактуем как UTC
    (SQLite/Postgres). Порог N (settings.executing_stale_minutes, дефолт 30) ≫ худшего run_ads_call
    (4 попытки × 60с + backoff ≈ 5 мин) — живой процесс не зацепим; гонку с его finalize выигрывает
    finalize (CAS mark_needs_review вернёт False). bot=None (тесты) → без уведомлений."""
    stale = settings.executing_stale_minutes if stale_minutes is None else stale_minutes
    with request_scope("scheduler:reconcile-executing"):
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(minutes=stale)
        store = ConfirmStore()
        async with Session() as s:
            rows = (
                (await s.execute(select(Proposal).where(Proposal.status == "executing")))
                .scalars()
                .all()
            )
            stale_rows: list[tuple[str, str, str, int]] = []
            for p in rows:
                ts = p.decided_at or p.created_at  # decided_at = момент claim
                if ts is None:
                    continue
                if ts.tzinfo is None:  # SQLite хранит наивный UTC
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    stale_rows.append((p.confirmation_id, p.operation, p.customer_id, p.chat_id))
        n = 0
        for cid, op, customer_id, chat_id in stale_rows:
            err = (
                f"исполнение прервано (процесс упал посреди мутации, зависло >{stale} мин) — "
                "исход в Google Ads НЕИЗВЕСТЕН, сверь аккаунт вручную; авто-повтора не будет"
            )
            if not await store.mark_needs_review(cid, error=err):
                continue  # живой процесс успел finalize/failed — не наша строка
            n += 1
            await capture_exception(
                RuntimeError(
                    f"proposal {cid} завис в executing (op={op}, аккаунт {customer_id}) — "
                    "исход мутации неизвестен, сверь в Google Ads"
                ),
                where="scheduler:reconcile-executing",
            )
            if bot is not None:
                try:
                    await bot.send_message(
                        chat_id,
                        "⚠️ Операция "
                        f"«{op}» была прервана рестартом бота посреди выполнения — "
                        "применилось ли изменение в Google Ads, НЕИЗВЕСТНО.\n"
                        "Проверь аккаунт вручную (журнал: /journal). Авто-повтора не будет — "
                        "если изменение не применилось, дай команду заново.",
                    )
                except Exception as e:  # один недоступный чат не должен ронять реконсиляцию
                    log.warning(
                        "scheduler: needs_review-уведомление не доставлено в %s: %s: %s",
                        chat_id,
                        type(e).__name__,
                        e,
                    )
        if n:
            log.warning("scheduler: зависших executing-черновиков помечено needs_review: %d", n)
        return n


async def reconcile_stale_crawls(
    *, now: datetime | None = None, stale_minutes: int | None = None
) -> int:
    """§20.4: зависшие задачи краулинга (status='running' дольше N мин) → failed. Фоновый краул —
    in-process asyncio-задача: на рестарте процесса она гибнет, а строка crawl_jobs остаётся
    'running' навсегда. Реконсиляция закрывает их (SDK/сеть не звались — деньги не тратились).
    Возраст считаем в Python (наивный created_at трактуем как UTC) — корректно на SQLite и Postgres."""
    stale = settings.crawl_stale_minutes if stale_minutes is None else stale_minutes
    with request_scope("scheduler:reconcile-crawls"):
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(minutes=stale)
        n = 0
        async with Session() as s:
            rows = (
                (await s.execute(select(CrawlJob).where(CrawlJob.status == "running")))
                .scalars()
                .all()
            )
            for j in rows:
                created = j.created_at
                if created is None:
                    continue
                if created.tzinfo is None:  # SQLite хранит наивный UTC
                    created = created.replace(tzinfo=timezone.utc)
                if created < cutoff:
                    j.status = "failed"
                    j.error = "прервано рестартом (реконсиляция)"
                    j.finished_at = func.now()
                    n += 1
            if n:
                await s.commit()
        if n:
            log.info("scheduler: зависших краул-задач помечено failed: %d", n)
        return n
