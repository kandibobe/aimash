"""Сборка глубокого отчёта по аккаунту: totals + сравнение период-к-периоду + разбивки.

READ-ONLY. SDK-вызовы синхронные → бот оборачивает build_account_report в asyncio.to_thread.
Экспорт в .xlsx — reports.xlsx. Экспорт в Google Sheets — reports.sheets (publish_report_to_sheets,
вкладка на разбивку), команда /sheets; scope spreadsheets/drive.file проверяется на боевом OAuth.
"""

from __future__ import annotations

import asyncio
import html
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any

from ads.client import ensure_read_allowed
from core.resilience import run_ads_read_call
from reports.period import Period
from reports.queries import BREAKDOWN_FETCHERS, Breakdown, Metrics, fetch_totals


@dataclass
class ReportData:
    customer_id: str
    period: Period
    totals: Metrics
    prev_totals: Metrics | None  # None, если сравнение не запрашивали
    breakdowns: list[Breakdown]
    currency: str = ""  # код валюты аккаунта (§9), напр. "USD"; "" → не показываем


def build_account_report(
    client,
    customer_id: str,
    period: Period,
    *,
    with_comparison: bool = True,
    currency: str = "",
    campaign_id: str | None = None,
) -> ReportData:
    """Собрать отчёт: итоги, (опц.) предыдущий равный период, все разбивки ТЗ §9.

    currency — код валюты аккаунта для денежных метрик (§9); читается вызывающим (bot) через
    ads.read.account_currency и передаётся сюда. Пустой → отчёт без явной валюты (как раньше).
    campaign_id — опц. фильтр по ОДНОЙ кампании (id). None ⇒ отчёт по всему аккаунту (как раньше)."""
    ensure_read_allowed(customer_id)  # быстрый отказ; каждый fetch_* проверяет ещё раз
    totals = fetch_totals(client, customer_id, period, campaign_id)
    prev_totals = (
        fetch_totals(client, customer_id, period.previous(), campaign_id)
        if with_comparison
        else None
    )
    breakdowns = [f(client, customer_id, period, campaign_id) for f in BREAKDOWN_FETCHERS]
    return ReportData(str(customer_id), period, totals, prev_totals, breakdowns, currency)


async def build_account_report_async(
    client,
    customer_id: str,
    period: Period,
    *,
    with_comparison: bool = True,
    currency: str = "",
    campaign_id: str | None = None,
) -> ReportData:
    """Async-сборка отчёта — ИДЕНТИЧНА build_account_report по данным, но независимые GAQL-фетчеры
    идут ПАРАЛЛЕЛЬНО, а не последовательно (раньше 9 round-trip подряд в одном to_thread).

    Каждый fetch_* (totals + prev + 7 разбивок) данных друг от друга НЕ зависит — собираем их через
    run_ads_read_call (read-safe: ретраит TimeoutError+транзиентные) под общим семафором Google Ads
    (ADS_MAX_CONCURRENCY): 9 запросов укладываются в ~ceil(9/4)=3 волны вместо 9 → ~2.5-3x быстрее
    /report, /export, /sheets. asyncio.gather СОХРАНЯЕТ порядок отправки → разбивки остаются в
    порядке BREAKDOWN_FETCHERS, summary_text по ключу 'campaign' резолвится как прежде. Замок ЧТЕНИЯ
    держится и здесь (быстрый fail-closed до фан-аута), и внутри каждого fetch_* (ensure_read_allowed, §8).
    Синхронный build_account_report оставлен дословно — на нём строятся тесты/не-async вызовы."""
    ensure_read_allowed(customer_id)  # fail-closed ДО фан-аута; каждый fetch_* проверит ещё раз
    # Гетерогенный список: fetch_totals → Metrics, разбивки → Breakdown. gather распаковываем
    # позиционно (totals/prev/breakdowns), статически тип элементов тут не отследить → Awaitable[Any].
    # campaign_id идёт 4-м ПОЗИЦИОННЫМ аргументом в каждый fetch_* (run_ads_read_call форвардит *args);
    # None ⇒ запросы БАЙТ-в-БАЙТ как раньше (обратная совместимость с тестами).
    tasks: list[Awaitable[Any]] = [
        run_ads_read_call(
            fetch_totals, client, customer_id, period, campaign_id, label="rpt_totals"
        )
    ]
    if with_comparison:
        tasks.append(
            run_ads_read_call(
                fetch_totals, client, customer_id, period.previous(), campaign_id, label="rpt_prev"
            )
        )
    tasks += [
        run_ads_read_call(f, client, customer_id, period, campaign_id, label=f"rpt_{f.__name__}")
        for f in BREAKDOWN_FETCHERS
    ]
    results = await asyncio.gather(*tasks)  # порядок результатов == порядок tasks
    totals = results[0]
    idx = 1
    prev_totals = None
    if with_comparison:
        prev_totals = results[idx]
        idx += 1
    breakdowns = list(results[idx:])  # порядок == BREAKDOWN_FETCHERS
    return ReportData(str(customer_id), period, totals, prev_totals, breakdowns, currency)


def _thou(n: float, dec: int = 0) -> str:
    """Число с пробелом-разделителем тысяч: 12480 -> '12 480', 4512.3 -> '4 512.30'."""
    return f"{n:,.{dec}f}".replace(",", " ")


def _money(n: float, currency: str) -> str:
    """Денежное значение с разделителем тысяч и (опц.) кодом валюты: '4 512.30 USD'."""
    s = _thou(n, 2)
    return f"{s} {currency}" if currency else s


def _delta_pct(now: float, prev: float) -> str:
    """Дельта период-к-периоду со стрелкой ▲/▼ (направление, без семантики «хорошо/плохо»)."""
    if not prev:
        return "—" if not now else "▲ +∞"
    pct = (now - prev) / prev * 100
    arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "•")
    return f"{arrow} {pct:+.0f}%"


def _resolve_lang(lang: str | None) -> str:
    """Язык сводки: явный lang → он; None → язык текущего запроса (contextvar bot.i18n,
    ставит LangMiddleware). i18n импортируем ЛЕНИВО — reports не должен жёстко зависеть от bot."""
    if lang is None:
        from bot import i18n

        return i18n.current_lang()
    return lang if lang in ("ru", "en") else "ru"


def summary_text(report: ReportData, lang: str | None = None) -> str:
    """Короткая сводка для Telegram (/report): период, итоги, сравнение, топ-кампании.
    Денежные метрики — с разделителем тысяч и кодом валюты (§9); дельты — со стрелками ▲/▼."""
    t = report.totals
    p = report.period
    cur = report.currency
    if _resolve_lang(lang) == "en":
        lines = [
            f"📊 Account {report.customer_id} · {p.label} ({p.date_from} — {p.date_to})",
            f"Impressions {_thou(t.impressions)} · Clicks {_thou(t.clicks)} · "
            f"CTR {t.ctr * 100:.1f}%",
            f"Cost {_money(t.cost, cur)} · CPC {_money(t.avg_cpc, cur)} · "
            f"Conv. {t.conversions:.1f} · CPA {_money(t.cpa, cur)} · ROAS {t.roas:.2f}",
        ]
        if t.conv_value > 0:  # §9: ценность конверсий — была в xlsx/Sheets, но не в тексте
            lines.append(f"Conv. value {_money(t.conv_value, cur)}")
        if report.prev_totals is not None:
            pr = report.prev_totals
            lines.append(
                f"vs. prev. period: cost {_delta_pct(t.cost, pr.cost)}, "
                f"clicks {_delta_pct(t.clicks, pr.clicks)}, "
                f"conv. {_delta_pct(t.conversions, pr.conversions)}"
            )
        camp = next((b for b in report.breakdowns if b.key == "campaign"), None)
        if camp and camp.rows:
            lines.append("Top campaigns by cost:")
            for (name, _status), m in camp.rows[:3]:
                lines.append(
                    f"  • {name}: cost {_money(m.cost, cur)}, clicks {_thou(m.clicks)}, "
                    f"conv. {m.conversions:.1f}"
                )
        return "\n".join(lines)
    lines = [
        f"📊 Аккаунт {report.customer_id} · {p.label} ({p.date_from} — {p.date_to})",
        f"Показы {_thou(t.impressions)} · Клики {_thou(t.clicks)} · CTR {t.ctr * 100:.1f}%",
        f"Расход {_money(t.cost, cur)} · CPC {_money(t.avg_cpc, cur)} · Конв. {t.conversions:.1f} · "
        f"CPA {_money(t.cpa, cur)} · ROAS {t.roas:.2f}",
    ]
    if t.conv_value > 0:  # §9: ценность конверсий — была в xlsx/Sheets, но не в тексте
        lines.append(f"Ценность {_money(t.conv_value, cur)}")
    if report.prev_totals is not None:
        pr = report.prev_totals
        lines.append(
            f"к пред. периоду: расход {_delta_pct(t.cost, pr.cost)}, "
            f"клики {_delta_pct(t.clicks, pr.clicks)}, конв. {_delta_pct(t.conversions, pr.conversions)}"
        )
    camp = next((b for b in report.breakdowns if b.key == "campaign"), None)
    if camp and camp.rows:
        lines.append("Топ кампаний по расходу:")
        for (name, _status), m in camp.rows[:3]:
            lines.append(
                f"  • {name}: расход {_money(m.cost, cur)}, клики {_thou(m.clicks)}, "
                f"конв. {m.conversions:.1f}"
            )
    return "\n".join(lines)


# Сколько аккаунтов показываем построчно в /mcc, прежде чем отослать за полной таблицей в /export
# (Telegram-лимит + читаемость: полный список всегда есть в .xlsx через build_mcc_workbook).
MCC_MAX_ACCT_LINES = 60


def _esc(s: str) -> str:
    """HTML-escape для parse_mode=HTML (имена аккаунтов приходят от клиента — экранируем < > &)."""
    return html.escape(str(s), quote=False)


def summary_text_mcc(summary, lang: str | None = None) -> str:
    """Читаемая сводка по дочерним MCC (§8) для Telegram (/mcc), <b>parse_mode=HTML</b>: подытоги ПО
    ВАЛЮТАМ (без FX — деньги не выдумываем), СПИСОК аккаунтов (имя · расход · клики · конв. · CTR),
    и ЧЕСТНЫЕ секции — неактивные / нет доступа / ошибки — ИМЕНАМИ и причиной, а не голым счётчиком.
    `summary` — reports.mcc.MccSummary (duck-typed, чтобы не тянуть reports.mcc на уровне модуля)."""
    p = summary.period
    en = _resolve_lang(lang) == "en"
    n_children = len(summary.children)
    L = _MCC_LABELS_EN if en else _MCC_LABELS_RU
    lines = [
        f"🏢 <b>MCC {summary.manager_id}</b> · {p.label} ({p.date_from} — {p.date_to})",
        f"{L['with_data']}: <b>{n_children}</b>",
    ]

    # Подытоги по валюте (отсортированы по расходу убыв. в aggregate_by_currency). FX НЕ делаем.
    if summary.subtotals:
        lines.append("")
        lines.append(f"💰 <b>{L['by_currency']}</b>")
        for sub in summary.subtotals:
            t = sub.totals
            cur = sub.currency
            lines.append(
                f"• {cur} — {sub.accounts} {L['acct']}: {L['cost']} <b>{_money(t.cost, cur)}</b> · "
                f"{L['clicks']} {_thou(t.clicks)} · {L['conv']} {t.conversions:.1f} · "
                f"CPA {_money(t.cpa, cur)} · ROAS {t.roas:.2f}"
            )

    # Список аккаунтов ПО ВАЛЮТНЫМ СЕКЦИЯМ (3H: кросс-валютное ранжирование по сырым cost_micros
    # вводило в заблуждение — большой номинал «дешёвой» валюты вставал выше). Порядок секций —
    # как в subtotals (валюты по расходу); внутри секции — по расходу убыв. Общий кап строк прежний.
    if summary.children:
        lines.append("")
        lines.append(f"📊 <b>{L['accounts']}</b>")
        by_cur: dict[str, list] = {}
        for cr in summary.children:
            by_cur.setdefault(cr.account.currency or "?", []).append(cr)
        cur_order = [s.currency for s in summary.subtotals] or sorted(by_cur)
        shown = 0
        for cur in cur_order:
            group = sorted(by_cur.get(cur, []), key=lambda c: c.totals.cost_micros, reverse=True)
            if not group or shown >= MCC_MAX_ACCT_LINES:
                continue
            if len(by_cur) > 1:
                lines.append(f"<i>{cur}:</i>")
            for cr in group:
                if shown >= MCC_MAX_ACCT_LINES:
                    break
                name = _esc(getattr(cr.account, "name", "") or cr.account.id)
                m = cr.totals
                lines.append(
                    f"• <b>{name}</b> ({cur}): {L['cost']} <b>{_money(m.cost, cur)}</b> · "
                    f"{L['clicks']} {_thou(m.clicks)} · {L['conv']} {m.conversions:.1f} · "
                    f"CTR {m.ctr * 100:.1f}%"
                )
                shown += 1
        rest = len(summary.children) - shown
        if rest > 0:
            lines.append(f"  <i>{L['more'].format(n=rest)}</i>")

    # Неактивные (не ENABLED) — ИМЕНАМИ + статус (это и есть большинство прежних «ошибок чтения»).
    inactive = getattr(summary, "inactive", [])
    if inactive:
        lines.append("")
        lines.append(f"😴 <b>{L['inactive']}</b> ({len(inactive)})")
        for ch in inactive:
            name = _esc(getattr(ch, "name", "") or ch.id)
            lines.append(f"• {name} — {_esc(getattr(ch, 'status', '') or '?')}")

    # Нет доступа на чтение (fail-closed) — id (имя недоступно, аккаунт не читали).
    if summary.skipped:
        lines.append("")
        lines.append(
            f"🔒 <b>{L['no_access']}</b> ({len(summary.skipped)}): " + ", ".join(summary.skipped)
        )

    # Реальные сбои чтения на ENABLED-аккаунтах — id + причина (редактированная).
    if summary.errors:
        lines.append("")
        lines.append(f"❗ <b>{L['errors']}</b> ({len(summary.errors)})")
        for cid, reason in summary.errors:
            lines.append(f"• {cid}: {_esc(reason)}")

    lines.append("")
    lines.append(f"<i>{L['full_export']}</i>")
    return "\n".join(lines)


_MCC_LABELS_RU = {
    "with_data": "Аккаунтов с данными",
    "by_currency": "Итоги по валютам",
    "acct": "акк",
    "cost": "расход",
    "clicks": "клики",
    "conv": "конв.",
    "accounts": "Аккаунты по расходу",
    "more": "…ещё {n} — полная таблица в /export",
    "inactive": "Неактивные (не ENABLED)",
    "no_access": "Нет доступа на чтение",
    "errors": "Ошибки чтения",
    "full_export": "Полная таблица по всем аккаунтам — /export",
}
_MCC_LABELS_EN = {
    "with_data": "Accounts with data",
    "by_currency": "Totals by currency",
    "acct": "acct",
    "cost": "cost",
    "clicks": "clicks",
    "conv": "conv.",
    "accounts": "Accounts by cost",
    "more": "…{n} more — full table in /export",
    "inactive": "Inactive (not ENABLED)",
    "no_access": "No read access",
    "errors": "Read errors",
    "full_export": "Full per-account table — /export",
}
