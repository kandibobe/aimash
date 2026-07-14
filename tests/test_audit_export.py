"""Профессиональная выгрузка /audit в Google Sheets (+ .xlsx файлом) — Часть B.

Инварианты (каждый — про честность/деньги, не про красоту):
• 3 вкладки: Обзор (карточка) + По семьям (свод) + Находки (весь список worst-first) — и в Sheets,
  и в .xlsx (одна раскладка, reports.findings);
• вкладка «По семьям» == result.families (движок посчитал — экспорт не пересчитывает);
• экспорт — БУМАГА: ни колонки suggested_operation, ни кнопки «применить» (золотое правило №3);
• ячейки клиента обезврежены (safe_row: имя кампании `=HYPERLINK(...)` — текст, не формула);
• денежный формат ТОЛЬКО на денежной колонке (иначе проза форматировалась бы как проценты);
• EN-артефакт без кириллицы (RU-утечка), первая вкладка «Overview»;
• publish_audit_to_sheets шарит reader (финансовая бумага), НЕ writer;
• клик по кнопке НЕ пересобирает аудит: холодный кэш → stale-алерт, тёплый → тот же result из кэша.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import openpyxl  # noqa: E402
import pytest  # noqa: E402

from audit.engine import build_audit  # noqa: E402
from reports.findings import (  # noqa: E402
    FAMILY_MONEY_COL,
    FAMILY_SUMMARY_TITLE,
    FINDINGS_TITLE,
    MONEY_COL,
    OVERVIEW_TITLE,
    family_summary_rows,
)
from reports.period import last_n_days  # noqa: E402
from reports.queries import Breakdown, Metrics  # noqa: E402
from reports.sheets import (  # noqa: E402
    SHARE_FAILED,
    build_audit_sheets_data,
    format_requests,
    is_shared,
    publish_audit_to_sheets,
)
from reports.xlsx import build_audit_workbook  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
EVIL = '=HYPERLINK("http://evil","click")'
_CYR = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ")


def _report(campaign: str = "Search Brand"):
    """Аккаунт с расходом и БЕЗ конверсий — движку есть что сказать (иначе тест зелен вхолостую)."""
    m = Metrics(impressions=1000, clicks=100, cost_micros=500_000_000, conversions=0.0)
    camp = Breakdown(
        key="campaign",
        title="Кампании",
        dim_headers=["Кампания", "Статус"],
        rows=[((campaign, "ENABLED"), m)],
    )
    return SimpleNamespace(
        customer_id="7753643025",
        totals=m,
        prev_totals=m,
        period=last_n_days(7, today=date(2026, 6, 25)),
        currency="USD",
        breakdowns=[camp],
    )


def _result(campaign: str = "Search Brand"):
    r = build_audit(_report(campaign))
    assert r.findings and r.families, "движку есть что сказать про этот аккаунт"
    return r


def _cells(rows) -> list[str]:
    return [str(c) for row in rows for c in row if c is not None]


def _xlsx_texts(wb, sheet: str) -> list[str]:
    ws = wb[sheet]
    return [str(c.value) for row in ws.iter_rows() for c in row if c.value is not None]


# ── структура: 3 вкладки в Sheets и в .xlsx ────────────────────────────────────────
def test_audit_export_has_three_sections():
    r = _result()
    assert [t.title for t in build_audit_sheets_data(r, "ru")] == [
        OVERVIEW_TITLE,
        FAMILY_SUMMARY_TITLE,
        FINDINGS_TITLE,
    ]
    assert build_audit_workbook(r, "ru").sheetnames == [
        OVERVIEW_TITLE,
        FAMILY_SUMMARY_TITLE,
        FINDINGS_TITLE,
    ]


def test_overview_tab_is_the_audit_card_prose():
    """Вкладка «Обзор» — та же проза, что в карточке /audit (render_audit actions=False)."""
    from audit.render import render_audit

    r = _result()
    tab = build_audit_sheets_data(r, "ru")[0]
    texts = _cells(tab.rows)
    for line in render_audit(r, "ru", actions=False).split("\n"):
        if line:
            assert line in texts, f"обзор карточки не доехал до вкладки: {line}"


# ── вкладка «По семьям» == result.families (движок посчитал, экспорт не пересчитывает) ──
def test_family_summary_matches_result_families():
    from audit.render import family_label

    r = _result()
    rows = family_summary_rows(r, "ru")
    got = {tuple(row) for row in rows}
    exp = set()
    for fam, d in r.families.items():
        at = round(float(d["at_risk"]), 2) if d["at_risk"] > 0 else ""
        exp.add((family_label(fam, "ru"), int(d["count"]), at, round(float(d["penalty"]), 2)))
    assert got == exp, "свод семей разошёлся с движком"
    penalties = [row[3] for row in rows]
    assert penalties == sorted(penalties, reverse=True), "свод должен идти worst-first (по штрафу)"


def test_family_summary_zero_at_risk_is_blank_not_zero():
    """Неденежная семья (at_risk == 0) → пустая ячейка, а НЕ 0.00 (иначе читалось бы как «денег ноль»)."""
    r = _result()
    for row in family_summary_rows(r, "ru"):
        assert row[2] == "" or row[2] > 0  # «Под риском»: либо пусто, либо положительное число


# ── GR3: экспорт — бумага, не кнопка ────────────────────────────────────────────────
def test_audit_export_is_paper_not_a_button():
    """Золотое правило №3: ни в Sheets, ни в .xlsx нет операции находки — только бумага."""
    src = (ROOT / "reports" / "findings.py").read_text(encoding="utf-8")
    assert "suggested_operation" not in src
    r = _result()
    ops = {getattr(f, "suggested_operation", None) for f in r.findings} - {None}
    assert ops, "фикстура без one_tap-находок не проверяет запрет (нужна хоть одна)"
    sheet_cells = set(_cells([row for t in build_audit_sheets_data(r, "ru") for row in t.rows]))
    assert not (sheet_cells & ops), "операция находки утекла во вкладку Sheets"
    for name in build_audit_workbook(r, "ru").sheetnames:
        assert not (set(_xlsx_texts(build_audit_workbook(r, "ru"), name)) & ops)


def test_audit_cells_are_formula_safe(tmp_path):
    """Имя кампании из Google (`=HYPERLINK…`) в ячейке находки → обезврежено safe_row."""
    r = _result(campaign=EVIL)
    path = str(tmp_path / "audit.xlsx")
    build_audit_workbook(r, "ru").save(path)
    wb = openpyxl.load_workbook(path)
    cells = [
        c
        for name in wb.sheetnames
        for row in wb[name].iter_rows()
        for c in row
        if isinstance(c.value, str)
    ]
    for c in cells:
        assert not c.value.startswith(("=", "+", "-", "@")), f"формула в ячейке: {c.value!r}"
    named = [c for c in cells if c.value.lstrip("'") == EVIL]
    assert named, "имя кампании не доехало до книги — тест бесполезен"
    for c in named:
        assert c.data_type == "s" and c.value.startswith("'=")  # текст, НЕ формула


# ── формат: денежная колонка одна ───────────────────────────────────────────────────
def test_money_format_only_on_money_columns():
    r = _result()
    tabs = build_audit_sheets_data(r, "ru")
    reqs = format_requests(tabs)

    def _money_cols(tab_idx: int) -> list[int]:
        return [
            rq["repeatCell"]["range"]["startColumnIndex"]
            for rq in reqs
            if "repeatCell" in rq
            and "numberFormat" in rq["repeatCell"]["cell"]["userEnteredFormat"]
            and rq["repeatCell"]["range"]["sheetId"] == tab_idx
        ]

    assert _money_cols(0) == []  # «Обзор» — проза, числовых колонок нет
    assert _money_cols(1) == [FAMILY_MONEY_COL]  # «По семьям» — только «Под риском»
    assert _money_cols(2) == [MONEY_COL]  # «Находки» — только «Под риском»


# ── EN-артефакт без кириллицы ───────────────────────────────────────────────────────
def test_audit_en_has_no_cyrillic():
    r = _result()
    tabs = build_audit_sheets_data(r, "en")
    assert tabs[0].title == "Overview"
    leaks = [c for c in _cells([row for t in tabs for row in t.rows]) if _CYR & set(c)]
    leaks += [t.title for t in tabs if _CYR & set(t.title)]
    assert not leaks, f"RU-утечка в EN-вкладках Sheets: {leaks}"
    wb = build_audit_workbook(r, "en")
    xleaks = [c for name in wb.sheetnames for c in _xlsx_texts(wb, name) if _CYR & set(c)] + [
        n for n in wb.sheetnames if _CYR & set(n)
    ]
    assert not xleaks, f"RU-утечка в EN-книге: {xleaks}"


# ── publish_audit_to_sheets: сеть замокана, шарит reader ─────────────────────────────
class _Exec:
    def __init__(self, res):
        self._res = res

    def execute(self):
        return self._res


class _Values:
    def __init__(self, log):
        self.log = log

    def batchUpdate(self, *, spreadsheetId, body):
        self.log.append(("values.batchUpdate", spreadsheetId, body))
        return _Exec({})


class _Spreadsheets:
    def __init__(self, log):
        self.log = log

    def create(self, *, body, fields):
        self.log.append(("create", body, fields))
        return _Exec(
            {
                "spreadsheetId": "SID123",
                "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/SID123",
            }
        )

    def values(self):
        return _Values(self.log)

    def batchUpdate(self, *, spreadsheetId, body):  # форматирование (best-effort)
        self.log.append(("format.batchUpdate", spreadsheetId, body))
        return _Exec({})


class FakeService:
    def __init__(self):
        self.log: list = []

    def spreadsheets(self):
        return _Spreadsheets(self.log)


class _Perms:
    def __init__(self, log, boom=False):
        self.log, self._boom = log, boom

    def create(self, *, fileId, body, fields):
        if self._boom:
            raise RuntimeError("drive down")
        self.log.append(("permissions.create", fileId, body))
        return _Exec({"id": "perm1"})


class FakeDrive:
    def __init__(self, boom=False):
        self.log: list = []
        self._boom = boom

    def permissions(self):
        return _Perms(self.log, self._boom)


def test_publish_audit_to_sheets_shares_reader():
    svc, drive = FakeService(), FakeDrive()
    url, share = publish_audit_to_sheets(_result(), service=svc, drive_service=drive)
    assert url == "https://docs.google.com/spreadsheets/d/SID123"
    assert share == "reader"  # финансовая бумага — reader, НЕ writer
    create = next(e for e in svc.log if e[0] == "create")
    titles = [s["properties"]["title"] for s in create[1]["sheets"]]
    assert titles == [OVERVIEW_TITLE, FAMILY_SUMMARY_TITLE, FINDINGS_TITLE]
    perm = next(e for e in drive.log if e[0] == "permissions.create")
    assert perm[2] == {"type": "anyone", "role": "reader"}


def test_publish_audit_share_failure_degrades_without_raising():
    url, share = publish_audit_to_sheets(
        _result(), service=FakeService(), drive_service=FakeDrive(boom=True)
    )
    assert url.endswith("SID123") and share == SHARE_FAILED and not is_shared(share)


# ── bot-слой: клик не пересобирает аудит (кэш) ──────────────────────────────────────
class _FakeChat:
    id = 4242


class _FakeMessage:
    def __init__(self):
        self.chat = _FakeChat()
        self.answers: list[str] = []

    async def answer(self, text, **kw):
        self.answers.append(text)


async def test_audit_export_cold_cache_is_stale(monkeypatch):
    """Холодный кэш (рестарт бота / старая клавиатура) → stale-алерт; выгрузка НЕ зовётся."""
    import bot.main as bm

    monkeypatch.setattr(bm, "_AUDIT_EXPORT_CACHE", {})
    called: list = []

    async def _no(*a, **kw):
        called.append(a)

    monkeypatch.setattr(bm, "_run_audit_sheets", _no)
    monkeypatch.setattr(bm, "_run_audit_xlsx", _no)
    m = _FakeMessage()
    await bm._run_audit_export(m, "sheets", 4242)
    assert called == []  # публикация не звана (аудит не пересобираем)
    assert m.answers and "audit" in m.answers[0].lower() or "аудит" in m.answers[0].lower()


async def test_audit_export_warm_cache_uses_cached_result(monkeypatch):
    """Тёплый кэш → _run_audit_sheets получает ТОТ ЖЕ result из кэша (пере-сбор аудита не гоняем)."""
    import bot.main as bm

    r = _result()
    monkeypatch.setattr(bm, "_AUDIT_EXPORT_CACHE", {4242: (r, "7753643025")})
    seen: list = []

    async def _sheets(m, result, acct):
        seen.append((result, acct))

    async def _xlsx(m, result, acct):
        seen.append(("xlsx", result, acct))

    monkeypatch.setattr(bm, "_run_audit_sheets", _sheets)
    monkeypatch.setattr(bm, "_run_audit_xlsx", _xlsx)

    await bm._run_audit_export(_FakeMessage(), "sheets", 4242)
    assert seen == [(r, "7753643025")]  # тот же объект — без повторного gather
    assert seen[0][0] is r

    seen.clear()
    await bm._run_audit_export(_FakeMessage(), "xlsx", 4242)
    assert seen == [("xlsx", r, "7753643025")]


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
