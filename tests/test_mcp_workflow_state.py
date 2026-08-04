from __future__ import annotations

from types import SimpleNamespace
from datetime import date

import pytest

from ads.client import DRAFT_ACCOUNT_ID
from ads.keyword_plan import KeywordIdea
from mcp_server import tools_workflow_state as ws
from mcp_server.trusted_transport import TrustedMedia, TrustedTurn, trusted_turn_scope
from reports.period import Period
from reports.queries import Breakdown, Metrics
from reports.service import ReportData


def _turn(chat_id: int = 100) -> TrustedTurn:
    return TrustedTurn(
        actor_user_id=777,
        actor_chat_id=chat_id,
        actor_username="operator",
        chat_type="supergroup",
        thread_id="7",
        message_id=10,
        language_code="ru",
        reply_to_message_id=None,
        reply_to_is_own_message=False,
        reply_confirmation_id=None,
        reply_to_text=None,
        issued_at=1,
        expires_at=2,
    )


@pytest.mark.asyncio
async def test_rsa_curation_15_4_round_trip_to_one_proposal(monkeypatch):
    headlines = [f"Заголовок {idx}" for idx in range(1, 16)]
    descriptions = [f"Описание объявления {idx}" for idx in range(1, 5)]
    captured = {}

    async def fake_propose(**kwargs):
        captured.update(kwargs)
        return {"status": "pending", "confirmation_id": "c" * 32}

    monkeypatch.setattr("mcp_server.tools_write.propose_create_rsa", fake_propose)
    with trusted_turn_scope(_turn()):
        started = await ws.curation_start(
            DRAFT_ACCOUNT_ID,
            "Campaign",
            "123",
            "Group",
            "https://example.com",
            headlines,
            descriptions,
        )
        session_id = started["session_id"]
        for index in range(1, 16):
            state = await ws.curation_apply(
                DRAFT_ACCOUNT_ID, session_id, "headline", index, "approve"
            )
        for index in range(1, 5):
            state = await ws.curation_apply(
                DRAFT_ACCOUNT_ID, session_id, "description", index, "approve"
            )
        result = await ws.curation_finalize(DRAFT_ACCOUNT_ID, session_id)

    assert state["can_finalize"] is True
    assert result["confirmation_id"] == "c" * 32
    assert captured["headlines"] == headlines
    assert captured["descriptions"] == descriptions
    assert captured["account"] == DRAFT_ACCOUNT_ID


@pytest.mark.asyncio
async def test_curation_is_scoped_to_chat_and_account():
    with trusted_turn_scope(_turn(chat_id=100)):
        started = await ws.curation_start(
            DRAFT_ACCOUNT_ID,
            "Campaign",
            "123",
            "Group",
            "https://example.com",
            ["A", "B", "C"],
            ["Description A", "Description B"],
        )
    with trusted_turn_scope(_turn(chat_id=200)):
        with pytest.raises(PermissionError):
            await ws.curation_state(DRAFT_ACCOUNT_ID, started["session_id"])


@pytest.mark.asyncio
async def test_keyword_research_builds_signed_delivery_artifact(monkeypatch, tmp_path):
    ideas = [KeywordIdea("купить цветы", 100), KeywordIdea("цветы бесплатно", 20)]
    cluster = SimpleNamespace(
        name="Покупка", intent="транзакционный", keywords=[item.text for item in ideas], priority=0
    )

    monkeypatch.setattr(ws, "ensure_read_allowed", lambda account: None)
    monkeypatch.setattr(ws, "artifact_path", lambda suffix: tmp_path / f"keywords{suffix}")
    monkeypatch.setattr(
        ws,
        "publish_artifact",
        lambda path, **kwargs: {"filename": kwargs["filename"], "token": "signed"},
    )
    monkeypatch.setattr(ws, "build_client_async", lambda account: _async_value(object()))
    monkeypatch.setattr(
        ws, "generate_seed_keywords", lambda **kwargs: _async_value(["цветы", "букет"])
    )
    monkeypatch.setattr(
        ws.ClientProfileStore,
        "profile_context_text",
        lambda self, account: _async_value("Бренд Flowers"),
    )
    monkeypatch.setattr(
        ws.ClientProfileStore,
        "protected_negative_terms",
        lambda self, account: _async_value({"flowers"}),
    )

    async def fake_read(fn, *args, **kwargs):
        return "UAH" if fn is ws.account_currency else ideas

    monkeypatch.setattr(ws, "run_ads_read_call", fake_read)
    monkeypatch.setattr(
        ws,
        "filter_relevance",
        lambda **kwargs: _async_value({ideas[0].text: True, ideas[1].text: False}),
    )
    monkeypatch.setattr(ws, "cluster_keywords", lambda *args, **kwargs: _async_value([cluster]))
    monkeypatch.setattr(
        ws, "suggest_negative_keywords", lambda *args, **kwargs: _async_value(["бесплатно"])
    )
    monkeypatch.setattr(ws, "rank_clusters", lambda clusters, *args: clusters)

    def fake_write(_clusters, _ideas, path, **kwargs):
        from pathlib import Path

        Path(path).write_bytes(b"xlsx")
        return path

    monkeypatch.setattr(ws, "write_keywords_xlsx", fake_write)

    with trusted_turn_scope(_turn()):
        result = await ws.start_keyword_research(DRAFT_ACCOUNT_ID, "доставка цветов", output="xlsx")

    assert result["artifact"]["token"] == "signed"
    assert result["ideas"] == 2
    assert result["relevant"] == 1
    assert result["negative_suggestions"] == ["бесплатно"]
    assert result["metric_rows"] == 2
    assert result["artifact"]["filename"].startswith(f"aimash_keywords_{DRAFT_ACCOUNT_ID}_")


@pytest.mark.asyncio
async def test_keyword_research_does_not_publish_zero_metric_workbook(monkeypatch):
    ideas = [KeywordIdea("fisch restaurant essen", 0), KeywordIdea("restaurant essen", 0)]
    seen: dict[str, object] = {}

    monkeypatch.setattr(ws, "ensure_read_allowed", lambda account: None)
    monkeypatch.setattr(ws, "build_client_async", lambda account: _async_value(object()))
    monkeypatch.setattr(
        ws.ClientProfileStore,
        "profile_context_text",
        lambda self, account: _async_value("Restaurant profile from another task"),
    )
    monkeypatch.setattr(
        ws.ClientProfileStore,
        "protected_negative_terms",
        lambda self, account: _async_value(set()),
    )

    async def fake_seeds(**kwargs):
        seen.update(kwargs)
        return ["angeln deutschland"]

    monkeypatch.setattr(ws, "generate_seed_keywords", fake_seeds)

    async def fake_read(fn, *args, **kwargs):
        return "AUD" if fn is ws.account_currency else ideas

    monkeypatch.setattr(ws, "run_ads_read_call", fake_read)
    monkeypatch.setattr(
        ws, "filter_relevance", lambda **kwargs: _async_value({item.text: True for item in ideas})
    )
    monkeypatch.setattr(ws, "cluster_keywords", lambda *args, **kwargs: _async_value([]))
    monkeypatch.setattr(ws, "suggest_negative_keywords", lambda *args, **kwargs: _async_value([]))
    monkeypatch.setattr(ws, "rank_clusters", lambda clusters, *args: clusters)
    monkeypatch.setattr(
        ws,
        "write_keywords_xlsx",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("zero-metric workbook must not be written")
        ),
    )
    monkeypatch.setattr(
        ws,
        "publish_artifact",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("zero-metric workbook must not be published")
        ),
    )

    with trusted_turn_scope(_turn()):
        result = await ws.start_keyword_research(
            DRAFT_ACCOUNT_ID, "angeln deutschland", output="xlsx"
        )

    assert seen["profile"] == ""
    assert result["artifact_status"] == "not_published"
    assert result["data_gap"] == "planner_metrics_unavailable"
    assert result["metric_rows"] == 0
    assert "artifact" not in result


@pytest.mark.asyncio
async def test_keyword_sheet_rejects_substitution(monkeypatch):
    monkeypatch.setattr(ws, "ensure_read_allowed", lambda account: None)
    monkeypatch.setattr(ws, "parse_spreadsheet_id", lambda value: "sheet-id")

    async def not_owned(**kwargs):
        return False

    monkeypatch.setattr(ws.sheets_registry, "is_owned_keyword_sheet", not_owned)
    with trusted_turn_scope(_turn()):
        with pytest.raises(PermissionError):
            await ws.read_keyword_sheet(DRAFT_ACCOUNT_ID, "sheet-id")


@pytest.mark.asyncio
async def test_search_term_review_has_real_wow_evidence_and_no_mutation(monkeypatch):
    from reports.queries import Metrics, SearchTermRow

    monkeypatch.setattr(ws, "ensure_read_allowed", lambda account: None)
    monkeypatch.setattr(ws, "build_client_async", lambda account: _async_value(object()))
    current = SearchTermRow(
        "free games",
        "Premium",
        "Sales",
        "buy product",
        "BROAD",
        Metrics(impressions=1000, clicks=10, cost_micros=25_000_000, conversions=0),
    )
    previous = SearchTermRow(
        "free games",
        "Premium",
        "Sales",
        "buy product",
        "BROAD",
        Metrics(impressions=500, clicks=5, cost_micros=10_000_000, conversions=0),
    )
    calls = 0

    async def fake_read(fn, *args, **kwargs):
        nonlocal calls
        if fn is ws.account_currency:
            return "EUR"
        calls += 1
        return current and ([current] if calls == 1 else [previous])

    monkeypatch.setattr(ws, "run_ads_read_call", fake_read)
    monkeypatch.setattr(
        ws,
        "publish_search_term_review_to_sheets",
        lambda items, **kwargs: (
            "https://docs.google.com/spreadsheets/d/SHEET1234567890123456",
            "SHEET1234567890123456",
            "writer",
        ),
    )

    async def fake_record(**kwargs):
        assert kwargs["kind"] == "search_terms"

    monkeypatch.setattr(ws.sheets_registry, "record", fake_record)
    with trusted_turn_scope(_turn()):
        result = await ws.create_search_term_review(
            DRAFT_ACCOUNT_ID,
            [{"search_term": "free games", "campaign": "Premium", "reason": "off-topic"}],
        )

    assert result["error"] is None and result["advisory_only"] is True
    assert result["rows"][0]["cost_wow_pct"] == 150.0
    assert result["sheet"]["share"] == "writer"


@pytest.mark.asyncio
async def test_search_term_review_rejects_foreign_sheet(monkeypatch):
    monkeypatch.setattr(ws, "ensure_read_allowed", lambda account: None)
    monkeypatch.setattr(ws, "parse_spreadsheet_id", lambda value: "sheet-id")
    monkeypatch.setattr(
        ws.sheets_registry,
        "is_owned_sheet",
        lambda **kwargs: _async_value(False),
    )
    with trusted_turn_scope(_turn()):
        with pytest.raises(PermissionError):
            await ws.read_search_term_review(DRAFT_ACCOUNT_ID, "sheet-id")


@pytest.mark.asyncio
async def test_monthly_pdf_requires_trusted_human_turn_before_reads(monkeypatch):
    monkeypatch.setattr(ws, "ensure_read_allowed", lambda account: None)

    async def must_not_read(account):
        raise AssertionError("cron/model-only turn reached Google Ads reader")

    monkeypatch.setattr(ws, "build_client_async", must_not_read)
    with pytest.raises(PermissionError):
        await ws.build_monthly_pdf(DRAFT_ACCOUNT_ID, "Краткая сводка без чисел")


@pytest.mark.asyncio
async def test_monthly_pdf_is_advisory_artifact_after_human_command(monkeypatch, tmp_path):
    report = ReportData(
        customer_id=DRAFT_ACCOUNT_ID,
        period=Period(date(2026, 7, 1), date(2026, 7, 31), "июль"),
        totals=Metrics(
            impressions=10_000,
            clicks=500,
            cost_micros=1_000_000_000,
            conversions=50,
            conv_value=4_000,
        ),
        prev_totals=Metrics(
            impressions=9_000,
            clicks=450,
            cost_micros=900_000_000,
            conversions=45,
            conv_value=3_600,
        ),
        breakdowns=[
            Breakdown(
                key="campaign",
                title="Кампании",
                dim_headers=["Кампания", "Статус"],
                rows=[(("Brand", "ENABLED"), Metrics(cost_micros=600_000_000, conversions=40))],
            )
        ],
        currency="EUR",
    )
    monkeypatch.setattr(ws, "ensure_read_allowed", lambda account: None)
    monkeypatch.setattr(ws, "build_client_async", lambda account: _async_value(object()))
    monkeypatch.setattr(ws, "run_ads_read_call", lambda *args, **kwargs: _async_value("EUR"))
    monkeypatch.setattr(
        "reports.service.build_account_report_async",
        lambda *args, **kwargs: _async_value(report),
    )
    output = tmp_path / "monthly.pdf"
    monkeypatch.setattr(ws, "artifact_path", lambda suffix: output)
    monkeypatch.setattr(
        ws,
        "publish_artifact",
        lambda path, **kwargs: {"filename": kwargs["filename"], "media_type": kwargs["media_type"]},
    )
    captured = {}

    def fake_write(report_arg, path, **kwargs):
        captured.update(kwargs)
        path.write_bytes(b"%PDF-1.7\nfixture")

    monkeypatch.setattr("reports.pdf.write_monthly_report_pdf", fake_write)
    with trusted_turn_scope(_turn()):
        result = await ws.build_monthly_pdf(
            DRAFT_ACCOUNT_ID,
            "Расход вырос на 11.1%; причинность не установлена.",
            risks=["Нужна ручная проверка tracking."],
            next_month_plan=["Проверить приоритеты до изменения аккаунта."],
        )

    assert result["error"] is None and result["advisory_only"] is True
    assert result["artifact"]["media_type"] == "application/pdf"
    assert result["human_command_message_id"] == _turn().message_id
    assert captured["executive_summary"].startswith("Расход")


@pytest.mark.asyncio
async def test_ingest_image_uses_only_trusted_media_and_deletes_copy(monkeypatch, tmp_path):
    import hashlib

    payload = b"trusted-image"
    path = tmp_path / "input.jpg"
    path.write_bytes(payload)
    media = TrustedMedia(
        path=str(path), suffix=".jpg", size=len(payload), sha256=hashlib.sha256(payload).hexdigest()
    )
    turn = _turn()
    turn = TrustedTurn(
        **{
            name: getattr(turn, name)
            for name in turn.__dataclass_fields__
            if name != "inbound_media"
        },
        inbound_media=(media,),
    )
    saved = {}
    monkeypatch.setattr(ws, "ensure_read_allowed", lambda account: None)
    monkeypatch.setattr("ads.assets.prepare_display_images", lambda data: (b"landscape", b"square"))
    monkeypatch.setattr(
        "ads.assets.save_pending_media",
        lambda media_id, landscape, square: saved.update(
            {"media_id": media_id, "landscape": landscape, "square": square}
        ),
    )

    with trusted_turn_scope(turn):
        result = await ws.ingest_media(DRAFT_ACCOUNT_ID, "image")

    assert result["google_ads_ready"] is True
    assert result["rows"][0]["media_id"] == saved["media_id"]
    assert saved["landscape"] == b"landscape" and saved["square"] == b"square"
    assert not path.exists()


@pytest.mark.asyncio
async def test_ingest_video_is_received_but_requires_youtube_id(monkeypatch, tmp_path):
    import hashlib

    payload = b"trusted-video"
    path = tmp_path / "input.mp4"
    path.write_bytes(payload)
    base = _turn()
    turn = TrustedTurn(
        **{
            name: getattr(base, name)
            for name in base.__dataclass_fields__
            if name != "inbound_media"
        },
        inbound_media=(
            TrustedMedia(
                path=str(path),
                suffix=".mp4",
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            ),
        ),
    )
    monkeypatch.setattr(ws, "ensure_read_allowed", lambda account: None)

    with trusted_turn_scope(turn):
        result = await ws.ingest_media(DRAFT_ACCOUNT_ID, "video")

    assert result["received"] is True
    assert result["google_ads_ready"] is False
    assert result["required_next"] == "youtube_video_id"
    assert not path.exists()


@pytest.mark.asyncio
async def test_profile_save_stops_at_pending_confirmation(monkeypatch):
    saved = {}

    class _Store:
        async def save_proposal(self, **kwargs):
            saved.update(kwargs)
            return SimpleNamespace(
                confirmation_id=kwargs["confirmation_id"],
                operation=kwargs["operation"],
                customer_id=kwargs["customer_id"],
                summary=kwargs["summary"],
            )

    monkeypatch.setattr(ws, "ConfirmStore", _Store)

    with trusted_turn_scope(_turn()):
        result = await ws._profile_proposal(
            account=DRAFT_ACCOUNT_ID,
            operation="profile_save",
            params={"customer_id": DRAFT_ACCOUNT_ID, "patch": {"brand": "Aimash"}},
            before=None,
            after={"brand": "Aimash"},
        )

    assert result["status"] == "pending"
    assert result["operation"] == "profile_save"
    assert result["preview"]
    assert saved["operation"] == "profile_save"
    assert saved["source_message_id"] == _turn().message_id
    assert saved["idempotency_args"]["account"] == DRAFT_ACCOUNT_ID


async def _async_value(value):
    return value
