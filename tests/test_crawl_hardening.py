"""§20.4 (доводка краула): фронтир, robots fail-closed, sitemap-index, шаблон, диагностика.

Что тут закреплено (каждый пункт — реальный баг живого прогона по darial.co.jp):
- бюджет страниц съедал личный кабинет (/login, /dashboard, /password-reset) — deny-list;
- /about (там ровно те факты, которых не хватало в профиле) обходился ПОСЛЕ блога — приоритет;
- сбой сети на robots.txt означал «разрешено всё» — fail-open, нарушение правила 10;
- sitemap-индекс из 8 карт раскрывался на 5 — три терялись молча;
- половина текста каждой страницы была меню/подвалом — вычитание шаблона;
- пользователю показывалось «Краулинг не удался: ?» — str(TimeoutError()) пуст.
"""

from __future__ import annotations

import gzip
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clients.crawler as C  # noqa: E402
from clients import boilerplate  # noqa: E402
from clients.crawl_fetch import (  # noqa: E402
    BREAKER_THRESHOLD,
    CircuitOpen,
    FetchStats,
    SiteFetcher,
    _decode_html,
)
from clients.crawl_frontier import (  # noqa: E402
    PRIO_LOW,
    Frontier,
    dedup_key,
    is_denied,
    normalize,
    priority,
)


# ── фронтир: deny-list и приоритет ────────────────────────────────────────────────
@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/login/",
        "https://x.com/logout/",
        "https://x.com/register/",
        "https://x.com/dashboard/",
        "https://x.com/password-reset/",
        "https://x.com/profile-settings/",
        "https://x.com/cart/",
        "https://x.com/wp-admin/options.php",
        "https://x.com/feed/",
        "https://x.com/?s=toyota",
        "https://x.com/catalog.pdf",
        # живой прогон: фасеты каталога и SSO-обвязка — тот же HTML под другим URL
        "https://x.com/?taxonomy=auto-tags&term=gibrid",
        "https://x.com/about-us/?oauthWindow=true&provider=google&um_sso=445",
    ],
)
def test_denied_urls(url):
    assert is_denied(url) is True


@pytest.mark.parametrize("url", ["https://x.com/", "https://x.com/about/", "https://x.com/lot/12"])
def test_allowed_urls(url):
    assert is_denied(url) is False


def test_priority_content_beats_blog():
    assert priority("https://x.com/about/") < priority("https://x.com/random-page/")
    assert priority("https://x.com/services/") < priority("https://x.com/blog/post-1/")
    assert priority("https://x.com/blog/post-1/") == PRIO_LOW


def test_priority_root_with_query_is_not_top():
    """Корень с query — фильтр каталога, а не главная: копии главной не должны выбивать /about/."""
    assert priority("https://x.com/") == 0
    assert priority("https://x.com/?page=2") == PRIO_LOW


def test_frontier_pops_by_priority_then_depth():
    f = Frontier(max_depth=3)
    f.push("https://x.com/blog/a/", 1)
    f.push("https://x.com/about/", 1)
    f.push("https://x.com/junk/", 1)
    assert (
        f.pop()[0] == "https://x.com/about/"
    )  # контентная страница первой, а не по порядку добавления


def test_frontier_dedups_and_respects_depth():
    f = Frontier(max_depth=1)
    f.push("https://x.com/a/", 1)
    f.push("https://x.com/a/#anchor", 1)  # тот же URL
    f.push("https://x.com/deep/", 2)  # глубже max_depth
    assert len(f) == 1
    assert f.pop()[0] == "https://x.com/a/"
    assert f.pop() is None


def test_normalize_strips_tracking_but_keeps_slash():
    # хвостовой слэш НЕ режем: WordPress отвечает на /about 301-редиректом на /about/
    assert normalize("https://x.com/about/?utm_source=fb#top") == "https://x.com/about/"
    assert normalize("https://x.com/p/?id=7&gclid=abc") == "https://x.com/p/?id=7"
    assert dedup_key("https://www.x.com/about/") == dedup_key("https://x.com/about")


def test_frontier_counts_denied():
    f = Frontier(max_depth=3)
    f.push("https://x.com/login/", 1)
    assert len(f) == 0 and f.denied == 1


# ── crawl_site: фронтир применяется на живом обходе ───────────────────────────────
def _fetcher(graph: dict[str, str]):
    async def _f(url: str) -> str:
        if url not in graph:
            raise RuntimeError("404")
        return graph[url]

    return _f


async def test_crawl_skips_account_area_and_prefers_about():
    home = (
        '<a href="/login/">Вход</a><a href="/dashboard/">Кабинет</a>'
        '<a href="/blog/p1/">Блог</a><a href="/about/">О нас</a>'
    )
    graph = {
        "https://x.com/": home,
        "https://x.com/about/": "<h1>О компании</h1>" + "факты про компанию. " * 30,
        "https://x.com/blog/p1/": "<h1>Пост</h1>" + "текст поста. " * 30,
        "https://x.com/login/": "<h1>Вход</h1>",
        "https://x.com/dashboard/": "<h1>Кабинет</h1>",
    }
    seen: list[str] = []

    async def spy(url: str) -> str:
        seen.append(url)
        return await _fetcher(graph)(url)

    res = await C.crawl_site("https://x.com/", fetcher=spy, max_pages=10, strip_boilerplate=False)
    urls = [p.url for p in res.pages]
    assert "https://x.com/login/" not in seen and "https://x.com/dashboard/" not in seen
    assert urls.index("https://x.com/about/") < urls.index("https://x.com/blog/p1/")


async def test_crawl_stops_on_circuit_open():
    async def dead(url: str) -> str:
        if url == "https://x.com/":
            return '<a href="/a/">a</a><a href="/b/">b</a>'
        raise CircuitOpen("сайт лёг")

    res = await C.crawl_site("https://x.com/", fetcher=dead, max_pages=10)
    assert res.stopped == "circuit" and res.partial is True
    assert res.pages_count == 1  # собранное не выброшено


async def test_crawl_counts_blocked_by_robots():
    graph = {"https://x.com/": '<a href="/secret/">s</a>', "https://x.com/secret/": "<h1>s</h1>"}
    stats = FetchStats()
    res = await C.crawl_site(
        "https://x.com/",
        fetcher=_fetcher(graph),
        can_fetch=lambda u: "/secret/" not in u,
        stats=stats,
    )
    assert res.pages_count == 1 and stats.blocked == 1


# ── robots: fail-closed ───────────────────────────────────────────────────────────
def _http_error(code: int):
    import httpx

    req = httpx.Request("GET", "https://x.com/robots.txt")
    return httpx.HTTPStatusError("err", request=req, response=httpx.Response(code, request=req))


async def test_robots_404_allows_everything(monkeypatch):
    async def _raise(url, **kw):
        raise _http_error(404)

    monkeypatch.setattr(C, "fetch_url_html", _raise)
    can_fetch, delay, sitemaps = await C.load_robots("https://x.com/")
    assert can_fetch("https://x.com/any") is True and delay is None and sitemaps == []


async def test_robots_403_denies_everything(monkeypatch):
    async def _raise(url, **kw):
        raise _http_error(403)

    monkeypatch.setattr(C, "fetch_url_html", _raise)
    can_fetch, _, _ = await C.load_robots("https://x.com/")
    assert can_fetch("https://x.com/any") is False  # RFC 9309: закрытый robots.txt = запрет


async def test_robots_5xx_fails_closed(monkeypatch):
    """Правило 10: сбой сети/5xx НЕ означает «разрешено всё» — обход не начинается вовсе."""
    import httpx

    async def _raise(url, **kw):
        raise _http_error(503)

    monkeypatch.setattr(C, "fetch_url_html", _raise)
    with pytest.raises(httpx.HTTPStatusError):
        await C.load_robots("https://x.com/")

    async def _boom(url, **kw):
        raise ConnectionError("сеть лежит")

    monkeypatch.setattr(C, "fetch_url_html", _boom)
    with pytest.raises(ConnectionError):
        await C.load_robots("https://x.com/")


async def test_robots_parses_delay_and_sitemaps(monkeypatch):
    txt = (
        "User-agent: *\nDisallow: /wp-admin/\nCrawl-delay: 2\n"
        "Sitemap: https://x.com/wp-sitemap.xml\nSitemap: https://x.com/news.xml\n"
    )

    async def _txt(url, **kw):
        return txt

    monkeypatch.setattr(C, "fetch_url_html", _txt)
    can_fetch, delay, sitemaps = await C.load_robots("https://x.com/")
    assert delay == 2.0
    assert sitemaps == ["https://x.com/wp-sitemap.xml", "https://x.com/news.xml"]
    assert can_fetch("https://x.com/wp-admin/x") is False
    assert can_fetch("https://x.com/about/") is True


# ── sitemap: индекс раскрывается ВЕСЬ, robots-карты идут первыми ──────────────────
async def test_fetch_sitemap_expands_all_children(monkeypatch):
    children = [f"https://x.com/sm-{i}.xml" for i in range(8)]
    index = "<sitemapindex>" + "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in children)
    index += "</sitemapindex>"
    served: list[str] = []

    async def _fetch(url, **kw):
        served.append(url)
        if url == "https://x.com/sitemap.xml":
            return index
        if url in children:
            return f"<urlset><url><loc>https://x.com/p{children.index(url)}/</loc></url></urlset>"
        raise RuntimeError("404")

    monkeypatch.setattr(C, "fetch_url_html", _fetch)
    xml = await C.fetch_sitemap("https://x.com/")
    assert xml is not None
    for i in range(8):  # раньше брались только 5 из 8 — три карты терялись молча
        assert f"https://x.com/p{i}/" in xml


async def test_fetch_sitemap_uses_robots_declared_url_first(monkeypatch):
    served: list[str] = []

    async def _fetch(url, **kw):
        served.append(url)
        if url == "https://x.com/wp-sitemap.xml":
            return "<urlset><url><loc>https://x.com/a/</loc></url></urlset>"
        raise RuntimeError("404")

    monkeypatch.setattr(C, "fetch_url_html", _fetch)
    xml = await C.fetch_sitemap("https://x.com/", extra_urls=["https://x.com/wp-sitemap.xml"])
    assert xml and "https://x.com/a/" in xml
    assert served[0] == "https://x.com/wp-sitemap.xml"  # карта из robots — первый кандидат


def test_decode_gunzips_sitemap():
    raw = gzip.compress("<urlset><url><loc>https://x.com/a/</loc></url></urlset>".encode())
    assert "https://x.com/a/" in _decode_html(raw, None)  # .xml.gz — это ФАЙЛ, httpx его не жмёт


# ── шаблон (меню/подвал) ──────────────────────────────────────────────────────────
def test_boilerplate_subtracts_menu_but_keeps_content():
    menu = "Главная\nО нас\nУслуги\nКонтакты\n"
    pages = [menu + f"Уникальный текст страницы {i}. " * 20 for i in range(6)]
    out = boilerplate.subtract(pages)
    assert all("Главная" not in t for t in out)
    assert all(f"Уникальный текст страницы {i}." in out[i] for i in range(6))


def test_boilerplate_failsafe_keeps_short_page():
    """Если после вычитания от страницы почти ничего не осталось — отдаём оригинал (иначе краул
    «обходит» сайт и приносит пустоту)."""
    menu = "Главная\nО нас\nУслуги\nКонтакты\n"
    pages = [menu + f"Длинный уникальный текст {i}. " * 30 for i in range(5)]
    pages.append(menu + "Коротко.")  # контента почти нет — вычитание оставит < MIN_KEEP_CHARS
    out = boilerplate.subtract(pages)
    assert "Главная" in out[-1] and "Коротко." in out[-1]


def test_boilerplate_noop_on_small_corpus():
    pages = ["Меню\nТекст один", "Меню\nТекст два"]  # < MIN_PAGES — частота ничего не значит
    assert boilerplate.subtract(pages) == pages


def test_html_to_text_drops_chrome():
    from core.ingest import _html_to_text

    html = (
        "<nav>Главная Услуги Контакты</nav><header>Шапка</header>"
        "<main><h1>О компании</h1><p>Мы экспортируем авто с 2016 года.</p></main>"
        "<aside>Виджет</aside><footer>© 2026 Все права</footer>"
    )
    txt = _html_to_text(html)
    assert "экспортируем авто" in txt
    for junk in ("Главная Услуги", "Шапка", "Виджет", "Все права"):
        assert junk not in txt
    assert "Виджет" in _html_to_text(html, drop_chrome=False)  # прочие вызовы ingest не поехали


# ── контакты: tel:/mailto:/JSON-LD, а не «регекс поверх всего» ────────────────────
async def test_contacts_from_href_and_jsonld_not_from_reg_number():
    html = """
    <html><body>
      <p>Регистрационный номер T1140001100973, индекс 658-0044 1-7-12</p>
      <a href="tel:+81788556530">позвонить</a>
      <a href="mailto:info@darial.org">написать</a>
      <script type="application/ld+json">
        {"@type":"Organization","telephone":"+81 78-855-6531","email":"sales@darial.org"}
      </script>
    </body></html>
    """
    res = await C.crawl_site(
        "https://d.jp/", fetcher=_fetcher({"https://d.jp/": html}), strip_boilerplate=False
    )
    assert "info@darial.org" in res.emails and "sales@darial.org" in res.emails
    joined = " ".join(res.phones)
    assert "8155" not in joined  # ← ни рег.номер, ни индекс не попали в телефоны
    assert "1140001100973" not in joined and "658-0044" not in joined
    assert any("788556530" in p.replace(" ", "").replace("-", "") for p in res.phones)


async def test_phones_deduped_by_digits_across_pages():
    """Живой прогон: один номер приезжал в профиль тремя строками — `+81 78-855-6530`,
    `+81788556530`, `81788556530`. Ключ дедупа — только цифры, а не написание."""
    graph = {
        "https://d.jp/": '<a href="tel:+81 78-855-6530">звонить</a><a href="/contacts/">к</a>',
        "https://d.jp/contacts/": (
            '<a href="tel:81788556530">звонить</a><a href="tel:+81788556531">факс</a>'
        ),
    }
    res = await C.crawl_site("https://d.jp/", fetcher=_fetcher(graph), strip_boilerplate=False)
    assert {C._phone_key(p) for p in res.phones} == {"81788556530", "81788556531"}
    assert len(res.phones) == 2  # тел + факс, а не пять написаний двух номеров
    assert all(p.startswith("+") for p in res.phones)  # междунар. запись побеждает голые цифры


# ── предохранитель: 404 — битая ссылка, 5xx — лежащий сайт ────────────────────────
def _breaker_fetcher(code: int) -> SiteFetcher:
    """SiteFetcher, у которого каждый запрос отвечает `code`. Сеть не трогаем: `_get` подменён."""
    f = SiteFetcher(concurrency=1, delay_s=0.0)
    f.interval_s = 0.0  # тесту незачем выжидать вежливую паузу
    f._client = object()  # без него fetch() решит, что мы вне async-контекста

    async def _get(url: str, *, allow_xml: bool = False) -> str:
        raise _http_error(code)

    f._get = _get
    return f


async def test_breaker_ignores_404_but_opens_on_5xx():
    """Живой прогон darial: в sitemap 51 битая ссылка из 87. Четырнадцать 404 ПОДРЯД не значат,
    что сайт лёг, — раньше предохранитель срабатывал и обход обрывался на 17-й странице."""
    import httpx

    dead_links = _breaker_fetcher(404)
    for _ in range(BREAKER_THRESHOLD + 4):
        with pytest.raises(httpx.HTTPStatusError):
            await dead_links.fetch("https://x.com/p")
    assert dead_links.breaker_open is False
    assert dead_links.stats.by_status[404] == BREAKER_THRESHOLD + 4

    dead_site = _breaker_fetcher(503)
    for _ in range(BREAKER_THRESHOLD):
        with pytest.raises(httpx.HTTPStatusError):
            await dead_site.fetch("https://x.com/p")
    assert dead_site.breaker_open is True
    with pytest.raises(CircuitOpen):  # дальше не долбим чужой сервер
        await dead_site.fetch("https://x.com/p")


# ── диагностика: пользователю фраза, а не «?» ─────────────────────────────────────
def test_crawl_fail_reason_never_empty():
    import httpx

    import bot.main as bm

    cases = [
        TimeoutError(),  # str(e) == "" — ровно это давало пользователю «?»
        httpx.ConnectTimeout(""),
        httpx.ConnectError(""),
        CircuitOpen(""),
        httpx.TooManyRedirects(""),
        ValueError("адрес заблокирован (внутренний/небезопасный): 127.0.0.1"),
        RuntimeError(""),
    ]
    for e in cases:
        reason = bm._crawl_fail_reason(e)
        assert reason and reason.strip() not in ("?", "")
        assert type(e).__name__ not in reason  # имя класса наружу не светим (решение P1-аудита)


def test_crawl_fail_reason_http_codes():
    import httpx

    import bot.main as bm

    req = httpx.Request("GET", "https://x.com/")

    def _err(code):
        return httpx.HTTPStatusError("e", request=req, response=httpx.Response(code, request=req))

    assert bm._crawl_fail_reason(_err(403)) != bm._crawl_fail_reason(_err(404))
    assert "500" in bm._crawl_fail_reason(_err(500))


def test_fetch_stats_summary_counts_swallowed_failures():
    st = FetchStats(ok=36, by_status={404: 51}, by_error={"ReadTimeout": 2}, skipped_ctype=3)
    s = st.summary()
    assert st.failed == 53
    assert "ok=36" in s and "404×51" in s and "ReadTimeout×2" in s and "ctype=3" in s
