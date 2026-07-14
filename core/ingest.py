"""Чтение ссылок и файлов в ТЕКСТ для постановки задачи агенту (ingest).

Пользователь даёт ссылку/файл + задачу → бот извлекает текст и передаёт его агенту как
СПРАВОЧНЫЕ ДАННЫЕ (не команды). Любая мутация всё равно проходит confirm-гейт — это backstop
против prompt-injection из стороннего контента (golden rule #8).

Безопасность:
- URL-фетч под SSRF-гардом: только http/https; хост резолвится и ВСЕ адреса должны быть
  публичными (приватные/loopback/link-local/reserved/multicast — отказ). Редиректы валидируются
  тем же гардом (event hook на каждый запрос). Таймаут + потолок размера ответа.
- Размер файла/текста ограничен (MAX_*). PDF без зависимости pypdf — мягкий отказ.
- Никаких секретов: читаем только то, что прислал пользователь.
"""

from __future__ import annotations

import asyncio
import io
import ipaddress
import re
import socket
from urllib.parse import urlparse

MAX_FETCH_BYTES = 2_000_000  # потолок тела ответа (HTML-страницы лендингов невелики)
MAX_TEXT_CHARS = 8_000  # сколько текста отдаём агенту (ограничивает токены и поверхность инъекции)
MAX_FILE_BYTES = 5_000_000  # потолок принимаемого файла
FETCH_TIMEOUT_S = 10.0
MAX_REDIRECTS = 3
_UA = "AimashBot/1.0 (+google-ads assistant; reads the link you sent)"

_URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)
# Текстовые/парсимые расширения файлов (по имени; pdf — отдельным мягким отказом).
_TEXT_EXTS = {"txt", "md", "markdown", "csv", "tsv", "json", "log", "text"}


class IngestError(Exception):
    """Понятная пользователю ошибка чтения (показывается как есть, без сырых трейсбеков)."""


def extract_urls(text: str) -> list[str]:
    """Все http(s)-URL из текста (с обрезкой хвостовой пунктуации .,;)!?»)."""
    out: list[str] = []
    for m in _URL_RE.finditer(text or ""):
        out.append(m.group(0).rstrip(".,;)!?»"))
    return out


def _is_public_host(host: str) -> bool:
    """True, если ВСЕ резолвы host — публичные IP. Блокирует SSRF к внутренним адресам.
    Блокирующий resolve → вызывать через asyncio.to_thread."""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            return False
    return True


# Обвязка страницы (меню/подвал/сайдбар/формы) — она одинакова на всех страницах сайта и раньше
# уезжала в LLM как «текст страницы»: на живом сайте это ровно половина корпуса (8 страниц darial
# отдали по 1431 символа — одно меню). Роли (role=navigation/banner/contentinfo) ловят тему, где
# семантические теги подменены div'ами.
_CHROME_TAGS = ("nav", "footer", "aside", "form")
_CHROME_ROLES = ("navigation", "banner", "contentinfo", "search", "menubar", "complementary")


def _html_to_text(html: str, *, drop_chrome: bool = True) -> str:
    """HTML → читаемый текст: убираем script/style, обвязку страницы, заголовок страницы — в начало.

    drop_chrome=True (дефолт) вырезает nav/footer/aside/form и элементы с навигационными ARIA-ролями.
    <header> сносится только верхнеуровневый: внутри <article>/<main> он несёт H1 самой статьи.
    Отключать имеет смысл лишь там, где меню — и есть искомое содержимое (в проекте таких мест нет)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.string if soup.title and soup.title.string else "").strip()  # ДО decompose
    for tag in soup(["script", "style", "noscript", "template", "svg", "head"]):
        tag.decompose()
    if drop_chrome:
        for tag in soup(list(_CHROME_TAGS)):
            tag.decompose()
        for tag in soup.find_all("header"):
            if tag.find_parent(["article", "main"]) is None:
                tag.decompose()
        for tag in soup.find_all(attrs={"role": True}):
            if str(tag.get("role", "")).strip().lower() in _CHROME_ROLES:
                tag.decompose()
    body = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in body.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return (f"{title}\n\n{text}" if title else text).strip()


def _is_textual(content_type: str) -> bool:
    ct = (content_type or "").lower()
    return ct.startswith("text/") or "html" in ct or "xml" in ct or "json" in ct


async def fetch_url_text(url: str) -> str:
    """Прочитать ссылку → текст (HTML → плоский текст). SSRF-гард на исходный URL и каждый
    редирект; таймаут; потолок размера. IngestError при отказе/нетекстовом контенте."""
    import httpx

    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise IngestError("поддерживаются только http/https-ссылки")

    async def _guard(request: "httpx.Request") -> None:
        # Валидируем КАЖДЫЙ запрос (вкл. редиректы): host должен резолвиться в публичные IP.
        if not await asyncio.to_thread(_is_public_host, request.url.host):
            raise IngestError(f"адрес заблокирован (внутренний/небезопасный): {request.url.host}")

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            timeout=FETCH_TIMEOUT_S,
            headers={"User-Agent": _UA},
            event_hooks={"request": [_guard]},
        ) as client:
            async with client.stream("GET", url) as r:
                r.raise_for_status()
                ctype = r.headers.get("content-type", "")
                if not _is_textual(ctype):
                    raise IngestError(f"ссылка не текстовая (тип: {ctype or 'неизвестно'})")
                chunks: list[bytes] = []
                total = 0
                async for chunk in r.aiter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > MAX_FETCH_BYTES:
                        break
                encoding = r.encoding or "utf-8"
            raw = b"".join(chunks)
    except IngestError:
        raise
    except Exception as e:  # сеть/таймаут/HTTP-статус
        raise IngestError(f"не удалось прочитать ссылку ({type(e).__name__})") from e

    try:
        html = raw.decode(encoding, errors="replace")
    except (LookupError, TypeError):
        html = raw.decode("utf-8", errors="replace")
    text = _html_to_text(html) if ("html" in ctype.lower()) else html
    text = text.strip()
    if not text:
        raise IngestError("страница не содержит читаемого текста")
    return text[:MAX_TEXT_CHARS]


def _xlsx_to_text(data: bytes) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out: list[str] = []
    rows = 0
    for ws in wb.worksheets:
        out.append(f"# {ws.title}")
        for row in ws.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None]
            if cells:
                out.append(", ".join(cells))
            rows += 1
            if rows > 2000:  # потолок строк (защита от гигантских книг)
                out.append("…(обрезано)")
                wb.close()
                return "\n".join(out)
    wb.close()
    return "\n".join(out)


def _docx_to_text(data: bytes) -> str:
    from docx import Document

    doc = Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def extract_file_text(filename: str, data: bytes) -> str:
    """Файл (по расширению) → текст. Поддержаны: txt/md/csv/tsv/json/log, .docx, .xlsx. PDF —
    мягкий отказ (нет зависимости pypdf). IngestError при неподдержанном типе/пустоте/слишком большом."""
    if len(data) > MAX_FILE_BYTES:
        raise IngestError(f"файл слишком большой (> {MAX_FILE_BYTES // 1_000_000} МБ)")
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    if ext == "pdf":
        raise IngestError("PDF пока не поддержан — пришли текст, .docx, .csv или .xlsx")
    try:
        if ext == "docx":
            text = _docx_to_text(data)
        elif ext == "xlsx":
            text = _xlsx_to_text(data)
        elif ext in _TEXT_EXTS or ext == "":
            text = data.decode("utf-8", errors="replace")
        else:
            raise IngestError(f"тип файла .{ext} не поддержан — пришли txt/csv/json/.docx/.xlsx")
    except IngestError:
        raise
    except Exception as e:  # битый файл/неверный формат
        raise IngestError(f"не удалось прочитать файл ({type(e).__name__})") from e
    text = (text or "").strip()
    if not text:
        raise IngestError("файл пустой или без читаемого текста")
    return text[:MAX_TEXT_CHARS]
