"""§8/§9: фильтр отчёта по кампании (GAQL-скоуп) + классификация неактивных дочерних в MCC-сводке +
перечислитель аккаунтов для пикера. Офлайн, без живого Google Ads."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.read import ChildAccount  # noqa: E402
from core.config import settings  # noqa: E402
from reports import period as P  # noqa: E402
from reports import queries as Q  # noqa: E402
from reports.mcc import build_mcc_summary  # noqa: E402
from reports.queries import Metrics  # noqa: E402


@contextmanager
def _ids(allowed: str = "", read: str = ""):
    pa, pr = settings.google_ads_allowed_customer_ids, settings.google_ads_read_customer_ids
    settings.google_ads_allowed_customer_ids = allowed
    settings.google_ads_read_customer_ids = read
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = pa
        settings.google_ads_read_customer_ids = pr


class _CapturingGA:
    def __init__(self):
        self.queries: list[str] = []

    def search(self, *, customer_id, query):
        self.queries.append(query)
        return []


class _CapturingClient:
    def __init__(self):
        self.ga = _CapturingGA()

    def get_service(self, name):
        return self.ga


DRAFT = "7753643025"


# ── Фильтр по кампании: FROM campaign + campaign.id = X, unscoped — байт-в-байт как раньше ──
def test_campaign_scoped_totals_switch_to_from_campaign_with_id():
    client = _CapturingClient()
    period = P.last_n_days(7)
    with _ids(allowed=DRAFT):
        Q.fetch_totals(client, DRAFT, period, "123")
    q = client.ga.queries[-1]
    assert "FROM campaign" in q
    assert "campaign.id = 123" in q


def test_unscoped_totals_are_from_customer_without_campaign_filter():
    client = _CapturingClient()
    period = P.last_n_days(7)
    with _ids(allowed=DRAFT):
        Q.fetch_totals(client, DRAFT, period, None)
    q = client.ga.queries[-1]
    assert "FROM customer" in q
    assert "campaign.id" not in q


def test_campaign_scoped_device_breakdown_switches_source():
    client = _CapturingClient()
    period = P.last_n_days(7)
    with _ids(allowed=DRAFT):
        Q.fetch_by_device(client, DRAFT, period, "555")
    q = client.ga.queries[-1]
    assert "FROM campaign" in q and "campaign.id = 555" in q


def test_campaign_scoped_id_is_int_coerced_no_injection():
    client = _CapturingClient()
    period = P.last_n_days(7)
    with _ids(allowed=DRAFT):
        Q.fetch_by_campaign(client, DRAFT, period, "42")
    assert "campaign.id = 42" in client.ga.queries[-1]


# ── MCC: неактивные (не ENABLED) уходят в inactive, НЕ в errors, метрику не читаем ──
def _child(cid: str, status: str = "ENABLED", manager: bool = False) -> ChildAccount:
    return ChildAccount(
        id=cid, name=f"acct-{cid}", currency="USD", manager=manager, level=1, status=status
    )


def test_inactive_child_goes_to_inactive_bucket_not_errors():
    active, canceled = "1112223334", "2223334445"
    children = [_child(active, "ENABLED"), _child(canceled, "CANCELED")]
    fetched: list[str] = []

    def fake_fetch(_client, cid, _period):
        fetched.append(cid)
        return Metrics(cost_micros=1_000_000, clicks=10)

    with _ids(allowed=DRAFT, read=f"{active},{canceled}"):
        summary = build_mcc_summary(
            object(),
            "5556667778",
            P.last_n_days(7),
            list_children=lambda *_: children,
            fetch=fake_fetch,
        )
    assert [c.account.id for c in summary.children] == [active]  # только ENABLED прочитан
    assert [c.id for c in summary.inactive] == [canceled]  # CANCELED — в inactive
    assert summary.errors == []  # и НЕ в ошибках
    assert canceled not in fetched  # метрику неактивного не запрашивали


# ── Пикер: перечислитель аккаунтов = Draft + read-list, все проходят ensure_read_allowed ──
async def test_read_account_rows_lists_draft_and_read_ids():
    import bot.main as bm
    from db.session import init_db

    await init_db()
    extra = "6764040266"
    with _ids(allowed=DRAFT, read=extra):
        rows = await bm._read_account_rows(chat_id=1)
    ids = {r.id for r in rows}
    assert DRAFT in ids
    assert extra in ids  # §8: 676-404-0266 доступен на чтение через env read-list (legacy-проход)


async def test_read_account_rows_sorted_draft_active_then_name():
    # D2: Draft первым, затем активные (ENABLED) по алфавиту, затем неактивные — предсказуемо
    # для человека (раньше был порядок по числовому id).
    import bot.main as bm
    from ads.client import set_discovered_read_children, set_discovered_read_children_meta
    from db.session import init_db

    await init_db()
    ids = ["9000000001", "9000000002", "9000000003"]
    metas = [
        ChildAccount(
            id="9000000001", name="Zebra", currency="USD", manager=False, level=1, status="ENABLED"
        ),
        ChildAccount(
            id="9000000002", name="Alpha", currency="USD", manager=False, level=1, status="ENABLED"
        ),
        ChildAccount(
            id="9000000003",
            name="AAA Paused",
            currency="USD",
            manager=False,
            level=1,
            status="PAUSED",
        ),
    ]
    set_discovered_read_children(ids)
    set_discovered_read_children_meta(metas)
    try:
        with _ids(allowed=DRAFT, read=""):
            rows = await bm._read_account_rows(chat_id=1)
    finally:
        set_discovered_read_children([])
        set_discovered_read_children_meta([])
    assert str(rows[0].id) == DRAFT  # Draft закреплён первым
    names_after_draft = [r.name for r in rows[1:]]
    # ENABLED по имени (Alpha, Zebra) → неактивный (AAA Paused) в самом низу, несмотря на имя
    assert names_after_draft == ["Alpha", "Zebra", "AAA Paused"]


async def test_read_account_rows_filters_by_grant_when_enforced(monkeypatch):
    """2B: при активном enforcement (есть гранты) пикер показывает не-Draft ТОЛЬКО грантованным."""
    import bot.main as bm
    from core.access import grant_account_access, revoke_account_access
    from db.session import init_db

    await init_db()
    extra = "6764040266"
    granted_chat, other_chat = 77_101, 77_102
    with _ids(allowed=DRAFT, read=extra):
        await grant_account_access(granted_chat, extra)  # первый грант включает enforcement
        try:
            ids_granted = {r.id for r in await bm._read_account_rows(granted_chat)}
            ids_other = {r.id for r in await bm._read_account_rows(other_chat)}
        finally:
            await revoke_account_access(granted_chat, extra)  # вернуть legacy другим тестам
    assert extra in ids_granted and DRAFT in ids_granted
    assert extra not in ids_other and DRAFT in ids_other  # Draft виден всем, чужой — нет


# ── Само-восстановление: read упал на залипшем не-Draft аккаунте → сброс глобального выбора на Draft ──
class _FakeMsg:
    def __init__(self, chat_id: int):
        self.chat = type("C", (), {"id": chat_id})()
        self.answers: list = []

    async def answer(self, text: str = "", **kw):
        self.answers.append(text)


async def test_heal_resets_stuck_global_account_to_draft():
    import bot.main as bm
    from db.session import init_db

    await init_db()
    chat_id = 55_001
    extra = "6764040266"
    with _ids(allowed=DRAFT, read=extra):
        await bm._save_selected_account(chat_id, extra)  # залипший глобальный выбор
        assert await bm._active_read_account(chat_id) == extra  # активен недоступный аккаунт
        await bm._heal_if_stuck_global(_FakeMsg(chat_id), extra)  # имитируем сбой read → heal
        assert await bm._active_read_account(chat_id) == DRAFT  # вернулись на Draft


async def test_heal_noop_when_account_is_draft():
    import bot.main as bm
    from db.session import init_db

    await init_db()
    msg = _FakeMsg(55_002)
    await bm._heal_if_stuck_global(msg, DRAFT)  # Draft — сбрасывать нечего
    assert msg.answers == []
