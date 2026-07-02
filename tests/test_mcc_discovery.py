"""§8 (полный мульти-аккаунт): обнаружение дочерних обходом MCC + TZ-нормализация окна. Офлайн.

Проверяют:
- discover_read_children наполняет эффективный read-allow-list ТОЛЬКО лист-аккаунтами (не менеджерами);
- обнаруженный дочерний СТАНОВИТСЯ читаемым (ensure_read_allowed), но НЕ мутируемым (ensure_allowed) —
  ключевой инвариант разделения замков (чтение дочернего ≠ право его менять);
- fail-closed: без настроенных MCC / при пустом наборе обход ничего не открывает;
- build_mcc_summary_async с tz_of/period_for строит окно каждого дочернего в ЕГО таймзоне.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ads.client as ac  # noqa: E402
import ads.read as ar  # noqa: E402
from ads.read import ChildAccount  # noqa: E402
from core.config import settings  # noqa: E402
from reports.mcc import build_mcc_summary_async  # noqa: E402
from reports.period import Period  # noqa: E402
from reports.queries import Metrics  # noqa: E402

DRAFT = "7753643025"
CH1 = "1112223334"
CH2 = "2223334445"
MGR = "5556667778"
SUBMGR = "9990001112"


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


@contextmanager
def _login(value: str):
    prev = settings.google_ads_login_customer_id
    settings.google_ads_login_customer_id = value
    try:
        yield
    finally:
        settings.google_ads_login_customer_id = prev


@contextmanager
def _fake_hierarchy(children: list[ChildAccount]):
    """Подменить build_client (без SDK) и list_child_accounts (фейковая иерархия). discover
    импортирует list_child_accounts из ads.read по имени в момент вызова → патч ловится."""
    orig_build, orig_list = ac.build_client, ar.list_child_accounts
    ac.build_client = lambda cid=None: object()
    ar.list_child_accounts = lambda _client, _mid: children
    try:
        yield
    finally:
        ac.build_client = orig_build
        ar.list_child_accounts = orig_list
        ac.set_discovered_read_children([])  # не протекаем в другие тесты


def _child(cid: str, currency: str, *, manager: bool = False) -> ChildAccount:
    return ChildAccount(
        id=cid, name=f"acct-{cid}", currency=currency, manager=manager, level=1, status="ENABLED"
    )


def _m(cost_micros: int = 0) -> Metrics:
    return Metrics(cost_micros=cost_micros)


def test_discover_populates_leaves_only():
    children = [
        _child(MGR, "USD", manager=True),  # менеджер — не добавляется
        _child(SUBMGR, "USD", manager=True),  # суб-менеджер — не добавляется
        _child(CH1, "USD"),  # лист — добавляется
        _child(CH2, "EUR"),  # лист — добавляется
    ]
    with _login(MGR), _fake_hierarchy(children):
        n = asyncio.run(ac.discover_read_children())
        assert n == 2
        assert ac.discovered_read_children() == {CH1, CH2}


def test_discovered_child_readable_but_not_mutable():
    # ⚠️ КЛЮЧЕВОЙ инвариант: обнаруженный дочерний читается, но мутации на нём запрещены.
    children = [_child(CH1, "USD"), _child(CH2, "EUR")]
    with _login(MGR), _ids(allowed=DRAFT, read=""), _fake_hierarchy(children):
        asyncio.run(ac.discover_read_children())
        ac.ensure_read_allowed(CH1)  # читать — можно (обнаружен обходом MCC)
        ac.ensure_read_allowed(CH2)
        try:
            ac.ensure_allowed(CH1)  # менять — НЕЛЬЗЯ (замок мутаций не пробит)
            raise AssertionError("замок мутаций пробит обнаруженным дочерним — критично!")
        except PermissionError:
            pass


def test_discover_fail_closed_without_managers():
    # Нет настроенных MCC → обход невозможен, набор пуст (fail-closed).
    with _login(""), _fake_hierarchy([_child(CH1, "USD")]):
        n = asyncio.run(ac.discover_read_children())
        assert n == 0
        assert ac.discovered_read_children() == set()


def test_ensure_read_allowed_fail_closed_when_all_empty():
    # allowed + read + discovered пусты → чтение запрещено.
    ac.set_discovered_read_children([])
    with _ids(allowed="", read=""):
        try:
            ac.ensure_read_allowed(CH1)
            raise AssertionError("ожидался PermissionError (fail-closed)")
        except PermissionError:
            pass


def test_async_summary_uses_per_account_timezone_window():
    # build_mcc_summary_async с tz_of/period_for строит окно каждого дочернего в ЕГО таймзоне.
    children = [_child(DRAFT, "USD"), _child(CH1, "EUR")]
    captured: dict[str, str] = {}

    def fake_tz(_client, cid):
        return "Africa/Nairobi" if cid == CH1 else "America/New_York"

    def period_for(tz_name):  # различимый Period на каждую TZ (label = имя TZ)
        return Period(date(2026, 2, 1), date(2026, 2, 7), tz_name)

    def fake_fetch(_client, cid, period):
        captured[cid] = period.label
        return _m(cost_micros=1_000_000)

    nominal = Period(date(2026, 1, 1), date(2026, 1, 7), "nominal")
    with _ids(allowed=DRAFT, read=CH1):
        summary = asyncio.run(
            build_mcc_summary_async(
                object(),
                MGR,
                nominal,
                list_children=lambda *_: children,
                fetch=fake_fetch,
                tz_of=fake_tz,
                period_for=period_for,
            )
        )
    assert captured[CH1] == "Africa/Nairobi"  # окно дочернего — в его TZ
    assert captured[DRAFT] == "America/New_York"
    assert {c.account.id for c in summary.children} == {DRAFT, CH1}


if __name__ == "__main__":
    test_discover_populates_leaves_only()
    test_discovered_child_readable_but_not_mutable()
    test_discover_fail_closed_without_managers()
    test_ensure_read_allowed_fail_closed_when_all_empty()
    test_async_summary_uses_per_account_timezone_window()
    print("OK: §8 discovery + TZ-нормализация")
