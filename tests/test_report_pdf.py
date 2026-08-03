from __future__ import annotations

from datetime import date

import pytest

pytest.importorskip("reportlab")

from reports.pdf import write_monthly_report_pdf
from reports.period import Period
from reports.queries import Breakdown, Metrics
from reports.service import ReportData


def _report() -> ReportData:
    return ReportData(
        customer_id="1234567890",
        account_name="Тестовый клиент",
        period=Period(date(2026, 7, 1), date(2026, 7, 31), "июль"),
        totals=Metrics(
            impressions=120_000,
            clicks=6_000,
            cost_micros=12_500_000_000,
            conversions=250,
            conv_value=50_000,
        ),
        prev_totals=Metrics(
            impressions=100_000,
            clicks=5_000,
            cost_micros=10_000_000_000,
            conversions=200,
            conv_value=38_000,
        ),
        breakdowns=[
            Breakdown(
                key="campaign",
                title="Кампании",
                dim_headers=["Кампания", "Статус"],
                rows=[
                    (
                        (f"Кампания {index}", "ENABLED"),
                        Metrics(cost_micros=(20 - index) * 100_000_000, conversions=20 - index),
                    )
                    for index in range(1, 18)
                ],
                note="Показаны кампании с наибольшим расходом.",
            )
        ],
        currency="EUR",
    )


def test_monthly_pdf_has_cyrillic_font_and_multiple_pages(tmp_path):
    path = tmp_path / "monthly.pdf"
    write_monthly_report_pdf(
        _report(),
        path,
        executive_summary="Рост результата требует проверки причин и качества трекинга.",
        work_completed=["Проведён аудит структуры и поисковых запросов."],
        measured_results=["Показатели приведены только из Google Ads API."],
        risks=["Причинность изменений не доказана."],
        next_month_plan=["Сначала проверить конверсии, затем обсуждать изменения."],
    )
    payload = path.read_bytes()
    assert payload.startswith(b"%PDF-")
    assert len(payload) > 20_000
    assert payload.count(b"/Type /Page") >= 2


def test_monthly_pdf_rejects_missing_summary(tmp_path):
    with pytest.raises(ValueError, match="executive_summary"):
        write_monthly_report_pdf(_report(), tmp_path / "bad.pdf", executive_summary="")
