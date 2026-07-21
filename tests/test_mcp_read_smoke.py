"""Офлайн-smoke MCP READ-слоя (без SDK/БД/сети): конверт, сериализация, редакция, форвард kwargs.

SDK-путь подменён (monkeypatch `build_client_async`/`run_ads_read_call` в `mcp_server.tools_read`) —
как `mut._apply_*_via_sdk` в test_write_layer.py. Проверяем ровно наш слой поверх ридера:
пагинацию/усечение/`total_rows`, `code_numbers ⊇ числа rows`, метрики, считаемые КОДОМ, редакцию
ошибки (правило 5) и условный проброс kwargs в keyword_ideas. Живой Draft-прогон — отдельно.

Стиль — как tests/test_safety_core.py: `sys.path.insert` + `# noqa: E402`, in-process, `asyncio.run`.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from core.config import settings  # noqa: E402
from mcp_server import envelope, serialize  # noqa: E402
from mcp_server import tools_read as tr  # noqa: E402
from mcp_server.redact import redact_error  # noqa: E402
from reports.queries import Breakdown, Metrics  # noqa: E402


@contextmanager
def _read_allowed():
    """Замок ЧТЕНИЯ стоит на ГРАНИЦЕ слоя (`tools_read._guarded`), а не только внутри ридера, поэтому
    офлайн-смоук обязан звать инструмент на РАЗРЕШЁННОМ аккаунте. Иначе он молча проверял бы отказ
    замка вместо конверта — и остался бы зелёным при сломанной сериализации (сам замок покрыт
    параметризованным инвариантом в tests/test_hermes_isolation.py)."""
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = DRAFT_ACCOUNT_ID
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


def _sample_breakdown() -> Breakdown:
    return Breakdown(
        key="campaign",
        title="Кампании",
        dim_headers=["Кампания", "Статус"],
        rows=[
            (("Camp A", "ENABLED"), Metrics(100, 10, 5_000_000, 2.0, 20.0)),
            (("Camp B", "PAUSED"), Metrics(50, 5, 1_000_000, 1.0, 5.0)),
        ],
    )


# ── Конверт: пагинация / усечение / total_rows ──────────────────────────────────────
def test_envelope_paginate_signals():
    rows = [{"n": i} for i in range(5)]
    page, total, truncated = envelope.paginate(rows, offset=0, limit=2)
    assert total == 5 and len(page) == 2 and truncated is True
    page, total, truncated = envelope.paginate(rows, offset=4, limit=2)
    assert len(page) == 1 and truncated is False  # хвост: 4-я строка, дальше нет


def test_envelope_ok_shape_and_code_numbers():
    env = envelope.ok([{"cost": 5.0}, {"cost": 2.5}], offset=0, limit=1, extra={"title": "T"})
    assert env["total_rows"] == 2 and env["returned"] == 1 and env["truncated"] is True
    assert env["error"] is None and env["title"] == "T"
    # code_numbers покрывает и rows, и extra-числа; сериализуемо (list, не set).
    assert isinstance(env["code_numbers"], list)
    assert 5.0 in env["code_numbers"]


def test_envelope_err_skeleton_and_redaction():
    env = envelope.err(RuntimeError("boom: token=SUPERSECRETVALUE123"))
    assert env["rows"] == [] and env["total_rows"] == 0 and env["truncated"] is False
    assert env["code_numbers"] == []
    assert env["error"] and "SUPERSECRETVALUE123" not in env["error"]  # правило 5


# ── Сериализация: производные метрики считает КОД ───────────────────────────────────
def test_metrics_dict_computed_by_code():
    m = Metrics(100, 10, 5_000_000, 2.0, 20.0)
    d = serialize.metrics_dict(m)
    assert d["cost"] == 5.0  # 5_000_000 micros
    assert d["ctr"] == 0.1  # 10/100
    assert d["avg_cpc"] == 0.5  # 5.0/10
    assert d["cpa"] == 2.5  # 5.0/2
    assert d["roas"] == 4.0  # 20/5


def test_breakdown_rows_shape():
    rows = serialize.breakdown_rows(_sample_breakdown())
    assert rows[0]["dimensions"] == {"Кампания": "Camp A", "Статус": "ENABLED"}
    assert rows[0]["metrics"]["cost"] == 5.0


# ── Редакция ошибок наружу (без aiogram) ────────────────────────────────────────────
def test_redact_error_scrubs_secret_and_returns_str():
    # Синтетический refresh-token (форма '1//', не реальный); тест доказывает редакцию.
    secret = "1//0abcSECRETxyz"  # gitleaks:allow
    out = redact_error(RuntimeError(f"oauth failed: refresh_token={secret}"))
    assert isinstance(out, str)
    assert secret not in out and "Traceback" not in out


# ── Полный путь обёртки офлайн (SDK подменён) ───────────────────────────────────────
def test_get_campaign_stats_envelope_offline(monkeypatch):
    async def fake_client(customer_id=None):
        return object()

    async def fake_read(*args, **kwargs):
        return _sample_breakdown()

    monkeypatch.setattr(tr, "build_client_async", fake_client)
    monkeypatch.setattr(tr, "run_ads_read_call", fake_read)

    with _read_allowed():
        env = asyncio.run(
            tr.get_campaign_stats(account=DRAFT_ACCOUNT_ID, period_days=7, limit=1, offset=0)
        )
    assert env["error"] is None and env["error_code"] is None
    assert env["total_rows"] == 2 and env["returned"] == 1 and env["truncated"] is True
    assert env["title"] == "Кампании"
    assert env["rows"][0]["dimensions"]["Кампания"] == "Camp A"
    assert 5.0 in env["code_numbers"]  # cost первой кампании — citeable-число


def test_wrapper_catches_reader_failure_into_envelope(monkeypatch):
    async def fake_client(customer_id=None):
        return object()

    async def boom(*args, **kwargs):
        raise RuntimeError("upstream failed: api_key=SECRET_TOKEN_ABC")

    monkeypatch.setattr(tr, "build_client_async", fake_client)
    monkeypatch.setattr(tr, "run_ads_read_call", boom)

    with _read_allowed():
        env = asyncio.run(tr.get_campaign_stats(account=DRAFT_ACCOUNT_ID))
    assert env["rows"] == [] and env["error"]  # fail-closed: сбой → error-конверт, не исключение
    assert "SECRET_TOKEN_ABC" not in env["error"]  # правило 5
    # Код доказывает, что поймали сбой РИДЕРА, а не отказ замка — иначе тест зелёный по другой причине.
    assert env["error_code"] == "internal"


def test_keyword_ideas_forwards_only_set_kwargs(monkeypatch):
    captured: dict = {}

    async def fake_client(customer_id=None):
        return object()

    async def capture(fn, *args, **kwargs):
        captured.update(kwargs)
        return []  # generate_keyword_ideas отдал бы list[KeywordIdea]; пустой список валиден

    monkeypatch.setattr(tr, "build_client_async", fake_client)
    monkeypatch.setattr(tr, "run_ads_read_call", capture)

    with _read_allowed():
        asyncio.run(
            tr.keyword_ideas(
                account=DRAFT_ACCOUNT_ID, seeds=["a", "b"], geo_ids=[2840], reader_limit=10
            )
        )
    # заданные — проброшены (geo_ids → tuple, reader_limit → limit); незаданные (url/language/network) — нет
    assert captured["seeds"] == ["a", "b"]
    assert captured["geo_ids"] == (2840,)
    assert captured["limit"] == 10
    assert "url" not in captured and "language" not in captured and "network" not in captured
    # служебные метки run_ads_read_call (account/label) не в счёт — это не kwargs ридера
    assert captured.get("account") == DRAFT_ACCOUNT_ID
