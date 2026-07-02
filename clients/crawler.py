"""§20.4: статический краулер сайта клиента (без headless). Чистая логика BFS, без БД и бота.

Переиспользует SSRF-гард и HTML→текст из core.ingest — НЕ ослабляя их. Обходит один домен от
главной (+ sitemap.xml), уважает robots.txt, с жёсткими лимитами (страницы/глубина/время/пауза),
чтобы не перегружать чужой сайт и не голодить общий event loop (bs4-разбор — в to_thread).

Сеть инкапсулирована в `fetcher` (async url→HTML) и `can_fetch` (robots) — их можно подменить в
тестах фейковым графом. Извлечение (title/текст/ссылки/контакты/соцсети) — КОДОМ; сведе́ние в
профиль (LLM) делает clients.profile_extract.structure_crawl отдельно.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse

from core.ingest import (
    FETCH_TIMEOUT_S,
    MAX_FETCH_BYTES,
    _UA,
    _html_to_text,
    _is_public_host,
)

_PHONE_RE = re.compile(r"\+?\d[\d\s()\-]{7,}\d")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_SOCIAL_HOSTS = {
    "instagram.com": "instagram",
    "facebook.com": "facebook",
    "fb.com": "facebook",
    "t.me": "telegram",
    "telegram.me": "telegram",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "tiktok.com": "tiktok",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "linkedin.com": "linkedin",
}
# Эвристика типа страницы по сегментам пути (для карты страниц / будущих sitelinks).
_PAGE_TYPE_HINTS = (
    ("contact", "contacts"),
    ("контакт", "contacts"),
    ("about", "about"),
    ("о-нас", "about"),
    ("o-nas", "about"),
    ("price", "price"),
    ("прайс", "price"),
    ("цен", "price"),
    ("service", "services"),
    ("услуг", "services"),
    ("catalog", "catalog"),
    ("catalogue", "catalog"),
    ("shop", "catalog"),
    ("product", "catalog"),
    ("товар", "catalog"),
    ("blog", "blog"),
    ("news", "blog"),
)

Fetcher = Callable[[str], Awaitable[str]]


def _content_hash(title: str, text: str) -> str:
    """Стабильная сигнатура контента страницы (§20.5 инкрементальный краул): меняется ⇒ страница
    изменилась. Считаем по тому, что СОХРАНЯЕМ (title + усечённый текст), чтобы сравнение при
    повторном крауле было apples-to-apples."""
    return hashlib.sha1(f"{title}\n{text}".encode()).hexdigest()[:16]


@dataclass
class CrawlPage:
    url: str
    title: str = ""
    page_type: str = "other"
    text: str = ""
    key_links: list[str] = field(default_factory=list)
    content_hash: str = ""


@dataclass
class CrawlResult:
    domain: str
    pages: list[CrawlPage] = field(default_factory=list)
    socials: dict[str, str] = field(default_factory=dict)
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)

    @property
    def pages_count(self) -> int:
        return len(self.pages)

    def combined_text(self, max_chars: int = 8000) -> str:
        """Единый текст для LLM-сведе́ния: заголовки+текст страниц + найденные контакты/соцсети."""
        parts: list[str] = []
        for p in self.pages:
            head = f"# {p.title or p.url} ({p.page_type})"
            parts.append(f"{head}\n{p.text}".strip())
        if self.phones:
            parts.append("Телефоны: " + ", ".join(self.phones[:10]))
        if self.emails:
            parts.append("E-mail: " + ", ".join(self.emails[:10]))
        if self.socials:
            parts.append("Соцсети: " + ", ".join(f"{k}: {v}" for k, v in self.socials.items()))
        return "\n\n".join(parts).strip()[:max_chars]

    def site_pages_payload(self, limit: int = 60) -> list[dict]:
        """Карта страниц для сохранения (client_site_pages)."""
        return [
            {
                "url": p.url,
                "title": p.title or None,
                "page_type": p.page_type,
                "key_links": p.key_links or None,
                "content_hash": p.content_hash or None,
            }
            for p in self.pages[:limit]
        ]

    def diff_against(self, prev_hashes: dict[str, str]) -> tuple[list[str], list[str]]:
        """§20.5: сравнить обойденные страницы с прошлыми хэшами (url→hash). Возвращает
        (new_urls, changed_urls). Неизменённые не попадают ни туда, ни туда."""
        new_urls: list[str] = []
        changed_urls: list[str] = []
        for p in self.pages:
            prev = prev_hashes.get(p.url)
            if prev is None:
                new_urls.append(p.url)
            elif p.content_hash and p.content_hash != prev:
                changed_urls.append(p.url)
        return new_urls, changed_urls


def _norm(url: str) -> str:
    """Отбросить фрагмент (#...) и хвостовой слэш-шум для дедупликации фронтира."""
    u, _frag = urldefrag((url or "").strip())
    return u


def _same_domain(url: str, domain: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return False
    host = host.split("@")[-1]  # убрать user:pass@
    d = domain.lower()
    return host == d or host == f"www.{d}" or f"www.{host}" == d


def _page_type(url: str, title: str) -> str:
    path = (urlparse(url).path or "/").lower()
    if path in ("", "/"):
        return "home"
    hay = f"{path} {title.lower()}"
    for needle, ptype in _PAGE_TYPE_HINTS:
        if needle in hay:
            return ptype
    return "other"


def _extract(html: str, base_url: str, domain: str) -> tuple:
    """bs4-разбор одной страницы (CPU-bound → вызывать в to_thread). Возвращает
    (title, text, same_domain_links, socials, phones, emails).

    §20.4 «Мета-данные»: meta description и заголовки H1–H3 — явный сигнал тематики страницы —
    вшиваются В НАЧАЛО text (до усечения crawl_max_text_chars), чтобы LLM-сведе́ние профиля видело
    их даже на длинных страницах (раньше брались только <title> + сырой текст)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.string if soup.title and soup.title.string else "").strip()
    links: list[str] = []
    socials: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        absu = _norm(urljoin(base_url, href))
        host = urlparse(absu).netloc.lower().split("@")[-1]
        bare = host[4:] if host.startswith("www.") else host
        if bare in _SOCIAL_HOSTS:
            socials.setdefault(_SOCIAL_HOSTS[bare], absu)
        elif _same_domain(absu, domain) and absu not in links:
            links.append(absu)
    # Мета-описание + H1–H3 (сигнал тематики) — компактным префиксом перед основным текстом.
    meta_bits: list[str] = []
    md = soup.find("meta", attrs={"name": "description"})
    if md and (md.get("content") or "").strip():
        meta_bits.append(f"Описание страницы: {md['content'].strip()[:300]}")
    heads = [
        h.get_text(" ", strip=True)
        for h in soup.find_all(["h1", "h2", "h3"], limit=12)
        if h.get_text(strip=True)
    ]
    if heads:
        meta_bits.append("Заголовки: " + " · ".join(h[:80] for h in heads))
    text = _html_to_text(html)
    if meta_bits:
        text = "\n".join(meta_bits) + "\n" + text
    phones = list(dict.fromkeys(m.group(0).strip() for m in _PHONE_RE.finditer(text)))
    emails = list(dict.fromkeys(m.group(0).strip() for m in _EMAIL_RE.finditer(text)))
    return title, text, links, socials, phones, emails


def _parse_sitemap(xml: str, domain: str) -> list[str]:
    """URL из sitemap.xml (<loc>) в пределах домена. Терпимо к битому XML (regex, не парсер)."""
    out: list[str] = []
    for m in re.finditer(r"<loc>\s*([^<\s]+)\s*</loc>", xml or "", re.IGNORECASE):
        u = _norm(m.group(1))
        if _same_domain(u, domain) and u not in out:
            out.append(u)
    return out


async def crawl_site(
    start_url: str,
    *,
    fetcher: Fetcher,
    can_fetch: Callable[[str], bool] | None = None,
    sitemap_xml: str | None = None,
    max_pages: int = 50,
    max_depth: int = 3,
    delay_s: float = 0.0,
    max_text_chars: int = 5000,
) -> CrawlResult:
    """BFS-обход одного домена. fetcher(url)->HTML (бросает при отказе/блокировке); can_fetch(url)
    — robots-гейт (None ⇒ всё разрешено). sitemap_xml — опц. предзагруженный sitemap для сидов.
    Внешние ссылки НЕ обходятся (фиксируются как соцсети). Лимиты жёсткие (страницы/глубина/пауза)."""
    p = urlparse(start_url)
    domain = (p.netloc or "").lower()
    if domain.startswith("www."):
        domain = domain[4:]
    result = CrawlResult(domain=domain)
    if not domain:
        return result

    start = _norm(start_url)
    seen: set[str] = {start}
    queue: list[tuple[str, int]] = [(start, 0)]
    for u in _parse_sitemap(sitemap_xml or "", domain):
        if u not in seen:
            seen.add(u)
            queue.append((u, 1))

    while queue and len(result.pages) < max_pages:
        url, depth = queue.pop(0)
        if can_fetch is not None and not can_fetch(url):
            continue
        try:
            html = await fetcher(url)
        except Exception:  # noqa: BLE001 — недоступная страница не роняет обход
            continue
        title, text, links, socials, phones, emails = await asyncio.to_thread(
            _extract, html, url, domain
        )
        page_text = text[:max_text_chars]
        result.pages.append(
            CrawlPage(
                url=url,
                title=title,
                page_type=_page_type(url, title),
                text=page_text,
                key_links=links[:20],
                content_hash=_content_hash(title, page_text),
            )
        )
        for k, v in socials.items():
            result.socials.setdefault(k, v)
        for ph in phones:
            if ph not in result.phones:
                result.phones.append(ph)
        for em in emails:
            if em not in result.emails:
                result.emails.append(em)
        if depth < max_depth:
            for ln in links:
                n = _norm(ln)
                if n not in seen and _same_domain(n, domain):
                    seen.add(n)
                    queue.append((n, depth + 1))
        if delay_s:
            await asyncio.sleep(delay_s)
    return result


# ── реальная сеть (переиспользует SSRF-гард core.ingest; в тестах подменяется fetcher'ом) ──
async def fetch_url_html(url: str) -> str:
    """Прочитать URL → сырой HTML (для разбора ссылок). SSRF-гард на исходный URL и КАЖДЫЙ редирект
    (event hook, как core.ingest.fetch_url_text); таймаут; потолок размера. Бросает при отказе."""
    import httpx

    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError("поддерживаются только http/https-ссылки")

    async def _guard(request: "httpx.Request") -> None:
        if not await asyncio.to_thread(_is_public_host, request.url.host):
            raise ValueError(f"адрес заблокирован (внутренний/небезопасный): {request.url.host}")

    async with httpx.AsyncClient(
        follow_redirects=True,
        max_redirects=3,
        timeout=FETCH_TIMEOUT_S,
        headers={"User-Agent": _UA},
        event_hooks={"request": [_guard]},
    ) as client:
        async with client.stream("GET", url) as r:
            r.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            async for chunk in r.aiter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_FETCH_BYTES:
                    break
            encoding = r.encoding or "utf-8"
        raw = b"".join(chunks)
    try:
        return raw.decode(encoding, errors="replace")
    except (LookupError, TypeError):
        return raw.decode("utf-8", errors="replace")


async def load_robots(start_url: str) -> Callable[[str], bool]:
    """Построить can_fetch по robots.txt домена (через SSRF-гард fetch_url_html). Сбой/нет файла →
    разрешаем всё (permissive) — как большинство краулеров при отсутствии robots."""
    from urllib.robotparser import RobotFileParser

    p = urlparse(start_url)
    robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
    rp = RobotFileParser()
    try:
        text = await fetch_url_html(robots_url)
        rp.parse(text.splitlines())
    except Exception:  # noqa: BLE001 — нет/битый robots → permissive
        return lambda _u: True
    return lambda u: rp.can_fetch(_UA, u)


async def fetch_sitemap(start_url: str) -> str | None:
    """Опц. sitemap.xml домена (через SSRF-гард). Сбой/нет → None (обход всё равно пойдёт от главной)."""
    p = urlparse(start_url)
    try:
        return await fetch_url_html(f"{p.scheme}://{p.netloc}/sitemap.xml")
    except Exception:  # noqa: BLE001
        return None
