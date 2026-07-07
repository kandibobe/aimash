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
def _login_plural(value: str):
    prev = settings.google_ads_login_customer_ids
    settings.google_ads_login_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_login_customer_ids = prev


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


def test_discover_skips_inactive_children():
    # A3: не-ENABLED дочерние (CANCELED/SUSPENDED/CLOSED) НЕ попадают в read-набор — их запрос
    # всё равно упёрся бы в PERMISSION_DENIED/CUSTOMER_NOT_ENABLED (флудило scheduler+/diag).
    def _child_status(cid: str, status: str) -> ChildAccount:
        return ChildAccount(
            id=cid, name=f"acct-{cid}", currency="USD", manager=False, level=1, status=status
        )

    children = [
        _child_status(CH1, "ENABLED"),  # активный лист — добавляется
        _child_status(CH2, "CANCELED"),  # деактивирован — пропускается
        _child_status("3334445556", "SUSPENDED"),  # приостановлен — пропускается
        _child_status("4445556667", "CLOSED"),  # закрыт — пропускается
    ]
    with _login(MGR), _fake_hierarchy(children):
        n = asyncio.run(ac.discover_read_children())
        assert n == 1
        assert ac.discovered_read_children() == {CH1}
        # неактивный дочерний НЕ читается через discovered-набор (env read-list его не включал)
        with _ids(allowed=DRAFT, read=""):
            try:
                ac.ensure_read_allowed(CH2)
                raise AssertionError("неактивный дочерний просочился в read-замок")
            except PermissionError:
                pass


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


# ── Ленивая само-починка обхода MCC перед пикером (fail-quiet «обход на старте не прошёл») ──
# asyncio.run() создаёт НОВЫЙ loop на каждый вызов, а asyncio.Lock биндится к loop'у первого
# acquire → перед каждым тестом сбрасываем _discover_lock (свежий лок под loop теста) и кулдаун.
def test_lazy_rediscovery_heals_empty_set():
    # Обход на старте «не прошёл» (набор пуст) → первый пикер зовёт ensure_read_children_discovered,
    # тот обходит MCC СЕЙЧАС и наполняет набор — аккаунты появляются без рестарта/ручного /refresh.
    children = [_child(CH1, "USD"), _child(CH2, "EUR")]
    with _login(MGR), _fake_hierarchy(children):
        ac.set_discovered_read_children([])  # старт не наполнил набор
        ac._discover_lock = None
        ac._discover_last_attempt = 0.0
        assert ac.discovered_read_children() == set()
        n = asyncio.run(ac.ensure_read_children_discovered())
        assert n == 2
        assert ac.discovered_read_children() == {CH1, CH2}  # само-починка сработала


def test_lazy_rediscovery_noop_when_already_populated():
    # Набор непуст (обход на старте прошёл) → ленивый путь даже НЕ трогает API (нулевая латентность).
    children = [_child(CH1, "USD")]
    calls = {"n": 0}
    orig = ac.discover_read_children

    async def _counting():
        calls["n"] += 1
        return await orig()

    with _login(MGR), _fake_hierarchy(children):
        ac.discover_read_children = _counting
        ac._discover_lock = None
        ac._discover_last_attempt = 0.0
        try:
            asyncio.run(ac.ensure_read_children_discovered())  # 1-й: пусто → обход
            asyncio.run(ac.ensure_read_children_discovered())  # 2-й: непусто → no-op (без обхода)
            assert calls["n"] == 1
        finally:
            ac.discover_read_children = orig


def test_lazy_rediscovery_cooldown_blocks_repeat_while_empty():
    # Обход стабильно возвращает пусто → кулдаун не даёт долбить API на КАЖДЫЙ пикер.
    calls = {"n": 0}
    orig = ac.discover_read_children

    async def _counting():
        calls["n"] += 1
        return await orig()

    with _login(MGR), _fake_hierarchy([]):  # ни одного дочернего → набор остаётся пуст
        ac.discover_read_children = _counting
        ac._discover_lock = None
        ac._discover_last_attempt = 0.0
        try:
            asyncio.run(ac.ensure_read_children_discovered())  # обход (результат пуст)
            asyncio.run(ac.ensure_read_children_discovered())  # в кулдауне → без повторного обхода
            assert calls["n"] == 1
        finally:
            ac.discover_read_children = orig


def test_lazy_rediscovery_noop_without_mcc():
    # Нет настроенного MCC → обход невозможен, ленивый путь даже не пытается (fail-closed).
    calls = {"n": 0}
    orig = ac.discover_read_children

    async def _counting():
        calls["n"] += 1
        return await orig()

    with _login(""):
        ac.discover_read_children = _counting
        ac.set_discovered_read_children([])
        ac._discover_lock = None
        ac._discover_last_attempt = 0.0
        try:
            n = asyncio.run(ac.ensure_read_children_discovered())
            assert n == 0
            assert calls["n"] == 0  # без MCC обход не запускается
        finally:
            ac.discover_read_children = orig
            ac.set_discovered_read_children([])


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


# ── Регресс: нормализованно-пустой id не протекает в замки/обход (golden rule #10) ──
# Прод-баг: inline-комментарий из .env.defaults «просачивался» как значение
# GOOGLE_ADS_LOGIN_CUSTOMER_IDS; `x.strip()` его пропускал, но normalize→'' → '' попадал в
# login_customer_id_set → discover делал ga.search(customer_id='') (Invalid customer ID '') и
# ensure_manager_allowed был fail-open на ''. Ниже — гард на весь класс.
def test_login_set_drops_non_digit_garbage_token():
    with (
        _login("7753643025"),
        _login_plural("# доп. MCC для обхода (CSV); пусто = только основной"),
    ):
        s = settings.login_customer_id_set
        assert "" not in s, "нормализованно-пустой id протёк в множество MCC (fail-open класс)"
        assert s == {"7753643025"}  # только валидный основной; мусор без цифр отброшен


def test_login_set_empty_when_only_garbage():
    with _login("# comment"), _login_plural(""):
        # мусор без цифр → пустое множество → обход невозможен (fail-closed)
        assert settings.login_customer_id_set == set()


def test_ensure_manager_allowed_rejects_empty_id():
    # golden rule #10: пустой/ненормализуемый manager_id — явный отказ, НЕ fail-open.
    with _login("7753643025"):
        for bad in ("", "   ", "# not an id"):
            try:
                ac.ensure_manager_allowed(bad)
                raise AssertionError(f"ensure_manager_allowed({bad!r}) не отказал — fail-open!")
            except PermissionError:
                pass


def test_discover_never_searches_empty_customer_id():
    # Стартовый обход не делает ga.search(customer_id='') из-за мусорного логина.
    seen: list[str] = []
    orig_build, orig_list = ac.build_client, ar.list_child_accounts
    ac.build_client = lambda cid=None: object()

    def _capture(_client, mid):
        seen.append(mid)
        return [_child(CH1, "USD")]

    ar.list_child_accounts = _capture
    try:
        with _login("7753643025"), _login_plural("garbage-no-digits"):
            asyncio.run(ac.discover_read_children())
        assert "" not in seen, "пустой customer_id ушёл в поиск (регресс fail-open)"
        assert seen == ["7753643025"]  # обойдён только валидный MCC
    finally:
        ac.build_client = orig_build
        ar.list_child_accounts = orig_list
        ac.set_discovered_read_children([])


if __name__ == "__main__":
    test_discover_populates_leaves_only()
    test_discovered_child_readable_but_not_mutable()
    test_discover_fail_closed_without_managers()
    test_lazy_rediscovery_heals_empty_set()
    test_lazy_rediscovery_noop_when_already_populated()
    test_lazy_rediscovery_cooldown_blocks_repeat_while_empty()
    test_lazy_rediscovery_noop_without_mcc()
    test_ensure_read_allowed_fail_closed_when_all_empty()
    test_async_summary_uses_per_account_timezone_window()
    test_login_set_drops_non_digit_garbage_token()
    test_login_set_empty_when_only_garbage()
    test_ensure_manager_allowed_rejects_empty_id()
    test_discover_never_searches_empty_customer_id()
    print("OK: §8 discovery + TZ-нормализация")
