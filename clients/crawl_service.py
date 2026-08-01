"""Bot-free client-site crawl that prepares, but never applies, a profile patch."""

from __future__ import annotations

import asyncio
from typing import Any, Literal
from urllib.parse import urlparse

from clients import crawl_jobs, crawler
from clients.dossier import build_dossier, dossier_patch
from clients.dossier_render import render_llm_context, render_markdown
from clients.dossier_store import ClientDossierStore
from clients.profile_extract import structure_crawl
from clients.store import ClientProfileStore
from core.config import settings


def _profile_patch(extract: Any, result: Any) -> dict[str, Any]:
    patch = dict(extract) if isinstance(extract, dict) else extract.to_patch() if extract else {}
    # External content may enrich but can never wipe manager-owned categories.
    patch.pop("replace_services", None)
    patch.pop("replace_contacts", None)
    socials = {**(patch.get("socials") or {}), **dict(result.socials or {})}
    if socials:
        patch["socials"] = socials
    contacts = list(patch.get("contacts") or [])
    seen = {str(item.get("value") or "") for item in contacts}
    for kind, values in (("phone", result.phones[:5]), ("email", result.emails[:5])):
        for value in values:
            if value not in seen:
                contacts.append({"kind": kind, "value": value})
                seen.add(value)
    if contacts:
        patch["contacts"] = contacts
    return patch


async def prepare_profile_crawl(
    customer_id: str,
    chat_id: int,
    *,
    mode: Literal["full", "incremental"] = "full",
    language: str = "ru",
) -> dict[str, Any]:
    """Crawl only the URL already stored for this account and return proposal material."""
    store = ClientProfileStore()
    before = await store.get_by_account(customer_id)
    url = str((before or {}).get("website") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("client profile has no valid website; save the profile first")
    domain = urlparse(url).netloc or url
    job_id = await crawl_jobs.create_running(
        customer_id=customer_id,
        chat_id=chat_id,
        domain=domain,
        mode=mode,
    )
    try:
        can_fetch, robots_delay, robots_sitemaps = await crawler.load_robots(url)
        sitemap = await crawler.fetch_sitemap(url, extra_urls=robots_sitemaps)
        async with crawler.SiteFetcher(
            concurrency=settings.crawl_concurrency,
            delay_s=max(settings.crawl_delay_s, robots_delay or 0.0),
        ) as site_fetcher:
            result = await asyncio.wait_for(
                crawler.crawl_site(
                    url,
                    fetcher=site_fetcher.fetch,
                    can_fetch=can_fetch,
                    sitemap_xml=sitemap,
                    max_pages=settings.crawl_max_pages,
                    max_depth=settings.crawl_max_depth,
                    delay_s=0.0,
                    max_text_chars=settings.crawl_max_text_chars,
                    time_budget_s=settings.crawl_time_budget_s,
                    concurrency=settings.crawl_concurrency,
                    stats=site_fetcher.stats,
                ),
                timeout=settings.crawl_time_budget_s + 60.0,
            )
        if not result.pages:
            raise ValueError("crawl returned no usable pages")
        pages = result.site_pages_payload(limit=settings.crawl_store_max_pages)
        if mode == "incremental" and before is not None:
            previous = await store.site_page_hashes(customer_id)
            new_urls, changed_urls = result.diff_against(previous)
            if previous and not new_urls and not changed_urls:
                await crawl_jobs.mark_done(job_id, pages_crawled=result.pages_count)
                return {
                    "job_id": job_id,
                    "unchanged": True,
                    "pages": result.pages_count,
                    "domain": domain,
                    "memory_status": "unchanged",
                }

        # Build the durable, structured client memory from the complete page map. The compact
        # profile extractor remains a fallback for tiny sites or an empty dossier extraction.
        crawl_facts = _profile_patch(None, result)
        dossier = await build_dossier(
            pages=pages,
            domain=domain,
            website=url,
            contacts=list(crawl_facts.get("contacts") or []),
            socials=dict(crawl_facts.get("socials") or {}),
            chat_id=chat_id,
            language=language,
        )
        dossier_id: int | None = None
        dossier_counts: dict[str, int] | None = None
        if dossier is not None:
            patch = _profile_patch(dossier_patch(dossier), result)
            dossier_id = await ClientDossierStore().save_draft(
                customer_id,
                markdown=render_markdown(dossier),
                llm_context=render_llm_context(dossier),
                data=dossier.model_dump(mode="json"),
            )
            dossier_counts = dossier.counts()
        else:
            extract = await structure_crawl(
                pages_text=result.combined_text(
                    max_chars=settings.crawl_llm_text_chars,
                    per_page_chars=settings.crawl_llm_per_page_chars,
                ),
                website=url,
                language=language,
            )
            patch = _profile_patch(extract, result)
        await crawl_jobs.mark_done(job_id, pages_crawled=result.pages_count)
        return {
            "job_id": job_id,
            "unchanged": False,
            "pages": result.pages_count,
            "domain": domain,
            "partial": bool(result.partial),
            "before": before,
            "patch": patch,
            "dossier_id": dossier_id,
            "dossier_counts": dossier_counts,
            "memory_status": "pending_confirmation",
            "crawl_extra": {
                "website": url,
                "last_crawled_at_now": True,
                "site_pages": pages,
                "site_pages_merge": mode == "incremental",
            },
        }
    except Exception as exc:
        await crawl_jobs.mark_failed(job_id, error=f"{type(exc).__name__}: {exc}")
        raise
