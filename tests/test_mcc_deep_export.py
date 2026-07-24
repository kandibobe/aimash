"""2.2 (аудит 2026-07-06): «📊 Все аккаунты (MCC)» в пикерах + глубокий xlsx (лист на аккаунт).

Инварианты: build_mcc_deep_async держит те же замки, что /mcc (менеджерские/вне read-list/
неактивные — в бухгалтерию, не в items; сбой одного ребёнка → errors, не провал); книга содержит
«Сводка» + лист на аккаунт (имя санитизировано, ≤31) + «Пропущено и ошибки»; кнопка idx=-3 есть в
пикерах report/export/sheets и отсутствует у setacct/advise/status.
"""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ads.read import ChildAccount  # noqa: E402
from bot.keyboards import report_accounts_kb  # noqa: E402
from reports.mcc import build_mcc_deep_async  # noqa: E402
from reports.period import last_n_days  # noqa: E402
from reports.queries import Breakdown, Metrics  # noqa: E402
from reports.service import ReportData  # noqa: E402
from reports.xlsx import _sheet_title, build_mcc_deep_workbook  # noqa: E402

DRAFT = "7753643025"


def _child(cid, name, status="ENABLED", manager=False):
    return ChildAccount(id=cid, name=name, currency="UAH", manager=manager, level=1, status=status)


def _report(cid, name):
    m = Metrics(impressions=10, clicks=2, cost_micros=5_000_000, conversions=1.0, conv_value=0.0)
    camp = Breakdown(
        key="campaign",
        title="Кампании",
        dim_headers=["Кампания", "Статус"],
        rows=[(("Camp A", "ENABLED"), m)],
    )
    return ReportData(cid, last_n_days(7), m, None, [camp], "UAH", name)


async def test_deep_traversal_locks_and_partial_failure(monkeypatch):
    import ads.client as ads_client

    kids = [
        _child("1111111111", "Башня"),
        _child("2222222222", "DARIAL"),
        _child("3333333333", "Старый", status="CANCELED"),
        _child("4444444444", "SubMCC", manager=True),
        _child("5555555555", "Чужой"),  # не в read-list → skipped
    ]

    def _fake_children(_client, _mid):
        return kids

    def _fake_ensure_read(cid):
        if cid == "5555555555":
            raise PermissionError("вне read-list")

    monkeypatch.setattr("reports.mcc.ensure_read_allowed", _fake_ensure_read)
    monkeypatch.setattr(ads_client, "ensure_read_allowed", _fake_ensure_read, raising=False)

    async def _fake_report(_client, cid, per, **kw):
        if cid == "2222222222":
            raise RuntimeError("SDK boom")
        return _report(cid, kw.get("account_name", ""))

    deep = await build_mcc_deep_async(
        object(),
        "9990001112",
        last_n_days(7),
        list_children=_fake_children,
        build_report=_fake_report,
    )
    assert [ch.id for ch, _r in deep.items] == ["1111111111"]  # только живой удачный
    assert deep.errors and deep.errors[0][0] == "2222222222"  # сбой виден, книгу не валит
    assert deep.inactive[0].id == "3333333333"
    assert deep.managers == ["4444444444"]
    assert deep.skipped == ["5555555555"]
    assert "boom" in deep.errors[0][1]


def test_deep_workbook_sheets_and_titles():
    deep = SimpleNamespace(
        manager_id="9990001112",
        period=last_n_days(7),
        items=[
            (_child("1111111111", "Башня"), _report("1111111111", "Башня")),
            (_child("2222222222", "Very/Long*Name:Здесь[]?"), _report("2222222222", "X")),
        ],
        skipped=["5555555555"],
        managers=[],
        inactive=[_child("3333333333", "Старый", status="CANCELED")],
        errors=[("6666666666", "RuntimeError: x")],
    )
    wb = build_mcc_deep_workbook(deep)
    names = wb.sheetnames
    assert names[0] == "Сводка" and names[-1] == "Пропущено и ошибки"
    assert len(names) == 2 + len(deep.items)  # сводка + лист на аккаунт + ошибки
    assert all(len(n) <= 31 for n in names)
    assert not any(set("\\/*?:[]") & set(n) for n in names)  # санитизация
    # лист аккаунта содержит таблицу кампаний
    acct_ws = wb[names[1]]
    texts = [str(c.value) for row in acct_ws.iter_rows() for c in row if c.value is not None]
    assert any("Camp A" in t for t in texts)


def test_sheet_title_sanitize_and_cap():
    # D1: в хвосте — ПОЛНЫЙ customer_id (был last4): id уникален ⇒ коллизий имён листов нет
    t = _sheet_title("Очень длинное имя аккаунта клиента", "5437782039")
    assert len(t) <= 31 and t.endswith("_5437782039")
    assert _sheet_title("A/B:C", "1234567890") == "A_B_C_1234567890"


def test_sheet_titles_unique_for_similar_names():
    """D1: раньше «имя[:24]_last4» совпадали у похожих имён, а дедуп-цикл
    `while title in used: title = (title[:29] + "_x")[:31]` для 31-символьного имени был
    ТОЖДЕСТВОМ → бесконечный цикл в потоке пула (вызов шёл без таймаута)."""
    long_name = "Клиент с очень длинным названием компании"
    a = _sheet_title(long_name, "1111112039")
    b = _sheet_title(long_name, "2222222039")  # тот же префикс имени И те же last4
    assert a != b and len(a) <= 31 and len(b) <= 31


def test_unique_sheet_title_terminates_on_collision():
    from reports.xlsx import _unique_sheet_title

    used = {"X" * 31}
    got = _unique_sheet_title("X" * 31, used)  # раньше здесь был вечный цикл
    assert got not in used and len(got) <= 31


def test_picker_has_mcc_button_only_for_report_flows():
    rows = [SimpleNamespace(id=DRAFT, name="Draft", currency="")]
    for target in ("report", "export", "sheets"):
        kb = report_accounts_kb(rows, target)
        texts = [b.text for row in kb.inline_keyboard for b in row]
        assert any("MCC" in t for t in texts), target
    for target in ("status", "advise", "setacct", "campaigns"):
        kb = report_accounts_kb(rows, target)
        texts = [b.text for row in kb.inline_keyboard for b in row]
        assert not any("MCC" in t for t in texts), target
