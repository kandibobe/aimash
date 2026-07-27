"""Гарды §8-инструментов MCP (`get_mcc_summary` / `get_mcc_deep`) — по классам отказа, а не по строкам.

Инструменты пересажены из ветки `main` НЕ как есть: в исходной версии каждый из четырёх пунктов
ниже был написан так, что инструмент оставался ЗЕЛЁНЫМ на вид и молча отдавал не то. Тесты
закрывают именно эти классы — они переживут перепись тела обёртки:

  1. **Конверт пагинирует ПЕРВЫЙ аргумент.** `envelope.ok(rows, ...)` зовёт `paginate`, а тот делает
     `rows[offset:offset+limit]`. Отдай туда цельный dict — TypeError, который `_guarded` превратит
     в аккуратный error-конверт: инструмент «работает», отвечает по форме и не работает НИКОГДА.
     Ловится только проверкой `error_code is None` на успешном пути.
  2. **`tz_of` обязан быть СИНХРОННЫМ.** `reports/mcc.py` зовёт его через `run_ads_read_call` →
     `asyncio.to_thread`: async-функция вернёт оттуда неожиданную корутину, `except` внутри обхода
     съест её как «TZ не прочиталась», и §8-нормализация окна не сработает ни разу — без единой
     ошибки в логе. Числа при этом разойдутся с интерфейсом Google Ads на сутки.
  3. **Окно дочернего = окно, которое запросил вызывающий.** Фабрика пере-якоряет период на «сегодня»
     таймзоны аккаунта — она НЕ вправе подменить его длину (в исходной версии там были зашиты 30
     дней независимо от `period_days`, то есть `period_days=7` тихо возвращал месяц).
  4. **`at_risk == 0.0` — это результат аудита, а не его отсутствие.** Сворачивание нуля в `None`
     (проверка на truthy вместо `is not None`) сообщает модели «аудит не прогонялся» про аккаунт,
     который прогонялся и чист.

Тест офлайн и детерминирован: и Google Ads (`build_client_async`), и сам обход MCC
(`build_mcc_summary_async` / `build_mcc_deep_async`) застаблены. Проверяется наша обвязка —
сериализация, конверт и то, ЧТО именно уходит в ридер, — а не ридер.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from ads.read import ChildAccount  # noqa: E402
from core.config import settings  # noqa: E402
from mcp_server import tools_read as tr  # noqa: E402
from mcp_server.serialize import mcc_deep_payload, mcc_summary_payload  # noqa: E402
from reports.mcc import ChildReport, CurrencySubtotal, MccDeep, MccSummary  # noqa: E402
from reports.period import Period, custom, last_n_days  # noqa: E402
from reports.queries import Breakdown, Metrics  # noqa: E402
from reports.service import ReportData  # noqa: E402


@pytest.fixture
def mcc_allowed(monkeypatch):
    """Draft — разрешённый менеджер: замок `ensure_manager_allowed` пропускает, дальше едет тело."""
    monkeypatch.setattr(settings, "google_ads_login_customer_id", DRAFT_ACCOUNT_ID, raising=False)
    monkeypatch.setattr(tr, "build_client_async", _fake_client)
    return DRAFT_ACCOUNT_ID


async def _fake_client(*_a, **_kw):
    return object()  # клиент никуда не ходит: обход MCC застаблен целиком


def _child(idx: int, *, currency: str = "USD") -> ChildAccount:
    return ChildAccount(
        id=f"111111111{idx}",
        name=f"Аккаунт {idx}",
        currency=currency,
        manager=False,
        level=1,
        status="ENABLED",
    )


def _metrics(clicks: int = 10) -> Metrics:
    return Metrics(impressions=1000, clicks=clicks, cost_micros=5_000_000, conversions=2.0)


def _summary(children: list[ChildReport], *, period: Period | None = None) -> MccSummary:
    period = period or last_n_days(30)
    return MccSummary(
        manager_id=DRAFT_ACCOUNT_ID,
        period=period,
        children=children,
        subtotals=[CurrencySubtotal(currency="USD", accounts=len(children), totals=_metrics())],
        skipped=["2222222222"],
        managers=["3333333333"],
        inactive=[],
        errors=[("4444444444", "read failed")],
    )


def _stub_summary(monkeypatch, summary: MccSummary) -> dict:
    """Подменить обход MCC и вернуть словарь, куда он запишет полученные kwargs."""
    seen: dict = {}

    async def _fake(client, manager_id, period, **kwargs):
        seen.update(client=client, manager_id=manager_id, period=period, **kwargs)
        return summary

    monkeypatch.setattr("reports.mcc.build_mcc_summary_async", _fake)
    return seen


# ── 1. Конверт: строки — список, успешный путь без error_code ─────────────────────────
def test_mcc_summary_returns_success_envelope_and_paginates_rows(mcc_allowed, monkeypatch):
    """Три дочерних, страница 2 ⇒ rows=2, total_rows=3, truncated. Главное здесь — `error_code is
    None`: отдай сериализатор dict вместо списка, конверт был бы error'ом при живом Google Ads."""
    children = [ChildReport(account=_child(i), totals=_metrics(clicks=i)) for i in range(3)]
    _stub_summary(monkeypatch, _summary(children))

    env = asyncio.run(tr.get_mcc_summary(manager_id=mcc_allowed, limit=2))

    assert env["error_code"] is None, f"успешный путь вернул ошибку: {env['error']!r}"
    assert env["returned"] == 2 and env["total_rows"] == 3 and env["truncated"] is True
    assert [r["account"]["id"] for r in env["rows"]] == [_child(0).id, _child(1).id]
    # Бухгалтерия обхода — на верхнем уровне и ВСЕГДА: «аккаунта нет в строках» ≠ «аккаунта нет».
    assert env["skipped"] == ["2222222222"] and env["managers"] == ["3333333333"]
    assert env["errors"] == [{"account_id": "4444444444", "reason": "read failed"}]
    assert env["period"]["days"] == 30


def test_mcc_summary_rows_are_a_list_not_a_mapping():
    """Прямой контракт сериализатора: `ok()` пагинирует свой первый аргумент срезом."""
    rows, extra = mcc_summary_payload(_summary([ChildReport(account=_child(0), totals=_metrics())]))
    assert isinstance(rows, list) and isinstance(extra, dict)
    assert rows[0:1] == [rows[0]], (
        "срез страницы конверта неприменим — форма (rows, extra) нарушена"
    )


# ── 2. tz_of обязан быть синхронным ──────────────────────────────────────────────────
@pytest.mark.parametrize("tool_name", ["get_mcc_summary", "get_mcc_deep"])
def test_mcc_tools_pass_sync_timezone_reader(mcc_allowed, monkeypatch, tool_name):
    """`tz_of` уходит в `run_ads_read_call` → `asyncio.to_thread`. Корутинная функция там даёт
    невыполненную корутину, обход тихо считает TZ непрочитанной, и §8 не работает молча."""
    seen: dict = {}

    async def _fake(client, manager_id, period, **kwargs):
        seen.update(kwargs)
        return _summary([]) if tool_name == "get_mcc_summary" else MccDeep(manager_id, period)

    monkeypatch.setattr(f"reports.mcc.build_{tool_name[4:]}_async", _fake)
    env = asyncio.run(getattr(tr, tool_name)(manager_id=mcc_allowed))

    assert env["error_code"] is None, f"{tool_name}: {env['error']!r}"
    tz_of = seen.get("tz_of")
    assert callable(tz_of), f"{tool_name} не передал tz_of — §8 отключён"
    assert not asyncio.iscoroutinefunction(tz_of), (
        f"{tool_name}: tz_of — async-функция. `run_ads_read_call` исполняет её в потоке "
        "(`asyncio.to_thread`) и получит корутину вместо строки TZ: окно останется хостовым, "
        "а ошибки не будет ни одной"
    )


# ── 3. Фабрика окна дочернего не подменяет длину периода ─────────────────────────────
def test_child_period_factory_keeps_requested_window_length():
    """`period_days=7` обязан остаться семью днями после пере-якоря на TZ аккаунта."""
    base = last_n_days(7)
    got = tr._child_period_factory(base)("America/New_York")
    assert got is not None and got.days == 7, f"длина окна подменена: {got}"
    assert got.kind == "last_n" and got.n == 7


def test_child_period_factory_leaves_explicit_dates_alone():
    """custom (обе даты названы человеком явно) не двигаем — `reanchor` возвращает его как есть."""
    base = custom(date(2026, 7, 1), date(2026, 7, 3))
    got = tr._child_period_factory(base)("Africa/Kampala")
    assert got == base


def test_child_period_factory_unknown_tz_degrades_to_common_window():
    """Неизвестная зона → None: обход откатится на общее окно, а не уронит строку аккаунта."""
    assert tr._child_period_factory(last_n_days(30))("Нет/Такой/Зоны") is None
    assert tr._child_period_factory(last_n_days(30))("") is None


def test_mcc_tools_hand_the_requested_window_to_the_reader(mcc_allowed, monkeypatch):
    """Сквозь инструмент: `period_days=7` доезжает до ридера семидневным окном, и фабрика
    дочернего периода строится ОТ НЕГО же (а не от зашитого дефолта)."""
    seen = _stub_summary(monkeypatch, _summary([]))
    env = asyncio.run(tr.get_mcc_summary(manager_id=mcc_allowed, period_days=7))

    assert env["error_code"] is None, env["error"]
    assert seen["period"].days == 7
    assert seen["period_for"]("Europe/Kyiv").days == 7


def test_mcc_summary_explicit_dates_reach_reader_unchanged(mcc_allowed, monkeypatch):
    seen = _stub_summary(monkeypatch, _summary([]))
    env = asyncio.run(
        tr.get_mcc_summary(manager_id=mcc_allowed, date_from="2026-07-01", date_to="2026-07-03")
    )

    assert env["error_code"] is None, env["error"]
    assert (seen["period"].date_from, seen["period"].date_to) == (
        date(2026, 7, 1),
        date(2026, 7, 3),
    )


# ── 4. Ноль в at_risk — результат, а не отсутствие результата ─────────────────────────
def test_zero_at_risk_survives_serialization():
    """0.0 ≠ None: «под риском ничего» — вывод аудита. None означает «аудит не прогонялся»."""
    clean = ChildReport(
        account=_child(0),
        totals=_metrics(),
        health_score=95,
        health_grade="A",
        health_at_risk=0.0,
        health_date="2026-07-26",
    )
    rows, _ = mcc_summary_payload(_summary([clean]))
    assert rows[0]["health"]["at_risk"] == 0.0, (
        "ноль свернулся в None — модель прочитает «не аудировано»"
    )


def test_missing_audit_stays_none():
    """Обратная половина: аудита не было ⇒ блока health нет вовсе (а не нули, похожие на результат)."""
    rows, _ = mcc_summary_payload(_summary([ChildReport(account=_child(0), totals=_metrics())]))
    assert "health" not in rows[0]


# ── deep: прочитанные, но не выложенные разбивки названы поимённо ─────────────────────
def test_mcc_deep_names_omitted_breakdowns():
    """Разбивки собраны и оплачены запросом — молча их не выкладывать нельзя: отсутствие модель
    читает как «данных нет». Выкладываем `campaign`, остальные перечисляем."""
    period = last_n_days(30)
    report = ReportData(
        customer_id=_child(0).id,
        period=period,
        totals=_metrics(),
        prev_totals=_metrics(clicks=8),
        breakdowns=[
            Breakdown(
                key="campaign",
                title="Кампании",
                dim_headers=["Кампания"],
                rows=[(("Поиск — бренд",), _metrics())],
            ),
            Breakdown(
                key="device",
                title="Устройства",
                dim_headers=["Устройство"],
                rows=[(("MOBILE",), _metrics())],
            ),
        ],
        currency="USD",
    )
    deep = MccDeep(manager_id=DRAFT_ACCOUNT_ID, period=period, items=[(_child(0), report)])

    rows, extra = mcc_deep_payload(deep)

    assert [bd["key"] for bd in rows[0]["breakdowns"]] == ["campaign"]
    assert rows[0]["breakdowns_omitted"] == ["device"]
    assert rows[0]["breakdowns"][0]["rows"][0]["dimensions"] == {"Кампания": "Поиск — бренд"}
    assert rows[0]["prev_totals"]["clicks"] == 8
    assert extra["period"]["days"] == 30


def test_mcc_deep_prev_totals_absent_is_none():
    """Сравнение не запрашивали ⇒ `prev_totals: null`, а не нулевые метрики (их не с чем сравнивать)."""
    period = last_n_days(7)
    report = ReportData(
        customer_id=_child(0).id,
        period=period,
        totals=_metrics(),
        prev_totals=None,
        breakdowns=[],
        currency="USD",
    )
    rows, _ = mcc_deep_payload(MccDeep(DRAFT_ACCOUNT_ID, period, [(_child(0), report)]))
    assert rows[0]["prev_totals"] is None


# ── дорогой обход не висит вечно ─────────────────────────────────────────────────────
def test_mcc_deep_timeout_becomes_honest_envelope(mcc_allowed, monkeypatch):
    """~70 GAQL под семафором: зависший обход обязан приехать `timeout`-конвертом, а не держать
    MCP-клиента бесконечно."""

    async def _hang(*_a, **_kw):
        await asyncio.sleep(3600)

    monkeypatch.setattr("reports.mcc.build_mcc_deep_async", _hang)
    monkeypatch.setattr(tr.asyncio, "wait_for", _immediate_timeout)

    env = asyncio.run(tr.get_mcc_deep(manager_id=mcc_allowed))
    assert env["error_code"] == "timeout", f"таймаут потерян: {env['error_code']!r}"
    assert env["rows"] == [] and env["total_rows"] == 0


async def _immediate_timeout(awaitable, timeout=None):
    """Стаб `asyncio.wait_for`: закрыть корутину и сразу отдать таймаут (тест не ждёт 300 с)."""
    assert timeout and timeout > 0, "у дорогого обхода пропал общий таймаут"
    awaitable.close()
    raise TimeoutError


# ── период сериализуется целиком (модель видит границы окна, а не только длину) ───────
def test_period_dict_reports_inclusive_days():
    from mcp_server.serialize import period_dict

    p = custom(date(2026, 7, 1), date(2026, 7, 7))
    d = period_dict(p)
    assert d == {
        "date_from": "2026-07-01",
        "date_to": "2026-07-07",
        "label": p.label,
        "days": 7,
    }
    assert p.date_to - p.date_from == timedelta(days=6), (
        "days считает КОД включительно, не разностью"
    )
