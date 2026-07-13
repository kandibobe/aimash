"""Устойчивость рантайма (волна 2.8): висящие gRPC-потоки, схема БД, спам аномалий, дайджест.

Что закрыто:
1. run_ads_read_call ретраил TimeoutError до 4 раз, но `asyncio.to_thread` НЕОТМЕНЯЕМ, а google-ads
   v24 не ставит gRPC-дедлайн → каждый таймаут оставлял живой поток с висящим RPC (до 4 на один
   /report). Дедлайн теперь ставится в САМ SDK-вызов (ads.client._DeadlineClient) — на ЧТЕНИЯХ.
   Мутации намеренно без дедлайна: DEADLINE_EXCEEDED на mutate не значит «не применилось».
2. init_db() гонял Base.metadata.create_all на Postgres вопреки докам → таблицы создавались в обход
   Alembic, alembic_version не двигалась, дрейф «модель ⟂ миграции» невидим, а следующая миграция
   с op.create_table падала DuplicateTable на старте контейнера (крэш-луп прода).
3. Аномалии слались на КАЖДОМ прогоне джобы (каждые 6ч) всю неделю окна → ~28 одинаковых сообщений.
4. Недельный дайджест резался сырым слайсом HTML → битый тег → Telegram отбивал сообщение, ошибка
   глоталась except → дайджест И файл молча не доставлялись.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads import client as C  # noqa: E402
from core.resilience import ADS_TIMEOUT_S  # noqa: E402
from db.models import Base  # noqa: E402
from db.session import _missing_tables  # noqa: E402
from scheduler import jobs  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


# ── 1. Дедлайн gRPC на чтениях ────────────────────────────────────────────────────
class _Svc:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def search(self, **kw):
        self.calls.append(kw)
        return []

    def search_stream(self, **kw):
        self.calls.append(kw)
        return []

    def generate_keyword_ideas(self, **kw):
        self.calls.append(kw)
        return []

    def mutate_campaigns(self, **kw):
        self.calls.append(kw)
        return object()


class _RawClient:
    def __init__(self) -> None:
        self.svc = _Svc()
        self.enums = "enums-sentinel"

    def get_service(self, name: str, **kw):
        return self.svc

    def get_type(self, name: str):
        return f"type:{name}"


def test_read_deadline_fires_before_asyncio_timeout():
    """SDK должен отвалиться САМ (поток освободится), а не быть брошен висеть отменой корутины."""
    assert 0 < C._read_deadline_s() < ADS_TIMEOUT_S


def test_search_gets_grpc_deadline():
    raw = _RawClient()
    ga = C._DeadlineClient(raw).get_service("GoogleAdsService")
    ga.search(customer_id="123", query="SELECT 1")
    assert raw.svc.calls[0]["timeout"] == C._read_deadline_s(), (
        "чтение ушло без дедлайна — поток повиснет"
    )


def test_search_stream_and_keyword_ideas_get_deadline():
    raw = _RawClient()
    proxy = C._DeadlineClient(raw)
    proxy.get_service("GoogleAdsService").search_stream(customer_id="1", query="q")
    proxy.get_service("KeywordPlanIdeaService").generate_keyword_ideas(request=object())
    assert all("timeout" in c for c in raw.svc.calls)


def test_explicit_timeout_from_call_site_wins():
    raw = _RawClient()
    ga = C._DeadlineClient(raw).get_service("GoogleAdsService")
    ga.search(customer_id="1", query="q", timeout=3.0)
    assert raw.svc.calls[0]["timeout"] == 3.0


def test_mutations_are_not_given_a_deadline():
    """Денежный путь: DEADLINE_EXCEEDED на mutate не означает, что операция не применилась."""
    raw = _RawClient()
    svc = C._DeadlineClient(raw).get_service("CampaignService")
    svc.mutate_campaigns(customer_id="1", operations=[])
    assert "timeout" not in raw.svc.calls[0], "дедлайн на мутации = риск задвоения при ретрае"


def test_proxy_delegates_everything_else():
    proxy = C._DeadlineClient(_RawClient())
    assert proxy.get_type("Campaign") == "type:Campaign"
    assert proxy.enums == "enums-sentinel"


def test_build_client_returns_proxied_client(monkeypatch):
    """Прокси навешивается в ЕДИНСТВЕННОЙ точке сборки клиента (call-site'ов ga.search ~40)."""
    import google.ads.googleads.client as gac

    class _FakeSDK:
        @staticmethod
        def load_from_dict(cfg, version=None):
            return _RawClient()

    monkeypatch.setattr(gac, "GoogleAdsClient", _FakeSDK)
    C._CLIENT_CACHE.clear()
    try:
        client = C.build_client(C.DRAFT_ACCOUNT_ID)
        assert isinstance(client, C._DeadlineClient)
        ga = client.get_service("GoogleAdsService")
        ga.search(customer_id=C.DRAFT_ACCOUNT_ID, query="SELECT 1")
        assert "timeout" in client._client.svc.calls[0]
    finally:
        C._CLIENT_CACHE.clear()


# ── 2. Схема БД: create_all только на SQLite ──────────────────────────────────────
def test_missing_tables_detects_unmigrated_schema():
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        assert _missing_tables(conn), "пустая БД должна выглядеть как ненакатанная"
        Base.metadata.create_all(conn)
        assert _missing_tables(conn) == []


def test_create_all_is_sqlite_only():
    """Гард класса: на Postgres истина — Alembic. create_all там создавал бы таблицы в обход
    миграций (alembic_version не двигается) → следующая op.create_table = DuplicateTable."""
    src = (ROOT / "db/session.py").read_text(encoding="utf-8")
    body = src[src.index("async def init_db") :]
    guard = body.index('_db_url.startswith("sqlite")')
    assert body.index("Base.metadata.create_all") > guard, "create_all вне sqlite-ветки"
    assert "RuntimeError" in body, "на Postgres ненакатанная схема должна падать громко"


# ── 3. Кулдаун аномалий ───────────────────────────────────────────────────────────
class _A:
    def __init__(self, kind: str) -> None:
        self.kind = kind


def test_anomaly_cooldown_suppresses_repeat():
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    blob = {"777:spend_spike": (now - timedelta(hours=6)).isoformat()}
    alerts = [_A("spend_spike"), _A("conv_drop")]
    fresh = jobs._anomaly_fresh(blob, "777", alerts, now, 24.0)
    assert [a.kind for a in fresh] == ["conv_drop"], "тот же алерт повторился внутри кулдауна"


def test_anomaly_cooldown_expires():
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    blob = {"777:spend_spike": (now - timedelta(hours=30)).isoformat()}
    assert jobs._anomaly_fresh(blob, "777", [_A("spend_spike")], now, 24.0)


def test_anomaly_first_time_always_sent():
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    assert len(jobs._anomaly_fresh(None, "777", [_A("spend_spike")], now, 24.0)) == 1
    assert len(jobs._anomaly_fresh({}, "777", [_A("spend_spike")], now, 0.0)) == 1  # 0 = как раньше


def test_anomaly_seen_blob_stamps_and_prunes():
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    blob = {
        "111:spend_spike": (now - timedelta(days=40)).isoformat(),  # протухло → выбросить
        "222:conv_drop": (now - timedelta(days=2)).isoformat(),  # живое → сохранить
        "333:x": "мусор",  # битое → выбросить
    }
    out = jobs._anomaly_seen_updated(blob, [("777", [_A("spend_spike")])], now)
    assert out["777:spend_spike"] == now.isoformat()
    assert "222:conv_drop" in out
    assert "111:spend_spike" not in out and "333:x" not in out


def test_undelivered_alert_is_not_marked_seen():
    """Гард: отметку «показано» ставим ПОСЛЕ успешной отправки, иначе алерт теряется навсегда."""
    src = (ROOT / "scheduler/jobs.py").read_text(encoding="utf-8")
    body = src[src.index("async def run_anomaly_check") : src.index("_error_alert_last_id: int")]
    assert body.index("continue  # не доставили") < body.index("_save_ui_pref_blob")


# ── 4. Недельный дайджест: обрезка по строкам, файл отдельно ──────────────────────
def test_weekly_digest_truncates_by_lines_not_raw_slice():
    src = (ROOT / "scheduler/jobs.py").read_text(encoding="utf-8")
    body = src[src.index("async def run_weekly_digest") : src.index("async def _advise_proactive")]
    assert "[:_DIGEST_MAX]" not in body, "сырой слайс рвёт HTML-тег → Telegram отбивает сообщение"
    assert "ux.split_by_lines(" in body
    # файл с деталями шлётся ОТДЕЛЬНЫМ try: сбой текста не должен отменять вложение
    assert body.count("except Exception as e:") >= 2


@pytest.mark.parametrize("limit", [30, 60])
def test_split_by_lines_keeps_tags_intact(limit: int):
    from bot import ux

    html = "<b>Заголовок</b>\n<code>1</code> ошибка\n<i>деталь</i>"
    for chunk in ux.split_by_lines(html, limit):
        assert chunk.count("<") == chunk.count(">")
