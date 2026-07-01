"""§20.4 (Фаза D): статический краулер clients.crawler.crawl_site с фейковым фетчером (без сети).

Проверяем: BFS в пределах домена, внешние ссылки не обходятся (но соцсети фиксируются), кап
глубины и числа страниц, robots-запрет (can_fetch), сидинг из sitemap, извлечение телефонов.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clients.crawler import crawl_site  # noqa: E402


def _fetcher(graph: dict):
    async def _f(url: str) -> str:
        if url in graph:
            return graph[url]
        raise RuntimeError("404")

    return _f


@pytest.mark.asyncio
async def test_bfs_same_domain_socials_and_phones():
    graph = {
        "https://ex.com/": (
            "<title>Home</title>"
            '<a href="/about">About</a><a href="/services">Services</a>'
            '<a href="https://instagram.com/kasi">IG</a>'
            '<a href="https://other.com/x">ext</a>'
            "<p>Звоните: +254 712 345 678</p>"
        ),
        "https://ex.com/about": "<title>About</title>",
        "https://ex.com/services": "<title>Services</title>",
    }
    result = await crawl_site("https://ex.com/", fetcher=_fetcher(graph), max_pages=50, max_depth=3)
    urls = {p.url for p in result.pages}
    assert urls == {"https://ex.com/", "https://ex.com/about", "https://ex.com/services"}
    assert not any("other.com" in u for u in urls)  # внешний домен не обойдён
    assert result.socials.get("instagram") == "https://instagram.com/kasi"
    assert any("+254" in ph for ph in result.phones)
    home = next(p for p in result.pages if p.url == "https://ex.com/")
    assert home.page_type == "home"


@pytest.mark.asyncio
async def test_depth_cap():
    graph = {
        "https://d.com/": '<a href="/a">a</a>',
        "https://d.com/a": '<a href="/b">b</a>',
        "https://d.com/b": '<a href="/c">c</a>',
        "https://d.com/c": "end",
    }
    result = await crawl_site("https://d.com/", fetcher=_fetcher(graph), max_depth=1)
    urls = {p.url for p in result.pages}
    assert urls == {"https://d.com/", "https://d.com/a"}  # b (depth 2) не обойдён


@pytest.mark.asyncio
async def test_page_cap():
    graph = {"https://p.com/": '<a href="/1">1</a><a href="/2">2</a><a href="/3">3</a>'}
    for i in (1, 2, 3):
        graph[f"https://p.com/{i}"] = f"page {i}"
    result = await crawl_site("https://p.com/", fetcher=_fetcher(graph), max_pages=2)
    assert result.pages_count == 2


@pytest.mark.asyncio
async def test_robots_disallow_respected():
    graph = {
        "https://r.com/": '<a href="/admin">admin</a><a href="/ok">ok</a>',
        "https://r.com/ok": "ok",
        "https://r.com/admin": "secret",
    }
    result = await crawl_site(
        "https://r.com/", fetcher=_fetcher(graph), can_fetch=lambda u: "/admin" not in u
    )
    urls = {p.url for p in result.pages}
    assert "https://r.com/admin" not in urls
    assert "https://r.com/ok" in urls


@pytest.mark.asyncio
async def test_sitemap_seeds_frontier():
    graph = {"https://s.com/": "home only, no links", "https://s.com/deep": "<title>Deep</title>"}
    sitemap = "<urlset><url><loc>https://s.com/deep</loc></url></urlset>"
    result = await crawl_site("https://s.com/", fetcher=_fetcher(graph), sitemap_xml=sitemap)
    urls = {p.url for p in result.pages}
    assert "https://s.com/deep" in urls  # засеяно из sitemap, хотя ссылки с главной нет


@pytest.mark.asyncio
async def test_fetch_error_does_not_crash():
    graph = {
        "https://e.com/": '<a href="/broken">x</a><a href="/good">y</a>',
        "https://e.com/good": "ok",
    }
    result = await crawl_site("https://e.com/", fetcher=_fetcher(graph))
    urls = {p.url for p in result.pages}
    assert "https://e.com/good" in urls  # битая /broken пропущена, обход продолжился
    assert "https://e.com/broken" not in urls
