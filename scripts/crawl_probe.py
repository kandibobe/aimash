"""Read-only проба краула по живому сайту: сколько страниц, за сколько, что отдал сервер.

Ничего не пишет — ни в БД, ни в LLM (профиль не трогается). Нужен, чтобы приёмку §20.4 можно было
повторить одной командой, а не «в боте вроде выглядело нормально».

    python -m scripts.crawl_probe darial.co.jp [--pages 1000] [--budget 240] [--concurrency 6]

Печатает: robots (разрешено/Crawl-delay/карты), счётчики фетча (ok / 404 / таймауты), время,
причину остановки, top-15 страниц по порядку обхода и первые 400 символов /about — то самое место,
где живут «6000+ авто / 100+ стран», которых не хватало в профиле клиента.
"""

from __future__ import annotations

import argparse
import asyncio
import time

from scripts._win_console import enable_utf8

enable_utf8()

from clients import crawler  # noqa: E402
from core.config import settings  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("site")
    ap.add_argument("--pages", type=int, default=settings.crawl_max_pages)
    ap.add_argument("--budget", type=float, default=settings.crawl_time_budget_s)
    ap.add_argument("--concurrency", type=int, default=settings.crawl_concurrency)
    ap.add_argument("--depth", type=int, default=settings.crawl_max_depth)
    a = ap.parse_args()

    url = a.site if a.site.startswith(("http://", "https://")) else f"https://{a.site}/"
    t0 = time.monotonic()

    can_fetch, delay, sitemaps = await crawler.load_robots(url)
    print(
        f"robots: главная разрешена={can_fetch(url)} crawl_delay={delay} "
        f"карт в robots={len(sitemaps)}"
    )
    sitemap = await crawler.fetch_sitemap(url, extra_urls=sitemaps)
    locs = sitemap.count("<loc>") if sitemap else 0
    print(f"sitemap: {locs} <loc>" if sitemap else "sitemap: нет")

    async with crawler.SiteFetcher(
        concurrency=a.concurrency, delay_s=max(settings.crawl_delay_s, delay or 0.0)
    ) as f:
        res = await crawler.crawl_site(
            url,
            fetcher=f.fetch,
            can_fetch=can_fetch,
            sitemap_xml=sitemap,
            max_pages=a.pages,
            max_depth=a.depth,
            delay_s=0.0,
            max_text_chars=settings.crawl_max_text_chars,
            time_budget_s=a.budget,
            concurrency=a.concurrency,
            stats=f.stats,
        )

    dt = time.monotonic() - t0
    print(f"\nстраниц: {res.pages_count} за {dt:.1f} c ({dt / max(1, res.pages_count):.2f} c/стр)")
    print(f"фетч:    {res.stats.summary()}")
    print(f"стоп:    {res.stopped or '—'} (partial={res.partial})")
    print(f"контакты: телефоны={res.phones} e-mail={res.emails} соцсети={list(res.socials)}")
    print("\nпорядок обхода (первые 15):")
    for p in res.pages[:15]:
        print(f"  [{p.page_type:8}] {len(p.text):5} симв  {p.url}")
    account = [
        p.url for p in res.pages if any(s in p.url for s in ("/login", "/dashboard", "/cart"))
    ]
    print(f"\nстраниц личного кабинета в обходе: {len(account)} (ожидание: 0)")
    about = next((p for p in res.pages if p.page_type == "about"), None)
    if about:
        print(f"\n{about.url} — первые 400 символов очищенного текста:\n{about.text[:400]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
