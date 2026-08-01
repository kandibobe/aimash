from __future__ import annotations

from types import SimpleNamespace

import pytest

from clients import crawl_service
from clients.dossier_schema import Company, Dossier, Fact, Service
from mcp_server import tools_workflow_state


@pytest.mark.asyncio
async def test_profile_crawl_builds_and_persists_dossier_draft(monkeypatch):
    pages = [
        {
            "url": "https://example.test/",
            "title": "Home",
            "page_type": "home",
            "text": "Acme exports industrial pumps worldwide. " * 30,
        }
    ]
    result = SimpleNamespace(
        pages=[object()],
        pages_count=1,
        partial=False,
        socials={"linkedin": "https://linkedin.com/company/acme"},
        phones=["+1 555 0100"],
        emails=["sales@example.test"],
        site_pages_payload=lambda limit: pages,
    )

    class FakeProfileStore:
        async def get_by_account(self, customer_id):
            return {"customer_id": customer_id, "website": "https://example.test"}

    class FakeFetcher:
        stats = SimpleNamespace()

        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def fetch(self, url):
            return ""

    saved: dict = {}

    class FakeDossierStore:
        async def save_draft(self, customer_id, **kwargs):
            saved.update(customer_id=customer_id, **kwargs)
            return 41

    async def fake_build_dossier(**kwargs):
        assert kwargs["pages"] == pages
        assert kwargs["contacts"] == [
            {"kind": "phone", "value": "+1 555 0100"},
            {"kind": "email", "value": "sales@example.test"},
        ]
        return Dossier(
            domain="example.test",
            website="https://example.test",
            company=Company(legal_name="Acme"),
            overview="Industrial pump exporter",
            services=[Service(name="Pump export")],
            facts=[Fact(claim="Ships to 40 countries")],
            usp=["24-hour quote"],
            pages_count=1,
            map_calls=1,
        )

    async def fail_compact_extract(**kwargs):
        raise AssertionError("compact extractor must be only a dossier fallback")

    async def crawl_site(*args, **kwargs):
        return result

    monkeypatch.setattr(crawl_service, "ClientProfileStore", FakeProfileStore)
    monkeypatch.setattr(crawl_service, "ClientDossierStore", FakeDossierStore)
    monkeypatch.setattr(crawl_service, "build_dossier", fake_build_dossier)
    monkeypatch.setattr(crawl_service, "structure_crawl", fail_compact_extract)
    monkeypatch.setattr(crawl_service.crawler, "SiteFetcher", FakeFetcher)
    monkeypatch.setattr(crawl_service.crawler, "crawl_site", crawl_site)
    monkeypatch.setattr(
        crawl_service.crawler, "load_robots", lambda url: _async((lambda _: True, None, []))
    )
    monkeypatch.setattr(crawl_service.crawler, "fetch_sitemap", lambda *a, **k: _async(None))
    monkeypatch.setattr(crawl_service.crawl_jobs, "create_running", lambda **k: _async("job-1"))
    monkeypatch.setattr(crawl_service.crawl_jobs, "mark_done", lambda *a, **k: _async(None))
    monkeypatch.setattr(crawl_service.crawl_jobs, "mark_failed", lambda *a, **k: _async(None))

    out = await crawl_service.prepare_profile_crawl("123", 77)

    assert out["dossier_id"] == 41
    assert out["memory_status"] == "pending_confirmation"
    assert out["patch"]["brand"] == "Acme"
    assert out["patch"]["services"][0]["name"] == "Pump export"
    assert "24-hour quote" in out["patch"]["notes"]
    assert saved["customer_id"] == "123"
    assert "24-hour quote" in saved["llm_context"]
    assert saved["data"]["company"]["legal_name"] == "Acme"


@pytest.mark.asyncio
async def test_start_client_crawl_links_dossier_to_memory_proposal(monkeypatch):
    prepared = {
        "job_id": "job-2",
        "unchanged": False,
        "pages": 3,
        "domain": "example.test",
        "partial": False,
        "before": {"website": "https://example.test"},
        "patch": {"business_desc": "Industrial pump exporter"},
        "dossier_id": 52,
        "dossier_counts": {"services": 2, "usp": 3},
        "memory_status": "pending_confirmation",
        "crawl_extra": {"website": "https://example.test", "site_pages": []},
    }
    captured: dict = {}

    async def fake_proposal(**kwargs):
        captured.update(kwargs)
        return {"status": "proposed"}

    monkeypatch.setattr(tools_workflow_state, "ensure_read_allowed", lambda account: None)
    monkeypatch.setattr(
        tools_workflow_state,
        "get_trusted_turn",
        lambda: SimpleNamespace(actor_chat_id=77),
    )
    monkeypatch.setattr(
        tools_workflow_state, "prepare_profile_crawl", lambda *a, **k: _async(prepared)
    )
    monkeypatch.setattr(tools_workflow_state, "_profile_proposal", fake_proposal)

    out = await tools_workflow_state.start_client_crawl("123")

    assert captured["params"]["dossier_id"] == 52
    assert out["crawl"]["dossier_built"] is True
    assert out["crawl"]["memory_status"] == "pending_confirmation"


async def _async(value):
    return value
