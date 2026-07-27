"""Тесты Shadow Mode (scheduler.shadow_mode)."""

from datetime import date, timedelta
from scheduler.shadow_mode import (
    ShadowRecommendation,
    ShadowOutcome,
    ShadowReport,
    _build_report,
    format_for_telegram,
)


class TestBuildReport:
    def test_empty(self):
        report = _build_report("6764040266", date.today() - timedelta(days=1), [])
        assert report.recommendations_total == 0
        assert report.would_help_count == 0
        assert report.would_hurt_count == 0
        assert report.undetermined_count == 0

    def test_mixed_outcomes(self):
        outcomes = [
            ShadowOutcome(
                recommendation=ShadowRecommendation(
                    account="6764040266",
                    campaign_id="123",
                    campaign_name="Test Campaign",
                    operation="budget_change",
                ),
                would_help=True,
                actual_cpa_change=-5.0,
                verdict="Помогло бы",
            ),
            ShadowOutcome(
                recommendation=ShadowRecommendation(
                    account="6764040266",
                    campaign_id="456",
                    campaign_name="Another Campaign",
                    operation="pause_campaign",
                ),
                would_help=False,
                actual_cpa_change=10.0,
                verdict="Ухудшило бы",
            ),
            ShadowOutcome(
                recommendation=ShadowRecommendation(
                    account="6764040266",
                    campaign_id="789",
                    campaign_name="Unknown",
                    operation="bid_adjust",
                ),
                would_help=None,
                verdict="Неопределено",
            ),
        ]
        report = _build_report("6764040266", date.today() - timedelta(days=1), outcomes)
        assert report.recommendations_total == 3
        assert report.would_help_count == 1
        assert report.would_hurt_count == 1
        assert report.undetermined_count == 1


class TestFormatForTelegram:
    def test_formatted_output(self):
        report = ShadowReport(
            date="2026-07-24",
            accounts_checked=1,
            campaigns_checked=10,
            recommendations_total=10,
            would_help_count=6,
            would_hurt_count=2,
            undetermined_count=2,
            summary="Test summary",
        )
        output = format_for_telegram(report)
        assert "Test summary" in output