"""Bot-free READ workflows used by Hermes for reports, keywords, RSA and client profiles."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Literal

from adcopy.display_path import build_display_path as _build_display_path
from adcopy.generate import CopyBrief, generate_rsa as _generate_rsa
from adcopy.validate import (
    any_cta,
    find_duplicates,
    keyword_coverage,
    moderation_issues,
    validate,
)
from ads.client import build_client_async, ensure_read_allowed
from ads.read import account_currency
from clients import crawl_jobs
from clients.store import ClientProfileStore
from core import observe
from core.resilience import run_ads_read_call
from keywords.cluster import cluster_keywords as _cluster_keywords
from keywords.cluster import suggest_negative_keywords
from keywords.filter import filter_relevance
from keywords.ingest import parse_keywords_text
from keywords.seeds import generate_seed_keywords
from mcp_server.envelope import err, ok
from reports.service import build_account_report_async
from reports.xlsx import write_report_xlsx


async def _guarded(
    work: Callable[[], Awaitable[dict[str, Any]]], *, account: str
) -> dict[str, Any]:
    try:
        ensure_read_allowed(str(account))
        async with observe.run_scope("mcp_read"):
            return await work()
    except Exception as exc:  # noqa: BLE001 - MCP boundary, never leak raw exception text
        tool_name = work.__qualname__.split(".<locals>", 1)[0].rsplit(".", 1)[-1]
        return err(exc, tool_name=tool_name, account=str(account))


async def build_report(
    account: str,
    date_from: str | None = None,
    date_to: str | None = None,
    period_days: int | None = 30,
    period_preset: Literal["7", "14", "30", "90", "MTD", "LM"] | None = None,
    campaign_id: str | None = None,
    language: Literal["ru", "en"] = "ru",
) -> dict[str, Any]:
    """Build a deep account report and deliver the resulting .xlsx to the current Telegram topic."""
    from mcp_server.artifacts import artifact_path, publish_artifact, remove_artifact
    from mcp_server.tools_read import _period

    async def _work() -> dict[str, Any]:
        window = _period(date_from, date_to, period_days, period_preset)
        client = await build_client_async(account)
        currency = await run_ads_read_call(
            account_currency,
            client,
            str(account),
            account=str(account),
            label="mcp.build_report.currency",
        )
        report = await build_account_report_async(
            client,
            str(account),
            window,
            currency=currency or "",
            campaign_id=campaign_id,
        )
        path = artifact_path(".xlsx")
        try:
            await asyncio.to_thread(write_report_xlsx, report, str(path), language)
            artifact = publish_artifact(
                path,
                filename=f"aimash_report_{account}.xlsx",
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        except Exception:
            remove_artifact(path)
            raise
        return ok(
            [],
            extra={
                "artifact": artifact,
                "account": str(account),
                "period": {
                    "date_from": report.period.date_from.isoformat(),
                    "date_to": report.period.date_to.isoformat(),
                },
                "currency": currency or "",
            },
        )

    return await _guarded(_work, account=str(account))


async def seed_keywords(
    account: str,
    topic: str,
    url: str | None = None,
    language: str = "ru",
    count: int = 15,
) -> dict[str, Any]:
    """Generate seed phrases from a topic plus the account's PII-free client profile."""

    async def _work() -> dict[str, Any]:
        if not 1 <= int(count) <= 50:
            raise ValueError("count must be between 1 and 50")
        profile = await ClientProfileStore().profile_context_text(str(account))
        rows = await generate_seed_keywords(
            topic=topic,
            url=url,
            profile=profile,
            language=language,
            n=int(count),
        )
        return ok([{"keyword": item} for item in rows], limit=50)

    return await _guarded(_work, account=str(account))


async def cluster_keywords(
    account: str, keywords: list[str], language: str = "ru"
) -> dict[str, Any]:
    """Cluster keyword phrases by intent; advisory only."""

    async def _work() -> dict[str, Any]:
        clusters = await _cluster_keywords(keywords, language)
        return ok(
            [
                {
                    "name": item.name,
                    "intent": item.intent,
                    "keywords": list(item.keywords),
                    "priority": item.priority,
                }
                for item in clusters
            ],
            limit=100,
        )

    return await _guarded(_work, account=str(account))


async def filter_keyword_relevance(
    account: str,
    topic: str,
    keywords: list[str],
    language: str = "ru",
) -> dict[str, Any]:
    """Classify keyword relevance using the PII-free client profile."""

    async def _work() -> dict[str, Any]:
        profile = await ClientProfileStore().profile_context_text(str(account))
        verdict = await filter_relevance(
            texts=keywords,
            topic=topic,
            profile=profile,
            language=language,
        )
        return ok(
            [{"keyword": keyword, "relevant": relevant} for keyword, relevant in verdict.items()],
            limit=500,
        )

    return await _guarded(_work, account=str(account))


async def suggest_negatives(
    account: str,
    topic: str,
    keywords: list[str],
    language: str = "ru",
    limit: int = 20,
) -> dict[str, Any]:
    """Suggest negatives with deterministic protection of client brand/service tokens."""

    async def _work() -> dict[str, Any]:
        if not 1 <= int(limit) <= 100:
            raise ValueError("limit must be between 1 and 100")
        store = ClientProfileStore()
        profile, protected = await asyncio.gather(
            store.profile_context_text(str(account)),
            store.protected_negative_terms(str(account)),
        )
        rows = await suggest_negative_keywords(
            topic,
            keywords,
            language=language,
            limit=int(limit),
            profile=profile,
            protected=protected,
        )
        return ok([{"negative": item} for item in rows], limit=100)

    return await _guarded(_work, account=str(account))


async def parse_keywords_input(
    account: str, text: str, default_match_type: str | None = None
) -> dict[str, Any]:
    """Parse pasted keyword text and explicit match-type markers without touching Google Ads."""

    async def _work() -> dict[str, Any]:
        rows = parse_keywords_text(text, default_match_type=default_match_type)
        return ok(
            [{"keyword": item.text, "match_type": item.match_type} for item in rows], limit=1000
        )

    return await _guarded(_work, account=str(account))


async def generate_rsa(
    account: str,
    topic: str,
    keywords: list[str] | None = None,
    usp: str | None = None,
    tone: str | None = None,
    geo: str | None = None,
    language: str = "ru",
    headlines: int = 15,
    descriptions: int = 4,
) -> dict[str, Any]:
    """Generate an RSA set; code enforces every 30/90 character limit."""

    async def _work() -> dict[str, Any]:
        if not 3 <= int(headlines) <= 15 or not 2 <= int(descriptions) <= 4:
            raise ValueError("RSA requires 3..15 headlines and 2..4 descriptions")
        profile = await ClientProfileStore().profile_context_text(str(account))
        draft = await _generate_rsa(
            CopyBrief(
                topic=topic,
                keywords=list(keywords or []),
                usp=usp,
                profile=profile,
                tone=tone,
                geo=geo,
                language=language,
                n_headlines=int(headlines),
                n_descriptions=int(descriptions),
            )
        )
        rows = [
            {
                "kind": "headline",
                "index": idx,
                "text": text,
                "length": validate(text, "headline")[1],
            }
            for idx, text in enumerate(draft.headlines, 1)
        ] + [
            {
                "kind": "description",
                "index": idx,
                "text": text,
                "length": validate(text, "description")[1],
            }
            for idx, text in enumerate(draft.descriptions, 1)
        ]
        return ok(
            rows,
            limit=25,
            extra={
                "headline_count": len(draft.headlines),
                "description_count": len(draft.descriptions),
                "keyword_coverage": draft.keyword_coverage,
                "coverage_repaired": draft.coverage_repaired,
                "attempts": draft.attempts,
            },
        )

    return await _guarded(_work, account=str(account))


async def validate_adcopy(
    account: str,
    headlines: list[str],
    descriptions: list[str],
    keywords: list[str] | None = None,
) -> dict[str, Any]:
    """Validate RSA lengths, duplicates, policy heuristics, CTA and keyword coverage in code."""

    async def _work() -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for kind, values in (("headline", headlines), ("description", descriptions)):
            for idx, text in enumerate(values, 1):
                valid, length = validate(text, kind)
                rows.append(
                    {
                        "kind": kind,
                        "index": idx,
                        "text": text,
                        "length": length,
                        "valid": valid,
                        "moderation_issues": moderation_issues(text),
                    }
                )
        return ok(
            rows,
            limit=25,
            extra={
                "valid": all(row["valid"] for row in rows)
                and not find_duplicates(headlines)
                and not find_duplicates(descriptions),
                "headline_duplicates": find_duplicates(headlines),
                "description_duplicates": find_duplicates(descriptions),
                "has_cta": any_cta([*headlines, *descriptions]),
                "keyword_coverage": keyword_coverage(headlines, list(keywords or [])),
            },
        )

    return await _guarded(_work, account=str(account))


async def build_display_path(
    account: str,
    url: str | None = None,
    keywords: list[str] | None = None,
    campaign_name: str | None = None,
    geo_locations: list[str] | None = None,
) -> dict[str, Any]:
    """Build two display-path segments and count their lengths in code."""

    async def _work() -> dict[str, Any]:
        path1, path2 = _build_display_path(
            url,
            keywords,
            {"campaign_name": campaign_name, "geo_locations": geo_locations or []},
        )
        return ok(
            [
                {"segment": "path1", "text": path1, "length": validate(path1, "path")[1]},
                {"segment": "path2", "text": path2, "length": validate(path2, "path")[1]},
            ]
        )

    return await _guarded(_work, account=str(account))


async def get_client_card(account: str) -> dict[str, Any]:
    """Return the full operator-facing client card, including stored contacts."""

    async def _work() -> dict[str, Any]:
        profile = await ClientProfileStore().get_by_account(str(account))
        return ok([profile] if profile else [], limit=1, extra={"has_profile": bool(profile)})

    return await _guarded(_work, account=str(account))


async def list_client_facts_structured(account: str) -> dict[str, Any]:
    """Return structured services/prices/categories and contacts for deterministic asset building."""

    async def _work() -> dict[str, Any]:
        profile = await ClientProfileStore().get_by_account(str(account)) or {}
        rows = [{"kind": "service", **item} for item in profile.get("services") or []] + [
            {"kind": "contact", **item} for item in profile.get("contacts") or []
        ]
        return ok(rows, limit=200)

    return await _guarded(_work, account=str(account))


async def list_site_pages(account: str, limit: int = 20) -> dict[str, Any]:
    """List stored crawl pages suitable for sitelink planning."""

    async def _work() -> dict[str, Any]:
        if not 1 <= int(limit) <= 100:
            raise ValueError("limit must be between 1 and 100")
        rows = await ClientProfileStore().top_site_pages(str(account), limit=int(limit))
        return ok(rows, limit=int(limit))

    return await _guarded(_work, account=str(account))


async def get_crawl_status(account: str, job_id: str) -> dict[str, Any]:
    """Read one crawl job status. Account lock is applied before the DB lookup."""

    async def _work() -> dict[str, Any]:
        status = await crawl_jobs.get_status(str(job_id), customer_id=str(account))
        return ok([{"job_id": str(job_id), "status": status}] if status else [], limit=1)

    return await _guarded(_work, account=str(account))


WORKFLOW_READ_TOOL_FUNCS: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    "build_report": build_report,
    "seed_keywords": seed_keywords,
    "cluster_keywords": cluster_keywords,
    "filter_keyword_relevance": filter_keyword_relevance,
    "suggest_negatives": suggest_negatives,
    "parse_keywords_input": parse_keywords_input,
    "generate_rsa": generate_rsa,
    "validate_adcopy": validate_adcopy,
    "build_display_path": build_display_path,
    "get_client_card": get_client_card,
    "list_client_facts_structured": list_client_facts_structured,
    "list_site_pages": list_site_pages,
    "get_crawl_status": get_crawl_status,
}
