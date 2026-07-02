"""Тесты async MCC-сводки §8 (reports.mcc.build_mcc_summary_async) + форматтер summary_text_mcc +
xlsx-экспорт build_mcc_workbook. Офлайн, без SDK: list_children/fetch инъектируются.

Проверяют: параллельный фан-аут с частичными сбоями (один упавший дочерний → summary.errors, а не
провал всей сводки), пропуск менеджерских/не-read-allowed (fail-closed), агрегацию по валютам в
тексте, и структуру .xlsx (3 листа)."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.read import ChildAccount  # noqa: E402
from core.config import settings  # noqa: E402
from reports.mcc import build_mcc_summary_async  # noqa: E402
from reports.period import last_n_days  # noqa: E402
from reports.queries import Metrics  # noqa: E402
from reports.service import summary_text_mcc  # noqa: E402
from reports.xlsx import build_mcc_workbook  # noqa: E402


@contextmanager
def _ids(allowed: str, read: str):
    pa, pr = settings.google_ads_allowed_customer_ids, settings.google_ads_read_customer_ids
    settings.google_ads_allowed_customer_ids = allowed
    settings.google_ads_read_customer_ids = read
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = pa
        settings.google_ads_read_customer_ids = pr


def _child(cid: str, currency: str, *, manager: bool = False) -> ChildAccount:
    return ChildAccount(
        id=cid, name=f"acct-{cid}", currency=currency, manager=manager, level=1, status="ENABLED"
    )


def _m(cost_micros: int = 0, clicks: int = 0, conv_value: float = 0.0) -> Metrics:
    return Metrics(clicks=clicks, cost_micros=cost_micros, conv_value=conv_value)


DRAFT = "7753643025"
CHILD_OK = "1112223334"
CHILD_FAIL = "2223334445"
CHILD_BLOCKED = "9998887776"
MGR = "5556667778"


async def test_async_summary_parallel_with_partial_failure():
    """Один дочерний роняет fetch → попадает в errors (редактированно), остальные собираются;
    менеджерский и не-read-allowed — пропущены (fail-closed)."""
    children = [
        _child(MGR, "USD", manager=True),
        _child(DRAFT, "USD"),
        _child(CHILD_OK, "EUR"),
        _child(CHILD_FAIL, "EUR"),
        _child(CHILD_BLOCKED, "USD"),
    ]

    def fake_list(_client, _manager_id):
        return children

    def fake_fetch(_client, cid, _period):
        if cid == CHILD_FAIL:
            raise RuntimeError("boom secret=1//abc")  # должно отредактироваться
        return _m(cost_micros=1_000_000, clicks=10, conv_value=5.0)

    period = last_n_days(7)
    with _ids(allowed=DRAFT, read=f"{CHILD_OK},{CHILD_FAIL}"):
        summary = await build_mcc_summary_async(
            object(), MGR, period, list_children=fake_list, fetch=fake_fetch
        )

    assert summary.managers == [MGR]
    assert summary.skipped == [CHILD_BLOCKED]
    assert {c.account.id for c in summary.children} == {DRAFT, CHILD_OK}  # FAIL не в children
    assert len(summary.errors) == 1 and summary.errors[0][0] == CHILD_FAIL
    assert "1//abc" not in summary.errors[0][1]  # секрет отредактирован (golden rule #5)


async def test_summary_text_mcc_renders_currencies_and_counts():
    children = [_child(DRAFT, "USD"), _child(CHILD_OK, "EUR")]

    def fake_fetch(_client, _cid, _period):
        return _m(cost_micros=2_000_000, clicks=20, conv_value=8.0)

    period = last_n_days(7)
    with _ids(allowed=DRAFT, read=CHILD_OK):
        summary = await build_mcc_summary_async(
            object(), MGR, period, list_children=lambda *_: children, fetch=fake_fetch
        )
    text = summary_text_mcc(summary, lang="ru")
    assert f"MCC {MGR}" in text
    assert "USD" in text and "EUR" in text
    assert "Аккаунтов с данными: <b>2</b>" in text  # HTML parse_mode (§8 читаемая сводка)


async def test_build_mcc_workbook_has_three_sheets():
    children = [_child(DRAFT, "USD"), _child(CHILD_OK, "EUR")]
    period = last_n_days(7)
    with _ids(allowed=DRAFT, read=CHILD_OK):
        summary = await build_mcc_summary_async(
            object(),
            MGR,
            period,
            list_children=lambda *_: children,
            fetch=lambda *_: _m(cost_micros=3_000_000, clicks=30),
        )
    wb = build_mcc_workbook(summary)
    assert wb.sheetnames == ["Сводка MCC", "Аккаунты", "Пропущено и ошибки"]
    acc = wb["Аккаунты"]
    # шапка + 2 аккаунта
    assert acc.max_row == 3
    assert acc.cell(row=1, column=1).value == "ID"
