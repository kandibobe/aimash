from __future__ import annotations

import asyncio
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


def test_execute_google_ads_query_passes_gaql_unchanged_and_caps_rows():
    service = _FakeService([{"id": "1"}, {"id": "2"}, {"id": "3"}])
    query = "SELECT campaign.id FROM campaign WHERE campaign.status = 'ENABLED'"

    rows, capped = tr._execute_google_ads_query_sync(
        _FakeClient(service), "1234567890", query, row_limit=2
    )

    assert rows == [{"id": "1"}, {"id": "2"}]
    assert capped is True
    assert service.calls == [{"customer_id": "1234567890", "query": query}]


def test_google_ads_row_serializer_is_schema_agnostic():
    message = Struct()
    message.update({"campaign": {"id": "42"}, "metrics": {"clicks": 7}})

    row = tr._google_ads_row_dict(message)

    assert row == {"campaign": {"id": "42"}, "metrics": {"clicks": 7.0}}


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
