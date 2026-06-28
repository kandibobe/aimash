"""Сборка глубокого отчёта по аккаунту: totals + сравнение период-к-периоду + разбивки.

READ-ONLY. SDK-вызовы синхронные → бот оборачивает build_account_report в asyncio.to_thread.
Экспорт в .xlsx — reports.xlsx. Google Sheets отложен (под-шаг, нужен живой OAuth scope
spreadsheets/drive.file — валидируется только на боевом OAuth, как A-geo).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any

from ads.client import ensure_allowed
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
    client, customer_id: str, period: Period, *, with_comparison: bool = True, currency: str = ""
) -> ReportData:
    """Собрать отчёт: итоги, (опц.) предыдущий равный период, все разбивки ТЗ §9.

    currency — код валюты аккаунта для денежных метрик (§9); читается вызывающим (bot) через
    ads.read.account_currency и передаётся сюда. Пустой → отчёт без явной валюты (как раньше)."""
    ensure_allowed(customer_id)  # быстрый отказ; каждый fetch_* проверяет ещё раз
    totals = fetch_totals(client, customer_id, period)
    prev_totals = fetch_totals(client, customer_id, period.previous()) if with_comparison else None
    breakdowns = [f(client, customer_id, period) for f in BREAKDOWN_FETCHERS]
    return ReportData(str(customer_id), period, totals, prev_totals, breakdowns, currency)


async def build_account_report_async(
    client, customer_id: str, period: Period, *, with_comparison: bool = True, currency: str = ""
) -> ReportData:
    """Async-сборка отчёта — ИДЕНТИЧНА build_account_report по данным, но независимые GAQL-фетчеры
    идут ПАРАЛЛЕЛЬНО, а не последовательно (раньше 9 round-trip подряд в одном to_thread).

    Каждый fetch_* (totals + prev + 7 разбивок) данных друг от друга НЕ зависит — собираем их через
    run_ads_read_call (read-safe: ретраит TimeoutError+транзиентные) под общим семафором Google Ads
    (ADS_MAX_CONCURRENCY): 9 запросов укладываются в ~ceil(9/4)=3 волны вместо 9 → ~2.5-3x быстрее
    /report, /export, /sheets. asyncio.gather СОХРАНЯЕТ порядок отправки → разбивки остаются в
    порядке BREAKDOWN_FETCHERS, summary_text по ключу 'campaign' резолвится как прежде. ensure_allowed
    держится и здесь (быстрый fail-closed до фан-аута), и внутри каждого fetch_* (замок аккаунта §9).
    Синхронный build_account_report оставлен дословно — на нём строятся тесты/не-async вызовы."""
    ensure_allowed(customer_id)  # быстрый fail-closed ДО фан-аута; каждый fetch_* проверит ещё раз
    # Гетерогенный список: fetch_totals → Metrics, разбивки → Breakdown. gather распаковываем
    # позиционно (totals/prev/breakdowns), статически тип элементов тут не отследить → Awaitable[Any].
    tasks: list[Awaitable[Any]] = [
        run_ads_read_call(fetch_totals, client, customer_id, period, label="rpt_totals")
    ]
    if with_comparison:
        tasks.append(
            run_ads_read_call(
                fetch_totals, client, customer_id, period.previous(), label="rpt_prev"
            )
        )
    tasks += [
        run_ads_read_call(f, client, customer_id, period, label=f"rpt_{f.__name__}")
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


def summary_text(report: ReportData) -> str:
    """Короткая сводка для Telegram (/report): период, итоги, сравнение, топ-кампании.
    Денежные метрики — с разделителем тысяч и кодом валюты (§9); дельты — со стрелками ▲/▼."""
    t = report.totals
    p = report.period
    cur = report.currency
    lines = [
        f"📊 Аккаунт {report.customer_id} · {p.label} ({p.date_from} — {p.date_to})",
        f"Показы {_thou(t.impressions)} · Клики {_thou(t.clicks)} · CTR {t.ctr * 100:.1f}%",
        f"Расход {_money(t.cost, cur)} · CPC {_money(t.avg_cpc, cur)} · Конв. {t.conversions:.1f} · "
        f"CPA {_money(t.cpa, cur)} · ROAS {t.roas:.2f}",
    ]
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
