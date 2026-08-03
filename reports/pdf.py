"""Polished monthly Google Ads PDF from verified report data plus advisory narrative."""

from __future__ import annotations

import html
import os
from pathlib import Path
from typing import Iterable

from reports.service import ReportData


def _font_paths() -> tuple[Path, Path]:
    configured = os.getenv("AIMASH_PDF_FONT_PATH", "").strip()
    regular_candidates = [
        Path(configured) if configured else None,
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/dejavu/DejaVuSans.ttf"),
        Path("C:/Windows/Fonts/DejaVuSans.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
    ]
    for regular in regular_candidates:
        if regular is None or not regular.is_file():
            continue
        bold_candidates = [
            regular.with_name("DejaVuSans-Bold.ttf"),
            regular.with_name("arialbd.ttf"),
        ]
        bold = next((item for item in bold_candidates if item.is_file()), regular)
        return regular, bold
    raise RuntimeError(
        "PDF font with Cyrillic support is unavailable; install fonts-dejavu-core or set "
        "AIMASH_PDF_FONT_PATH"
    )


def _safe(value: object, *, limit: int = 4000) -> str:
    text = str(value or "").replace("\x00", " ").strip()[:limit]
    return html.escape(text).replace("\n", "<br/>")


def _money(value: float, currency: str) -> str:
    rendered = f"{value:,.2f}".replace(",", " ")
    return f"{rendered} {currency}" if currency else rendered


def _delta(current: float, previous: float) -> str:
    if previous == 0:
        return "n/a" if current == 0 else "+inf"
    return f"{(current - previous) / previous * 100:+.1f}%"


def _bounded_items(items: Iterable[str] | None, *, max_items: int = 30) -> list[str]:
    rows = [str(item).strip()[:2000] for item in (items or []) if str(item).strip()]
    if len(rows) > max_items:
        raise ValueError(f"PDF narrative section exceeds {max_items} items")
    return rows


def write_monthly_report_pdf(
    report: ReportData,
    path: str | Path,
    *,
    language: str = "ru",
    executive_summary: str,
    work_completed: list[str] | None = None,
    measured_results: list[str] | None = None,
    risks: list[str] | None = None,
    next_month_plan: list[str] | None = None,
) -> None:
    """Write a human-review PDF. Narrative is advisory; metrics come from ``ReportData``."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        KeepTogether,
        ListFlowable,
        ListItem,
        LongTable,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    if language not in {"ru", "en"}:
        raise ValueError("language must be ru or en")
    summary = str(executive_summary or "").strip()
    if not summary:
        raise ValueError("executive_summary is required for a monthly PDF")
    if len(summary) > 12_000:
        raise ValueError("executive_summary exceeds 12000 characters")
    sections = {
        "work": _bounded_items(work_completed),
        "results": _bounded_items(measured_results),
        "risks": _bounded_items(risks),
        "plan": _bounded_items(next_month_plan),
    }

    regular_path, bold_path = _font_paths()
    if "AimashSans" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("AimashSans", str(regular_path)))
    if "AimashSans-Bold" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("AimashSans-Bold", str(bold_path)))

    labels = (
        {
            "title": "Ежемесячный отчёт Google Ads",
            "summary": "Executive summary",
            "verified": "Проверенные метрики",
            "work": "Что сделано",
            "results": "Измеренный результат",
            "risks": "Риски и ограничения",
            "plan": "План на следующий месяц",
            "campaigns": "Кампании с наибольшим расходом",
            "metric": "Метрика",
            "current": "Текущий период",
            "previous": "Предыдущий период",
            "change": "Изменение",
            "campaign": "Кампания",
            "status": "Статус",
            "spend": "Расход",
            "conversions": "Конверсии",
            "note": (
                "Метрики получены из Google Ads API. Нарратив является рекомендацией для "
                "проверки; он не доказывает причинность и не означает, что изменения применены."
            ),
            "page": "Страница",
        }
        if language == "ru"
        else {
            "title": "Monthly Google Ads Report",
            "summary": "Executive summary",
            "verified": "Verified performance",
            "work": "Work completed",
            "results": "Measured results",
            "risks": "Risks and limitations",
            "plan": "Next month plan",
            "campaigns": "Campaigns with the highest spend",
            "metric": "Metric",
            "current": "Current period",
            "previous": "Previous period",
            "change": "Change",
            "campaign": "Campaign",
            "status": "Status",
            "spend": "Spend",
            "conversions": "Conversions",
            "note": (
                "Metrics come from the Google Ads API. Narrative is advisory for human review; "
                "it does not prove causality or state that any change was applied."
            ),
            "page": "Page",
        }
    )

    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "AimashBody",
        parent=styles["BodyText"],
        fontName="AimashSans",
        fontSize=9.5,
        leading=14,
        textColor=colors.HexColor("#263238"),
        spaceAfter=5,
    )
    title_style = ParagraphStyle(
        "AimashTitle",
        parent=body,
        fontName="AimashSans-Bold",
        fontSize=22,
        leading=27,
        alignment=TA_LEFT,
        textColor=colors.HexColor("#102A43"),
        spaceAfter=8,
    )
    heading = ParagraphStyle(
        "AimashHeading",
        parent=body,
        fontName="AimashSans-Bold",
        fontSize=13,
        leading=17,
        textColor=colors.HexColor("#0F6B78"),
        spaceBefore=10,
        spaceAfter=7,
    )
    small = ParagraphStyle(
        "AimashSmall",
        parent=body,
        fontSize=7.5,
        leading=10,
        textColor=colors.HexColor("#52606D"),
        alignment=TA_CENTER,
    )
    table_header = ParagraphStyle(
        "AimashTableHeader",
        parent=body,
        fontName="AimashSans-Bold",
        fontSize=8,
        leading=10,
        textColor=colors.white,
        alignment=TA_CENTER,
    )

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(
        str(target),
        pagesize=A4,
        leftMargin=17 * mm,
        rightMargin=17 * mm,
        topMargin=18 * mm,
        bottomMargin=17 * mm,
        title=labels["title"],
        author="Aimash",
    )
    account = report.account_name or report.customer_id
    footer_account = " ".join(str(account).split())[:100]
    period_text = f"{report.period.date_from.isoformat()} - {report.period.date_to.isoformat()}"
    story = [
        Paragraph(labels["title"], title_style),
        Paragraph(f"{_safe(account)} · {_safe(period_text)}", body),
        Spacer(1, 4 * mm),
    ]

    t = report.totals
    p = report.prev_totals
    card_data = [
        [
            Paragraph(labels["spend"], table_header),
            Paragraph(labels["conversions"], table_header),
            Paragraph("CPA", table_header),
            Paragraph("ROAS", table_header),
        ],
        [
            _money(t.cost, report.currency),
            f"{t.conversions:.2f}",
            _money(t.cpa, report.currency),
            f"{t.roas:.2f}",
        ],
    ]
    cards = Table(card_data, colWidths=[42 * mm] * 4, rowHeights=[9 * mm, 13 * mm])
    cards.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0F6B78")),
                ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#E8F3F5")),
                ("FONTNAME", (0, 1), (-1, 1), "AimashSans-Bold"),
                ("FONTSIZE", (0, 1), (-1, 1), 11),
                ("TEXTCOLOR", (0, 1), (-1, 1), colors.HexColor("#102A43")),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#B8CDD1")),
            ]
        )
    )
    story.extend([cards, Paragraph(labels["summary"], heading), Paragraph(_safe(summary), body)])

    metric_rows = [
        [labels["metric"], labels["current"], labels["previous"], labels["change"]],
        [
            labels["spend"],
            _money(t.cost, report.currency),
            _money(p.cost, report.currency) if p else "n/a",
            _delta(t.cost, p.cost) if p else "n/a",
        ],
        [
            "Clicks",
            f"{t.clicks:,}".replace(",", " "),
            f"{p.clicks:,}".replace(",", " ") if p else "n/a",
            _delta(t.clicks, p.clicks) if p else "n/a",
        ],
        [
            labels["conversions"],
            f"{t.conversions:.2f}",
            f"{p.conversions:.2f}" if p else "n/a",
            _delta(t.conversions, p.conversions) if p else "n/a",
        ],
        [
            "CPA",
            _money(t.cpa, report.currency),
            _money(p.cpa, report.currency) if p else "n/a",
            _delta(t.cpa, p.cpa) if p else "n/a",
        ],
        [
            "ROAS",
            f"{t.roas:.2f}",
            f"{p.roas:.2f}" if p else "n/a",
            _delta(t.roas, p.roas) if p else "n/a",
        ],
    ]
    metrics_table = Table(metric_rows, colWidths=[45 * mm, 43 * mm, 43 * mm, 35 * mm], repeatRows=1)
    metrics_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#102A43")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "AimashSans-Bold"),
                ("FONTNAME", (0, 1), (0, -1), "AimashSans-Bold"),
                ("FONTNAME", (1, 1), (-1, -1), "AimashSans"),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                (
                    "ROWBACKGROUNDS",
                    (0, 1),
                    (-1, -1),
                    [colors.white, colors.HexColor("#F4F7F9")],
                ),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#C7D3DD")),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.extend([Paragraph(labels["verified"], heading), metrics_table])

    def add_list_section(key: str) -> None:
        items = sections[key]
        if not items:
            return
        flow = ListFlowable(
            [
                ListItem(Paragraph(_safe(item, limit=2000), body), leftIndent=4 * mm)
                for item in items
            ],
            bulletType="bullet",
            start="circle",
            leftIndent=6 * mm,
            bulletFontName="AimashSans",
            bulletFontSize=7,
        )
        story.extend([Paragraph(labels[key], heading), flow])

    add_list_section("work")
    add_list_section("results")
    add_list_section("risks")
    add_list_section("plan")

    campaign_breakdown = next((item for item in report.breakdowns if item.key == "campaign"), None)
    if campaign_breakdown and campaign_breakdown.rows:
        story.extend([PageBreak(), Paragraph(labels["campaigns"], heading)])
        rows = [[labels["campaign"], labels["status"], labels["spend"], labels["conversions"]]]
        for dimensions, metrics in campaign_breakdown.rows[:15]:
            name = dimensions[0] if dimensions else ""
            status = dimensions[1] if len(dimensions) > 1 else ""
            rows.append(
                [
                    Paragraph(_safe(name, limit=300), body),
                    _safe(status, limit=80),
                    _money(metrics.cost, report.currency),
                    f"{metrics.conversions:.2f}",
                ]
            )
        campaign_table = LongTable(
            rows, colWidths=[78 * mm, 30 * mm, 35 * mm, 27 * mm], repeatRows=1
        )
        campaign_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#102A43")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "AimashSans-Bold"),
                    ("FONTNAME", (0, 1), (-1, -1), "AimashSans"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    (
                        "ROWBACKGROUNDS",
                        (0, 1),
                        (-1, -1),
                        [colors.white, colors.HexColor("#F4F7F9")],
                    ),
                    ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#C7D3DD")),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        story.append(campaign_table)
        if campaign_breakdown.note:
            story.append(Paragraph(_safe(campaign_breakdown.note), body))

    story.append(KeepTogether([Spacer(1, 5 * mm), Paragraph(_safe(labels["note"]), small)]))

    def draw_footer(canvas, doc) -> None:
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#C7D3DD"))
        canvas.line(17 * mm, 12 * mm, A4[0] - 17 * mm, 12 * mm)
        canvas.setFillColor(colors.HexColor("#52606D"))
        canvas.setFont("AimashSans", 7.5)
        canvas.drawString(17 * mm, 7.5 * mm, f"Aimash · {footer_account}")
        canvas.drawRightString(A4[0] - 17 * mm, 7.5 * mm, f"{labels['page']} {doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
