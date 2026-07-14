"""§20.4: сетевой слой краула — один пул соединений на весь обход, вежливая конкурентность.

Зачем отдельный модуль: `fetch_url_html` открывал НОВЫЙ `httpx.AsyncClient` на каждый URL — TLS-
хендшейк заново на каждую страницу. Замер на живом сайте: 5.35 c/страница, 17 страниц за 91 c.
С пулом keep-alive и 6 одновременными запросами тот же сайт отдаёт 36 страниц за 51 c.

Вежливость (мы ходим по ЧУЖОМУ серверу):
- потолок конкурентности (`MAX_CONCURRENCY`) и минимальный интервал между стартами запросов
  (≤ ~3.3 rps на хост), с уважением `Crawl-delay` из robots.txt — берётся максимум из двух;
- 429/503 → пауза по `Retry-After` (или экспонента) и до `MAX_RETRIES` повторов;
- предохранитель: `BREAKER_THRESHOLD` отказов подряд ⇒ обход останавливается (`CircuitOpen`),
  а не продолжает долбить лежащий сайт.

Безопасность: SSRF-гард `core.ingest._is_public_host` навешан event-хуком на КАЖДЫЙ запрос (включая
редиректы) — ровно как в `core.ingest.fetch_url_text`; не ослаблять. Content-Type проверяется ДО
чтения тела (бинарь не должен доехать до LLM), тело режется по `MAX_FETCH_BYTES`.

Диагностика: `FetchStats` считает всё, что раньше глотал `except Exception: continue` — на живом
сайте это 51 битая ссылка из 87, о которых никто не узнавал.
"""

from __future__ import annotations

import asyncio
import gzip
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from core.ingest import FETCH_TIMEOUT_S, MAX_FETCH_BYTES, _UA, _is_public_host

# B13: типы контента, которые краулер парсит как текст (кроме них — всё text/*). Прочее
# (application/pdf, image/*, application/zip…) отвергаем ДО чтения тела — бинарь не в LLM-профиль.
_ALLOWED_CONTENT_TYPES = frozenset({"text/html", "text/plain", "application/xhtml+xml"})
# sitemap.xml сайты отдают как application/xml (а .xml.gz — как gzip). Для СТРАНИЦ эти типы
# по-прежнему запрещены: allow_xml=True передаётся только при загрузке карты сайта.
_XML_CONTENT_TYPES = frozenset(
    {
        "application/xml",
        "text/xml",
        "application/rss+xml",
        "application/gzip",
        "application/x-gzip",
        "binary/octet-stream",
        "application/octet-stream",
    }
)

DEFAULT_CONCURRENCY = 6  # решение владельца: «сбалансированно»
MAX_CONCURRENCY = 8  # потолок вежливости — конфигом выше не поднять
MIN_INTERVAL_S = 0.3  # минимум между стартами запросов к хосту ⇒ ≤ ~3.3 rps
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRIES = 2
MAX_RETRY_WAIT_S = 10.0
BREAKER_THRESHOLD = 8  # столько отказов ПОДРЯД ⇒ считаем сайт лежащим и уходим

_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.IGNORECASE
)


class CircuitOpen(RuntimeError):
    """Сайт отказывает подряд — обход остановлен (не долбим чужой сервер и не жжём бюджет)."""


class ContentTypeRejected(ValueError):
    """Не-HTML (pdf/картинка/архив) — тело не читаем, в LLM не отдаём."""


@dataclass
class FetchStats:
    """Что случилось с каждым запросом обхода (раньше всё это молча глоталось)."""

    ok: int = 0
    by_status: dict[int, int] = field(default_factory=dict)  # HTTP-отказы: 404 → 51, 500 → 2…
    by_error: dict[str, int] = field(default_factory=dict)  # классы сетевых ошибок
    skipped_ctype: int = 0  # отвергнуто по content-type (pdf/картинки)
    retried: int = 0  # повторов после 429/503
    blocked: int = 0  # robots / SSRF-гард

    @property
    def failed(self) -> int:
        return sum(self.by_status.values()) + sum(self.by_error.values())

    def summary(self) -> str:
        """Короткая строка для лога/сводки: «ok=36 404×51 timeout×2 ctype=3»."""
        bits = [f"ok={self.ok}"]
        bits += [f"{code}×{n}" for code, n in sorted(self.by_status.items())]
        bits += [f"{name}×{n}" for name, n in sorted(self.by_error.items())]
        if self.skipped_ctype:
            bits.append(f"ctype={self.skipped_ctype}")
        if self.retried:
            bits.append(f"retry={self.retried}")
        if self.blocked:
            bits.append(f"blocked={self.blocked}")
        return " ".join(bits)


def _charset_from_ctype(content_type: str | None) -> str | None:
    """charset из заголовка Content-Type ('text/html; charset=windows-1251' → 'windows-1251')."""
    m = re.search(r"charset\s*=\s*\"?([A-Za-z0-9_\-]+)", content_type or "", re.IGNORECASE)
    return m.group(1) if m else None


def _decode_html(raw: bytes, header_charset: str | None) -> str:
    """Декодировать тело. Кодировка: HTTP-заголовок → <meta charset> → utf-8. Gzip — распаковать.

    Раньше брали `r.encoding`, а httpx при ОТСУТСТВИИ charset в заголовке молча отдаёт
    default_encoding='utf-8' — сайт в windows-1251 (их немало) декодировался с errors='replace' и
    приезжал в профиль клиента как «???».

    Gzip: `.xml.gz`-карты сайта отдаются как ФАЙЛ (Content-Encoding тут ни при чём, httpx их не
    распаковывает) — ловим по magic-байтам, иначе вместо карты в парсер уезжал бинарный мусор."""
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError):  # битый gzip — пусть дальше декодируется как есть
            pass
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


def _retry_after_s(value: str | None) -> float | None:
    """`Retry-After: 12` → 12.0. HTTP-дату не парсим — вернём None (сработает экспонента)."""
    if not value:
        return None
    try:
        return max(0.0, min(float(value.strip()), MAX_RETRY_WAIT_S))
    except (TypeError, ValueError):
        return None


def _check_ctype(raw_ctype: str, url: str, *, allow_xml: bool) -> None:
    ctype = (raw_ctype or "").split(";", 1)[0].strip().lower()
    allowed = _ALLOWED_CONTENT_TYPES | (_XML_CONTENT_TYPES if allow_xml else frozenset())
    if ctype and not (ctype in allowed or ctype.startswith("text/")):
        raise ContentTypeRejected(f"неподдерживаемый тип контента: {ctype} ({url})")


async def _guard_request(request) -> None:
    """SSRF-гард на КАЖДЫЙ запрос, включая редиректы (event hook)."""
    if not await asyncio.to_thread(_is_public_host, request.url.host):
        raise ValueError(f"адрес заблокирован (внутренний/небезопасный): {request.url.host}")


class SiteFetcher:
    """Пул соединений + вежливая конкурентность + предохранитель на ОДИН обход одного сайта.

    Использовать как async-контекст:

        async with SiteFetcher(concurrency=6, delay_s=0.5) as f:
            html = await f.fetch(url)          # бросает при отказе; счётчики — в f.stats
    """

    def __init__(
        self,
        *,
        concurrency: int = DEFAULT_CONCURRENCY,
        delay_s: float = 0.0,
        timeout_s: float = FETCH_TIMEOUT_S,
    ) -> None:
        self.concurrency = max(1, min(int(concurrency or 1), MAX_CONCURRENCY))
        # Интервал между стартами: уважаем и свой потолок вежливости, и Crawl-delay сайта.
        self.interval_s = max(MIN_INTERVAL_S, float(delay_s or 0.0))
        self.timeout_s = timeout_s
        self.stats = FetchStats()
        self._sem = asyncio.Semaphore(self.concurrency)
        self._pace_lock = asyncio.Lock()
        self._next_at = 0.0
        self._consecutive_failures = 0
        self._breaker_open = False
        self._client = None  # httpx.AsyncClient — создаётся в __aenter__

    async def __aenter__(self) -> SiteFetcher:
        import httpx

        self._client = httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=3,
            timeout=self.timeout_s,
            headers={"User-Agent": _UA},
            event_hooks={"request": [_guard_request]},
            limits=httpx.Limits(
                max_connections=self.concurrency,
                max_keepalive_connections=self.concurrency,
                keepalive_expiry=30.0,
            ),
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def breaker_open(self) -> bool:
        return self._breaker_open

    async def _pace(self) -> None:
        """Не чаще одного старта раз в interval_s (общий для всех воркеров)."""
        async with self._pace_lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_at = now + self.interval_s

    def _note_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= BREAKER_THRESHOLD:
            self._breaker_open = True

    async def fetch(self, url: str, *, allow_xml: bool = False) -> str:
        """URL → HTML/XML-текст. Бросает при отказе; всё учтено в self.stats.

        CircuitOpen — сайт лежит (BREAKER_THRESHOLD отказов подряд): обход обязан остановиться,
        а не перебирать оставшуюся тысячу ссылок в пустоту."""
        import httpx

        if self._breaker_open:
            raise CircuitOpen(f"сайт не отвечает ({BREAKER_THRESHOLD} отказов подряд)")
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            raise ValueError("поддерживаются только http/https-ссылки")
        if self._client is None:
            raise RuntimeError("SiteFetcher используется вне async-контекста")

        attempt = 0
        while True:
            async with self._sem:
                await self._pace()
                try:
                    text = await self._get(url, allow_xml=allow_xml)
                except httpx.HTTPStatusError as e:
                    code = e.response.status_code
                    if code in RETRY_STATUSES and attempt < MAX_RETRIES:
                        wait = _retry_after_s(e.response.headers.get("retry-after"))
                        if wait is None:
                            wait = min(MAX_RETRY_WAIT_S, self.interval_s * (2 ** (attempt + 1)))
                        attempt += 1
                        self.stats.retried += 1
                        await asyncio.sleep(wait)
                        continue
                    self.stats.by_status[code] = self.stats.by_status.get(code, 0) + 1
                    # 4xx (кроме 429) — сайт ЖИВ, просто ссылка битая: предохранитель не трогаем.
                    # Иначе живой прогон darial обрывался на 17-й странице: в sitemap 51 битая
                    # ссылка из 87, четырнадцать 404 подряд «доказали», что сайт лёг.
                    if code >= 500 or code == 429:
                        self._note_failure()
                    else:
                        self._consecutive_failures = 0
                    raise
                except ContentTypeRejected:
                    self.stats.skipped_ctype += 1
                    self._consecutive_failures = 0  # сайт жив, просто отдал pdf
                    raise
                except ValueError:  # SSRF-гард / не-http схема
                    self.stats.blocked += 1
                    raise
                except Exception as e:  # noqa: BLE001 — сетевые сбои: таймаут/DNS/TLS/reset
                    name = type(e).__name__
                    self.stats.by_error[name] = self.stats.by_error.get(name, 0) + 1
                    self._note_failure()
                    raise
                self.stats.ok += 1
                self._consecutive_failures = 0
                return text

    async def _get(self, url: str, *, allow_xml: bool) -> str:
        async with self._client.stream("GET", url) as r:
            r.raise_for_status()
            raw_ctype = r.headers.get("content-type") or ""
            _check_ctype(raw_ctype, url, allow_xml=allow_xml)  # ДО чтения тела
            chunks: list[bytes] = []
            total = 0
            async for chunk in r.aiter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_FETCH_BYTES:
                    break
        return _decode_html(b"".join(chunks), _charset_from_ctype(raw_ctype))


async def fetch_url_html(url: str, *, allow_xml: bool = False) -> str:
    """Разовый запрос (robots.txt, sitemap) — без пула: их единицы, а не сотни.

    Для самого обхода использовать SiteFetcher (пул + вежливость + счётчики)."""
    async with SiteFetcher(concurrency=1, delay_s=0.0) as f:
        return await f.fetch(url, allow_xml=allow_xml)
