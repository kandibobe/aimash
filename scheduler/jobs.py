"""Задачи планировщика: плановые отчёты, проверка аномалий, очистка просроченных черновиков.

READ-ONLY + уведомления. ⛔ НИКОГДА не импортирует ads.mutations и не вызывает
execute_confirmed / apply_* — планировщик не меняет аккаунт (golden rule #3). Очистка лишь
ОТКЛОНЯЕТ (reject, с аудитом) старые pending-черновики: они не подтверждались → SDK не звался →
деньги не тратились, отклонить безопасно. Реконсиляция зависших executing (needs_review) — тоже
только запись в ЛОКАЛЬНУЮ БД + уведомление (НЕ авто-ретрай: мутации не идемпотентны).
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

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
from scheduler import transport
from scheduler.anomaly import detect_anomalies

# 2.6: окна — из core.config (env REPORT_WINDOW_DAYS/ANOMALY_WINDOW_DAYS); алиасы для тестов.
REPORT_WINDOW_DAYS = int(settings.report_window_days)  # окно планового отчёта
ANOMALY_WINDOW_DAYS = int(settings.anomaly_window_days)  # окно сравнения аномалий (N дн. vs пред.)
# 2.6: TTL черновика — из core.config (env PROPOSAL_TTL_HOURS); имя-алиас сохранён для тестов.
PROPOSAL_TTL_HOURS = int(settings.proposal_ttl_hours)
_DIGEST_MAX = 3800  # потолок длины дайджеста для Telegram (лимит 4096, оставляем запас)
# 3.4: срез /competitors старше стольких дней → нудж «обнови» в дайджесте. Только для аккаунтов,
# где импорт уже был (кто фичей не пользуется — не спамим), и только в блоке НЕтихого аккаунта.
_AUCTION_STALE_DAYS = 30

# Анти-спам аномалий. Джоба крутится каждые anomaly_interval_hours (дефолт 6), а окно сравнения —
# anomaly_window_days (дефолт 7): ОДНА и та же аномалия («расход +60% к прошлой неделе») попадала в
# рассылку на КАЖДОМ прогоне ≈ 4 раза в сутки всю неделю (~28 одинаковых сообщений). Оператор
# перестаёт читать алерты — и пропускает настоящий. Кулдаун: повтор того же (аккаунт, kind) не
# раньше чем через N часов. Состояние — в ui_prefs получателя (механика _ui_pref_blob, как у
# thr-tune): ключ "acct:kind" → ISO-время последней ДОСТАВКИ.
_ANOMALY_SEEN_KEY = "anomaly_seen"
_ANOMALY_SEEN_TTL_D = 30  # старше — выбрасываем из блоба, чтобы не рос вечно


async def _recipients() -> set[int]:
    """Кому слать: доверенные операторы бота = env-whitelist (бутстрап) ∪ рантайм-таблица whitelist
    (/adduser). D9: раньше был env-only — рантайм-добавленные операторы не получали плановые
    отчёты/аномалии/advise до рестарта планировщика. Fail-closed: сбой БД ⇒ только env
    (core.access.whitelisted_ids). Scheduler — не hot-path, свежий набор на каждый запуск джобы ок."""
    from core.access import whitelisted_ids

    return await whitelisted_ids()


async def _custom_report_chats() -> set[int]:
    """§14 (P1-I): chat_id операторов с СОБСТВЕННЫМ расписанием отчёта (UserSettings.report_schedule
    непусто) — им шлёт отдельная per-chat cron-джоба (register_user_report_schedules), а глобальная
    рассылка их ПРОПУСКАЕТ (иначе дубль). Только whitelisted (env ∪ БД — D9; чужие игнорируем)."""
    wl = await _recipients()
    if not wl:
        return set()
    async with Session() as s:
        rows = (
            (
                await s.execute(
                    select(UserSettings.chat_id).where(UserSettings.report_schedule.isnot(None))
                )
            )
            .scalars()
            .all()
        )
    return {int(c) for c in rows if int(c) in wl}


def _scheduled_accounts() -> list[str]:
    """Аккаунты для плановых отчётов/аномалий (§8): мутационный allow-list ∪ env read-list ∪
    дочерние, ОБНАРУЖЕННЫЕ обходом MCC на старте (discover_read_children — полный мульти-аккаунт).
    Пусто ⇒ дефолт [Draft] (поведение как раньше — единственный аккаунт). READ-ONLY: scheduler
    только читает (golden rule #3)."""
    from ads.client import discovered_read_children

    accts = settings.allowed_customer_ids | settings.read_customer_ids | discovered_read_children()
    return sorted(accts) if accts else [DRAFT_ACCOUNT_ID]


async def _account_period(client, acct: str, n_days: int):
    """§8 (P1-H): период последних N дней в ТАЙМЗОНЕ аккаунта (а не host-local) — раньше плановый
    дайджест/аномалии считали окно по времени хоста, что для аккаунтов далеко от TZ хоста смещало
    границы дней. Логика одна на весь проект — reports.tz (TZ best-effort: сбой → host-дата)."""
    from reports.tz import account_period

    return await account_period(client, acct, last_n_days(n_days), label=f"sched_tz_{acct}")


def _account_health(report):
    """Аудит по УЖЕ собранному отчёту — engine-only: НИ ОДНОГО доп. чтения Google Ads. Планировщик
    ходит веером по всем аккаунтам, и полный gather_audit (23 чтения × N аккаунтов × каждый прогон)
    съел бы квоту — поэтому здесь только чеки, которым хватает отчёта (остальные молчат честно, а не
    говорят «в норме»: GR8). Косметика: сбой не валит дайджест.

    ⛔ Снимок тренда (record_snapshot) отсюда НЕ пишем. Индекс account_health_snapshot —
    (customer_id, snapshot_date, period_days), без измерения «режим сбора», а score_model_version у
    engine-only и полного /audit ОДИНАКОВ (хэш конфигурации модели, не полноты данных) ⇒ upsert
    дайджеста молча затёр бы честную базу /audit, и клиент увидел бы выдуманное «▲ +15 за неделю».
    Дайджесты — читатели снимков, не писатели."""
    try:
        from audit.engine import build_audit

        return build_audit(report)
    except Exception:  # noqa: BLE001 — здоровье необязательно, отчёт важнее
        return None


def _health_line(result, lang: str) -> str:
    """Строка «🩺 Здоровье: N/100 (B) · под риском X · /audit» — тот же рендер, что у /audit и
    /report (audit.render.audit_headline): новой прозы ноль, расхождению текстов взяться неоткуда.
    Пустой/мёртвый аккаунт → '' (без фейковых 100)."""
    if result is None:
        return ""
    try:
        from audit.render import audit_headline

        return audit_headline(result, lang)
    except Exception:  # noqa: BLE001
        return ""


def _report_delta_line(report, currency: str, lang: str) -> str:
    """3.3: дельта CPA/ROAS к предыдущему периоду — КОД считает (golden rule #4), без FX.
    summary_text уже даёт ▲/▼ по расходу/кликам/конверсиям; здесь — юнит-экономика, которой в
    сводке нет. Нет сравнения (prev_totals=None) или нечего считать → '' (косметика)."""
    t, p = getattr(report, "totals", None), getattr(report, "prev_totals", None)
    if t is None or p is None:
        return ""

    def _cpa(m) -> float | None:
        try:
            conv = float(getattr(m, "conversions", 0.0) or 0.0)
            cost = float(getattr(m, "cost_micros", 0) or 0) / 1_000_000
        except (TypeError, ValueError):
            return None
        return (cost / conv) if conv > 0 else None

    def _roas(m) -> float | None:
        try:
            val = float(getattr(m, "conv_value", 0.0) or 0.0)
            cost = float(getattr(m, "cost_micros", 0) or 0) / 1_000_000
        except (TypeError, ValueError):
            return None
        return (val / cost) if cost > 0 and val > 0 else None

    code = f" {currency}" if currency else ""
    parts: list[str] = []
    cur_cpa, prev_cpa = _cpa(t), _cpa(p)
    if cur_cpa is not None or prev_cpa is not None:
        left = f"{prev_cpa:.2f}" if prev_cpa is not None else "—"
        right = f"{cur_cpa:.2f}{code}" if cur_cpa is not None else "—"
        parts.append(f"CPA {left} → {right}")
    cur_roas, prev_roas = _roas(t), _roas(p)
    if cur_roas is not None or prev_roas is not None:
        left = f"{prev_roas:.2f}" if prev_roas is not None else "—"
        right = f"{cur_roas:.2f}" if cur_roas is not None else "—"
        parts.append(f"ROAS {left} → {right}")
    if not parts:
        return ""
    suffix = "vs prev. period" if lang == "en" else "к пред. периоду"
    return " · ".join(parts) + f" ({suffix})"


def _top_findings_block(health, lang: str) -> str:
    """3.3: топ-2 находки аудита из УЖЕ посчитанного AuditResult (0 доп. чтений Google Ads) — тот
    же текст, что в карточке /audit (audit.render.finding_text: находки ранжированы worst-first).
    Пусто/сбой → '' (косметика: дайджест важнее)."""
    findings = getattr(health, "findings", None) if health is not None else None
    if not findings:
        return ""
    try:
        from audit.render import finding_text
        from core import i18n

        cur = getattr(health, "currency", "") or ""
        lines = [ln for f in findings[:2] if (ln := finding_text(f, lang, cur))]
        if not lines:
            return ""
        title = i18n.t("sched_digest_findings_title", lang)
        return title + "\n" + "\n".join(f"• {ln}" for ln in lines)
    except Exception:  # noqa: BLE001
        return ""


def _quiet_account(info: dict | None, fresh_alerts, applied_n: int) -> bool:
    """3.3 (тихий режим, решение владельца 2026-07-17): аккаунт «без событий» — нет расходов И
    кликов И свежих аномалий И применённых нами мутаций за сутки И аудиту нечего сказать. Такие
    схлопываются в одну строку-счётчик вместо блока цифр. Неизвестные цифры (сломанный/фейковый
    отчёт) — НЕ тихий: блок (в т.ч. «⚠️ отчёт недоступен») обязан дойти."""
    if not info:
        return False
    cost, clicks = info.get("cost"), info.get("clicks")
    if cost is None or clicks is None or cost > 0 or clicks > 0:
        return False
    if fresh_alerts or applied_n:
        return False
    health = info.get("health")
    return health is None or not getattr(health, "findings", None)


async def _auction_snapshot_age(acct: str) -> int | None:
    """3.4: возраст (дни) последнего среза /competitors; None — импортов не было или БД недоступна
    (нудж не показываем — кто фичей не пользуется, того не спамим). Дата хоста, не TZ аккаунта:
    нуджу с порогом «месяц» точная граница суток не нужна."""
    try:
        from db.competitors import latest_snapshot

        snap = await latest_snapshot(acct)
        if snap is None:
            return None
        return (date.today() - date.fromisoformat(snap.snapshot_date)).days
    except Exception:  # noqa: BLE001 — нудж — довесок, отчёт важнее
        return None


async def _digest_action(chat_id: int, lang: str, accts: list[str], acct_info: dict):
    """3.3: одна actionable-секция в плановом дайджесте — топ-рекомендация с ИСПОЛНИМОЙ неденежной
    операцией (тот же двойной гейт, что в /advise: allow-list ONE_TAP_OPS + исполнимость params,
    advisor.apply.one_tap_op). Кнопка лишь СТАРТУЕТ confirm-гейт по тапу пользователя —
    proposal из scheduler НЕ создаётся (golden rule #1/#3). Анти-дубль: операторам с
    advise_proactive карточки уже шлёт run_recommendations_digest — им кнопку не кладём.
    Всё best-effort: любой сбой → ('', None), дайджест уходит без кнопки."""
    try:
        if chat_id in await _advise_proactive_chats({chat_id}):
            return "", None
        from advisor import service as advisor_service
        from advisor import store as advisor_store
        from advisor.apply import one_tap_op
        from advisor.rules import rank_cross_account
        from core import i18n
        from scheduler import delivery

        items: list[tuple] = []  # (acct, currency, total_cost, rec) — формат rank_cross_account
        for acct in accts:
            info = acct_info.get(acct) or {}
            report = info.get("report")
            if report is None:
                continue
            try:
                rec_set = await advisor_service.build_recommendations(
                    chat_id,
                    acct,
                    source="scheduler",
                    lang=lang,
                    use_llm=False,
                    persist=False,
                    report=report,
                )
            except Exception:  # noqa: BLE001 — совет-довесок по одному аккаунту, не критичен
                continue
            total = float(info.get("cost") or 0.0)
            items.extend(
                (acct, info.get("currency", ""), total, r)
                for r in rec_set.recs
                if one_tap_op(r) is not None
            )
        top = rank_cross_account(items, top_n=1)
        if not top:
            return "", None
        acct, _cur, _total, rec = top[0]
        # Персист ТОЛЬКО показанной (rec_uid для кнопок 👍/👎/🙈/apply — как в advise-дайджесте).
        await advisor_store.record_recommendations(chat_id, acct, [rec], source="scheduler")
        body = (rec.body or "").strip()
        if len(body) > 400:
            body = body[:400] + "…"
        text = i18n.t("sched_digest_apply_now", lang) + "\n• " + body
        kb = delivery.markup(delivery.ADVISE_FEEDBACK, rec.rec_uid, lang, apply_op=one_tap_op(rec))
        return text, kb
    except Exception:  # noqa: BLE001 — кнопка-довесок, дайджест важнее
        return "", None


async def run_scheduled_report(bot, only_chat: int | None = None) -> None:
    """Плановый отчёт (последние N дн.) по ВСЕМ разрешённым на чтение аккаунтам (§8) — ОДИН дайджест
    на оператора (анти-спам, не N сообщений). READ-ONLY. Сбой одного аккаунта не валит остальные
    (capture_exception per-account) и не топит рассылку.

    §14 (P1-I): only_chat — персональная джоба оператора с СОБСТВЕННЫМ расписанием
    (register_user_report_schedules): шлём только ему. only_chat=None — ГЛОБАЛЬНАЯ джоба: шлём всем
    операторам БЕЗ персонального расписания (те получают отчёт своей per-chat джобой — без дубля).

    3.3 (2026-07-17, замечание 7 «дайджест — простыня цифр»): поверх health+summary добавлены
    дельты CPA/ROAS, «🔥 что горит» (detect_anomalies по per-chat порогам /alerts, дедуп с
    run_anomaly_check через общий блоб _ANOMALY_SEEN_KEY), топ-2 находки аудита (из уже посчитанного
    _account_health — 0 доп. чтений), строка «применено изменений за сутки», сортировка блоков по
    деньгам-под-риском, тихий режим (пустые аккаунты — одной строкой) и одна actionable-кнопка
    (_digest_action; кнопка лишь стартует confirm-гейт по тапу — мутаций из scheduler нет).
    Дайджест стал per-chat (пороги/кулдаун/кнопка персональны) — кэш (lang, accounts) снят."""
    with request_scope("scheduler:report"):  # §15: корреляция логов джобы по request_id
        if only_chat is not None:
            recipients = {only_chat} if only_chat in await _recipients() else set()
        else:
            recipients = await _recipients() - await _custom_report_chats()
        if not recipients:
            log.info(
                "scheduler: получателей нет — пропуск планового отчёта (only_chat=%s)", only_chat
            )
            return
        accounts = _scheduled_accounts()
        if not accounts:
            log.info("scheduler: нет аккаунтов для отчёта (allow/read-list пусты) — пропуск")
            return
        # 3H: блоки собираем per-lang (у операторов может быть RU и EN): summary_text локализуется,
        # а валюта аккаунта дочитывается per-account (раньше суммы шли голыми числами без кода).
        from core import i18n

        langs = {i18n.get_lang(chat_id) for chat_id in recipients} or {"ru"}
        # 3.3: пороги аномалий и применённые за сутки мутации — best-effort (сбой БД не валит отчёт).
        try:
            thr_by_chat = await _thresholds_by_chat(recipients)
        except Exception:  # noqa: BLE001
            thr_by_chat = {}
        try:
            from confirm.store import audit_applied_by_account_since

            applied_by_acct = await audit_applied_by_account_since(1)
        except Exception:  # noqa: BLE001
            applied_by_acct = {}
        # C2: блоки per-АККАУНТ (не плоским списком) — дайджест собирается ПОД получателя из
        # аккаунтов, доступных именно ему (enforced-режим: метрики чужого клиента не утекают).
        blocks: dict[str, dict[str, str]] = {lang: {} for lang in langs}
        acct_info: dict[str, dict] = {}  # 3.3: report/currency/health/at_risk/cost для сборки
        for acct in accounts:
            tok = set_context(customer_id=acct)  # §8: ошибки/логи этого аккаунта атрибутируются
            try:
                # per-account (Фаза 3: свой токен/MCC из oauth_tokens); холодная сборка вне loop
                client = await build_client_async(acct)
                period = await _account_period(client, acct, REPORT_WINDOW_DAYS)  # §8 (P1-H): TZ
                currency = ""
                try:  # валюта best-effort: без неё показываем числа без кода (как раньше)
                    from ads.read import account_currency

                    currency = await run_ads_read_call(
                        account_currency, client, acct, label=f"sched_cur_{acct}"
                    )
                except Exception:  # noqa: BLE001
                    currency = ""
                report = await build_account_report_async(client, acct, period, currency=currency)
                # Здоровье считаем ОДИН раз на аккаунт (движок — чистая функция, ctx не зависит от
                # языка), рендерим per-lang. Как в /report: сначала «что горит», потом цифры.
                health = _account_health(report)
                cost = clicks = None
                t = getattr(report, "totals", None)
                if t is not None:
                    try:  # цифры для тихого режима/сортировки; кривой отчёт → None (не тихий)
                        cost = float(getattr(t, "cost_micros", 0) or 0) / 1_000_000
                        clicks = int(getattr(t, "clicks", 0) or 0)
                    except (TypeError, ValueError):
                        cost = clicks = None
                acct_info[acct] = {
                    "report": report,
                    "currency": currency,
                    "health": health,
                    "at_risk": (
                        float(getattr(health, "at_risk", 0.0) or 0.0) if health is not None else 0.0
                    ),
                    "cost": cost,
                    "clicks": clicks,
                }
                auction_age = await _auction_snapshot_age(acct)  # 3.4: нудж про старый срез
                for lang in langs:
                    hl = _health_line(health, lang)
                    parts = [summary_text(report, lang)]
                    delta = _report_delta_line(report, currency, lang)
                    if delta:
                        parts.append(delta)
                    tf = _top_findings_block(health, lang)
                    if tf:
                        parts.append(tf)
                    if auction_age is not None and auction_age > _AUCTION_STALE_DAYS:
                        parts.append(i18n.t("sched_digest_auction_stale", lang, d=auction_age))
                    blocks[lang][acct] = (hl + "\n\n" if hl else "") + "\n".join(parts)
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
                        blocks[lang][acct] = f"⚠️ Аккаунт {acct}: отчёт недоступен (см. /diag)"
            finally:
                reset_context(tok)
        if not any(blocks.values()):
            return
        from core.access import accessible_accounts_for_user

        now = datetime.now(timezone.utc)
        cooldown_h = float(settings.anomaly_cooldown_hours)
        for chat_id in recipients:
            lang = i18n.get_lang(chat_id)
            allowed = await accessible_accounts_for_user(chat_id, accounts)
            visible = [a for a in allowed if a in blocks.get(lang, {})]
            if not visible:
                continue  # получателю нечего показать — не шлём пустой заголовок
            # 3.3: «что горит» per-chat (пороги /alerts) + дедуп против 6-часовой run_anomaly_check:
            # общий блоб «что уже доставлено» (acct:kind → время), кулдаун и TTL те же.
            try:
                seen = await _ui_pref_blob(chat_id, _ANOMALY_SEEN_KEY)
            except Exception:  # noqa: BLE001
                seen = None
            fresh_by_acct: dict[str, list] = {}
            for acct in visible:
                info = acct_info.get(acct)
                if info is None:
                    continue
                try:
                    alerts = detect_anomalies(
                        info["report"].totals,
                        info["report"].prev_totals,
                        _effective_thresholds(thr_by_chat.get(chat_id), acct),
                        currency=info["currency"],
                    )
                except Exception:  # noqa: BLE001 — аномалии-довесок, отчёт важнее
                    alerts = []
                fresh = _anomaly_fresh(seen, acct, alerts, now, cooldown_h)
                if fresh:
                    fresh_by_acct[acct] = fresh
            # 3.3 тихий режим: пустые аккаунты — одной строкой-счётчиком, не блоком цифр.
            loud: list[str] = []
            quiet_n = 0
            for acct in visible:
                if _quiet_account(
                    acct_info.get(acct), fresh_by_acct.get(acct), applied_by_acct.get(acct, 0)
                ):
                    quiet_n += 1
                else:
                    loud.append(acct)
            header = "🗓 Scheduled report" if lang == "en" else "🗓 Плановый отчёт"
            if not loud:  # тишина везде — одно короткое сообщение вместо простыни
                try:
                    await transport.send_bot_message(
                        bot,
                        chat_id,
                        header + "\n" + i18n.t("sched_digest_all_quiet", lang, n=quiet_n),
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "scheduler: не доставлено в %s: %s: %s", chat_id, type(e).__name__, e
                    )
                continue
            # 3.3: сортировка по деньгам-под-риском (fallback — расход): горящее сверху.
            loud.sort(
                key=lambda a: (
                    -float((acct_info.get(a) or {}).get("at_risk") or 0.0),
                    -float((acct_info.get(a) or {}).get("cost") or 0.0),
                )
            )
            body_blocks: list[str] = []
            for acct in loud:
                b = blocks[lang][acct]
                fresh = fresh_by_acct.get(acct)
                if fresh:
                    b = (
                        i18n.t("sched_digest_hot_title", lang)
                        + "\n"
                        + "\n".join(f"• {_alert_line(a, lang)}" for a in fresh)
                        + "\n\n"
                        + b
                    )
                body_blocks.append(b)
            # C2: счётчик «применено» — только по ВИДИМЫМ этому оператору аккаунтам (не глобальный).
            applied_n = sum(applied_by_acct.get(a, 0) for a in visible)
            digest = header
            if applied_n:
                digest += "\n" + i18n.t("sched_digest_applied", lang, n=applied_n)
            digest += "\n\n" + "\n\n———\n\n".join(body_blocks)
            if quiet_n:
                digest += "\n\n" + i18n.t("sched_digest_quiet", lang, n=quiet_n)
            # 3.3: actionable-секция считается ДО усечения — резервируем ей место, иначе клавиатура
            # приезжала бы без своего текста (или сообщение пробивало телеграм-лимит 4096).
            action_txt, markup = await _digest_action(chat_id, lang, loud, acct_info)
            limit = _DIGEST_MAX - (len(action_txt) + 2 if action_txt else 0)
            if len(digest) > limit:  # анти-спам: не дробим на N сообщений, усечём
                digest = digest[:limit] + "\n\n…(усечено — полный отчёт по аккаунту: /report)"
            if action_txt:
                digest += "\n\n" + action_txt
            try:
                await transport.send_bot_message(bot, chat_id, digest, reply_markup=markup)
            except Exception as e:  # один недоступный чат не должен ронять рассылку
                log.warning("scheduler: не доставлено в %s: %s: %s", chat_id, type(e).__name__, e)
                continue  # не доставили → не отмечаем аномалии как показанные
            if fresh_by_acct:
                try:
                    await _save_ui_pref_blob(
                        chat_id,
                        _ANOMALY_SEEN_KEY,
                        _anomaly_seen_updated(seen, list(fresh_by_acct.items()), now),
                    )
                except Exception as e:  # noqa: BLE001 — БД-сбой не роняет рассылку (в худшем повтор)
                    log.warning("scheduler: анти-спам аномалий не сохранён (%s)", type(e).__name__)


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


def _effective_thresholds(thr: dict | None, acct: str) -> dict | None:
    """§14 (P1-J): пороги аномалий для КОНКРЕТНОГО аккаунта — chat-дефолты, поверх которых наложен
    опциональный per-account оверлей `alert_thresholds["per_account"][customer_id]`. Позволяет
    тюнинговать шумный high-spend аккаунт иначе, чем тихий (особенно min_spend в мульти-валютном
    портфеле). Нет оверлея → плоские chat-пороги (обратная совместимость). Ключ `per_account` в
    detect_anomalies не течёт (он не порог)."""
    if not thr:
        return thr
    base = {k: v for k, v in thr.items() if k != "per_account"}
    overlay = (thr.get("per_account") or {}).get(acct)
    if isinstance(overlay, dict) and overlay:
        return {**base, **overlay}
    return base or None


def _alert_line(a, lang: str) -> str:
    """C3: рендер алерта НА ЯЗЫКЕ получателя — anomaly.Alert структурный (kind + params,
    числа отформатированы кодом), тексты живут в core/i18n (ключ anomaly_<kind>). Раньше
    Alert.message был RU-литералом и уходил EN-операторам смешанным RU/EN."""
    from core import i18n

    return i18n.t(f"anomaly_{a.kind}", lang, **a.params)


def _format_alerts_multi(account_alerts: list[tuple[str, list]], lang: str = "ru") -> str:
    """Единое сообщение об аномалиях по НЕСКОЛЬКИМ аккаунтам (§8, анти-спам: один месседж на
    оператора, а не по сообщению на аккаунт). Каждый блок помечен аккаунтом. C3: и рамка,
    и тексты алертов локализованы per-recipient (суммы несут код валюты)."""
    if lang == "en":
        parts = [f"🔔 <b>Anomalies</b> (last {ANOMALY_WINDOW_DAYS}d vs previous period):"]
    else:
        parts = [f"🔔 <b>Аномалии</b> (за {ANOMALY_WINDOW_DAYS} дн. к предыдущему периоду):"]
    for acct, alerts in account_alerts:
        parts.append(f"\n<b>{'Account' if lang == 'en' else 'Аккаунт'} {acct}</b>:")
        parts.extend("• " + _alert_line(a, lang) for a in alerts)
    if lang == "en":
        parts.append("\n<i>This is only a signal — I never change anything myself.</i>")
    else:
        parts.append("\n<i>Это только сигнал — сам я ничего не меняю. Реши и дай команду.</i>")
    return "\n".join(parts)


def _iso_ts(raw: object) -> datetime | None:
    """ISO-строка → aware datetime (UTC). Мусор/пусто → None (кулдауна нет)."""
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def _anomaly_fresh(
    blob: dict | None, acct: str, alerts: list, now: datetime, cooldown_h: float
) -> list:
    """Отфильтровать алерты, уже доставленные по этому (аккаунт, kind) внутри окна кулдауна.
    Чистая функция (тестируема без БД). Пустой блоб ⇒ шлём всё (первый раз)."""
    fresh = []
    for a in alerts:
        ts = _iso_ts((blob or {}).get(f"{acct}:{a.kind}"))
        if ts is not None and (now - ts).total_seconds() < cooldown_h * 3600:
            continue
        fresh.append(a)
    return fresh


def _anomaly_seen_updated(
    blob: dict | None, delivered: list[tuple[str, list]], now: datetime
) -> dict:
    """Новый блоб «что доставлено»: отметки now по доставленным (acct, kind) + чистка старше TTL."""
    out = {
        k: v
        for k, v in (blob or {}).items()
        if (ts := _iso_ts(v)) is not None and (now - ts).days < _ANOMALY_SEEN_TTL_D
    }
    stamp = now.isoformat()
    for acct, alerts in delivered:
        for a in alerts:
            out[f"{acct}:{a.kind}"] = stamp
    return out


async def run_anomaly_check(bot) -> None:
    """Сравнение последних N дн. с предыдущими ПО ВСЕМ разрешённым аккаунтам (§8); алерт при росте
    расхода/падении конверсий. Пороги — per-chat из UserSettings.alert_thresholds (иначе дефолтные).
    Анти-спам: ОДИН месседж на оператора со всеми его аккаунтами. READ-ONLY (golden rule #3):
    fetch_totals через run_ads_read_call (ретрай TimeoutError/транзиентных), без мутаций."""
    with request_scope("scheduler:anomaly"):  # §15: корреляция логов джобы по request_id
        recipients = await _recipients()
        if not recipients:
            return
        accounts = _scheduled_accounts()
        if not accounts:
            return
        # cur/prev (+валюта, 3H) на каждый аккаунт; сбой одного фиксируем и пропускаем.
        metrics: dict[str, tuple] = {}
        for acct in accounts:
            tok = set_context(customer_id=acct)  # §8: per-account атрибуция ошибок/логов
            try:
                client = await build_client_async(acct)
                period = await _account_period(client, acct, ANOMALY_WINDOW_DAYS)  # §8 (P1-H): TZ
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
        from core import i18n

        thresholds = await _thresholds_by_chat(recipients)
        from core.access import accessible_accounts_for_user

        # Пороги per-chat → для каждого получателя собираем алерты по ЕГО аккаунтам в ОДНО сообщение.
        now = datetime.now(timezone.utc)
        cooldown_h = float(settings.anomaly_cooldown_hours)
        for chat_id in recipients:
            thr = thresholds.get(chat_id)
            # C2: метрики аккаунта не уходят оператору без доступа к нему (enforced-режим).
            allowed = set(await accessible_accounts_for_user(chat_id, list(metrics)))
            seen = await _ui_pref_blob(chat_id, _ANOMALY_SEEN_KEY)  # анти-спам: что уже слали
            acct_alerts: list[tuple[str, list]] = []
            for acct, (cur, prev, currency) in metrics.items():
                if acct not in allowed:
                    continue
                # §14 (P1-J): пороги с per-account оверлеем поверх chat-дефолтов.
                alerts = detect_anomalies(
                    cur, prev, _effective_thresholds(thr, acct), currency=currency
                )
                # Кулдаун: тот же (аккаунт, kind) не повторяем каждые anomaly_interval_hours всю
                # неделю окна — иначе оператор перестаёт читать алерты (см. _ANOMALY_SEEN_KEY).
                alerts = _anomaly_fresh(seen, acct, alerts, now, cooldown_h)
                if alerts:
                    acct_alerts.append((acct, alerts))
            if not acct_alerts:
                continue
            try:
                await transport.send_bot_message(
                    bot,
                    chat_id,
                    _format_alerts_multi(acct_alerts, lang=i18n.get_lang(chat_id)),
                    parse_mode="HTML",
                )
            except Exception as e:  # один недоступный чат не должен ронять остальные
                log.warning(
                    "scheduler: алерт не доставлен в %s: %s: %s", chat_id, type(e).__name__, e
                )
                continue  # не доставили → не отмечаем как показанное (иначе алерт потеряется)
            try:
                await _save_ui_pref_blob(
                    chat_id, _ANOMALY_SEEN_KEY, _anomaly_seen_updated(seen, acct_alerts, now)
                )
            except Exception as e:  # noqa: BLE001 — БД-сбой не роняет рассылку (в худшем случае повтор)
                log.warning("scheduler: анти-спам аномалий не сохранён (%s)", type(e).__name__)


# Р6: курсор «уже показанных» правок аккаунта — per-chat блоб {customer_id: {"at": …, "tz": …}}.
# В ui_prefs, а не в памяти модуля (как у error-алертов): пропустить чужую правку бюджета из-за
# рестарта нельзя — «отмена возвращает настройку, но не потраченные деньги».
#
# Рядом с отметкой хранится ТАЙМЗОНА, в которой она снята, и это не украшение: `changed_at` Google
# отдаёт местным временем аккаунта БЕЗ указания зоны, поэтому смена таймзоны аккаунта сдвигает всю
# шкалу разом. Kyiv → New_York — минус 7 часов: новые события получают местное время МЕНЬШЕ старого
# курсора и молча читаются как «уже показанные». Не совпала зона — курсор недействителен, сбрасываем
# в базлайн (пропустить один цикл честнее, чем потерять сутки правок незаметно).
_EXT_CHANGES_SEEN_KEY = "ext_changes_seen"
# Сколько строк журнала берём за прогон. Читать надо ШИРЕ, чем показываем: `ORDER BY … DESC` + LIMIT
# отдаёт самые свежие, а фильтр канала (`_is_external_change`) применяется уже ПОСЛЕ выборки — на
# аккаунте с активным API-клиентом узкий лимит выел бы весь бюджет служебным шумом, и ровно та
# ручная правка бюджета, ради которой всё делается, в выборку не попала бы. 2000 при потолке ресурса
# 10000: с запасом для активного аккаунта и без выкачивания журнала целиком. Упёрлись в потолок —
# пишем warning: усечение должно быть видно, а не выглядеть как «правок больше нет».
_EXT_CHANGES_READER_LIMIT = 2000


def _is_external_change(row) -> bool:
    """Правка сделана НЕ через API — то есть точно не нашим слоем.

    Правки по каналу API отсекаем целиком, и это сознательный размен: наши собственные мутации
    проходят гейт и уже репортятся из audit-row (правило 15), а отличить их от правок ЧУЖОГО
    API-клиента нам нечем — `change_event` не несёт признака приложения, а в `audit_log` нет
    `resource_name`, по которому можно было бы сопоставить. Альтернатива «слать всё» означала бы
    алерт «кто-то поменял бюджет» на каждую собственную операцию — оператор перестанет их читать, и
    Р6 закроется формально, а не по существу. Сколько событий отсеяно, джоба пишет в лог: тихого
    усечения тут нет."""
    return not row.via_api


async def _account_tz(client, acct: str) -> str:
    """Таймзона аккаунта, best-effort ('' — не прочитана). Тот же кэш, что у `account_today`
    (`ads.read._TIMEZONE_CACHE`), поэтому второго GAQL за прогон не стоит. Провал чтения зоны не
    имеет права уронить алерт: без зоны курсор просто помечается неизвестной."""
    from ads.read import account_timezone

    try:
        tz = await run_ads_read_call(
            account_timezone, client, acct, account=acct, label=f"extchg_tz_{acct}"
        )
        return str(tz or "")
    except Exception:  # noqa: BLE001 — зона необязательна, правки важнее
        return ""


def _cursor_entry(entry, tz: str) -> tuple[str, bool]:
    """Разбор записи курсора → (отметка, снята_ли_в_ЭТОЙ_таймзоне).

    Запись без зоны (формат до её появления) принимаем как свою: переигрывать историю из-за смены
    ФОРМАТА нечестно, речь о смене ЧАСОВОГО ПОЯСА."""
    if isinstance(entry, dict):
        return str(entry.get("at") or ""), str(entry.get("tz") or "") == str(tz or "")
    return str(entry or ""), True


def _split_line_budget(fresh: list[tuple[str, list]]) -> tuple[list[tuple[str, list]], int]:
    """Разделить бюджет строк дайджеста МЕЖДУ аккаунтами и вернуть (батч, сколько осталось).

    Две вещи, каждая из которых по отдельности даёт молчаливую потерю правок:

    1. **Поровну, минимум по строке каждому.** Общий потолок «первые N строк» + курсор, который
       двигался бы на максимум окна, — это гарантированное голодание: аккаунты перебираются в
       устойчивом порядке (`sorted`), значит хвостовой аккаунт не показывается НИКОГДА, а его
       правки при этом помечаются показанными. Ровно тот отказ, ради которого заведён Р6.
    2. **Берём САМЫЕ СТАРЫЕ из свежих**, а не самые новые. Курсор двигается на максимум
       ПОКАЗАННОГО, поэтому непоказанный хвост обязан лежать ВЫШЕ курсора — иначе он окажется
       «старше показанного» и выпадет навсегда. Так очередь честно разбирается с головы."""
    if not fresh:
        return [], 0
    from core.texts import EXT_CHANGES_MAX_LINES

    share = max(1, EXT_CHANGES_MAX_LINES // len(fresh))
    batch: list[tuple[str, list]] = []
    pending = 0
    for acct, evs in fresh:
        head = sorted(evs, key=lambda e: str(e.changed_at))[:share]
        pending += len(evs) - len(head)
        batch.append((acct, head))
    return batch, pending


def _fresh_changes(events: list, cursor: str) -> list:
    """События строго новее курсора. `changed_at` — 'YYYY-MM-DD HH:MM:SS.ffffff' фиксированной
    ширины, поэтому лексикографическое сравнение здесь совпадает с хронологическим (парсить в
    datetime не нужно и вредно: таймзона аккаунта в строке не указана). Пустой курсор = базлайн,
    отдаём пусто — историей на первом прогоне не спамим (как run_error_alerts).

    Известная граница (осознанная, не дефект по недосмотру): время местное для аккаунта, и при
    ПЕРЕВОДЕ ЧАСОВ НАЗАД повторённый час читается как «старше курсора» — правки этого часа алертом
    не пойдут. Раз в год, до часа, только у аккаунтов с DST. Закрывать это вторым слоем дедупа
    (набор ключей показанных событий в блобе) дороже, чем стоит: журнал остаётся доступен
    инструментом `get_account_changes` и в веб-интерфейсе Google. Зафиксировано в RISK_REGISTER Р6.
    Назад курсор при этом не откатывается: события выпадают из окна строго от старых к новым, а
    значит max по окну не может стать меньше уже сохранённого — повтора уведомлений не будет."""
    if not cursor:
        return []
    return [e for e in events if str(e.changed_at) > cursor]


async def run_external_change_alerts(bot) -> int:
    """Р6: плановый алерт о правках, сделанных в аккаунте МИМО бота. READ-ONLY (golden rule #3).

    Требование Р6 дословно: «алерты о произошедших изменениях шлёт наш код по расписанию, а не
    агент». Отсюда джоба, а не инструмент: агент читает журнал, когда его спросили, — а ошибка,
    сделанная в аккаунте ночью, должна найтись сама.

    Дедуп — per-chat курсор `_EXT_CHANGES_SEEN_KEY` (последний ПОКАЗАННЫЙ `changed_at` на аккаунт
    плюс таймзона, в которой отметка снята). Первый прогон по (чат, аккаунт) ТОЛЬКО ставит базлайн:
    рассылать недельную историю на старте — верный способ научить оператора не читать эти сообщения.

    Курсор двигается ПОСЛЕ успешной доставки и ровно на ПОКАЗАННОЕ (BZ-3) — два разных требования, и
    второе не менее важно: бюджет строк делится между аккаунтами (`_split_line_budget`), поэтому
    непоказанный хвост обязан остаться выше курсора и прийти следующим циклом. Помечать показанным
    то, что не поместилось в сообщение, — это молчаливая потеря чужой правки бюджета.

    Возвращает число разосланных сообщений (0 — никому не слали/нечего слать)."""
    with request_scope("scheduler:ext-changes"):  # §15: корреляция логов джобы по request_id
        recipients = await _recipients()
        if not recipients:
            return 0
        accounts = _scheduled_accounts()
        if not accounts:
            return 0
        window = int(settings.external_changes_window_days)
        # Окно шире интервала джобы НАМЕРЕННО: после простоя/рестарта событие не должно выпасть из
        # выборки. Дубли отсекает курсор, а не узость окна.
        from reports.queries import fetch_change_events
        from reports.tz import account_today

        by_account: list[tuple[str, list, str]] = []
        for acct in accounts:
            tok = set_context(customer_id=acct)  # §8: per-account атрибуция ошибок/логов
            try:
                client = await build_client_async(acct)
                tz = await _account_tz(client, acct)
                today = await account_today(client, acct, label=f"extchg_tz_{acct}")
                events = await run_ads_read_call(
                    fetch_change_events,
                    client,
                    acct,
                    days=window,
                    today=today,
                    limit=_EXT_CHANGES_READER_LIMIT,
                    account=acct,
                    label=f"extchg_{acct}",
                )
                if len(events) >= _EXT_CHANGES_READER_LIMIT:
                    log.warning(
                        "scheduler ext-changes: %s — журнал упёрся в потолок ридера (%d строк за "
                        "%d дн.), часть правок в этот прогон не попала: сузьте окно",
                        acct,
                        _EXT_CHANGES_READER_LIMIT,
                        window,
                    )
                external = [e for e in events if _is_external_change(e)]
                if len(external) != len(events):
                    log.info(
                        "scheduler ext-changes: %s — %d событий по каналу API не показываем "
                        "(наши мутации репортятся из audit-row)",
                        acct,
                        len(events) - len(external),
                    )
                if external:
                    by_account.append((acct, external, tz))
            except Exception as e:  # сеть/доступ/SDK — остальные аккаунты живут (как в аномалиях)
                if is_account_access_error(e):
                    log.info(
                        "scheduler ext-changes: аккаунт %s недоступен на чтение (ожидаемо): %s",
                        acct,
                        type(e).__name__,
                    )
                else:
                    await capture_exception(e, where=f"scheduler:ext-changes:{acct}")
            finally:
                reset_context(tok)
        if not by_account:
            return 0

        from core import i18n, texts
        from core.access import accessible_accounts_for_user

        sent = 0
        for chat_id in sorted(recipients):
            # C2: правки аккаунта не уходят оператору без доступа к нему (enforced-режим).
            allowed = set(
                await accessible_accounts_for_user(chat_id, [a for a, _, _ in by_account])
            )
            seen = dict(await _ui_pref_blob(chat_id, _EXT_CHANGES_SEEN_KEY) or {})
            fresh: list[tuple[str, list]] = []
            cursor: dict[str, dict[str, str]] = {}
            for acct, events, tz in by_account:
                if acct not in allowed:
                    continue
                entry = seen.get(acct)
                at, same_tz = _cursor_entry(entry, tz)
                if entry is None:
                    new = []  # базлайн: первый прогон по (чат, аккаунт) историей не спамит
                elif not same_tz:
                    # Зона аккаунта сменилась ⇒ шкала `changed_at` сдвинулась целиком и курсор
                    # сравнивать не с чем. Показываем окно заново: дубль здесь дешевле пропуска,
                    # а очередь разберётся по `share` за цикл.
                    log.warning(
                        "scheduler ext-changes: %s — таймзона аккаунта сменилась, курсор чата %s "
                        "недействителен, показываем окно заново",
                        acct,
                        chat_id,
                    )
                    new = list(events)
                else:
                    new = _fresh_changes(events, at)
                if new:
                    fresh.append((acct, new))
                else:  # показывать нечего ⇒ отметку можно ставить сразу на максимум окна
                    cursor[acct] = {"at": max(str(e.changed_at) for e in events), "tz": tz}
            batch, pending = _split_line_budget(fresh)
            tz_of = {a: t for a, _, t in by_account}
            for acct, head in batch:
                # Ровно на показанное: непоказанный хвост обязан остаться ВЫШЕ курсора (BZ-3 —
                # двигаем чек-поинт только по тому, что реально доставлено).
                cursor[acct] = {"at": max(str(e.changed_at) for e in head), "tz": tz_of[acct]}
            if not cursor:
                continue
            if batch:
                text = texts.fmt_external_changes(
                    batch, lang=i18n.get_lang(chat_id), pending=pending
                )
                try:
                    # Нарезка по строкам: батч ограничен строками, а не символами, и упереться в
                    # 4096 он может (длинные FieldMask). Недоставленный дайджест из-за длины —
                    # это ровно «алерт молчит навсегда»: курсор бы не двигался, и следующий цикл
                    # собрал бы то же самое сообщение той же длины.
                    for chunk in texts.split_by_lines(text):
                        await transport.send_bot_message(bot, chat_id, chunk, parse_mode="HTML")
                    sent += 1
                except Exception as e:  # один недоступный чат не роняет остальных
                    log.warning(
                        "scheduler: алерт о чужих правках не доставлен в %s: %s",
                        chat_id,
                        type(e).__name__,
                    )
                    continue  # курсор НЕ двигаем — повторим в следующем цикле (BZ-3)
            merged = {**seen, **cursor}
            if merged == seen:
                continue  # ничего не сдвинулось — не трогаем БД каждый цикл
            try:
                await _save_ui_pref_blob(chat_id, _EXT_CHANGES_SEEN_KEY, merged)
            except Exception as e:  # noqa: BLE001 — БД-сбой не роняет рассылку (в худшем случае дубль)
                log.warning("scheduler: курсор чужих правок не сохранён (%s)", type(e).__name__)
        return sent


# A1 (§15): чек-поинт проактивных алертов — id последней уже разосланной error_events. Модульный
# (переживает рестарт через max(id) на ПЕРВОМ прогоне: историю не спамим ретроспективно). id
# (autoincrement PK) вместо created_at — монотонный и tz-нейтральный (SQLite наивный/Postgres aware).
_error_alert_last_id: int | None = None


async def run_error_alerts(bot) -> int:
    """A1 (§15): разослать админам (ADMIN_CHAT_IDS) дайджест НОВЫХ error_events с прошлого прогона.
    error_events наполняется всегда (on_error / scheduler / handlers-A2), но был ПАССИВНЫМ — узнать
    об ошибке можно было только вызвав /diag. READ-ONLY (golden rule #3): только чтение таблицы +
    уведомление. Нет админов ⇒ no-op (fail-closed, фича opt-in). ПЕРВЫЙ прогон лишь ставит базлайн
    (max(id)) и НЕ шлёт — не спамим историей на старте. Возвращает число новых инцидентов
    (0 — если доставить не удалось никому: чек-поинт не двигается, батч повторится, BZ-3)."""
    global _error_alert_last_id
    with request_scope("scheduler:error-alerts"):  # §15: корреляция логов джобы по request_id
        from core.access import admin_ids_all

        admins = await admin_ids_all()  # P4: env ∪ рантайм-админы (/addadmin — без рестарта)
        if not admins:
            return 0
        from db.models import ErrorEvent

        async with Session() as s:
            if (
                _error_alert_last_id is None
            ):  # базлайн: алертим только НОВЫЕ после старта (не историю)
                mx = (await s.execute(select(func.max(ErrorEvent.id)))).scalar_one_or_none()
                _error_alert_last_id = int(mx or 0)
                return 0
            rows = (
                (
                    await s.execute(
                        select(ErrorEvent)
                        .where(ErrorEvent.id > _error_alert_last_id)
                        .order_by(ErrorEvent.id)
                        .limit(
                            200
                        )  # анти-флуд: за цикл не более 200, остальное подождёт следующего
                    )
                )
                .scalars()
                .all()
            )
        if not rows:
            return 0
        from core import i18n, texts

        delivered = 0
        for chat_id in admins:
            try:
                await transport.send_bot_message(
                    bot,
                    chat_id,
                    texts.fmt_error_alert(rows, lang=i18n.get_lang(chat_id)),
                    parse_mode="HTML",
                )
                delivered += 1
            except (
                Exception
            ) as e:  # один недоступный админ не роняет остальных; НЕ capture (петля!)
                log.warning(
                    "scheduler: алерт ошибок не доставлен в %s: %s", chat_id, type(e).__name__
                )
        # BZ-3: чек-поинт двигаем ТОЛЬКО после доставки хотя бы одному админу. Раньше он уходил
        # вперёд ДО рассылки — недоставленный батч не переотправлялся уже никогда (чек-поинт
        # in-memory, о потере можно было узнать только догадавшись позвать /diag). Доставлено хоть
        # кому-то ⇒ алерт не потерян, чек-поинт вперёд (повторно не шлём — анти-дубль прежний);
        # не доставлено НИКОМУ ⇒ чек-поинт стоит, следующий цикл повторит тот же батч (рост
        # ограничен limit=200 выборки).
        if not delivered:
            log.warning(
                "scheduler: алерты о %d инцидентах не доставлены ни одному из %d админов — "
                "чек-поинт не двигаем, повтор в следующем цикле",
                len(rows),
                len(admins),
            )
            return 0
        _error_alert_last_id = int(rows[-1].id)
        log.info(
            "scheduler: разослано алертов о %d новых инцидентах (доставлено %d из %d админов)",
            len(rows),
            delivered,
            len(admins),
        )
        return len(rows)


WEEKLY_DIGEST_DAYS = 7  # окно еженедельного дайджеста (ошибки/баг-репорты/активность)


async def _error_events_since(days: int) -> list:
    """error_events за последние `days` дней (reverse-chron) для еженедельного дайджеста. Read-only;
    message/traceback уже редактированы на записи. Фильтр created_at — в Python (naive/aware SQLite)."""
    from db.models import ErrorEvent

    start = datetime.now(timezone.utc) - timedelta(days=int(days))
    async with Session() as s:
        rows = (
            (await s.execute(select(ErrorEvent).order_by(ErrorEvent.id.desc()).limit(1000)))
            .scalars()
            .all()
        )
    out = []
    for r in rows:
        dt = r.created_at
        if dt is not None and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt is not None and dt >= start:
            out.append(r)
    return out


async def run_weekly_digest(bot) -> int:
    """§6/§15 (1.3): еженедельный дайджест админам (ADMIN_CHAT_IDS) — ошибки за 7 дней + баг-репорты +
    сводка активности. Короткий ТЕКСТ + прикреплённый ФАЙЛ с деталями. READ-ONLY (golden rule #3):
    только чтение error_events/bug_reports/audit_log + рассылка. Нет админов ⇒ no-op (opt-in).
    Один недоступный админ не роняет остальных (НЕ capture — иначе петля наблюдаемости).
    Возвращает число обслуженных админов."""
    from core import i18n, texts
    from confirm.store import audit_activity_since
    from core import bugs
    from reports.diag_export import build_weekly_digest_file
    from scheduler.transport import send_bot_document

    with request_scope("scheduler:weekly-digest"):  # §15: корреляция логов джобы по request_id
        from core.access import admin_ids_all

        admins = await admin_ids_all()  # P4: env ∪ рантайм-админы
        if not admins:
            return 0
        errors = await _error_events_since(WEEKLY_DIGEST_DAYS)
        bug_rows = await bugs.bug_reports_since(WEEKLY_DIGEST_DAYS)
        activity = await audit_activity_since(WEEKLY_DIGEST_DAYS)
        file_text = build_weekly_digest_file(errors, bug_rows, activity, days=WEEKLY_DIGEST_DAYS)
        served = 0
        for chat_id in admins:
            lang = i18n.get_lang(chat_id)
            # Обрезаем ПО СТРОКАМ, а не сырым слайсом: `[:3800]` рвал HTML посреди тега/сущности →
            # Telegram отбивал сообщение ("can't parse entities"), исключение глоталось except ниже
            # → дайджест молча не доставлялся ВМЕСТЕ с файлом. Детали всё равно уходят вложением.
            text = texts.split_by_lines(
                texts.fmt_weekly_digest(
                    errors, bug_rows, activity, days=WEEKLY_DIGEST_DAYS, lang=lang
                ),
                _DIGEST_MAX,
            )[0]
            try:
                await transport.send_bot_message(bot, chat_id, text, parse_mode="HTML")
            except Exception as e:  # noqa: BLE001 — недоступный админ не роняет; НЕ capture (петля!)
                log.warning(
                    "scheduler: недельный дайджест не доставлен в %s: %s", chat_id, type(e).__name__
                )
            try:  # файл с деталями — отдельно: сбой текста не должен отменять вложение (и наоборот)
                await send_bot_document(bot, chat_id, text=file_text, filename="weekly_digest.txt")
                served += 1
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "scheduler: файл дайджеста не доставлен в %s: %s", chat_id, type(e).__name__
                )
        log.info("scheduler: недельный дайджест разослан (админов %d)", served)
        return served


async def _advise_proactive_chats(recipients: set[int]) -> set[int]:
    """Операторы с включённой проактивной подачей рекомендаций (UserSettings.ui_prefs.advise_proactive).
    По умолчанию ВЫКЛ (fail-closed к анти-спаму: не шлём непрошеные советы). READ-ONLY."""
    if not recipients:
        return set()
    async with Session() as s:
        rows = (
            await s.execute(
                select(UserSettings.chat_id, UserSettings.ui_prefs).where(
                    UserSettings.chat_id.in_(recipients)
                )
            )
        ).all()
    on: set[int] = set()
    for cid, prefs in rows:
        val = (prefs or {}).get("advise_proactive") if isinstance(prefs, dict) else None
        if str(val).lower() in ("1", "true", "on", "yes"):
            on.add(int(cid))
    return on


def _digest_account_label(acct: str) -> str:
    """Имя аккаунта для карточки дайджеста («Башня · 5437782039»); нет meta → голый id.

    B14: имя аккаунта из Google Ads может содержать «<»/«&» — все потребители шлют его с
    parse_mode=HTML (thr_tune_offer, recommendations digest), поэтому ЭКРАНИРУЕМ здесь (единый
    источник): без escape сообщение с «<» в имени молча НЕ доставлялось (Telegram отвергал разметку)."""
    from core.texts import esc

    try:
        from ads.client import discovered_read_children_meta

        ch = discovered_read_children_meta().get(str(acct))
        if ch is not None and (ch.name or "") and str(ch.name) != str(acct):
            return esc(f"{ch.name} · {acct}")
    except Exception:  # noqa: BLE001 — ярлык-косметика
        pass
    return esc(str(acct))


async def run_recommendations_digest(bot) -> None:
    """§advisor «утренний экран действий»: ПРОАКТИВНЫЕ рекомендации операторам с advise_proactive
    (opt-in). READ-ONLY: собирает отчёты + правила (advisor.service), НИЧЕГО не меняет и proposal
    НЕ создаёт (golden rule #1/#3) — кнопка «применить» лишь СТАРТУЕТ confirm-гейт по тапу человека
    (существующий путь AdviseCB apply → bm._advise_apply, ensure_allowed на минтинге и исполнении).

    Отличия от старого «одного блока текста»: (1) отчёт per-account собирается ОДИН раз на прогон
    (кэш; раньше — per-chat×per-account); (2) единый кросс-аккаунтный Top-N по ДОЛЕ расхода под
    риском (rank_cross_account, БЕЗ FX — golden rule #4); (3) каждая рекомендация — отдельным
    сообщением с кнопками 👍/👎/🙈/apply (как в /advise), персистятся ТОЛЬКО показанные Top-N;
    (4) пауза между сообщениями (flood-limits). Детерминированный render (use_llm=False)."""
    with request_scope("scheduler:advise"):
        recipients = await _recipients()
        chats = await _advise_proactive_chats(recipients)
        if not chats:
            return
        accounts = _scheduled_accounts()
        if not accounts:
            return
        from advisor import service as advisor_service
        from advisor import store as advisor_store
        from advisor.rules import _magnitude, rank_cross_account
        from core import i18n
        from scheduler import delivery

        # 3.2в: гейт кнопки «применить» — тот же, что в /advise (allow-list не-денежных операций +
        # исполнимость params). Кнопка лишь СТАРТУЕТ confirm-гейт по тапу пользователя — proposal
        # из scheduler НЕ создаётся (golden rule #3 цел).
        from advisor.apply import one_tap_op

        top_n = max(1, int(getattr(settings, "advise_digest_top_n", 5)))
        pause = max(0.0, float(getattr(settings, "advise_digest_send_pause", 0.7)))

        # 1) Отчёты per-account — ОДИН раз на прогон (кэш переиспользуется каждым чатом).
        reports: dict[str, tuple[object, str, float]] = {}  # acct → (report, currency, total_cost)
        for acct in accounts:
            tok = set_context(customer_id=acct)  # §8: per-account атрибуция ошибок/логов
            try:
                report = await advisor_service._gather_report(acct, 30)
                currency = getattr(report, "currency", "") or ""
                total = float(getattr(report.totals, "cost_micros", 0) or 0) / 1_000_000
                reports[str(acct)] = (report, currency, total)
            except Exception as e:  # сеть/доступ/SDK — фиксируем, остальные аккаунты живут
                if is_account_access_error(e):
                    log.info("advise digest: аккаунт %s недоступен (ожидаемо, пропуск)", acct)
                else:
                    await capture_exception(e, where=f"scheduler:advise:{acct}")
            finally:
                reset_context(tok)
        if not reports:
            return

        from core.access import accessible_accounts_for_user

        for chat_id in chats:
            lang = i18n.get_lang(chat_id)
            # C2: рекомендации строим только по аккаунтам, доступным этому оператору.
            allowed = set(await accessible_accounts_for_user(chat_id, list(reports)))
            # 2) Кандидаты по каждому аккаунту (persist=False — строки пишем ТОЛЬКО для Top-N).
            items: list[tuple] = []  # (acct, currency, total_cost, rec)
            for acct, (report, currency, total) in reports.items():
                if acct not in allowed:
                    continue
                try:
                    rec_set = await advisor_service.build_recommendations(
                        chat_id,
                        acct,
                        source="scheduler",
                        lang=lang,
                        use_llm=False,
                        persist=False,
                        report=report,
                    )
                except Exception as e:  # опыт/БД — не роняем дайджест целиком
                    await capture_exception(e, where=f"scheduler:advise:{acct}")
                    continue
                items.extend((acct, currency, total, r) for r in rec_set.recs)
            top = rank_cross_account(items, top_n=top_n)
            if not top:
                continue
            # 3) Персист ТОЛЬКО показанных (rec_uid для кнопок 👍/👎/🙈/apply).
            by_acct: dict[str, list] = {}
            for acct, _cur, _total, r in top:
                by_acct.setdefault(acct, []).append(r)
            try:
                for acct, recs in by_acct.items():
                    await advisor_store.record_recommendations(
                        chat_id, acct, recs, source="scheduler"
                    )
            except Exception as e:  # noqa: BLE001 — без rec_uid кнопки бессмысленны → пропуск чата
                await capture_exception(e, where="scheduler:advise:persist")
                continue
            # 4) Отправка: заголовок + карточки с кнопками (пауза между сообщениями — flood).
            try:
                await transport.send_bot_message(
                    bot, chat_id, i18n.t("advise_digest_header", lang, n=len(top))
                )
            except Exception as e:  # один недоступный чат не роняет остальных
                log.warning("advise digest не доставлен в %s: %s", chat_id, type(e).__name__)
                continue
            for acct, _cur, total, r in top:
                share = ""
                if total > 0 and _magnitude(r) > 0:
                    pct = min(100.0, 100.0 * _magnitude(r) / total)
                    share = " · " + i18n.t("advise_digest_share", lang, p=f"{pct:.0f}")
                text = i18n.t(
                    "advise_digest_item",
                    lang,
                    account=_digest_account_label(acct) + share,
                    body=r.body or "",
                )
                apply_op = one_tap_op(r)
                try:
                    await asyncio.sleep(pause)
                    await transport.send_bot_message(
                        bot,
                        chat_id,
                        text,
                        reply_markup=delivery.markup(
                            delivery.ADVISE_FEEDBACK, r.rec_uid, lang, apply_op=apply_op
                        ),
                    )
                except Exception as e:  # недоставка одной карточки не роняет рассылку
                    log.warning(
                        "advise digest: карточка не доставлена в %s: %s",
                        chat_id,
                        type(e).__name__,
                    )
            try:
                await asyncio.sleep(pause)
                await transport.send_bot_message(bot, chat_id, i18n.t("advise_disclaimer", lang))
            except Exception:  # noqa: BLE001
                pass


async def run_mcc_rediscovery() -> None:
    """2.4 (аудит 2026-07-06): суточный пере-обход детей MCC — новый/закрытый дочерний виден в
    пикерах/scheduler БЕЗ рестарта и без ручного /refresh («набор аккаунтов = снимок на старте»).
    READ-ONLY, под замком ensure_manager_allowed (внутри discover_read_children); идемпотентна и
    сама fail-safe per-MCC. Кэши клиентов/валют НЕ сбрасываем (в отличие от /refresh): за сутки
    они не протухают, а фоновый сброс дал бы латентный спайк."""
    with request_scope("scheduler:rediscovery"):
        from ads.client import discover_read_children

        n = await discover_read_children()
        log.info("scheduler: re-discovery дочерних MCC: %d", n)


async def _business_digest_chats(recipients: set[int]) -> set[int]:
    """Операторы с включённым недельным БИЗНЕС-дайджестом (ui_prefs.business_digest, тогл
    /bizdigest). По умолчанию ВЫКЛ (fail-closed к анти-спаму). READ-ONLY."""
    if not recipients:
        return set()
    async with Session() as s:
        rows = (
            await s.execute(
                select(UserSettings.chat_id, UserSettings.ui_prefs).where(
                    UserSettings.chat_id.in_(recipients)
                )
            )
        ).all()
    on: set[int] = set()
    for cid, prefs in rows:
        val = (prefs or {}).get("business_digest") if isinstance(prefs, dict) else None
        if str(val).lower() in ("1", "true", "on", "yes"):
            on.add(int(cid))
    return on


def _biz_cpa_line(report, currency: str, lang: str) -> str:
    """Строка CPA неделя-к-неделе из totals/prev_totals (КОД считает, без FX). Нет данных → ''."""
    t, p = getattr(report, "totals", None), getattr(report, "prev_totals", None)
    if t is None:
        return ""

    def _cpa(m) -> float | None:
        conv = float(getattr(m, "conversions", 0.0) or 0.0)
        cost = float(getattr(m, "cost_micros", 0) or 0) / 1_000_000
        return (cost / conv) if conv > 0 else None

    cur, prev = _cpa(t), (_cpa(p) if p is not None else None)
    if cur is None and prev is None:
        return ""
    code = f" {currency}" if currency else ""
    label = "CPA (WoW)" if lang == "en" else "CPA (неделя к неделе)"
    left = f"{prev:.2f}{code}" if prev is not None else "—"
    right = f"{cur:.2f}{code}" if cur is not None else "—"
    return f"{label}: {left} → {right}"


async def run_business_digest(bot) -> None:
    """1.6 (аудит 2026-07-06): недельный БИЗНЕС-дайджест менеджерам (opt-in /bizdigest). READ-ONLY:
    per-account сводка неделя-к-неделе (summary_text уже содержит ▲/▼-дельты) + CPA WoW + топ-3
    рекомендации (детерминированный render, persist=False — витрина, не карточки) + аномалии недели
    по per-chat порогам. ОДНО сообщение на оператора (анти-спам, потолок _DIGEST_MAX). НИКАКИХ
    мутаций/proposal (golden rule #1/#3)."""
    with request_scope("scheduler:bizdigest"):
        recipients = await _recipients()
        chats = await _business_digest_chats(recipients)
        if not chats:
            return
        accounts = _scheduled_accounts()
        if not accounts:
            return
        from advisor import service as advisor_service
        from core import i18n

        thr_by_chat = await _thresholds_by_chat(chats)
        # 1) Отчёты per-account — один раз на прогон (with_comparison=True: prev-период внутри).
        acct_data: dict[str, tuple[object, str, object]] = {}
        for acct in accounts:
            tok = set_context(customer_id=acct)
            try:
                client = await build_client_async(acct)
                period = await _account_period(client, acct, 7)
                currency = ""
                try:
                    from ads.read import account_currency

                    currency = await run_ads_read_call(
                        account_currency, client, acct, label=f"biz_cur_{acct}"
                    )
                except Exception:  # noqa: BLE001 — валюта best-effort
                    currency = ""
                report = await build_account_report_async(client, acct, period, currency=currency)
                # Здоровье — engine-only по уже собранному отчёту (0 доп. чтений, см. _account_health).
                acct_data[str(acct)] = (report, currency, _account_health(report))
            except Exception as e:  # сбой одного аккаунта не валит дайджест
                if is_account_access_error(e):
                    log.info("bizdigest: аккаунт %s недоступен (ожидаемо, пропуск)", acct)
                else:
                    await capture_exception(e, where=f"scheduler:bizdigest:{acct}")
            finally:
                reset_context(tok)
        if not acct_data:
            return
        # 2) Один локализованный дайджест на оператора.
        from core.access import accessible_accounts_for_user

        for chat_id in chats:
            lang = i18n.get_lang(chat_id)
            # C2: в дайджест оператора попадают только доступные ему аккаунты.
            allowed = set(await accessible_accounts_for_user(chat_id, list(acct_data)))
            blocks: list[str] = []
            for acct, (report, currency, health) in acct_data.items():
                if acct not in allowed:
                    continue
                part = [summary_text(report, lang)]
                cpa = _biz_cpa_line(report, currency, lang)
                if cpa:
                    part.append(cpa)
                hl = _health_line(
                    health, lang
                )  # балл/грейд/под-риском — тот же движок, что у /audit
                if hl:
                    part.append(hl)
                # топ-3 рекомендаций — витрина недели (persist=False: без карточек/кнопок)
                try:
                    rec_set = await advisor_service.build_recommendations(
                        chat_id,
                        acct,
                        source="scheduler",
                        lang=lang,
                        use_llm=False,
                        persist=False,
                        report=report,
                    )
                    tops = [r.body for r in rec_set.recs[:3] if r.body]
                    if tops:
                        part.append(
                            i18n.t("bizdigest_recs_title", lang)
                            + "\n"
                            + "\n".join(f"• {b}" for b in tops)
                        )
                except Exception:  # noqa: BLE001 — советы-довесок, не критичны
                    pass
                try:
                    alerts = detect_anomalies(
                        report.totals,
                        report.prev_totals,
                        _effective_thresholds(thr_by_chat.get(chat_id), acct),
                        currency=currency,
                    )
                    if alerts:
                        part.append(
                            i18n.t("bizdigest_anomalies_title", lang)
                            + "\n"
                            + "\n".join(f"• {_alert_line(a, lang)}" for a in alerts)
                        )
                except Exception:  # noqa: BLE001 — аномалии-довесок
                    pass
                blocks.append("\n".join(part))
            if not blocks:
                continue
            period_label = ""
            first = next(iter(acct_data.values()))[0]
            p = getattr(first, "period", None)
            if p is not None:
                period_label = f"{getattr(p, 'date_from', '')} — {getattr(p, 'date_to', '')}"
            text = (
                i18n.t("bizdigest_header", lang, period=period_label)
                + "\n\n"
                + "\n\n———\n\n".join(blocks)
            )
            if len(text) > _DIGEST_MAX:
                text = text[:_DIGEST_MAX] + "\n…"
            try:
                await transport.send_bot_message(bot, chat_id, text)
            except Exception as e:  # один недоступный чат не роняет рассылку
                log.warning("bizdigest не доставлен в %s: %s", chat_id, type(e).__name__)


# ── 2.11 (§14): авто-подстройка порогов аномалий — READ-ONLY анализ + ПРЕДЛОЖЕНИЕ ──
_THR_TUNE_PROPOSE_COOLDOWN_D = 14  # не предлагать чаще раза в 2 недели
_THR_TUNE_DECLINE_COOLDOWN_D = 28  # после отказа молчим ~4 недели


async def _ui_pref_blob(chat_id: int, key: str) -> dict | None:
    """ui_prefs[key] как JSON-блоб (прецедент last_report_sel). Нет/битый → None. READ-ONLY."""
    import json

    async with Session() as s:
        prefs = (
            await s.execute(select(UserSettings.ui_prefs).where(UserSettings.chat_id == chat_id))
        ).scalar_one_or_none()
    raw = (prefs or {}).get(key) if isinstance(prefs, dict) else None
    if not raw:
        return None
    try:
        d = json.loads(raw) if isinstance(raw, str) else dict(raw)
        return d if isinstance(d, dict) else None
    except Exception:  # noqa: BLE001 — битый блоб = нет блоба
        return None


async def _save_ui_pref_blob(chat_id: int, key: str, blob: dict) -> None:
    """Записать JSON-блоб в ui_prefs[key] (настройка БОТА; JSON переприсваиваем целиком)."""
    import json

    async with Session() as s:
        row = (
            await s.execute(select(UserSettings).where(UserSettings.chat_id == chat_id))
        ).scalar_one_or_none()
        prefs = dict((row.ui_prefs if row is not None else None) or {})
        prefs[key] = json.dumps(blob, ensure_ascii=False)
        if row is None:
            s.add(UserSettings(chat_id=chat_id, ui_prefs=prefs))
        else:
            row.ui_prefs = prefs
        await s.commit()


def _thr_tune_on_cooldown(blob: dict | None, now: datetime) -> bool:
    """Анти-спам: недавнее предложение (14д) или недавний отказ (28д) → молчим."""
    if not blob:
        return False
    for field, days in (
        ("proposed_at", _THR_TUNE_PROPOSE_COOLDOWN_D),
        ("declined_at", _THR_TUNE_DECLINE_COOLDOWN_D),
    ):
        raw = blob.get(field)
        if not raw:
            continue
        try:
            ts = datetime.fromisoformat(str(raw))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if (now - ts).days < days:
                return True
        except ValueError:
            continue
    return False


async def run_threshold_tuning(bot) -> None:
    """2.11 (§14): персональные пороги аномалий по ВОЛАТИЛЬНОСТИ аккаунта. READ-ONLY + notify:
    джоба читает дневную динамику (fetch_by_day, trailing 12 недель), считает предложение ЧИСТЫМИ
    формулами (scheduler.threshold_tuner) и ПРЕДЛАГАЕТ оверлей сообщением с кнопками. Сама НИЧЕГО
    не пишет в настройки — запись делает хендлер «✅ Принять» (bm._save_per_account_thresholds),
    т.е. только по тапу человека. Google Ads не мутируется (golden rule #3). Opt-in
    (threshold_tune_enabled) + анти-спам 14/28 дней в ui_prefs."""
    with request_scope("scheduler:thr-tune"):
        from uuid import uuid4

        from core import i18n
        from reports.period import custom as period_custom
        from reports.queries import fetch_by_day
        from scheduler import delivery
        from scheduler.threshold_tuner import TRAILING_DAYS, suggest_thresholds, weekly_buckets

        # Единственная джоба, которую БЕЗ кнопок слать нельзя: всё сообщение — это предложение,
        # принять которое можно ТОЛЬКО тапом «✅ Принять». Без кнопочного слоя (standalone-
        # планировщик) оно превратилось бы в неисполнимый текст + запись токена в ui_prefs,
        # которую некому погасить. Молчим целиком, а не шлём половину. Дайджесты — наоборот:
        # там цифры самоценны, кнопка довесок (см. scheduler/delivery.py).
        if delivery.THRESHOLD_TUNE not in delivery.registered():
            log.info("thr-tune: кнопочный слой не подключён — предложение порогов пропущено")
            return
        recipients = await _recipients()
        if not recipients:
            return
        accounts = _scheduled_accounts()
        if not accounts:
            return
        thr_by_chat = await _thresholds_by_chat(recipients)
        # A8/C2: per-account предложение НЕ должно уходить оператору без доступа к аккаунту
        # (enforced-режим). Все прочие per-account рассылки (report/anomaly/recommendations/
        # business) уже фильтруют через accessible_accounts_for_user — thr-tune был единственной
        # дырой. Считаем разрешённые аккаунты по каждому получателю ОДИН раз (до цикла по акк.).
        from core.access import accessible_accounts_for_user

        allowed_by_chat = {
            chat_id: set(await accessible_accounts_for_user(chat_id, accounts))
            for chat_id in recipients
        }
        now = datetime.now(timezone.utc)
        today = now.date()
        period = period_custom(today - timedelta(days=TRAILING_DAYS), today - timedelta(days=1))

        for acct in accounts:
            tok_ctx = set_context(customer_id=acct)
            try:
                client = await build_client_async(acct)
                day_bd = await run_ads_read_call(
                    fetch_by_day, client, acct, period, None, label=f"thr_tune_{acct}"
                )
                costs, convs = weekly_buckets(getattr(day_bd, "rows", None) or [])
                currency = ""
                try:
                    from ads.read import account_currency

                    currency = await run_ads_read_call(
                        account_currency, client, acct, label=f"thr_cur_{acct}"
                    )
                except Exception:  # noqa: BLE001 — валюта best-effort
                    currency = ""
            except Exception as e:  # сбой одного аккаунта не валит джобу
                if is_account_access_error(e):
                    log.info("thr-tune: аккаунт %s недоступен (ожидаемо, пропуск)", acct)
                else:
                    await capture_exception(e, where=f"scheduler:thr-tune:{acct}")
                continue
            finally:
                reset_context(tok_ctx)
            if not costs:
                continue
            for chat_id in recipients:
                if acct not in allowed_by_chat.get(chat_id, set()):
                    continue  # A8: нет доступа к аккаунту → не шлём его пороги
                key = f"thr_tune_{acct}"
                blob = await _ui_pref_blob(chat_id, key)
                if _thr_tune_on_cooldown(blob, now):
                    continue
                current = _effective_thresholds(thr_by_chat.get(chat_id), acct) or {}
                from scheduler.anomaly import DEFAULT_THRESHOLDS

                cur_all = {**DEFAULT_THRESHOLDS, **current}
                suggestion = suggest_thresholds(costs, convs, cur_all)
                if not suggestion:
                    continue
                token = uuid4().hex[:12]
                await _save_ui_pref_blob(
                    chat_id,
                    key,
                    {
                        "token": token,
                        "acct": str(acct),
                        "values": suggestion,
                        "proposed_at": now.isoformat(),
                        "declined_at": (blob or {}).get("declined_at"),
                    },
                )
                lang = i18n.get_lang(chat_id)
                try:
                    await transport.send_bot_message(
                        bot,
                        chat_id,
                        i18n.t(
                            "thr_tune_offer",
                            lang,
                            account=_digest_account_label(acct),
                            weeks=len(costs),
                            spike=f"{suggestion['spend_spike_pct']:.0f}",
                            cur_spike=f"{cur_all.get('spend_spike_pct', 0):.0f}",
                            drop=f"{suggestion['conv_drop_pct']:.0f}",
                            cur_drop=f"{cur_all.get('conv_drop_pct', 0):.0f}",
                            minspend=f"{suggestion['min_spend']:g}",
                            cur_minspend=f"{cur_all.get('min_spend', 0):g}",
                            currency=currency or "",
                        ),
                        reply_markup=delivery.markup(delivery.THRESHOLD_TUNE, token, lang),
                        parse_mode="HTML",
                    )
                except Exception as e:  # один недоступный чат не роняет рассылку
                    log.warning("thr-tune не доставлен в %s: %s", chat_id, type(e).__name__)


async def _notify_outcome(bot, outcome, verdict: str) -> None:
    """§advisor #2: сообщить оператору исход применённого совета (improved/worse) — обучение видимо.
    READ-ONLY (только уведомление). chat_id берём из связанной рекомендации по rec_uid."""
    from advisor import store as advisor_store
    from core import i18n

    rec = await advisor_store.get_recommendation(outcome.rec_uid)
    if rec is None:
        return
    lang = i18n.get_lang(rec.chat_id)
    key = "advise_outcome_improved" if verdict == "improved" else "advise_outcome_worse"
    campaign = outcome.target_campaign or (rec.target_campaign or "")
    try:
        await transport.send_bot_message(bot, rec.chat_id, i18n.t(key, lang, campaign=campaign))
    except Exception as e:  # один недоступный чат не роняет замер
        log.warning(
            "advise outcome-уведомление не доставлено в %s: %s", rec.chat_id, type(e).__name__
        )


async def run_recommendation_followups(bot=None) -> int:
    """§advisor Слой B: ЗАМЕР результата применённых рекомендаций, у которых наступил measure_after.
    READ-ONLY: advisor.outcome.measure_outcome читает метрики кампании ДО/ПОСЛЕ, delta+verdict в КОДЕ,
    ничего не мутирует (golden rule #3). Возвращает число замеренных. bot задан → уведомляем оператора
    об исходе improved/worse (#2: обучение видимо)."""
    with request_scope("scheduler:advise-followup"):
        from advisor.outcome import due_outcomes, measure_outcome

        due = await due_outcomes()
        if not due:
            return 0
        n = 0
        for outcome in due:
            tok = set_context(customer_id=outcome.customer_id)
            try:
                client = await build_client_async(outcome.customer_id)
                verdict = await measure_outcome(outcome, client)
                n += 1
                if bot is not None and verdict in ("improved", "worse"):
                    await _notify_outcome(bot, outcome, verdict)
            except Exception as e:  # сеть/доступ/SDK — фиксируем, остальные строки живут
                if is_account_access_error(e):
                    log.info(
                        "advise followup: аккаунт %s недоступен (ожидаемо, пропуск)",
                        outcome.customer_id,
                    )
                else:
                    await capture_exception(
                        e, where=f"scheduler:advise-followup:{outcome.customer_id}"
                    )
            finally:
                reset_context(tok)
        if n:
            log.info("scheduler: рекомендаций замерено (Слой B): %d", n)
        return n


async def deliver_proposal_attachments(bot, *, limit: int = 10) -> int:
    """Курьер обещанных .xlsx-вложений (Волна 1b). Возвращает число доставленных файлов.

    Зачем отдельная джоба, а не отправка на месте создания черновика. Карточку в MCP-контуре печатает
    gateway Hermes, а Bot-токен есть ровно у одного процесса фонового контура — этого. Тул-слой
    (`mcp_server/`) токена не имеет и иметь не должен (топология: OAuth и Telegram — разные процессы),
    протащить файл в MCP-конверте нельзя (base64 = весь файл через окно модели, +33% и платная запись
    кэша 1.25×), а `Proposal.tg_message_id` в этом контуре NULL — реплаем к карточке файл тоже не
    приложить. Остаётся вторая отправка из процесса, у которого транспорт уже есть.

    Цена решения названа честно: файл приходит ОТДЕЛЬНЫМ сообщением и ПОЗЖЕ карточки, а контейнер
    `scheduler` становится обязательным звеном. Он лёг — строки остаются в `attachment_state='pending'`
    и это ВИДНО (обещали и не отдали), а не теряется молча.

    READ-ONLY по отношению к Google Ads: ни одного вызова SDK, файл собирается из `params` той же
    строки. Одноразовость — CAS `claim_attachment`: два планировщика (или один после рестарта) не
    пришлют файл дважды."""
    import os

    from confirm.attachment import plan_attachment, plan_budget_chart
    from core import i18n

    with request_scope("scheduler:proposal-attachments"):
        store = ConfirmStore()
        rows = await store.list_pending_attachments(limit=limit)
        if not rows:
            return 0
        sent = 0
        for row in rows:
            lang = i18n.get_lang(row.chat_id)
            # Волна 5: две политики вложения на один флаг 'pending' — список ключей и график
            # проекции бюджета для L3. Обе ЧИСТЫЕ функции от (operation, params) той же строки,
            # поэтому курьер приходит ровно к тому же решению, что тул-слой минуту назад.
            spec = plan_attachment(
                row.operation, row.params, cid=row.confirmation_id, lang=lang
            ) or plan_budget_chart(row.operation, row.params, cid=row.confirmation_id)
            if spec is None:
                # Обещание было, а плана нет: строка пережила правку KEYWORD_XLSX_OPS/порога. Файла
                # не будет — честно закрываем 'failed', а не держим вечный 'pending'.
                if await store.claim_attachment(row.confirmation_id):
                    await store.finish_attachment(row.confirmation_id, "failed")
                    log.warning(
                        "attachments: план вложения не собрался для %s (%s) — помечено failed",
                        row.confirmation_id,
                        row.operation,
                    )
                continue
            if not await store.claim_attachment(row.confirmation_id):
                continue  # застолбил кто-то другой — не наша строка
            state = "failed"
            try:
                path = await asyncio.to_thread(_build_attachment_file, spec, lang)
            except Exception as e:  # noqa: BLE001 — сборка файла не должна валить остальные строки
                log.warning(
                    "attachments: сборка .xlsx для %s не удалась: %s",
                    row.confirmation_id,
                    type(e).__name__,
                )
                await store.finish_attachment(row.confirmation_id, state)
                continue
            try:
                from scheduler.transport import send_bot_file

                await send_bot_file(bot, row.chat_id, path=path, filename=spec.filename)
                state = "sent"
                sent += 1
            except Exception as e:  # noqa: BLE001 — недоступный чат не роняет остальные вложения
                log.warning(
                    "attachments: вложение %s не доставлено в %s: %s",
                    row.confirmation_id,
                    row.chat_id,
                    type(e).__name__,
                )
            finally:
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                await store.finish_attachment(row.confirmation_id, state)
        if sent:
            log.info("scheduler: вложений черновиков доставлено: %d", sent)
        return sent


def _build_attachment_file(spec, lang: str = "ru") -> str:
    """Собрать .xlsx во временный файл и вернуть путь. СИНХРОННО и НАРОЧНО: openpyxl — CPU-bound,
    вызывающий уносит его в `asyncio.to_thread`, чтобы сборка списка на тысячи ключей не держала
    event loop планировщика. Импорты локальные: они тянут openpyxl, а модуль джоб импортируется в
    процессе, где вложения могут не понадобиться ни разу.

    Ветвление по `spec.kind`, а не по типу: политика вложения (`confirm/attachment.py`) отдаёт
    ЧИСТЫЕ данные, и «какой это файл» — её слово, а не свойство класса в этом модуле."""
    import os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".xlsx", prefix="aimash_kw_")
    os.close(fd)  # openpyxl пишет по пути сам; дескриптор нам не нужен, но mkstemp его открывает
    try:
        if getattr(spec, "kind", "") == "budget_chart":
            from reports.xlsx import write_budget_projection_xlsx

            write_budget_projection_xlsx(spec, path, lang)
        else:
            from keywords.export import write_keyword_list_xlsx

            write_keyword_list_xlsx(
                list(spec.keywords),
                spec.match_type,
                spec.action,
                path,
                scope=spec.scope,
            )
    except Exception:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        raise
    return path


async def cleanup_stale_proposals(
    *, now: datetime | None = None, ttl_hours: int | None = None
) -> int:
    """Просроченные pending-черновики → reject (с аудитом). Возвращает число отклонённых.

    УБОРЩИК, НЕ ГАРД (Волна 1.2). Срок жизни подтверждения энфорсит `ConfirmStore.confirm`/`claim`
    условием возраста в самом CAS — не отработавшая джоба больше не делает вчерашний черновик
    исполнимым. Здесь остаётся то, чего CAS сделать не может: перевести мёртвую строку в терминальный
    `rejected` (чтобы она не висела в `pending` вечно и попадала в журнал) и освободить временные
    медиа §19/§11, которые иначе осиротеют на диске.

    Сравнение возраста — в Python (а не в SQL), чтобы корректно работать и на SQLite (наивный
    UTC), и на Postgres (tz-aware): наивный created_at трактуем как UTC."""
    with request_scope("scheduler:cleanup"):  # §15: корреляция логов джобы по request_id
        if ttl_hours is None:  # 2.6: живое значение из config (тесты могут передать своё)
            ttl_hours = int(settings.proposal_ttl_hours)
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
        rejected = 0
        for cid, chat_id in stale:
            # П7: СНАЧАЛА атомарный reject (pending→rejected, CAS), и ТОЛЬКО потом чистим медиа.
            # Раньше unlink шёл ДО reject: если владелец успевал нажать ✅ в окне между выборкой
            # stale и этим циклом, черновик становился confirmed/executing, reject давал False —
            # но кадры были уже удалены → кампания создавалась без изображений (тихо, при старом
            # глотании в service.py). Порядок «reject → clear» это закрывает: False (успел ✅ /
            # гонка / чужой) ⇒ continue, живые медиа не трогаем; True ⇒ черновик терминально мёртв,
            # кадры больше не нужны никому.
            if not await store.reject(cid, chat_id=chat_id):
                continue
            rejected += 1
            # §19/§11: TTL-просроченные create_search/gdn/demand_gen_campaign несут временные медиа
            # по media_id — после reject чистим их (иначе осиротеют на диске).
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
        if rejected:
            log.info("scheduler: отклонено просроченных черновиков: %d", rejected)
        return rejected


# B2: за сколько часов до истечения TTL черновика предупреждать владельца. При ttl <= окна
# предупреждение пропускается (иначе warn прилетал бы сразу после создания черновика).
DRAFT_EXPIRY_WARN_HOURS = 12


async def cleanup_stale_campaign_drafts(
    bot=None, *, now: datetime | None = None, ttl_hours: int | None = None
) -> int:
    """§19: брошенные активные черновики визарда «Создание кампании» → status='abandoned'.

    Не proposal и не мутация — просто гасим залежавшиеся active-черновики (SDK не звался, деньги
    не тратились). Возраст считаем в Python (наивный created/updated трактуем как UTC) — корректно
    и на SQLite, и на Postgres. TTL щедрый (settings.campaign_draft_ttl_hours, дефолт 72ч): Этап-2
    round-trip с Google Sheets может занять день.

    B2 (живой тест 2026-07-07): истечение больше НЕ молчаливое. За DRAFT_EXPIRY_WARN_HOURS до
    гашения владелец получает предупреждение (once per idle-период: повторный warn только если
    черновик трогали после прошлого предупреждения), по факту abandon — уведомление с подсказкой
    /newcampaign. bot=None (тесты/CLI) → без уведомлений, поведение как раньше."""
    ttl = settings.campaign_draft_ttl_hours if ttl_hours is None else ttl_hours
    with request_scope("scheduler:cleanup-drafts"):
        now_dt = now or datetime.now(timezone.utc)
        cutoff = now_dt - timedelta(hours=ttl)
        warn_cutoff = now_dt - timedelta(hours=max(ttl - DRAFT_EXPIRY_WARN_HOURS, 0))
        n = 0
        orphan_media: list[str] = []
        expired: list[tuple[int, int]] = []  # (chat_id, step) — уведомить после commit
        expiring: list[tuple[int, int, int]] = []  # (chat_id, step, часов до гашения)
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
                    expired.append((int(d.chat_id), int(d.current_step or 0)))
                elif updated < warn_cutoff and ttl > DRAFT_EXPIRY_WARN_HOURS:
                    # приближается к TTL: предупреждаем один раз на idle-период (активность после
                    # прошлого warn = новый период → предупредим снова при следующем простое)
                    from sqlalchemy.orm.attributes import flag_modified

                    ws = dict(d.wizard_state or {})
                    nav = dict(ws.get("nav") or {})
                    warned_raw = str(nav.get("expiry_warned_at") or "")
                    try:
                        warned_at = datetime.fromisoformat(warned_raw) if warned_raw else None
                    except ValueError:
                        warned_at = None
                    if warned_at is not None and warned_at.tzinfo is None:
                        warned_at = warned_at.replace(tzinfo=timezone.utc)
                    if warned_at is not None and warned_at >= updated:
                        continue  # уже предупреждали для этого простоя
                    nav["expiry_warned_at"] = now_dt.isoformat()
                    ws["nav"] = nav
                    d.wizard_state = ws
                    flag_modified(d, "wizard_state")
                    left_h = max(
                        1, int((updated + timedelta(hours=ttl) - now_dt).total_seconds() // 3600)
                    )
                    expiring.append((int(d.chat_id), int(d.current_step or 0), left_h))
            if n or expiring:
                await s.commit()
        if orphan_media:  # §19: чистим временные изображения брошенных черновиков (вне транзакции)
            from ads.assets import clear_pending_media_ids

            clear_pending_media_ids(orphan_media)
        if bot is not None and (expired or expiring):
            from core import i18n

            for chat_id, step, left_h in expiring:
                try:
                    await transport.send_bot_message(
                        bot,
                        chat_id,
                        i18n.t(
                            "cc_draft_expiring",
                            i18n.get_lang(chat_id),
                            step=max(1, min(step, 7)),
                            left_h=left_h,
                        ),
                    )
                except Exception:  # noqa: BLE001 — мёртвый чат не должен ронять cleanup
                    log.warning("cleanup-drafts: warn не доставлен (chat=%s)", chat_id)
            for chat_id, step in expired:
                try:
                    await transport.send_bot_message(
                        bot,
                        chat_id,
                        i18n.t(
                            "cc_draft_expired", i18n.get_lang(chat_id), step=max(1, min(step, 7))
                        ),
                    )
                except Exception:  # noqa: BLE001
                    log.warning("cleanup-drafts: expired-notify не доставлен (chat=%s)", chat_id)
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
                    await transport.send_bot_message(
                        bot,
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


async def reconcile_stale_confirmed(
    bot=None, *, now: datetime | None = None, stale_minutes: int | None = None
) -> int:
    """A6: черновики, зависшие в 'confirmed' дольше N мин: процесс упал в окне МЕЖДУ confirm()
    (pending→confirmed) и claim() (confirmed→executing, внутри apply_* перед SDK). Пока статус
    'confirmed', SDK НЕ вызывался (в отличие от 'executing' — reconcile_stale_executing) → изменение
    ТОЧНО не применено. Раньше этот статус не покрывал НИКТО (cleanup_stale_proposals — только
    'pending', reconcile_stale_executing — только 'executing'), и черновик висел в 'confirmed'
    навсегда, занимая слот и вводя в заблуждение в /journal.

    Помечаем 'failed' (mark_confirmed_failed, атомарный CAS confirmed→failed) + audit-строка +
    уведомление владельца «прервано ДО вызова SDK — НЕ применено, повтори команду». Пометка —
    запись в ЛОКАЛЬНУЮ БД, не мутация Ads (golden rule #3). Порог = executing_stale_minutes
    (тот же дефолт 30): до claim проходят секунды, живой процесс не зацепим (гонку с ним выигрывает
    CAS). bot=None (тесты) → без уведомлений."""
    stale = settings.executing_stale_minutes if stale_minutes is None else stale_minutes
    with request_scope("scheduler:reconcile-confirmed"):
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(minutes=stale)
        store = ConfirmStore()
        async with Session() as s:
            rows = (
                (await s.execute(select(Proposal).where(Proposal.status == "confirmed")))
                .scalars()
                .all()
            )
            stale_rows: list[tuple[str, str, int]] = []
            for p in rows:
                ts = p.decided_at or p.created_at  # decided_at = момент confirm()
                if ts is None:
                    continue
                if ts.tzinfo is None:  # SQLite хранит наивный UTC
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    stale_rows.append((p.confirmation_id, p.operation, p.chat_id))
        n = 0
        for cid, op, chat_id in stale_rows:
            err = (
                f"исполнение прервано (рестарт до вызова SDK, зависло >{stale} мин в confirmed) — "
                "изменение НЕ применено, повтори команду"
            )
            if not await store.mark_confirmed_failed(cid, error=err):
                continue  # живой процесс успел claim — не наша строка
            n += 1
            if bot is not None:
                try:
                    await transport.send_bot_message(
                        bot,
                        chat_id,
                        f"⚠️ Операция «{op}» была прервана рестартом бота ДО обращения к Google "
                        "Ads — изменение НЕ применено. Повтори команду, если оно ещё нужно.",
                    )
                except Exception as e:  # один недоступный чат не должен ронять реконсиляцию
                    log.warning(
                        "scheduler: confirmed-fail уведомление не доставлено в %s: %s: %s",
                        chat_id,
                        type(e).__name__,
                        e,
                    )
        if n:
            log.warning("scheduler: зависших confirmed-черновиков помечено failed: %d", n)
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


def _older_than(ts: datetime | None, cutoff: datetime) -> bool:
    """Строка старше порога? Наивный created_at трактуем как UTC (SQLite) — корректно и на Postgres
    (tz-aware). None → False (без даты не удаляем). Сравнение в Python — единый tz-нейтральный путь
    (как во всех cleanup-джобах), без риска строкового сравнения tz-aware/naive в SQL на SQLite."""
    if ts is None:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts < cutoff


async def purge_stale_rows(*, now: datetime | None = None) -> dict[str, int]:
    """C2 (§15): УДАЛИТЬ старые строки монотонно растущих таблиц (error_events / crawl_jobs /
    account_health_snapshot) и ОБНУЛИТЬ протухшие тексты страниц (client_site_pages.text — строка
    остаётся, карта sitelinks не рушится). Остальные cleanup-джобы лишь меняют статус — эти таблицы
    копятся вечно. Пороги — settings.error_events_retain_days / crawl_jobs_retain_days /
    account_health_retain_days / site_page_text_retain_days / ads_quota_ops_retain_days /
    agent_runs_retain_days (0 ⇒ ВЫКЛ для этой таблицы, fail-safe: не удаляем ничего).
    crawl_jobs чистим ТОЛЬКО в терминальном статусе (done|failed) — running/незавершённые закрывает
    reconcile_stale_crawls. ⛔ audit_log НЕ трогаем НИКОГДА (денежный реестр, ручной колд-архив —
    docs/BACKUP.md). Возраст — в Python (tz-нейтрально). Возвращает {table: удалено}.
    Удаляем батчами по 500 id (лимит переменных SQLite).

    Волна 3: каждая таблица — СВОЯ транзакция. Раньше все пять чистились одной сессией с одним
    коммитом, и отказ на любой откатывал уборку ЦЕЛИКОМ — включая уже посчитанные удаления, так что
    возвращённые числа описывали то, чего в БД не произошло. Класс перестал быть теоретическим, как
    только у `agent_run_events` появился триггер неизменяемости: одна отказавшая строка отменяла бы
    очистку четырёх посторонних таблиц. Сбой одной таблицы теперь виден отдельной строкой лога и
    остальных не трогает."""
    from sqlalchemy import delete as sa_delete
    from sqlalchemy import update as sa_update

    from core.observe import MONEY_KINDS
    from db.models import AccountHealthSnapshot, AdsQuotaOp, ClientSitePage, CrawlJob, ErrorEvent

    now = now or datetime.now(timezone.utc)
    result = {
        "error_events": 0,
        "crawl_jobs": 0,
        "account_health_snapshot": 0,
        "site_page_text": 0,
        "ads_quota_ops": 0,
        "agent_runs": 0,  # ЦЕЛЫХ прогонов убрано (заголовок + события), денежные не трогаем
        "rollback_watch": 0,  # закрытых наблюдений убрано; открытые (watching) не трогаем
    }

    async def _sweep(key: str, days: int, sweep) -> None:
        """Одна таблица = одна транзакция + собственная обработка отказа."""
        if days <= 0:
            return  # 0 ⇒ ВЫКЛ для этой таблицы (fail-safe: не удаляем ничего)
        cutoff = now - timedelta(days=days)
        try:
            async with Session() as s:
                n = await sweep(s, cutoff)
                if n:
                    await s.commit()
            result[key] = n
        except Exception as e:  # noqa: BLE001 — отказ одной таблицы не отменяет уборку остальных
            log.warning("scheduler: purge %s не выполнена (%s)", key, type(e).__name__)

    async def _error_events(s, cutoff) -> int:
        rows = (await s.execute(select(ErrorEvent.id, ErrorEvent.created_at))).all()
        stale = [rid for rid, ca in rows if _older_than(ca, cutoff)]
        for i in range(0, len(stale), 500):  # батч: не упереться в лимит переменных SQLite
            await s.execute(sa_delete(ErrorEvent).where(ErrorEvent.id.in_(stale[i : i + 500])))
        return len(stale)

    async def _crawl_jobs(s, cutoff) -> int:
        rows = (
            await s.execute(
                select(CrawlJob.id, CrawlJob.created_at).where(
                    CrawlJob.status.in_(("done", "failed"))
                )
            )
        ).all()
        stale = [rid for rid, ca in rows if _older_than(ca, cutoff)]
        for i in range(0, len(stale), 500):
            await s.execute(sa_delete(CrawlJob).where(CrawlJob.id.in_(stale[i : i + 500])))
        return len(stale)

    async def _health(s, cutoff) -> int:  # N1.1: снапшоты health-score (тренды)
        rows = (
            await s.execute(select(AccountHealthSnapshot.id, AccountHealthSnapshot.created_at))
        ).all()
        stale = [rid for rid, ca in rows if _older_than(ca, cutoff)]
        for i in range(0, len(stale), 500):
            await s.execute(
                sa_delete(AccountHealthSnapshot).where(
                    AccountHealthSnapshot.id.in_(stale[i : i + 500])
                )
            )
        return len(stale)

    async def _site_text(s, cutoff) -> int:  # §20: тексты краула (крупные, чужой контент)
        rows = (
            await s.execute(
                select(ClientSitePage.id, ClientSitePage.crawled_at).where(
                    ClientSitePage.text.is_not(None)
                )
            )
        ).all()
        stale = [rid for rid, ca in rows if _older_than(ca, cutoff)]
        for i in range(0, len(stale), 500):
            # СТРОКУ не удаляем: карта страниц (top_site_pages → sitelinks) должна жить,
            # протухает только тяжёлый текст — досье по нему всё равно уже собрано.
            await s.execute(
                sa_update(ClientSitePage)
                .where(ClientSitePage.id.in_(stale[i : i + 500]))
                .values(text=None)
            )
        return len(stale)

    async def _quota_ops(s, cutoff) -> int:  # C2: строки распределённого счётчика квоты
        rows = (await s.execute(select(AdsQuotaOp.id, AdsQuotaOp.ts))).all()
        stale = [rid for rid, ts in rows if _older_than(ts, cutoff)]
        for i in range(0, len(stale), 500):
            await s.execute(sa_delete(AdsQuotaOp).where(AdsQuotaOp.id.in_(stale[i : i + 500])))
        return len(stale)

    async def _agent_runs(s, cutoff) -> int:
        """Волна 3: журнал прогонов убирается ЦЕЛЫМИ прогонами и НИКОГДА — денежными.

        Почему не построчно, как везде выше: события сшиты хэш-цепочкой внутри run_id, и удаление
        отдельного звена неотличимо от подделки — `verify_chain` увидит `seq_gap` и объявит
        подделкой то, что сделала штатная уборка. Прогон уходит целиком (заголовок + все события)
        либо не уходит вовсе.

        Прогон с ХОТЬ ОДНИМ событием денежного пути пропускаем целиком. Это не оптимизация под
        триггер (он и так отказал бы), а правило: денежный след живёт дольше наблюдаемости, и его
        архивация — ручная процедура (docs/BACKUP.md), а не побочный эффект джобы."""
        from db.models import AgentRun, AgentRunEvent

        protected = set(
            (
                await s.execute(
                    select(AgentRunEvent.run_id)
                    .where(AgentRunEvent.kind.in_(sorted(MONEY_KINDS)))
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        rows = (await s.execute(select(AgentRun.run_id, AgentRun.started_at))).all()
        stale = sorted({rid for rid, started in rows if _older_than(started, cutoff)} - protected)
        for i in range(0, len(stale), 500):
            batch = stale[i : i + 500]
            await s.execute(sa_delete(AgentRunEvent).where(AgentRunEvent.run_id.in_(batch)))
            await s.execute(sa_delete(AgentRun).where(AgentRun.run_id.in_(batch)))
        return len(stale)

    async def _rollback_watch(s, cutoff) -> int:
        """Волна 4: журнал наблюдений за применёнными мутациями. Денежного следа тут нет — он в
        `audit_log` и `agent_run_events`, — поэтому строку можно убирать обычным ретеншном.
        Открытые наблюдения (`watching`) не трогаем: их окно могло не закрыться."""
        from db.models import RollbackWatch

        rows = (
            await s.execute(
                select(RollbackWatch.id, RollbackWatch.created_at).where(
                    RollbackWatch.state != "watching"
                )
            )
        ).all()
        ids = [i for i, created in rows if _older_than(created, cutoff)]
        for i in range(0, len(ids), 500):
            await s.execute(sa_delete(RollbackWatch).where(RollbackWatch.id.in_(ids[i : i + 500])))
        return len(ids)

    with request_scope("scheduler:purge"):  # §15: корреляция логов джобы по request_id
        await _sweep("error_events", settings.error_events_retain_days, _error_events)
        await _sweep("crawl_jobs", settings.crawl_jobs_retain_days, _crawl_jobs)
        await _sweep("account_health_snapshot", settings.account_health_retain_days, _health)
        await _sweep("site_page_text", settings.site_page_text_retain_days, _site_text)
        await _sweep("ads_quota_ops", settings.ads_quota_ops_retain_days, _quota_ops)
        await _sweep("agent_runs", settings.agent_runs_retain_days, _agent_runs)
        await _sweep("rollback_watch", settings.rollback_watch_retain_days, _rollback_watch)
        from operations.retention import purge_operational_rows

        result.update(await purge_operational_rows(now=now))
        if any(result.values()):
            log.info(
                "scheduler: purge — error_events удалено %d, crawl_jobs %d, health-снапшотов %d, "
                "текстов страниц обнулено %d, quota-строк удалено %d, прогонов агента %d, "
                "наблюдений отката %d",
                result["error_events"],
                result["crawl_jobs"],
                result["account_health_snapshot"],
                result["site_page_text"],
                result["ads_quota_ops"],
                result["agent_runs"],
                result["rollback_watch"],
            )
        return result
