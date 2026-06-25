"""Сборка глубокого отчёта по аккаунту: totals + сравнение период-к-периоду + разбивки.

READ-ONLY. SDK-вызовы синхронные → бот оборачивает build_account_report в asyncio.to_thread.
Экспорт в .xlsx — reports.xlsx. Google Sheets отложен (под-шаг, нужен живой OAuth scope
spreadsheets/drive.file — валидируется только на боевом OAuth, как A-geo).
"""

from __future__ import annotations

from dataclasses import dataclass

from ads.client import ensure_allowed
from reports.period import Period
from reports.queries import BREAKDOWN_FETCHERS, Breakdown, Metrics, fetch_totals


@dataclass
class ReportData:
    customer_id: str
    period: Period
    totals: Metrics
    prev_totals: Metrics | None  # None, если сравнение не запрашивали
    breakdowns: list[Breakdown]


def build_account_report(
    client, customer_id: str, period: Period, *, with_comparison: bool = True
) -> ReportData:
    """Собрать отчёт: итоги, (опц.) предыдущий равный период, все разбивки ТЗ §9."""
    ensure_allowed(customer_id)  # быстрый отказ; каждый fetch_* проверяет ещё раз
    totals = fetch_totals(client, customer_id, period)
    prev_totals = fetch_totals(client, customer_id, period.previous()) if with_comparison else None
    breakdowns = [f(client, customer_id, period) for f in BREAKDOWN_FETCHERS]
    return ReportData(str(customer_id), period, totals, prev_totals, breakdowns)


def _delta_pct(now: float, prev: float) -> str:
    if not prev:
        return "—" if not now else "+∞"
    return f"{(now - prev) / prev * 100:+.0f}%"


def summary_text(report: ReportData) -> str:
    """Короткая сводка для Telegram (/report): период, итоги, сравнение, топ-кампании."""
    t = report.totals
    p = report.period
    lines = [
        f"📊 Аккаунт {report.customer_id} · {p.label} ({p.date_from} — {p.date_to})",
        f"Показы {t.impressions} · Клики {t.clicks} · CTR {t.ctr * 100:.1f}%",
        f"Расход {t.cost:.2f} · CPC {t.avg_cpc:.2f} · Конв. {t.conversions:.1f} · "
        f"CPA {t.cpa:.2f} · ROAS {t.roas:.2f}",
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
                f"  • {name}: расход {m.cost:.2f}, клики {m.clicks}, конв. {m.conversions:.1f}"
            )
    return "\n".join(lines)
