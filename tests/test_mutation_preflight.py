from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import ads.mutations as mutations
import mcp_server.propose as propose
import mcp_server.tools_write as tools_write
from core.context import reset_context, set_context
from core.provenance import human_turn
from llm.schemas import CreateRsa


class _Node(SimpleNamespace):
    def __getattr__(self, name):
        value = _Node()
        setattr(self, name, value)
        return value


class _Request:
    def __init__(self) -> None:
        self.customer_id = ""
        self.operations = []
        self.partial_failure = False
        self.validate_only = False


class _ErrorCode:
    def __init__(self, name: str) -> None:
        self.value = SimpleNamespace(name=name)

    def WhichOneof(self, _field):
        return "value"


class _GoogleRejected(mutations.GoogleAdsException):
    def __init__(self, code: str, message: str) -> None:
        Exception.__init__(self, message)
        self.failure = SimpleNamespace(
            errors=[SimpleNamespace(error_code=_ErrorCode(code), message=message)]
        )
        self.error = None
        self.request_id = "preflight-request"


class _Client:
    enums = SimpleNamespace(
        KeywordMatchTypeEnum=SimpleNamespace(BROAD="BROAD", PHRASE="PHRASE", EXACT="EXACT"),
        AdGroupCriterionStatusEnum=SimpleNamespace(ENABLED="ENABLED"),
        AdGroupAdStatusEnum=SimpleNamespace(PAUSED="PAUSED"),
    )

    def __init__(self, *, rejection: Exception | None = None) -> None:
        self.rejection = rejection
        self.requests: list[_Request] = []

    def get_service(self, name: str):
        if name == "AdGroupService":
            return SimpleNamespace(
                ad_group_path=lambda customer_id, ad_group_id: (
                    f"customers/{customer_id}/adGroups/{ad_group_id}"
                )
            )
        if name in {"AdGroupCriterionService", "AdGroupAdService"}:
            return self
        raise AssertionError(name)

    def get_type(self, name: str):
        if name.startswith("Mutate"):
            return _Request()
        if name == "AdGroupAdOperation":
            rsa = SimpleNamespace(headlines=[], descriptions=[], path1="", path2="")
            ad = SimpleNamespace(final_urls=[], responsive_search_ad=rsa)
            return SimpleNamespace(create=SimpleNamespace(ad=ad, ad_group="", status=None))
        if name == "AdTextAsset":
            return SimpleNamespace(text="")
        return _Node()

    def _mutate(self, request: _Request):
        self.requests.append(request)
        if self.rejection is not None:
            raise self.rejection
        return SimpleNamespace(results=[])

    def mutate_ad_group_criteria(self, request: _Request):
        return self._mutate(request)

    def mutate_ad_group_ads(self, request: _Request):
        return self._mutate(request)


async def _direct_ads_call(fn, *args, **kwargs):  # noqa: ARG001
    return fn(*args)


@pytest.mark.parametrize(
    ("operation", "params", "expected_operations"),
    [
        (
            "add_keywords",
            {
                "ad_group_ids": ["10", "20"],
                "keywords": ["купить цветы", "доставка роз"],
                "match_type": "phrase",
            },
            4,
        ),
        (
            "create_rsa",
            {
                "ad_group_id": "10",
                "headlines": ["Свежие цветы", "Доставка сегодня", "Закажите онлайн"],
                "descriptions": ["Соберём свежий букет.", "Закажите доставку цветов онлайн."],
                "final_url": "https://example.com/flowers",
            },
            1,
        ),
    ],
)
async def test_preflight_sends_validate_only_and_returns_attestation(
    monkeypatch, operation, params, expected_operations
):
    client = _Client()
    monkeypatch.setattr(mutations, "run_ads_call", _direct_ads_call)
    attestation = await mutations.preflight_mutation(client, operation, "7753643025", params)
    assert len(client.requests) == 1
    request = client.requests[0]
    assert request.validate_only is True
    assert request.partial_failure is False
    assert len(request.operations) == expected_operations
    assert attestation["preflight_status"] == "passed"
    assert len(attestation["payload_hash"]) == 64
    assert attestation["checked_at"].endswith("+00:00")


async def test_preflight_google_rejection_is_structured_for_self_healing(monkeypatch):
    client = _Client(
        rejection=_GoogleRejected("POLICY_FINDING", "Headline violates an advertising policy")
    )
    monkeypatch.setattr(mutations, "run_ads_call", _direct_ads_call)
    with pytest.raises(mutations.PreflightRejected) as caught:
        await mutations.preflight_mutation(
            client,
            "create_rsa",
            "7753643025",
            {
                "ad_group_id": "10",
                "headlines": ["Свежие цветы", "Доставка сегодня", "Закажите онлайн"],
                "descriptions": ["Соберём свежий букет.", "Закажите доставку цветов онлайн."],
                "final_url": "https://example.com/flowers",
            },
        )
    assert client.requests[0].validate_only is True
    assert caught.value.error_codes == {"POLICY_FINDING"}
    assert "Headline violates" in str(caught.value)


async def test_google_rejection_happens_before_proposal_save(monkeypatch):
    client = _Client(rejection=_GoogleRejected("POLICY_FINDING", "Policy violation"))

    class _Store:
        async def save_proposal(self, **kwargs):  # pragma: no cover
            raise AssertionError(f"proposal must not be saved: {kwargs}")

    async def _target(*args, **kwargs):  # noqa: ARG001
        return client

    async def _unexpected_read(*args, **kwargs):  # pragma: no cover
        raise AssertionError("read_state must not run after a rejected preflight")

    monkeypatch.setattr(mutations, "run_ads_call", _direct_ads_call)
    monkeypatch.setattr(propose, "_validate_live_target", _target)
    monkeypatch.setattr(propose, "read_state", _unexpected_read)
    with pytest.raises(mutations.PreflightRejected):
        await propose.build_proposal(
            store=_Store(),
            operation="create_rsa",
            params={
                "campaign": "Search",
                "ad_group_id": "10",
                "headlines": ["Свежие цветы", "Доставка сегодня", "Закажите онлайн"],
                "descriptions": ["Соберём свежий букет.", "Закажите доставку цветов онлайн."],
                "final_url": "https://example.com/flowers",
            },
            cid="preflight-rejected",
            chat_id=1,
            customer_id="7753643025",
            user_initiated=True,
        )


@contextmanager
def _trusted_human_turn():
    token = set_context(request_id="preflight-reject", chat_id=9001)
    try:
        with human_turn(actor_user_id=42, run_id="preflight-reject"):
            yield
    finally:
        reset_context(token)


async def test_mcp_rejection_returns_ok_false_without_proposal(monkeypatch):
    async def _rejected(**kwargs):  # noqa: ARG001
        raise mutations.PreflightRejected("Headline too long [AD_ERROR]", error_codes={"AD_ERROR"})

    monkeypatch.setattr(tools_write, "build_proposal", _rejected)
    with _trusted_human_turn():
        result = await tools_write._propose(
            "create_rsa",
            CreateRsa,
            account="7753643025",
            ad_group_id="10",
            campaign="Search",
            headlines=["Свежие цветы", "Доставка сегодня", "Закажите онлайн"],
            descriptions=["Соберём свежий букет.", "Закажите доставку цветов онлайн."],
            final_url="https://example.com/flowers",
        )
    assert result["ok"] is False
    assert result["confirmation_id"] is None
    assert result["error_type"] == "GOOGLE_ADS_PREFLIGHT_REJECTED"
    assert result["google_ads_error_codes"] == ["AD_ERROR"]
    assert "Proposal ещё не создан" in result["suggested_action"]
