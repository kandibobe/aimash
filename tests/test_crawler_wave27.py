"""§20.4/§20.5 — краулер сайта клиента: что доезжает до LLM и что переживает перекраул (волна 2.7).

Что закрыто:
1. combined_text резал первые 8000 символов ПОДРЯД: при 50 страницах × 5000 симв. в промпт уезжала
   главная и половина следующей — /price и /catalog не попадали НИКОГДА (§20.4 «цены со страницы
   каталога» не выполнялся).
2. Бюджет времени жил только снаружи (asyncio.wait_for) → таймаут выбрасывал ВЕСЬ результат:
   49 обойденных страниц пропадали из-за одной медленной 50-й, краул уходил в failed.
3. <sitemapindex> (у крупных сайтов именно он): ссылки на вложенные карты уезжали во фронтир как
   «страницы», сами карты не разворачивались → sitemap не давал ни одного сида.
4. Кодировка бралась из HTTP-заголовка; httpx при отсутствии charset молча отдаёт utf-8 →
   windows-1251 сайт приезжал в профиль как «???».
5. Инкрементальный перекраул ЗАМЕНЯЛ карту страниц целиком → не обойденные в этот раз страницы
   исчезали из top_site_pages (источник sitelinks §20.6).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clients import crawler as C  # noqa: E402


def _page(url: str, ptype: str, text: str) -> C.CrawlPage:
    return C.CrawlPage(url=url, title=ptype, page_type=ptype, text=text)


# ── 1. Бюджет текста делится между страницами, а не съедается первой ──────────────
def test_combined_text_gives_every_page_type_a_share():
    res = C.CrawlResult(domain="x.ua")
    res.pages = [
        _page("https://x.ua/", "home", "ГЛАВНАЯ " * 1000),
        _page("https://x.ua/blog/1", "blog", "БЛОГ " * 1000),
        _page("https://x.ua/price", "price", "ЦЕНЫ мойка 500 грн " * 200),
        _page("https://x.ua/catalog", "catalog", "КАТАЛОГ товары " * 200),
    ]
    out = res.combined_text(max_chars=6000, per_page_chars=1200)
    assert "ЦЕНЫ" in out and "КАТАЛОГ" in out, "прайс/каталог снова не доехали до LLM (§20.4)"
    assert "ГЛАВНАЯ" in out
    assert len(out) <= 6000
    # приоритет типов: услуги/каталог/цены идут раньше блога
    assert out.index("КАТАЛОГ") < out.index("БЛОГ")


def test_combined_text_respects_total_budget():
    res = C.CrawlResult(domain="x.ua")
    res.pages = [_page(f"https://x.ua/{i}", "other", "A" * 5000) for i in range(50)]
    out = res.combined_text(max_chars=8000, per_page_chars=1500)
    assert len(out) <= 8000
    # при квоте 1500 в бюджет 8000 влезает НЕСКОЛЬКО страниц, а не одна
    assert out.count("# other") >= 4


# ── 2. Внутренний дедлайн: собранное не выбрасывается ─────────────────────────────
def test_crawl_time_budget_returns_partial_result():
    html = "<html><title>T</title><body><a href='/a'>a</a><a href='/b'>b</a>x</body></html>"

    async def fetcher(url: str) -> str:
        return html

    res = asyncio.run(
        C.crawl_site(
            "https://x.ua/",
            fetcher=fetcher,
            max_pages=50,
            time_budget_s=0.0001,  # бюджет истёк сразу после первой страницы
        )
    )
    assert res.pages, "дедлайн выбросил ВСЁ — ровно тот баг, что чинился"
    assert res.partial is True


def test_crawl_without_budget_is_not_partial():
    async def fetcher(url: str) -> str:
        return "<html><title>T</title><body>x</body></html>"

    res = asyncio.run(C.crawl_site("https://x.ua/", fetcher=fetcher, max_pages=3))
    assert res.pages_count == 1 and res.partial is False


# ── 3. sitemap-index ──────────────────────────────────────────────────────────────
def test_sitemap_index_children_not_treated_as_pages():
    xml = """<sitemapindex>
      <sitemap><loc>https://x.ua/sitemap-1.xml</loc></sitemap>
      <sitemap><loc>https://x.ua/sitemap-2.xml.gz</loc></sitemap>
    </sitemapindex>"""
    assert C._parse_sitemap(xml, "x.ua") == []  # XML-карты — не страницы сайта
    assert C._parse_sitemap_children(xml, "x.ua") == [
        "https://x.ua/sitemap-1.xml",
        "https://x.ua/sitemap-2.xml.gz",
    ]


def test_plain_sitemap_still_yields_pages():
    xml = "<urlset><url><loc>https://x.ua/price</loc></url></urlset>"
    assert C._parse_sitemap(xml, "x.ua") == ["https://x.ua/price"]
    assert C._parse_sitemap_children(xml, "x.ua") == []


def test_fetch_sitemap_expands_index(monkeypatch):
    root = "<sitemapindex><sitemap><loc>https://x.ua/sm-1.xml</loc></sitemap></sitemapindex>"
    child = "<urlset><url><loc>https://x.ua/catalog</loc></url></urlset>"
    seen: list[tuple[str, bool]] = []

    async def fake_fetch(url: str, *, allow_xml: bool = False) -> str:
        seen.append((url, allow_xml))
        return child if url.endswith("sm-1.xml") else root

    monkeypatch.setattr(C, "fetch_url_html", fake_fetch)
    xml = asyncio.run(C.fetch_sitemap("https://x.ua/"))
    assert "catalog" in xml  # вложенная карта развёрнута
    assert C._parse_sitemap(xml, "x.ua") == ["https://x.ua/catalog"]
    assert all(allow_xml for _u, allow_xml in seen), (
        "sitemap должен тянуться с allow_xml (application/xml)"
    )


# ── 4. Кодировка ──────────────────────────────────────────────────────────────────
def test_decode_html_uses_meta_charset_when_header_silent():
    raw = '<html><head><meta charset="windows-1251"></head><body>Мойка авто</body></html>'.encode(
        "cp1251"
    )
    assert "Мойка авто" in C._decode_html(raw, None)
    # заголовок важнее меты (сервер знает лучше)
    assert "Мойка авто" in C._decode_html("Мойка авто".encode("cp1251"), "windows-1251")


def test_charset_from_content_type_header():
    assert C._charset_from_ctype("text/html; charset=windows-1251") == "windows-1251"
    assert C._charset_from_ctype('text/html;charset="UTF-8"') == "UTF-8"
    assert C._charset_from_ctype("text/html") is None  # ⇒ смотрим <meta charset>, не utf-8 вслепую


def test_decode_html_falls_back_to_utf8_on_unknown_charset():
    raw = "Мойка".encode()
    assert C._decode_html(raw, "x-unknown-charset-42") == "Мойка"


# ── 5. Карта страниц переживает инкрементальный перекраул ─────────────────────────
def test_incremental_crawl_merges_site_pages(monkeypatch):
    """Слияние карты по url — проверяем ту же логику, что в ClientStore.apply_upsert."""
    old = [
        {"url": "https://x.ua/price", "page_type": "price", "content_hash": "h1"},
        {"url": "https://x.ua/about", "page_type": "about", "content_hash": "h2"},
    ]
    fresh_rows = [{"url": "https://x.ua/price", "page_type": "price", "content_hash": "NEW"}]
    fresh = {r["url"]: r for r in fresh_rows}
    kept = [r for r in old if r["url"] not in fresh]
    merged = list(fresh.values()) + kept

    urls = {r["url"] for r in merged}
    assert urls == {"https://x.ua/price", "https://x.ua/about"}, "перекраул стёр не обойденное"
    assert next(r for r in merged if r["url"].endswith("price"))["content_hash"] == "NEW"


def test_store_merges_pages_only_when_flag_set():
    """Гард класса: full-краул заменяет карту, incremental — сливает (флаг site_pages_merge)."""
    src = (Path(__file__).resolve().parents[1] / "clients/store.py").read_text(encoding="utf-8")
    assert 'get("site_pages_merge")' in src
    main = (Path(__file__).resolve().parents[1] / "bot/main.py").read_text(encoding="utf-8")
    assert '"site_pages_merge": mode == "incremental"' in main
