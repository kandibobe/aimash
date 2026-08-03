from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

from google.protobuf.struct_pb2 import Struct

from mcp_server import tools_read as tr


class _FakeService:
    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    def search_stream(self, *, customer_id, query):
        self.calls.append({"customer_id": customer_id, "query": query})
        return [SimpleNamespace(results=self._rows)]


class _FakeClient:
    def __init__(self, service):
        self.service = service

    def get_service(self, name):
        assert name == "GoogleAdsService"
        return self.service


def test_execute_google_ads_query_defaults_bound_context_before_model():
    signature = inspect.signature(tr.execute_google_ads_query)
    assert signature.parameters["row_limit"].default == 200
    assert tr.GOOGLE_ADS_QUERY_PAYLOAD_LIMIT_BYTES == 128 * 1024


def test_execute_google_ads_query_passes_gaql_unchanged_and_caps_rows():
    service = _FakeService([{"id": "1"}, {"id": "2"}, {"id": "3"}])
    query = "SELECT campaign.id FROM campaign WHERE campaign.status = 'ENABLED'"

    rows, row_capped, payload_capped = tr._execute_google_ads_query_sync(
        _FakeClient(service), "1234567890", query, row_limit=2
    )

    assert rows == [{"id": "1"}, {"id": "2"}]
    assert row_capped is True
    assert payload_capped is False
    assert service.calls == [{"customer_id": "1234567890", "query": query}]


def test_google_ads_row_serializer_is_schema_agnostic():
    message = Struct()
    message.update({"campaign": {"id": "42"}, "metrics": {"clicks": 7}})

    row = tr._google_ads_row_dict(message)

    assert row == {"campaign": {"id": "42"}, "metrics": {"clicks": 7.0}}


def test_execute_google_ads_query_caps_payload_before_context_bloat(monkeypatch):
    monkeypatch.setattr(tr, "GOOGLE_ADS_QUERY_PAYLOAD_LIMIT_BYTES", 40)
    service = _FakeService([{"text": "x" * 25}, {"text": "y" * 25}])

    rows, row_capped, payload_capped = tr._execute_google_ads_query_sync(
        _FakeClient(service),
        "1234567890",
        "SELECT campaign.name FROM campaign",
        row_limit=100,
    )

    assert rows == [{"text": "x" * 25}]
    assert row_capped is False
    assert payload_capped is True


def test_execute_google_ads_query_exposes_payload_cap_to_agent(monkeypatch):
    monkeypatch.setattr(tr, "ensure_read_allowed", lambda _account: None)

    async def fake_client(_account):
        return object()

    async def fake_read(*_args, **_kwargs):
        return [{"campaign": {"id": "1"}}], False, True

    monkeypatch.setattr(tr, "build_client_async", fake_client)
    monkeypatch.setattr(tr, "run_ads_read_call", fake_read)

    env = asyncio.run(
        tr.execute_google_ads_query(
            account="123",
            gaql_query="SELECT campaign.id FROM campaign",
        )
    )

    assert env["ok"] is True
    assert env["payload_capped"] is True
    assert env["payload_limit_bytes"] == tr.GOOGLE_ADS_QUERY_PAYLOAD_LIMIT_BYTES
    assert "Сузь" in env["payload_hint"]


def test_execute_google_ads_query_rejects_invalid_transport_args_before_client(monkeypatch):
    monkeypatch.setattr(tr, "ensure_read_allowed", lambda _account: None)

    async def client_must_not_be_built(_account):
        raise AssertionError("client built before argument validation")

    monkeypatch.setattr(tr, "build_client_async", client_must_not_be_built)

    empty = asyncio.run(tr.execute_google_ads_query(account="123", gaql_query=""))
    bad_limit = asyncio.run(
        tr.execute_google_ads_query(
            account="123", gaql_query="SELECT customer.id FROM customer", row_limit=True
        )
    )

    assert empty["error_code"] == "invalid_argument"
    assert bad_limit["error_code"] == "invalid_argument"
