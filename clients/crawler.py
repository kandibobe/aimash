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
import time
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

# B13: типы контента, которые краулер парсит как текст (кроме них — всё text/*). Прочее
# (application/pdf, image/*, application/zip…) отвергаем ДО чтения тела — бинарь не в LLM-профиль.
_ALLOWED_CONTENT_TYPES = frozenset({"text/html", "text/plain", "application/xhtml+xml"})
# sitemap.xml сайты отдают как application/xml — для СТРАНИЦ эти типы по-прежнему запрещены.
_XML_CONTENT_TYPES = frozenset({"application/xml", "application/rss+xml", "application/gzip"})

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

# Порядок важности типов страниц для LLM-сведе́ния профиля (§20.4: «услуги, цены, контакты»).
# Раньше combined_text резал первые 8000 символов ПОДРЯД — это главная + половина второй страницы:
# /price и /catalog в промпт не попадали никогда, сколько бы страниц ни обошли.
_LLM_PAGE_PRIORITY = ("home", "services", "catalog", "price", "about", "contacts", "blog", "other")
_LLM_PRIO = {t: i for i, t in enumerate(_LLM_PAGE_PRIORITY)}


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
    partial: bool = False  # обход прерван бюджетом времени — страницы собраны НЕ все

    @property
    def pages_count(self) -> int:
        return len(self.pages)

    def combined_text(self, max_chars: int = 24000, per_page_chars: int = 1500) -> str:
        """Единый текст для LLM-сведе́ния: заголовки+текст страниц + соцсети.

        Бюджет делится МЕЖДУ страницами (квота per_page_chars на страницу, порядок — по важности
        типа: home → services → catalog → price → …), а не съедается первой же длинной страницей.
        Раньше был сплошной срез `[:8000]` при 50 обойденных страницах по 5000 символов — в промпт
        уезжали главная и половина следующей, а /price и /catalog не попадали НИКОГДА (§20.4
        «цены со страницы каталога» не выполнялся).

        PII-egress (golden rule #5 расширительно): телефоны/e-mail НЕ включаются — они уже
        извлечены детерминированно регексами краулера (self.phones/self.emails) и попадают в патч
        профиля КОДОМ (_crawl_patch_from_result), LLM для них не нужен. Соцсети — публичные хэндлы
        бренда, не PII. Residual: контакт может встретиться в самом тексте страницы — это
        задокументировано (docs/CLIENTS_KB.md, «Egress в LLM»)."""
        order = sorted(
            enumerate(self.pages),
            key=lambda it: (_LLM_PRIO.get(it[1].page_type, len(_LLM_PAGE_PRIORITY)), it[0]),
        )
        parts: list[str] = []
        budget = max_chars
        if self.socials:  # соцсети коротки и полезны — резервируем место под них
            budget -= 200
        for _i, p in order:
            if budget <= 0:
                break
            head = f"# {p.title or p.url} ({p.page_type})"
            body = (p.text or "")[: max(0, per_page_chars)]
            chunk = f"{head}\n{body}".strip()[:budget]
            if not chunk:
                continue
            parts.append(chunk)
            budget -= len(chunk) + 2  # + разделитель
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


def _is_sitemap_url(u: str) -> bool:
    """Ссылка на сам sitemap (в т.ч. вложенный из <sitemapindex>), а не на страницу сайта."""
    path = (urlparse(u).path or "").lower()
    return path.endswith((".xml", ".xml.gz", ".gz"))


def _parse_sitemap(xml: str, domain: str) -> list[str]:
    """URL СТРАНИЦ из sitemap.xml (<loc>) в пределах домена. Терпимо к битому XML (regex, не парсер).

    Ссылки на сами карты (sitemapindex → sitemap-1.xml) отсеиваются: раньше они уезжали во фронтир
    и тратили сиды на XML-файлы (fetch отвергал их по content-type — обход шёл вхолостую), а сама
    вложенная карта не разворачивалась, т.е. у sitemap-index-сайтов sitemap не работал вовсе."""
    out: list[str] = []
    for m in re.finditer(r"<loc>\s*([^<\s]+)\s*</loc>", xml or "", re.IGNORECASE):
        u = _norm(m.group(1))
        if _same_domain(u, domain) and not _is_sitemap_url(u) and u not in out:
            out.append(u)
    return out


def _parse_sitemap_children(xml: str, domain: str) -> list[str]:
    """<sitemapindex> → адреса вложенных карт (в пределах домена)."""
    out: list[str] = []
    for m in re.finditer(r"<loc>\s*([^<\s]+)\s*</loc>", xml or "", re.IGNORECASE):
        u = _norm(m.group(1))
        if _same_domain(u, domain) and _is_sitemap_url(u) and u not in out:
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
    time_budget_s: float | None = None,
) -> CrawlResult:
    """BFS-обход одного домена. fetcher(url)->HTML (бросает при отказе/блокировке); can_fetch(url)
    — robots-гейт (None ⇒ всё разрешено). sitemap_xml — опц. предзагруженный sitemap для сидов.
    Внешние ссылки НЕ обходятся (фиксируются как соцсети). Лимиты жёсткие (страницы/глубина/пауза).

    time_budget_s — ВНУТРЕННИЙ дедлайн: вышло время ⇒ выходим из BFS и отдаём собранное
    (`result.partial=True`). Снаружи обход по-прежнему прикрыт `asyncio.wait_for`, но тот при
    срабатывании выбрасывал ВЕСЬ результат — 49 честно обойденных страниц пропадали из-за одной
    медленной 50-й, и краул падал в `failed`."""
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

    deadline = (time.monotonic() + time_budget_s) if time_budget_s else None

    while queue and len(result.pages) < max_pages:
        # дедлайн проверяем ПОСЛЕ первой страницы: обход без единой страницы бесполезен
        if deadline is not None and result.pages and time.monotonic() >= deadline:
            result.partial = True
            break
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
_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.IGNORECASE
)


def _charset_from_ctype(content_type: str | None) -> str | None:
    """charset из заголовка Content-Type ('text/html; charset=windows-1251' → 'windows-1251')."""
    m = re.search(r"charset\s*=\s*\"?([A-Za-z0-9_\-]+)", content_type or "", re.IGNORECASE)
    return m.group(1) if m else None


def _decode_html(raw: bytes, header_charset: str | None) -> str:
    """Декодировать тело страницы. Кодировку берём: из HTTP-заголовка → из <meta charset> → utf-8.

    Раньше брали `r.encoding`, а httpx при ОТСУТСТВИИ charset в заголовке молча отдаёт
    default_encoding='utf-8' — сайт в windows-1251 (их немало) декодировался с errors='replace' и
    приезжал в профиль клиента как «???»."""
    enc = (header_charset or "").strip()
    if not enc:
        m = _META_CHARSET_RE.search(raw[:4096])
        if m:
            enc = m.group(1).decode("ascii", errors="ignore")
    for candidate in (enc, "utf-8"):
        if not candidate:
            continue
        try:
            return raw.decode(candidate, errors="replace")
        except (LookupError, TypeError):
            continue
    return raw.decode("utf-8", errors="replace")


async def fetch_url_html(url: str, *, allow_xml: bool = False) -> str:
    """Прочитать URL → сырой HTML (для разбора ссылок). SSRF-гард на исходный URL и КАЖДЫЙ редирект
    (event hook, как core.ingest.fetch_url_text); таймаут; потолок размера. Бросает при отказе.

    allow_xml — только для sitemap: сайты отдают карту как application/xml, и B13-гард по
    content-type (для страниц он остаётся) резал её целиком → sitemap-сиды не работали."""
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
            raw_ctype = r.headers.get("content-type") or ""
            # B13: не тянем и не декодируем НЕ-HTML (PDF/картинки/архивы) — иначе бинарь уходил в
            # LLM-сведение профиля как мусорный «текст». Content-Type проверяем ДО чтения тела.
            ctype = raw_ctype.split(";", 1)[0].strip().lower()
            allowed = _ALLOWED_CONTENT_TYPES | (_XML_CONTENT_TYPES if allow_xml else frozenset())
            if ctype and not (ctype in allowed or ctype.startswith("text/")):
                raise ValueError(f"неподдерживаемый тип контента: {ctype} ({url})")
            chunks: list[bytes] = []
            total = 0
            async for chunk in r.aiter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_FETCH_BYTES:
                    break
        raw = b"".join(chunks)
    # charset берём из самого заголовка (а не из httpx r.encoding: он при отсутствии charset молча
    # подставляет utf-8, и windows-1251 сайт превращался в «???»).
    return _decode_html(raw, _charset_from_ctype(raw_ctype))


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


async def fetch_sitemap(start_url: str, *, max_children: int = 5) -> str | None:
    """Опц. sitemap.xml домена (через SSRF-гард). Сбой/нет → None (обход всё равно пойдёт от главной).

    <sitemapindex> разворачивается: докачиваем до max_children вложенных карт и склеиваем — иначе у
    крупных сайтов (а индекс как раз у них) sitemap не давал НИ ОДНОГО сида страниц."""
    p = urlparse(start_url)
    domain = (p.netloc or "").lower()
    if domain.startswith("www."):
        domain = domain[4:]
    try:
        root = await fetch_url_html(f"{p.scheme}://{p.netloc}/sitemap.xml", allow_xml=True)
    except Exception:  # noqa: BLE001
        return None
    children = _parse_sitemap_children(root, domain)[:max_children]
    if not children:
        return root
    parts = [root]
    for child in children:
        try:
            parts.append(await fetch_url_html(child, allow_xml=True))
        except Exception:  # noqa: BLE001 — недоступная вложенная карта не валит остальные
            continue
    return "\n".join(parts)
