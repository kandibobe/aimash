"""Trusted local workflow state for keyword review and RSA curation.

These tools do not mutate Google Ads.  They are nevertheless registered on the trusted PLAN
surface because their durable state is scoped to the real Telegram actor/chat and later feeds a
single confirm-gated Ads proposal.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from adcopy.session import CurationSession, SessionStore
from adcopy.validate import validate
from agent.tools.schemas import CreateSearchCampaign
from ads.client import build_client_async, ensure_read_allowed
from ads.geo import country_name_for_geo_id
from ads.keyword_plan import generate_keyword_ideas
from ads.read import account_currency
from clients.crawl_service import prepare_profile_crawl
from clients.profile_extract import extract_profile
from clients.store import ClientProfileStore, preview_merge
from confirm.store import ConfirmStore
from core import i18n, texts
from campaigns.wizard import build_create_params, public_state
from campaigns.wizard_store import CampaignDraftStore
from core.provenance import get_provenance
from core.resilience import run_ads_read_call
from db import sheets_registry
from keywords.cluster import cluster_keywords, rank_clusters, suggest_negative_keywords
from keywords.export import write_keywords_xlsx
from keywords.filter import filter_relevance
from keywords.seeds import generate_seed_keywords
from mcp_server.artifacts import artifact_path, publish_artifact, remove_artifact
from mcp_server.envelope import ok, proposed, refused
from mcp_server.trusted_transport import get_trusted_turn
from reports.sheets import (
    parse_spreadsheet_id,
    publish_keywords_to_sheets,
    read_keyword_column,
)


def _curation_payload(session: CurationSession) -> dict[str, Any]:
    h, d = session.counts()
    return {
        "session_id": session.session_id,
        "account": session.customer_id,
        "campaign": session.campaign,
        "ad_group_id": session.ad_group_id,
        "ad_group_name": session.ad_group_name,
        "final_url": session.final_url,
        "headlines": session.headlines,
        "descriptions": session.descriptions,
        "approved_headlines": h,
        "approved_descriptions": d,
        "can_finalize": session.can_finalize(),
        "next_pending": session.next_pending(),
    }


async def start_keyword_research(
    account: str,
    topic: str,
    url: str | None = None,
    language: str = "ru",
    geo_ids: list[int] | None = None,
    output: Literal["xlsx", "sheets", "both"] = "both",
    max_ideas: int = 200,
) -> dict[str, Any]:
    """Run seeds → Planner → relevance → clusters → negatives → XLSX/Sheets end-to-end."""
    if output not in {"xlsx", "sheets", "both"}:
        raise ValueError("output must be xlsx, sheets or both")
    if not 1 <= int(max_ideas) <= 500:
        raise ValueError("max_ideas must be between 1 and 500")
    ensure_read_allowed(str(account))
    normalized_geo_ids = tuple(int(item) for item in (geo_ids or []))
    target_geo = ", ".join(
        name for name in (country_name_for_geo_id(item) for item in normalized_geo_ids) if name
    )
    turn = get_trusted_turn()
    store = ClientProfileStore()
    profile = await store.profile_context_text(str(account))
    protected = await store.protected_negative_terms(str(account))
    seeds = await generate_seed_keywords(
        topic=topic,
        url=url,
        profile=profile,
        language=language,
        n=15,
    )
    if not seeds and not url:
        raise ValueError("не удалось получить seed-ключи: укажите тему или URL")
    client = await build_client_async(account)
    ideas = await run_ads_read_call(
        generate_keyword_ideas,
        client,
        str(account),
        seeds=seeds,
        url=url,
        language=language,
        geo_ids=normalized_geo_ids,
        limit=int(max_ideas),
        account=str(account),
        label="mcp.start_keyword_research",
    )
    texts = [item.text for item in ideas]
    relevance, clusters, negatives = await asyncio.gather(
        filter_relevance(
            texts=texts,
            topic=topic,
            profile=profile,
            language=language,
            target_geo=target_geo,
        ),
        cluster_keywords(texts, language),
        suggest_negative_keywords(
            topic,
            texts,
            language=language,
            profile=profile,
            protected=protected,
        ),
    )
    off_topic = {text for text, relevant in relevance.items() if not relevant}
    clusters = rank_clusters(
        clusters,
        {item.text: int(item.avg_monthly_searches or 0) for item in ideas},
        off_topic,
    )
    currency = await run_ads_read_call(
        account_currency,
        client,
        str(account),
        account=str(account),
        label="mcp.keyword_research.currency",
    )
    extra: dict[str, Any] = {
        "account": str(account),
        "seeds": seeds,
        "ideas": len(ideas),
        "relevant": sum(1 for value in relevance.values() if value),
        "irrelevant": len(off_topic),
        "clusters": len(clusters),
        "negative_suggestions": negatives,
        "currency": currency or "",
        "target_geo": target_geo,
    }
    if output in {"xlsx", "both"}:
        path = artifact_path(".xlsx")
        try:
            await asyncio.to_thread(
                write_keywords_xlsx,
                clusters,
                ideas,
                str(path),
                seeds=seeds,
                url=url,
                language=language,
                negatives=negatives,
                currency=currency or "",
                relevance=relevance,
            )
            extra["artifact"] = publish_artifact(
                path,
                filename=f"aimash_keywords_{account}.xlsx",
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        except Exception:
            remove_artifact(path)
            raise
    if output in {"sheets", "both"}:
        title = f"Aimash keywords · {account} · {topic}"[:255]
        sheet_url, sheet_id, share = await asyncio.to_thread(
            publish_keywords_to_sheets,
            ideas,
            relevance,
            title=title,
            currency=currency or "",
        )
        await sheets_registry.record(
            chat_id=turn.actor_chat_id,
            customer_id=str(account),
            kind="keywords",
            spreadsheet_id=sheet_id,
            url=sheet_url,
            title=title,
            share=share,
        )
        extra["sheet"] = {
            "url": sheet_url,
            "spreadsheet_id": sheet_id,
            "share": share,
            "review_expires_days": 14,
        }
    sample = [
        {
            "keyword": item.text,
            "avg_monthly_searches": int(item.avg_monthly_searches or 0),
            "relevant": relevance.get(item.text, True),
        }
        for item in ideas[:20]
    ]
    return ok(sample, limit=20, extra=extra)


async def read_keyword_sheet(
    account: str,
    spreadsheet: str,
    default_match_type: Literal["broad", "phrase", "exact"] = "phrase",
) -> dict[str, Any]:
    """Read only a still-valid keyword sheet previously minted for this exact chat/account."""
    ensure_read_allowed(str(account))
    turn = get_trusted_turn()
    sheet_id = parse_spreadsheet_id(spreadsheet)
    if not sheet_id:
        raise ValueError("не удалось распознать spreadsheet_id")
    if not await sheets_registry.is_owned_keyword_sheet(
        chat_id=turn.actor_chat_id,
        customer_id=str(account),
        spreadsheet_id=sheet_id,
        max_age_days=14,
    ):
        raise PermissionError(
            "таблица не принадлежит этому чату/аккаунту либо срок review истёк; запустите новый research"
        )
    keywords = await asyncio.to_thread(read_keyword_column, sheet_id, own_file=True)
    return ok(
        [{"keyword": keyword, "match_type": default_match_type} for keyword in keywords],
        limit=1000,
        extra={"spreadsheet_id": sheet_id, "verified": True},
    )


async def curation_start(
    account: str,
    campaign: str,
    ad_group_id: str,
    ad_group_name: str,
    final_url: str,
    headlines: list[str],
    descriptions: list[str],
    brief: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start durable per-element RSA curation; this is state, not an executable proposal."""
    if not 3 <= len(headlines) <= 15 or not 2 <= len(descriptions) <= 4:
        raise ValueError("RSA requires 3..15 headlines and 2..4 descriptions")
    for kind, values in (("headline", headlines), ("description", descriptions)):
        for text in values:
            valid, length = validate(text, kind)
            if not valid:
                raise ValueError(f"{kind} exceeds limit: {length}")
    turn = get_trusted_turn()
    session_id = await SessionStore().create(
        chat_id=turn.actor_chat_id,
        customer_id=str(account),
        campaign=campaign,
        ad_group_id=ad_group_id,
        ad_group_name=ad_group_name,
        final_url=final_url,
        headlines=headlines,
        descriptions=descriptions,
        brief=brief or {},
    )
    session = await SessionStore().get(session_id, expected_chat_id=turn.actor_chat_id)
    if session is None:
        raise RuntimeError("curation session was not persisted")
    return _curation_payload(session)


async def curation_state(account: str, session_id: str) -> dict[str, Any]:
    """Read one curation session owned by the current Telegram chat and account."""
    turn = get_trusted_turn()
    session = await SessionStore().get(session_id, expected_chat_id=turn.actor_chat_id)
    if session is None or session.customer_id != str(account):
        raise PermissionError("curation session not found for this chat/account")
    return _curation_payload(session)


async def curation_apply(
    account: str,
    session_id: str,
    kind: Literal["headline", "description"],
    index: int,
    action: Literal["approve", "reject", "replace"],
    new_text: str | None = None,
) -> dict[str, Any]:
    """Approve/reject/replace one 1-based RSA element; code recalculates its length."""
    turn = get_trusted_turn()
    store = SessionStore()
    session = await store.get(session_id, expected_chat_id=turn.actor_chat_id)
    if session is None or session.customer_id != str(account):
        raise PermissionError("curation session not found for this chat/account")
    short_kind = "h" if kind == "headline" else "d"
    values = session.headlines if short_kind == "h" else session.descriptions
    idx = int(index) - 1
    if idx < 0 or idx >= len(values):
        raise ValueError("element index is out of range")
    if action == "replace":
        if not new_text:
            raise ValueError("new_text is required for replace")
        valid, length = validate(new_text, kind)
        if not valid:
            raise ValueError(f"{kind} exceeds limit: {length}")
        session = await store.replace_element(
            session_id,
            short_kind,
            idx,
            new_text,
            expected_chat_id=turn.actor_chat_id,
        )
    else:
        session = await store.set_state(
            session_id,
            short_kind,
            idx,
            "approved" if action == "approve" else "rejected",
            expected_chat_id=turn.actor_chat_id,
        )
    if session is None:
        raise RuntimeError("curation state update failed")
    return _curation_payload(session)


async def curation_finalize(
    account: str,
    session_id: str,
    path1: str | None = None,
    path2: str | None = None,
) -> dict[str, Any]:
    """Mint the one real create_rsa proposal from approved elements; Ads remains untouched."""
    from mcp_server.tools_write import propose_create_rsa

    turn = get_trusted_turn()
    session = await SessionStore().get(session_id, expected_chat_id=turn.actor_chat_id)
    if session is None or session.customer_id != str(account):
        raise PermissionError("curation session not found for this chat/account")
    if not session.can_finalize():
        h, d = session.counts()
        raise ValueError(f"not enough approved RSA elements: headlines={h}, descriptions={d}")
    return await propose_create_rsa(
        account=str(account),
        campaign=session.campaign,
        ad_group_id=session.ad_group_id,
        final_url=session.final_url,
        headlines=session.approved_texts("h"),
        descriptions=session.approved_texts("d"),
        path1=path1,
        path2=path2,
    )


async def search_wizard_start(account: str) -> dict[str, Any]:
    """Start stage 0 of the durable eight-stage Search campaign wizard."""
    ensure_read_allowed(str(account))
    turn = get_trusted_turn()
    session_id = await CampaignDraftStore().create(
        chat_id=turn.actor_chat_id,
        customer_id=str(account),
        preview_customer_id=str(account),
    )
    snapshot = await CampaignDraftStore().get(session_id, expected_chat_id=turn.actor_chat_id)
    if snapshot is None:
        raise RuntimeError("wizard session was not persisted")
    return public_state(snapshot)


async def search_wizard_state(account: str, session_id: str) -> dict[str, Any]:
    """Return one wizard owned by this exact trusted Telegram chat/account."""
    turn = get_trusted_turn()
    snapshot = await CampaignDraftStore().get(session_id, expected_chat_id=turn.actor_chat_id)
    if snapshot is None or snapshot.customer_id != str(account):
        raise PermissionError("wizard session not found for this chat/account")
    return public_state(snapshot)


def _stage_patch(stage: int, data: dict[str, Any]) -> Callable[[dict[str, Any]], None]:
    if not isinstance(data, dict):
        raise ValueError("data must be an object")
    clean = dict(data)

    if stage == 1:
        allowed = {
            "campaign_name",
            "budget_daily_micros",
            "cpc_bid_micros",
            "geo_locations",
            "geo_country_code",
            "geo_locale",
            "languages",
            "bidding",
            "bidding_strategy",
            "target_cpa_micros",
            "networks",
            "ad_schedule_blocks",
            "start_date",
            "end_date",
        }
        if not clean.get("campaign_name") or not clean.get("budget_daily_micros"):
            raise ValueError("stage 1 requires campaign_name and budget_daily_micros")
        target = "settings"
    elif stage == 2:
        allowed = {"list", "match_type", "match_types", "source", "sheet_id", "verified"}
        if not isinstance(clean.get("list"), list):
            raise ValueError("stage 2 requires keyword list")
        target = "keywords"
    elif stage == 3:
        allowed = {"final_url", "headlines", "descriptions", "path1", "path2", "rsa_session_id"}
        # Reuse the production schema's RSA/URL validation with harmless placeholder fields.
        CreateSearchCampaign(
            campaign_name="validation",
            final_url=clean.get("final_url"),
            headlines=clean.get("headlines") or [],
            descriptions=clean.get("descriptions") or [],
            budget_daily_micros=1,
            path1=clean.get("path1"),
            path2=clean.get("path2"),
        )
        target = "ad"
    elif stage == 4:
        allowed = {"media_ids", "skipped", "eligible"}
        media_ids = clean.get("media_ids") or []
        if not isinstance(media_ids, list) or len(media_ids) > 10:
            raise ValueError("stage 4 accepts at most 10 media_ids")
        if any(not str(item).isalnum() for item in media_ids):
            raise ValueError("media_id must be alphanumeric")
        target = "images"
    elif stage == 5:
        allowed = {"reuse_links", "reuse_candidates", "reuse_excluded", "new"}
        target = "assets"
    elif stage == 6:
        allowed = {"tracking_url_template", "final_url_suffix", "custom_parameters"}
        target = "url_options"
    else:
        raise ValueError("stage must be between 1 and 6; stage 7 is finalize")
    unknown = set(clean) - allowed
    if unknown:
        raise ValueError(f"unsupported stage {stage} fields: {', '.join(sorted(unknown))}")

    def apply(state: dict[str, Any]) -> None:
        current = dict(state.get(target) or {})
        current.update(clean)
        state[target] = current

    return apply


async def search_wizard_update(
    account: str,
    session_id: str,
    stage: int,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Persist stages 1..6 sequentially; no Google Ads mutation and no proposal yet."""
    turn = get_trusted_turn()
    store = CampaignDraftStore()
    snapshot = await store.get(session_id, expected_chat_id=turn.actor_chat_id)
    if snapshot is None or snapshot.customer_id != str(account) or snapshot.status != "active":
        raise PermissionError("active wizard session not found for this chat/account")
    stage = int(stage)
    if stage > int(snapshot.current_step) + 1:
        raise ValueError(f"wizard stage {snapshot.current_step + 1} must be completed first")
    if stage < 1 or stage > 6:
        raise ValueError("stage must be between 1 and 6")
    updated = await store.patch(
        session_id,
        _stage_patch(stage, data),
        expected_chat_id=turn.actor_chat_id,
    )
    if updated is None:
        raise RuntimeError("wizard state update failed")
    updated = await store.set_step(session_id, stage, expected_chat_id=turn.actor_chat_id)
    if updated is None:
        raise RuntimeError("wizard stage update failed")
    return public_state(updated)


async def search_wizard_finalize(account: str, session_id: str) -> dict[str, Any]:
    """Validate stage 7 and mint exactly one composite create-PAUSED proposal."""
    from mcp_server.tools_write import propose_create_search_campaign

    turn = get_trusted_turn()
    store = CampaignDraftStore()
    snapshot = await store.get(session_id, expected_chat_id=turn.actor_chat_id)
    if snapshot is None or snapshot.customer_id != str(account) or snapshot.status != "active":
        raise PermissionError("active wizard session not found for this chat/account")
    if int(snapshot.current_step) < 6:
        raise ValueError(f"wizard is incomplete: current stage={snapshot.current_step}, required=6")
    params = build_create_params(snapshot.wizard_state)
    result = await propose_create_search_campaign(account=str(account), **params)
    if result.get("status") == "proposed":
        await store.set_step(session_id, 7, expected_chat_id=turn.actor_chat_id)
    return result


async def ingest_media(
    account: str,
    kind: Literal["image", "video"],
) -> dict[str, Any]:
    """Consume media attached to this trusted Telegram message; model paths are never accepted."""
    from ads.assets import prepare_display_images, save_pending_media

    ensure_read_allowed(str(account))
    turn = get_trusted_turn()
    candidates = [
        item
        for item in turn.inbound_media
        if (kind == "image" and item.suffix in {".jpg", ".jpeg", ".png", ".webp"})
        or (kind == "video" and item.suffix in {".mp4", ".mov"})
    ]
    if not candidates:
        raise ValueError(f"current Telegram message has no trusted {kind} attachment")
    media = candidates[0]
    path = Path(media.path)
    try:
        payload = await asyncio.to_thread(path.read_bytes)
        if len(payload) != media.size or hashlib.sha256(payload).hexdigest() != media.sha256:
            raise ValueError("inbound media changed after trusted gateway copy")
        if kind == "video":
            return ok(
                [{"kind": "video", "size": media.size, "sha256": media.sha256}],
                limit=1,
                extra={
                    "received": True,
                    "google_ads_ready": False,
                    "required_next": "youtube_video_id",
                },
            )
        landscape, square = await asyncio.to_thread(prepare_display_images, payload)
        media_id = secrets.token_hex(16)
        await asyncio.to_thread(save_pending_media, media_id, landscape, square)
        return ok(
            [{"kind": "image", "media_id": media_id}],
            limit=1,
            extra={"received": True, "google_ads_ready": True},
        )
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


async def _profile_proposal(
    *,
    account: str,
    operation: Literal["profile_save", "profile_update", "profile_clear"],
    params: dict[str, Any],
    before: dict[str, Any] | None,
    after: dict[str, Any],
) -> dict[str, Any]:
    turn = get_trusted_turn()
    provenance = get_provenance()
    store = ConfirmStore()
    if await store.count_run_pending_proposals(provenance.run_id) >= 1:
        return refused(i18n.t("propose_draft_limit"), error_code="refused")
    confirmation_id = uuid.uuid4().hex
    summary = texts.fmt_client_diff(
        before,
        after,
        str(account),
        operation=operation,
        lang=turn.language_code,
    )
    await store.save_proposal(
        confirmation_id=confirmation_id,
        operation=operation,
        customer_id=str(account),
        params=params,
        summary=summary,
        chat_id=turn.actor_chat_id,
        user_initiated=True,
    )
    return proposed(
        confirmation_id=confirmation_id,
        operation=operation,
        customer_id=str(account),
        preview=summary,
        unchanged_label="Профиль клиента",
    )


async def propose_profile_change(account: str, text: str) -> dict[str, Any]:
    """Extract free-form client data and propose a confirm-gated account profile change."""
    ensure_read_allowed(str(account))
    extracted = await extract_profile(text, language=i18n.current_lang())
    if extracted.is_empty():
        raise ValueError("profile text contains no usable client facts")
    patch = extracted.to_patch()
    profile_store = ClientProfileStore()
    before = await profile_store.get_by_account(str(account))
    operation: Literal["profile_save", "profile_update"] = (
        "profile_update" if before is not None else "profile_save"
    )
    return await _profile_proposal(
        account=str(account),
        operation=operation,
        params={"customer_id": str(account), "patch": patch, "source": "text"},
        before=before,
        after=preview_merge(before, patch),
    )


async def propose_profile_clear(account: str) -> dict[str, Any]:
    """Create a confirm-gated proposal to clear a client profile."""
    ensure_read_allowed(str(account))
    before = await ClientProfileStore().get_by_account(str(account))
    if before is None:
        raise ValueError("client profile does not exist")
    return await _profile_proposal(
        account=str(account),
        operation="profile_clear",
        params={"customer_id": str(account)},
        before=before,
        after={},
    )


async def start_client_crawl(
    account: str,
    mode: Literal["full", "incremental"] = "full",
) -> dict[str, Any]:
    """Crawl the stored profile URL and autonomously save the account-scoped profile update."""
    ensure_read_allowed(str(account))
    turn = get_trusted_turn()
    result = await prepare_profile_crawl(
        str(account),
        turn.actor_chat_id,
        mode=mode,
        language=i18n.current_lang(),
    )
    if result.get("unchanged"):
        return ok([], limit=1, extra=result)
    patch = dict(result["patch"])
    before = result.get("before")
    operation: Literal["profile_save", "profile_update"] = (
        "profile_update" if before is not None else "profile_save"
    )
    proposal = await _profile_proposal(
        account=str(account),
        operation=operation,
        params={
            "customer_id": str(account),
            "patch": patch,
            "source": "crawl",
            "crawl_extra": result["crawl_extra"],
        },
        before=before,
        after=preview_merge(before, patch),
    )
    proposal["crawl"] = {
        "job_id": result["job_id"],
        "pages": result["pages"],
        "domain": result["domain"],
        "partial": result.get("partial", False),
    }
    return proposal


WORKFLOW_STATE_TOOL_FUNCS: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    "start_keyword_research": start_keyword_research,
    "read_keyword_sheet": read_keyword_sheet,
    "curation_start": curation_start,
    "curation_state": curation_state,
    "curation_apply": curation_apply,
    "curation_finalize": curation_finalize,
    "search_wizard_start": search_wizard_start,
    "search_wizard_state": search_wizard_state,
    "search_wizard_update": search_wizard_update,
    "search_wizard_finalize": search_wizard_finalize,
    "ingest_media": ingest_media,
    "profile_change": propose_profile_change,
    "profile_clear": propose_profile_clear,
    "start_client_crawl": start_client_crawl,
}
