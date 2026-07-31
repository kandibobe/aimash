"""Ready-dark WRITE facade: account binding, redaction and production non-registration."""

from __future__ import annotations

from types import SimpleNamespace

import ads.service
import mcp_server.tools_write as tools_write


class _Store:
    def __init__(self, customer_id: str):
        self.customer_id = customer_id

    async def get_confirmed(self, confirmation_id: str):  # noqa: ARG002
        return SimpleNamespace(customer_id=self.customer_id)


async def test_execute_facade_rejects_account_mismatch_before_service(monkeypatch):
    called = False

    async def _execute(store, confirmation_id):  # noqa: ARG001
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(tools_write, "ConfirmStore", lambda: _Store("1111111111"))
    monkeypatch.setattr(ads.service, "execute_confirmed", _execute)

    result = await tools_write.execute_confirmed("2222222222", "cid")

    assert result["status"] == "failed"
    assert result["error_code"] == "refused"
    assert called is False


async def test_execute_facade_redacts_expected_errors(monkeypatch):
    secret = "sk-" + ("a" * 32)

    async def _execute(store, confirmation_id):  # noqa: ARG001
        raise ValueError(f"invalid credential {secret}")

    monkeypatch.setattr(tools_write, "ConfirmStore", lambda: _Store("1111111111"))
    monkeypatch.setattr(ads.service, "execute_confirmed", _execute)

    result = await tools_write.execute_confirmed("1111111111", "cid")

    assert result["status"] == "failed"
    assert result["error_code"] == "invalid_argument"
    assert secret not in result["error"]
