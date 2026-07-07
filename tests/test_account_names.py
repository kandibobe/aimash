"""2.1 (аудит 2026-07-06): заголовки показывают «Имя · id» как в пикере, а не голый id.

Регресс-гард: без имени (нет meta) — прежний формат (голый id / маска …last4).
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bot import texts  # noqa: E402
from reports.period import last_n_days  # noqa: E402
from reports.queries import Metrics  # noqa: E402
from reports.service import ReportData, summary_text  # noqa: E402


def _report(name: str = "") -> ReportData:
    m = Metrics(impressions=10, clicks=1, cost_micros=1_000_000, conversions=0.0, conv_value=0.0)
    return ReportData("5437782039", last_n_days(7), m, None, [], "UAH", name)


def test_summary_text_shows_name_when_known():
    txt = summary_text(_report("Башня"), "ru")
    assert "Башня · 5437782039" in txt
    txt_en = summary_text(_report("Башня"), "en")
    assert "Башня · 5437782039" in txt_en


def test_summary_text_plain_id_without_name():
    assert "Аккаунт 5437782039 ·" in summary_text(_report(), "ru")


def test_fmt_stats_and_campaigns_title_with_name():
    st = {"impressions": 1, "clicks": 1, "cost": 1.0, "conversions": 0, "conv_value": 0}
    out = texts.fmt_stats("5437782039", 30, st, "UAH", "ru", name="Башня")
    assert "Башня · …2039" in out
    out_old = texts.fmt_stats("5437782039", 30, st, "UAH", "ru")
    assert "…2039" in out_old and "Башня" not in out_old  # без имени — как раньше
    assert "Башня · …2039" in texts.campaigns_title("5437782039", "ru", name="Башня")
    assert "…2039" in texts.campaigns_title("5437782039", "ru")
