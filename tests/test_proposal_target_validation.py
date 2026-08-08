"""Live target resolution must fail before a mutation Proposal is persisted."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from ads.client import DRAFT_ACCOUNT_ID
from ads.composite import CaptureStore
from ads.freshness import Snapshot, SnapshotKind, strip_attestation
from ads.resolve import InvalidAdGroupTarget, find_ad_group_by_id, validate_ad_group_targets
from core.config import settings
from core.context import reset_context, set_context
from core.provenance import human_turn
import mcp_server.propose as propose
import mcp_server.tools_write as tools_write


@pytest.fixture(autouse=True)
def _stub_preflight_for_target_only_tests(monkeypatch):
    async def _passed(*args, **kwargs):  # noqa: ARG001
        return {
            "preflight_status": "passed",
            "payload_hash": "0" * 64,
            "checked_at": "2026-08-08T00:00:00+00:00",
            "api_version": "v25",
        }

    monkeypatch.setattr("ads.mutations.preflight_mutation", _passed)


@contextmanager
def _allowed_ids(value: str):
    previous = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = previous


class _FakeGoogleAdsService:
    def __init__(self, rows):
        self.rows = list(rows)
        self.customer_id: str | None = None
        self.query: str | None = None
        self.calls = 0

    def search(self, *, customer_id, query):
        self.calls += 1
        self.customer_id = customer_id
        self.query = query
        return list(self.rows)


class _FakeClient:
    def __init__(self, rows):
        self.service = _FakeGoogleAdsService(rows)

    def get_service(self, name):
        assert name == "GoogleAdsService"
        return self.service


def _ad_group_row(
    *,
    ad_group_id: str = "42",
    campaign_id: str = "123",
    campaign_name: str = "Search",
    ad_group_name: str = "Main",
    ad_group_type: str = "SEARCH_STANDARD",
    channel_type: str = "SEARCH",
):
    return SimpleNamespace(
        ad_group=SimpleNamespace(
            id=int(ad_group_id),
            name=ad_group_name,
            status=SimpleNamespace(name="ENABLED"),
            cpc_bid_micros=500_000,
            resource_name=f"customers/{DRAFT_ACCOUNT_ID}/adGroups/{ad_group_id}",
            type_=SimpleNamespace(name=ad_group_type),
        ),
        campaign=SimpleNamespace(
            id=int(campaign_id),
            name=campaign_name,
            advertising_channel_type=SimpleNamespace(name=channel_type),
        ),
    )


def _rsa_params(
    *, campaign: str = "Search", ad_group_id: str = "42", campaign_id: str | None = None
) -> dict:
    params = {
        "campaign": campaign,
        "ad_group_id": ad_group_id,
        "final_url": "https://example.com",
        "headlines": ["Заголовок 1", "Заголовок 2", "Заголовок 3"],
        "descriptions": ["Описание первое", "Описание второе"],
    }
    if campaign_id is not None:
        params["campaign_id"] = campaign_id
    return params


async def _direct_read(fn, *args, **kwargs):
    kwargs.pop("label", None)
    kwargs.pop("account", None)
    return fn(*args, **kwargs)


def test_find_ad_group_by_id_is_scoped_to_customer_and_returns_campaign_identity():
    client = _FakeClient([_ad_group_row(campaign_id="777", campaign_name="Brand")])

    with _allowed_ids(DRAFT_ACCOUNT_ID):
        target = find_ad_group_by_id(client, DRAFT_ACCOUNT_ID, "42")

    assert target is not None
    assert (target.id, target.campaign_id, target.campaign_name) == ("42", "777", "Brand")
    assert client.service.customer_id == DRAFT_ACCOUNT_ID
    assert "ad_group.id = 42" in (client.service.query or "")
    assert "campaign.status != 'REMOVED'" in (client.service.query or "")
    assert "ad_group.status != 'REMOVED'" in (client.service.query or "")


def test_find_ad_group_by_id_rejects_foreign_customer_before_google_call():
    client = _FakeClient([_ad_group_row()])

    with _allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(PermissionError):
            find_ad_group_by_id(client, "1234567890", "42")

    assert client.service.calls == 0


def test_target_snapshot_rejects_duplicate_or_noncanonical_ids():
    client = _FakeClient([_ad_group_row()])

    with _allowed_ids(DRAFT_ACCOUNT_ID):
        for stored_ids in (["42", "42"], ["042"]):
            with pytest.raises(InvalidAdGroupTarget, match="неканоничен"):
                validate_ad_group_targets(
                    client,
                    DRAFT_ACCOUNT_ID,
                    "Search",
                    campaign_id="123",
                    ad_group_ids=stored_ids,
                )


def test_target_snapshot_is_removed_before_history_replay():
    from db.history import _clean as clean_history_params

    params = {
        "campaign": "Search",
        "keywords": ["ремонт окон"],
        "_target_campaign_id": "123",
        "_target_ad_group_ids": ["42"],
        "_freshness": {"kind": "advisory", "reason": "not_diffable"},
    }
    expected = {"campaign": "Search", "keywords": ["ремонт окон"]}

    assert strip_attestation(params) == expected
    assert clean_history_params(params) == expected


@pytest.mark.parametrize(
    ("rows", "params", "message"),
    [
        ([], _rsa_params(), "не найден"),
        (
            [_ad_group_row(campaign_id="777", campaign_name="Other")],
            _rsa_params(campaign="Search"),
            "принадлежит кампании",
        ),
    ],
)
async def test_create_rsa_target_failure_happens_before_read_state_and_save(
    monkeypatch, rows, params, message
):
    client = _FakeClient(rows)
    store = CaptureStore()

    async def _client(customer_id):
        assert customer_id == DRAFT_ACCOUNT_ID
        return client

    async def _unexpected_read(*args, **kwargs):  # noqa: ARG001
        raise AssertionError("read_state must not run after target validation failed")

    monkeypatch.setattr("ads.client.build_client_async", _client)
    monkeypatch.setattr(propose, "run_ads_read_call", _direct_read)
    monkeypatch.setattr(propose, "read_state", _unexpected_read)

    with _allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(propose.InvalidProposalTarget, match=message):
            await propose.build_proposal(
                store=store,
                operation="create_rsa",
                params=params,
                cid="target-check",
                chat_id=1,
                customer_id=DRAFT_ACCOUNT_ID,
                user_initiated=True,
            )

    assert store.saved is None


@pytest.mark.parametrize(
    "params",
    [
        _rsa_params(campaign="123"),
        _rsa_params(campaign_id="999"),
    ],
)
async def test_create_rsa_rejects_numeric_name_collision_and_wrong_campaign_id(monkeypatch, params):
    client = _FakeClient([_ad_group_row(campaign_id="123", campaign_name="Other")])
    store = CaptureStore()

    async def _client(customer_id):
        assert customer_id == DRAFT_ACCOUNT_ID
        return client

    monkeypatch.setattr("ads.client.build_client_async", _client)
    monkeypatch.setattr(propose, "run_ads_read_call", _direct_read)

    with _allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(propose.InvalidProposalTarget, match="принадлежит кампании"):
            await propose.build_proposal(
                store=store,
                operation="create_rsa",
                params=params,
                cid="campaign-identity",
                chat_id=1,
                customer_id=DRAFT_ACCOUNT_ID,
                user_initiated=True,
            )

    assert store.saved is None


async def test_create_rsa_rejects_ambiguous_campaign_name_without_id(monkeypatch):
    client = _FakeClient(
        [
            _ad_group_row(campaign_id="123", campaign_name="Search"),
            _ad_group_row(ad_group_id="84", campaign_id="777", campaign_name="Search"),
        ]
    )
    store = CaptureStore()

    async def _client(customer_id):
        assert customer_id == DRAFT_ACCOUNT_ID
        return client

    monkeypatch.setattr("ads.client.build_client_async", _client)
    monkeypatch.setattr(propose, "run_ads_read_call", _direct_read)

    with _allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(propose.InvalidProposalTarget, match="неоднозначно"):
            await propose.build_proposal(
                store=store,
                operation="create_rsa",
                params=_rsa_params(),
                cid="ambiguous-campaign",
                chat_id=1,
                customer_id=DRAFT_ACCOUNT_ID,
                user_initiated=True,
            )

    assert store.saved is None


@pytest.mark.parametrize(
    ("ad_group_type", "channel_type"),
    [("SEARCH_DYNAMIC_ADS", "SEARCH"), ("DISPLAY_STANDARD", "DISPLAY")],
)
async def test_create_rsa_rejects_incompatible_ad_group_before_save(
    monkeypatch, ad_group_type, channel_type
):
    client = _FakeClient([_ad_group_row(ad_group_type=ad_group_type, channel_type=channel_type)])
    store = CaptureStore()

    async def _client(customer_id):
        assert customer_id == DRAFT_ACCOUNT_ID
        return client

    monkeypatch.setattr("ads.client.build_client_async", _client)
    monkeypatch.setattr(propose, "run_ads_read_call", _direct_read)

    with _allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(propose.InvalidProposalTarget, match="несовместим с RSA"):
            await propose.build_proposal(
                store=store,
                operation="create_rsa",
                params=_rsa_params(campaign_id="123"),
                cid="incompatible-rsa-target",
                chat_id=1,
                customer_id=DRAFT_ACCOUNT_ID,
                user_initiated=True,
            )

    assert store.saved is None


async def test_add_keywords_rejects_group_outside_campaign_before_save(monkeypatch):
    client = _FakeClient([_ad_group_row(campaign_name="Search")])
    store = CaptureStore()

    async def _client(customer_id):
        assert customer_id == DRAFT_ACCOUNT_ID
        return client

    async def _unexpected_read(*args, **kwargs):  # noqa: ARG001
        raise AssertionError("read_state must not run after target validation failed")

    monkeypatch.setattr("ads.client.build_client_async", _client)
    monkeypatch.setattr(propose, "run_ads_read_call", _direct_read)
    monkeypatch.setattr(propose, "read_state", _unexpected_read)

    with _allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(propose.InvalidProposalTarget, match="не найдена в кампании"):
            await propose.build_proposal(
                store=store,
                operation="add_keywords",
                params={
                    "campaign": "Search",
                    "ad_group": "Invented",
                    "keywords": ["ремонт окон"],
                    "match_type": "phrase",
                },
                cid="keyword-target-check",
                chat_id=1,
                customer_id=DRAFT_ACCOUNT_ID,
                user_initiated=True,
            )

    assert store.saved is None


async def test_add_keywords_rejects_case_mismatch_before_save(monkeypatch):
    client = _FakeClient([_ad_group_row(ad_group_name="Main")])
    store = CaptureStore()

    async def _client(customer_id):
        assert customer_id == DRAFT_ACCOUNT_ID
        return client

    monkeypatch.setattr("ads.client.build_client_async", _client)
    monkeypatch.setattr(propose, "run_ads_read_call", _direct_read)

    with _allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(propose.InvalidProposalTarget, match="не найдена в кампании"):
            await propose.build_proposal(
                store=store,
                operation="add_keywords",
                params={
                    "campaign": "Search",
                    "ad_group": "main",
                    "keywords": ["ремонт окон"],
                    "match_type": "phrase",
                },
                cid="keyword-case-target",
                chat_id=1,
                customer_id=DRAFT_ACCOUNT_ID,
                user_initiated=True,
            )

    assert store.saved is None


@pytest.mark.parametrize(
    ("operation", "params", "expected_ad_group_ids"),
    [
        (
            "add_keywords",
            {
                "campaign": "Search",
                "keywords": ["ремонт окон"],
                "match_type": "phrase",
            },
            ["42", "84"],
        ),
        (
            "add_keywords",
            {
                "campaign": "Search",
                "ad_group": "Main",
                "keywords": ["ремонт окон"],
                "match_type": "phrase",
            },
            ["42"],
        ),
        (
            "add_negative_keywords",
            {
                "campaign": "Search",
                "ad_group": "Main",
                "keywords": ["бесплатно"],
                "match_type": "broad",
            },
            ["42"],
        ),
        (
            "add_negative_keywords",
            {
                "campaign": "Search",
                "keywords": ["бесплатно"],
                "match_type": "broad",
            },
            None,
        ),
    ],
)
async def test_keyword_proposal_persists_exact_target_identity(
    monkeypatch, operation, params, expected_ad_group_ids
):
    client = _FakeClient(
        [
            _ad_group_row(ad_group_id="42", ad_group_name="Main"),
            _ad_group_row(ad_group_id="84", ad_group_name="Other"),
        ]
    )
    store = CaptureStore()

    async def _client(customer_id):
        assert customer_id == DRAFT_ACCOUNT_ID
        return client

    async def _read_state(*args, **kwargs):  # noqa: ARG001
        return Snapshot(SnapshotKind.NO_DIFF, reason="not_diffable")

    monkeypatch.setattr("ads.client.build_client_async", _client)
    monkeypatch.setattr(propose, "run_ads_read_call", _direct_read)
    monkeypatch.setattr(propose, "read_state", _read_state)

    with _allowed_ids(DRAFT_ACCOUNT_ID):
        await propose.build_proposal(
            store=store,
            operation=operation,
            params=params,
            cid=f"{operation}-identity",
            chat_id=1,
            customer_id=DRAFT_ACCOUNT_ID,
            user_initiated=True,
        )

    assert store.saved is not None
    saved_params = store.saved["params"]
    assert saved_params["_target_campaign_id"] == "123"
    if expected_ad_group_ids is None:
        assert "_target_ad_group_ids" not in saved_params
    else:
        assert saved_params["_target_ad_group_ids"] == expected_ad_group_ids


async def test_campaign_negative_rejects_missing_campaign_before_save(monkeypatch):
    client = _FakeClient([])
    store = CaptureStore()

    async def _client(customer_id):
        assert customer_id == DRAFT_ACCOUNT_ID
        return client

    async def _unexpected_read(*args, **kwargs):  # noqa: ARG001
        raise AssertionError("read_state must not run after target validation failed")

    monkeypatch.setattr("ads.client.build_client_async", _client)
    monkeypatch.setattr(propose, "run_ads_read_call", _direct_read)
    monkeypatch.setattr(propose, "read_state", _unexpected_read)

    with _allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(propose.InvalidProposalTarget, match="кампания .* не найдена"):
            await propose.build_proposal(
                store=store,
                operation="add_negative_keywords",
                params={
                    "campaign": "Invented",
                    "keywords": ["бесплатно"],
                    "match_type": "broad",
                },
                cid="negative-target-check",
                chat_id=1,
                customer_id=DRAFT_ACCOUNT_ID,
                user_initiated=True,
            )

    assert store.saved is None


async def test_live_target_read_failure_is_fail_closed_before_save(monkeypatch):
    store = CaptureStore()

    async def _client(customer_id):
        assert customer_id == DRAFT_ACCOUNT_ID
        return _FakeClient([])

    async def _timeout(*args, **kwargs):  # noqa: ARG001
        raise TimeoutError("live target lookup timed out")

    async def _unexpected_read(*args, **kwargs):  # noqa: ARG001
        raise AssertionError("read_state must not run after target validation failed")

    monkeypatch.setattr("ads.client.build_client_async", _client)
    monkeypatch.setattr(propose, "run_ads_read_call", _timeout)
    monkeypatch.setattr(propose, "read_state", _unexpected_read)

    with _allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(TimeoutError):
            await propose.build_proposal(
                store=store,
                operation="create_rsa",
                params=_rsa_params(),
                cid="target-timeout",
                chat_id=1,
                customer_id=DRAFT_ACCOUNT_ID,
                user_initiated=True,
            )

    assert store.saved is None


async def test_validated_target_can_reach_proposal_save(monkeypatch):
    client = _FakeClient([_ad_group_row(campaign_id="123", campaign_name="Search")])
    store = CaptureStore()

    async def _client(customer_id):
        assert customer_id == DRAFT_ACCOUNT_ID
        return client

    async def _read_state(*args, **kwargs):  # noqa: ARG001
        return Snapshot(SnapshotKind.NO_DIFF, reason="not_diffable")

    monkeypatch.setattr("ads.client.build_client_async", _client)
    monkeypatch.setattr(propose, "run_ads_read_call", _direct_read)
    monkeypatch.setattr(propose, "read_state", _read_state)

    with _allowed_ids(DRAFT_ACCOUNT_ID):
        built = await propose.build_proposal(
            store=store,
            operation="create_rsa",
            params=_rsa_params(),
            cid="valid-target",
            chat_id=1,
            customer_id=DRAFT_ACCOUNT_ID,
            user_initiated=True,
        )

    assert built.cid == "valid-target"
    assert store.saved is not None
    assert store.saved["params"]["ad_group_id"] == "42"
    assert store.saved["params"]["campaign_id"] == "123"
    assert store.saved["params"]["_preflight"]["preflight_status"] == "passed"
    assert "✅ Google Ads preflight пройден" in store.saved["summary"]
    assert "гарантированно" not in store.saved["summary"].casefold()


async def test_execute_rechecks_legacy_rsa_target_before_mutation(monkeypatch):
    import ads.service as service

    proposal = SimpleNamespace(
        operation="create_rsa",
        status="confirmed",
        customer_id=DRAFT_ACCOUNT_ID,
        params=_rsa_params(),  # legacy row: no persisted campaign_id
    )

    class _Store:
        async def get_confirmed(self, confirmation_id):
            assert confirmation_id == "legacy-target"
            return proposal

    async def _client(customer_id):
        assert customer_id == DRAFT_ACCOUNT_ID
        return object()

    async def _freshness(*args, **kwargs):  # noqa: ARG001
        return None

    def _reject(*args, **kwargs):  # noqa: ARG001
        raise InvalidAdGroupTarget("legacy ad_group_id не принадлежит кампании")

    async def _unexpected_apply(**kwargs):  # pragma: no cover - must stay unreachable
        raise AssertionError(f"mutation must not run: {kwargs}")

    monkeypatch.setattr(service, "build_client_async", _client)
    monkeypatch.setattr(service, "_verify_freshness", _freshness)
    monkeypatch.setattr(service.resolve, "validate_ad_group_target", _reject)
    monkeypatch.setattr(service.mutations, "apply_create_rsa", _unexpected_apply)

    with _allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(InvalidAdGroupTarget, match="не принадлежит"):
            await service._apply_confirmed(_Store(), "legacy-target")


@pytest.mark.parametrize(
    ("operation", "params", "message"),
    [
        (
            "add_keywords",
            {
                "campaign": "Search",
                "keywords": ["ремонт окон"],
                "match_type": "phrase",
            },
            "target identity",
        ),
        (
            "add_negative_keywords",
            {
                "campaign": "Search",
                "keywords": ["бесплатно"],
                "match_type": "broad",
            },
            "target identity",
        ),
        (
            "add_negative_keywords",
            {
                "campaign": "Search",
                "ad_group": "Main",
                "keywords": ["бесплатно"],
                "match_type": "broad",
                "_target_campaign_id": "123",
            },
            "ad_group identity",
        ),
    ],
)
async def test_execute_rejects_legacy_keyword_proposal_without_identity(
    monkeypatch, operation, params, message
):
    import ads.service as service

    proposal = SimpleNamespace(
        operation=operation,
        status="confirmed",
        customer_id=DRAFT_ACCOUNT_ID,
        params=params,
    )

    class _Store:
        async def get_confirmed(self, confirmation_id):  # noqa: ARG002
            return proposal

    async def _client(customer_id):
        assert customer_id == DRAFT_ACCOUNT_ID
        return object()

    async def _freshness(*args, **kwargs):  # noqa: ARG001
        return None

    async def _unexpected_apply(**kwargs):  # pragma: no cover - must stay unreachable
        raise AssertionError(f"mutation must not run: {kwargs}")

    monkeypatch.setattr(service, "build_client_async", _client)
    monkeypatch.setattr(service, "_verify_freshness", _freshness)
    monkeypatch.setattr(service.mutations, "apply_add_keywords", _unexpected_apply)
    monkeypatch.setattr(service.mutations, "apply_add_negative_keywords", _unexpected_apply)

    with _allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(ValueError, match=message):
            await service._apply_confirmed(_Store(), "legacy-keywords")


@pytest.mark.parametrize(
    ("operation", "params", "live_rows"),
    [
        (
            "add_keywords",
            {
                "campaign": "Search",
                "ad_group": "Main",
                "keywords": ["ремонт окон"],
                "match_type": "phrase",
                "_target_campaign_id": "123",
                "_target_ad_group_ids": ["42"],
            },
            [_ad_group_row(ad_group_id="84", ad_group_name="Main")],
        ),
        (
            "add_keywords",
            {
                "campaign": "Search",
                "keywords": ["ремонт окон"],
                "match_type": "phrase",
                "_target_campaign_id": "123",
                "_target_ad_group_ids": ["42"],
            },
            [
                _ad_group_row(ad_group_id="42", ad_group_name="Main"),
                _ad_group_row(ad_group_id="84", ad_group_name="Other"),
            ],
        ),
        (
            "add_keywords",
            {
                "campaign": "Search",
                "keywords": ["ремонт окон"],
                "match_type": "phrase",
                "_target_campaign_id": "123",
                "_target_ad_group_ids": ["42", "84"],
            },
            [_ad_group_row(ad_group_id="42", ad_group_name="Main")],
        ),
        (
            "add_negative_keywords",
            {
                "campaign": "Search",
                "ad_group": "Main",
                "keywords": ["бесплатно"],
                "match_type": "broad",
                "_target_campaign_id": "123",
                "_target_ad_group_ids": ["42"],
            },
            [_ad_group_row(ad_group_id="84", ad_group_name="Main")],
        ),
        (
            "add_negative_keywords",
            {
                "campaign": "Search",
                "keywords": ["бесплатно"],
                "match_type": "broad",
                "_target_campaign_id": "123",
            },
            [_ad_group_row(campaign_id="777", campaign_name="Search")],
        ),
    ],
)
async def test_execute_rejects_keyword_target_rebinding(monkeypatch, operation, params, live_rows):
    import ads.service as service

    proposal = SimpleNamespace(
        operation=operation,
        status="confirmed",
        customer_id=DRAFT_ACCOUNT_ID,
        params=params,
    )

    class _Store:
        async def get_confirmed(self, confirmation_id):  # noqa: ARG002
            return proposal

    async def _client(customer_id):
        assert customer_id == DRAFT_ACCOUNT_ID
        return _FakeClient(live_rows)

    async def _freshness(*args, **kwargs):  # noqa: ARG001
        return None

    async def _unexpected_apply(**kwargs):  # pragma: no cover - must stay unreachable
        raise AssertionError(f"mutation must not run: {kwargs}")

    monkeypatch.setattr(service, "build_client_async", _client)
    monkeypatch.setattr(service, "_verify_freshness", _freshness)
    monkeypatch.setattr(service.mutations, "apply_add_keywords", _unexpected_apply)
    monkeypatch.setattr(service.mutations, "apply_add_negative_keywords", _unexpected_apply)

    with _allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(InvalidAdGroupTarget, match="измени"):
            await service._apply_confirmed(_Store(), "rebound-keywords")


@pytest.mark.parametrize(
    ("operation", "params", "expected_ad_group_ids", "expected_ad_group_id"),
    [
        (
            "add_keywords",
            {
                "campaign": "Search",
                "ad_group": "Main",
                "keywords": ["ремонт окон"],
                "match_type": "phrase",
                "_target_campaign_id": "123",
                "_target_ad_group_ids": ["42"],
            },
            ["42"],
            None,
        ),
        (
            "add_negative_keywords",
            {
                "campaign": "Search",
                "keywords": ["бесплатно"],
                "match_type": "broad",
                "_target_campaign_id": "123",
            },
            None,
            None,
        ),
        (
            "add_negative_keywords",
            {
                "campaign": "Search",
                "ad_group": "Main",
                "keywords": ["бесплатно"],
                "match_type": "broad",
                "_target_campaign_id": "123",
                "_target_ad_group_ids": ["42"],
            },
            None,
            "42",
        ),
    ],
)
async def test_execute_uses_verified_keyword_target_snapshot(
    monkeypatch, operation, params, expected_ad_group_ids, expected_ad_group_id
):
    import ads.service as service

    proposal = SimpleNamespace(
        operation=operation,
        status="confirmed",
        customer_id=DRAFT_ACCOUNT_ID,
        params=params,
    )
    captured = {}

    class _Store:
        async def get_confirmed(self, confirmation_id):  # noqa: ARG002
            return proposal

    async def _client(customer_id):
        assert customer_id == DRAFT_ACCOUNT_ID
        return _FakeClient([_ad_group_row()])

    async def _freshness(*args, **kwargs):  # noqa: ARG001
        return None

    async def _apply(**kwargs):
        captured.update(kwargs)
        return {"applied": True}

    monkeypatch.setattr(service, "build_client_async", _client)
    monkeypatch.setattr(service, "_verify_freshness", _freshness)
    monkeypatch.setattr(service.mutations, "apply_add_keywords", _apply)
    monkeypatch.setattr(service.mutations, "apply_add_negative_keywords", _apply)

    with _allowed_ids(DRAFT_ACCOUNT_ID):
        result = await service._apply_confirmed(_Store(), "verified-keywords")

    assert result["applied"] is True
    assert captured["campaign_id"] == "123"
    if expected_ad_group_ids is not None:
        assert captured["ad_group_ids"] == expected_ad_group_ids
    if operation == "add_negative_keywords":
        assert captured["ad_group_id"] == expected_ad_group_id


async def test_target_error_returns_self_healing_invalid_argument(monkeypatch):
    class _Store:
        async def count_run_pending_proposals(self, run_id):  # noqa: ARG002
            return 0

        async def save_proposal(self, **kwargs):  # pragma: no cover - must stay unreachable
            raise AssertionError(f"proposal must not be saved: {kwargs}")

    async def _client(customer_id):
        assert customer_id == DRAFT_ACCOUNT_ID
        return _FakeClient([_ad_group_row(campaign_id="777", campaign_name="Other")])

    monkeypatch.setattr(tools_write, "ConfirmStore", _Store)
    monkeypatch.setattr("ads.client.build_client_async", _client)
    monkeypatch.setattr(propose, "run_ads_read_call", _direct_read)
    token = set_context(request_id="target-envelope", chat_id=1)
    try:
        with _allowed_ids(DRAFT_ACCOUNT_ID), human_turn(actor_user_id=7, run_id="target-envelope"):
            result = await tools_write.propose_create_rsa(
                account=DRAFT_ACCOUNT_ID,
                **_rsa_params(campaign="Search"),
            )
    finally:
        reset_context(token)

    assert result["status"] == "refused"
    assert result["confirmation_id"] is None
    assert result["error_code"] == "invalid_argument"
    assert result["error_type"] == "INVALID_ARGUMENT"
    assert result["suggested_action"]
