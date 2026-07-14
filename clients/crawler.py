"""§20.4: статический краулер сайта клиента (без headless). Чистая логика обхода, без БД и бота.

Переиспользует SSRF-гард и HTML→текст из core.ingest — НЕ ослабляя их. Обходит один домен от
главной (+ sitemap.xml), уважает robots.txt, с жёсткими лимитами (страницы/глубина/время/пауза),
чтобы не перегружать чужой сайт и не голодить общий event loop (bs4-разбор — в to_thread).

Сеть инкапсулирована в `fetcher` (async url→HTML) и `can_fetch` (robots) — их можно подменить в
тестах фейковым графом. Извлечение (title/текст/ссылки/контакты/соцсети) — КОДОМ; сведе́ние в
профиль/досье (LLM) делает clients.profile_extract / clients.dossier_* отдельно.

Порядок обхода — clients.crawl_frontier (приоритет + deny-list), сеть — clients.crawl_fetch
(пул соединений, вежливая конкурентность, предохранитель), чистка шаблона — clients.boilerplate.
"""

from __future__ import annotations

import asyncio
import hashlib
import html as _html_mod
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import unquote, urljoin, urlparse

from clients import boilerplate
from clients.crawl_fetch import (  # noqa: F401 — публичный ре-экспорт (тесты/оркестратор)
    _ALLOWED_CONTENT_TYPES,
    _XML_CONTENT_TYPES,
    CircuitOpen,
    FetchStats,
    SiteFetcher,
    _charset_from_ctype,
    _decode_html,
    fetch_url_html,
)
from clients.crawl_frontier import PRIO_TOP, Frontier, normalize
from core.ingest import _UA, _html_to_text

# Телефон из СЫРОГО ТЕКСТА берём только в международном формате (+…): без этого регекс тянул мусор —
# на живом сайте в «телефоны» попали регистрационный номер компании (1140001100973) и почтовый индекс
# с адресом (658-0044 1-7-12). Надёжные источники — href="tel:" и JSON-LD (см. _extract_contacts).
_PHONE_RE = re.compile(r"\+\d[\d\s().\-]{6,17}\d")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_MIN_DIGITS = 8
_PHONE_MAX_DIGITS = 15  # E.164
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
    "wa.me": "whatsapp",
    "api.whatsapp.com": "whatsapp",
}
# Эвристика типа страницы по сегментам пути (для карты страниц / будущих sitelinks).
_PAGE_TYPE_HINTS = (
    ("contact", "contacts"),
    ("контакт", "contacts"),
    ("about", "about"),
    ("о-нас", "about"),
    ("o-nas", "about"),
    ("team", "about"),
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
    stats: FetchStats = field(
        default_factory=FetchStats
    )  # ok/404/таймауты — раньше молча глотались
    stopped: str = ""  # причина досрочной остановки: "" | "time" | "circuit" | "pages"

    @property
    def pages_count(self) -> int:
        return len(self.pages)

    def combined_text(self, max_chars: int = 24000, per_page_chars: int = 1500) -> str:
        """Единый текст для LLM-сведе́ния: заголовки+текст страниц + соцсети.

        Бюджет делится МЕЖДУ страницами (квота per_page_chars на страницу, порядок — по важности
        типа: home → services → catalog → price → …), а не съедается первой же длинной страницей.

        PII-egress (golden rule #5 расширительно): телефоны/e-mail НЕ включаются — они уже
        извлечены детерминированно кодом краулера (self.phones/self.emails) и попадают в патч
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

    def site_pages_payload(self, limit: int = 1000) -> list[dict]:
        """Карта страниц для сохранения (client_site_pages) — вместе с ТЕКСТОМ страницы.

        Текст нужен, чтобы пересобрать досье (clients.dossier_*) без повторного обхода сайта:
        раньше он выбрасывался сразу после LLM-вызова, и любая правка сведе́ния означала новый краул."""
        return [
            {
                "url": p.url,
                "title": p.title or None,
                "page_type": p.page_type,
                "key_links": p.key_links or None,
                "content_hash": p.content_hash or None,
                "text": p.text or None,
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
    """Отбросить фрагмент (#...) и трекинг-параметры. Хвостовой слэш НЕ режем — на WordPress это
    редирект 301 на КАЖДОЙ странице; дедуп идёт по crawl_frontier.dedup_key."""
    return normalize(url)


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


def _phone_key(raw: str) -> str:
    """Ключ дедупликации телефона — только цифры: `+81 78-855-6530`, `+81788556530` и
    `81788556530` — ОДИН номер (на живом сайте он приезжал в профиль тремя строками)."""
    return re.sub(r"\D", "", raw or "")


def _valid_phone(raw: str) -> str | None:
    """Нормализовать телефон и отсеять мусор (рег.номера, индексы, даты) по числу цифр."""
    s = _html_mod.unescape((raw or "").strip())
    digits = _phone_key(s)
    if not (_PHONE_MIN_DIGITS <= len(digits) <= _PHONE_MAX_DIGITS):
        return None
    return re.sub(r"\s+", " ", s).strip()


def _extract_contacts(soup, text: str) -> tuple[list[str], list[str]]:
    """Телефоны/e-mail КОДОМ, по убыванию надёжности: href tel:/mailto: → JSON-LD → регекс (+…).

    Раньше единственным источником был регекс по тексту: он тянул регистрационный номер и почтовый
    индекс как «телефоны», а достоверные tel:-ссылки выбрасывались вместе с mailto: и javascript:."""
    phones: list[str] = []
    emails: list[str] = []

    def add_phone(v: str | None) -> None:
        p = _valid_phone(v or "")
        if not p:
            return
        key = _phone_key(p)
        for i, have in enumerate(phones):
            if _phone_key(have) == key:
                if p.startswith("+") and not have.startswith("+"):
                    phones[i] = p  # междунар. запись читабельнее — оставляем её
                return
        phones.append(p)

    def add_email(v: str | None) -> None:
        e = _html_mod.unescape((v or "").strip()).lower()
        if e and _EMAIL_RE.fullmatch(e) and e not in emails:
            emails.append(e)

    for a in soup.find_all("a", href=True):
        href = (a["href"] or "").strip()
        low = href.lower()
        if low.startswith("tel:"):
            add_phone(unquote(href[4:]))
        elif low.startswith("mailto:"):
            add_email(unquote(href[7:]).split("?", 1)[0])

    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for key, val in node.items():
                    k = key.lower()
                    if k == "telephone" and isinstance(val, str):
                        add_phone(val)
                    elif k == "email" and isinstance(val, str):
                        add_email(val)
                    elif isinstance(val, (dict, list)):
                        stack.append(val)
            elif isinstance(node, list):
                stack.extend(node)

    for m in _PHONE_RE.finditer(text or ""):
        add_phone(m.group(0))
    for m in _EMAIL_RE.finditer(text or ""):
        add_email(m.group(0))
    return phones, emails


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
        if href.lower().startswith(("mailto:", "tel:", "javascript:", "#")):
            continue  # контакты снимаются отдельно (_extract_contacts), в фронтир они не идут
        absu = _norm(urljoin(base_url, href))
        host = urlparse(absu).netloc.lower().split("@")[-1]
        bare = host[4:] if host.startswith("www.") else host
        # Свой домен важнее карты соцсетей: сайт клиента на x.com (это же хост Twitter) иначе
        # классифицировался бы как «соцсеть» и не обходился вовсе.
        if _same_domain(absu, domain):
            if absu not in links:
                links.append(absu)
        elif bare in _SOCIAL_HOSTS:
            socials.setdefault(_SOCIAL_HOSTS[bare], absu)
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
    text = _html_to_text(html)  # drop_chrome=True: меню/подвал не занимают место в промпте
    phones, emails = _extract_contacts(soup, text)
    if meta_bits:
        text = "\n".join(meta_bits) + "\n" + text
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
        u = _norm(_html_mod.unescape(m.group(1)))
        if _same_domain(u, domain) and not _is_sitemap_url(u) and u not in out:
            out.append(u)
    return out


def _parse_sitemap_children(xml: str, domain: str) -> list[str]:
    """<sitemapindex> → адреса вложенных карт (в пределах домена)."""
    out: list[str] = []
    for m in re.finditer(r"<loc>\s*([^<\s]+)\s*</loc>", xml or "", re.IGNORECASE):
        u = _norm(_html_mod.unescape(m.group(1)))
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
    concurrency: int = 1,
    strip_boilerplate: bool = True,
    stats: FetchStats | None = None,
) -> CrawlResult:
    """Обход одного домена по приоритетному фронтиру. fetcher(url)->HTML (бросает при отказе);
    can_fetch(url) — robots-гейт (None ⇒ всё разрешено). sitemap_xml — опц. предзагруженный sitemap.
    Внешние ссылки НЕ обходятся (фиксируются как соцсети). Лимиты жёсткие.

    concurrency>1 — воркеры тянут страницы параллельно (сеть должна сама быть вежливой: пауза и
    предохранитель живут в crawl_fetch.SiteFetcher). delay_s — пауза для «наивного» fetcher'а
    (тесты/одиночный режим); при SiteFetcher передавать 0.

    time_budget_s — ВНУТРЕННИЙ дедлайн: вышло время ⇒ выходим и отдаём собранное (`partial=True`).
    Снаружи обход по-прежнему прикрыт `asyncio.wait_for`, но тот при срабатывании выбрасывал ВЕСЬ
    результат — 49 честно обойденных страниц пропадали из-за одной медленной 50-й.

    CircuitOpen из fetcher'а (сайт лёг) останавливает обход целиком: `stopped="circuit"`."""
    p = urlparse(start_url)
    domain = (p.netloc or "").lower()
    if domain.startswith("www."):
        domain = domain[4:]
    result = CrawlResult(domain=domain, stats=stats if stats is not None else FetchStats())
    if not domain:
        return result

    frontier = Frontier(max_depth=max_depth)
    frontier.push(_norm(start_url), 0, prio=PRIO_TOP)
    for u in _parse_sitemap(sitemap_xml or "", domain):
        frontier.push(u, 1)

    deadline = (time.monotonic() + time_budget_s) if time_budget_s else None
    in_flight = 0
    stop = False

    async def worker() -> None:
        nonlocal in_flight, stop
        while not stop:
            if len(result.pages) >= max_pages:
                return
            # дедлайн проверяем ПОСЛЕ первой страницы: обход без единой страницы бесполезен
            if deadline is not None and result.pages and time.monotonic() >= deadline:
                result.partial = True
                result.stopped = "time"
                stop = True
                return
            item = frontier.pop()
            if item is None:
                if in_flight == 0:
                    return  # очередь пуста и никто её больше не пополнит
                await asyncio.sleep(0.02)  # кто-то ещё качает — вот-вот добавит ссылки
                continue
            url, depth = item
            if can_fetch is not None and not can_fetch(url):
                result.stats.blocked += 1
                continue
            # in_flight держим до КОНЦА разбора: иначе другой воркер увидит пустую очередь при
            # in_flight==0 и выйдет, хотя мы вот-вот добавим ссылки с этой страницы.
            in_flight += 1
            try:
                try:
                    html = await fetcher(url)
                except CircuitOpen:
                    result.partial = True
                    result.stopped = "circuit"
                    stop = True
                    return
                except Exception:  # noqa: BLE001 — битая страница не роняет обход (учтена в stats)
                    continue
                title, text, links, socials, phones, emails = await asyncio.to_thread(
                    _extract, html, url, domain
                )
                if len(result.pages) >= max_pages:
                    return
                result.pages.append(
                    CrawlPage(
                        url=url,
                        title=title,
                        page_type=_page_type(url, title),
                        text=text[:max_text_chars],
                        key_links=links[:20],
                    )
                )
                for k, v in socials.items():
                    result.socials.setdefault(k, v)
                for ph in phones:  # дедуп по цифрам: один номер в трёх написаниях — один номер
                    if not any(_phone_key(x) == _phone_key(ph) for x in result.phones):
                        result.phones.append(ph)
                for em in emails:
                    if em not in result.emails:
                        result.emails.append(em)
                if depth < max_depth:
                    for ln in links:
                        if _same_domain(ln, domain):
                            frontier.push(ln, depth + 1)
                if delay_s:
                    await asyncio.sleep(delay_s)
            finally:
                in_flight -= 1

    workers = [asyncio.create_task(worker()) for _ in range(max(1, concurrency))]
    try:
        await asyncio.gather(*workers)
    finally:
        for t in workers:
            t.cancel()

    if not result.stopped and len(result.pages) >= max_pages and len(frontier):
        result.stopped = "pages"
    # Шаблон (меню/подвал) вычитаем ПОСЛЕ обхода — по частоте строк в корпусе, а не по вёрстке.
    # content_hash считаем от очищенного текста: правка меню больше не «меняет» все страницы разом.
    if strip_boilerplate and result.pages:
        cleaned = boilerplate.subtract([p.text for p in result.pages])
        for page, txt in zip(result.pages, cleaned, strict=True):
            page.text = txt
    for page in result.pages:
        page.content_hash = _content_hash(page.title, page.text)
    return result


async def load_robots(start_url: str) -> tuple[Callable[[str], bool], float | None, list[str]]:
    """robots.txt домена → (can_fetch, crawl_delay, sitemaps).

    **Fail-closed (правило 10).** Раньше ЛЮБОЙ сбой (таймаут, 500, обрыв) означал «разрешено всё» —
    это fail-open. Теперь: 404/403 (файла нет ⇒ правил нет, так и требует RFC 9309) — разрешаем;
    сетевой сбой или 5xx после повторов — бросаем, обход не начинается и пользователь видит причину.

    Заодно достаём `Crawl-delay` (уважаем как минимальный интервал) и `Sitemap:` — у половины сайтов
    карта лежит НЕ по /sitemap.xml, и мы её просто не находили."""
    import httpx

    from urllib.robotparser import RobotFileParser

    p = urlparse(start_url)
    robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
    try:
        text = await fetch_url_html(robots_url)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):  # доступ закрыт ⇒ по RFC 9309 весь сайт запрещён
            return (lambda _u: False), None, []
        if e.response.status_code == 404:  # файла нет ⇒ правил нет
            return (lambda _u: True), None, []
        raise  # 5xx после повторов — сайт нездоров, обход не начинаем (fail-closed)
    except ValueError:  # SSRF-гард/схема — это не «нет robots», это отказ
        raise

    rp = RobotFileParser()
    rp.parse(text.splitlines())
    delay = None
    try:
        d = rp.crawl_delay(_UA)
        delay = float(d) if d else None
    except (AttributeError, TypeError, ValueError):
        delay = None
    sitemaps = [
        m.group(1).strip()
        for m in re.finditer(r"(?im)^\s*sitemap:\s*(\S+)\s*$", text)
        if m.group(1).strip()
    ]
    return (lambda u: rp.can_fetch(_UA, u)), delay, sitemaps


async def fetch_sitemap(
    start_url: str, *, max_children: int = 50, extra_urls: list[str] | None = None
) -> str | None:
    """Карта сайта (через SSRF-гард). Сбой/нет → None (обход всё равно пойдёт от главной).

    <sitemapindex> разворачивается ПОЛНОСТЬЮ (до max_children вложенных карт): раньше лимит был 5, и
    у сайта с восемью картами три терялись молча. extra_urls — карты, объявленные в robots.txt
    (`Sitemap:`), их проверяем в первую очередь: у половины сайтов карта лежит не по /sitemap.xml."""
    p = urlparse(start_url)
    domain = (p.netloc or "").lower()
    if domain.startswith("www."):
        domain = domain[4:]

    roots: list[str] = []
    for u in (extra_urls or []) + [
        f"{p.scheme}://{p.netloc}/sitemap.xml",
        f"{p.scheme}://{p.netloc}/sitemap_index.xml",
    ]:
        if u not in roots:
            roots.append(u)

    parts: list[str] = []
    children: list[str] = []
    for root_url in roots:
        try:
            root = await fetch_url_html(root_url, allow_xml=True)
        except Exception:  # noqa: BLE001 — нет такой карты, пробуем следующую
            continue
        parts.append(root)
        for child in _parse_sitemap_children(root, domain):
            if child not in children and child not in roots:
                children.append(child)
        if parts:  # первая же найденная карта — рабочая; остальные кандидаты не нужны
            break
    if not parts:
        return None
    for child in children[:max_children]:
        try:
            parts.append(await fetch_url_html(child, allow_xml=True))
        except Exception:  # noqa: BLE001 — недоступная вложенная карта не валит остальные
            continue
    return "\n".join(parts)
