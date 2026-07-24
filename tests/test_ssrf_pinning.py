"""SSRF-пиннинг IP (аудит #2): проверка «адрес публичный?» и сам коннект идут по ОДНОМУ IP.

Раньше `_is_public_host` резолвил хост ОТДЕЛЬНО от httpx-коннекта — классический TOCTOU
DNS-rebinding: между проверкой и запросом DNS мог отдать приватный адрес → обращение к внутренней
сети (метадата-эндпоинты и т.п.). Теперь `make_ssrf_safe_transport` резолвит и коннектится к
запиненному публичному IP на КАЖДЫЙ запрос, TLS-идентичность держит по имени (sni_hostname).

Всё офлайн: `socket.getaddrinfo` подменяется фейком, `super().handle_async_request` — тоже, чтобы
ни один тест не ходил в сеть.
"""

from __future__ import annotations

import socket
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.ingest as ing  # noqa: E402
from core.ingest import SSRFBlocked  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


def _gai(*ips: str):
    """Фейк getaddrinfo: отдаёт заданные IP как TCP-записи (порядок сохраняется)."""

    def _f(host, port=None, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0)) for ip in ips]

    return _f


def _gai_by_host(mapping: dict[str, str]):
    """Фейк getaddrinfo с разной картой по хосту (для редиректов/rebind)."""

    def _f(host, port=None, *a, **k):
        ip = mapping[host]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0))]

    return _f


# ── классификация адреса ──────────────────────────────────────────────────────────────
def test_validated_connect_ip_allows_public_and_unwraps_mapped():
    assert ing._validated_connect_ip("8.8.8.8") == "8.8.8.8"
    assert ing._validated_connect_ip("2606:4700:4700::1111") == "2606:4700:4700::1111"
    # IPv4-mapped IPv6 публичного → разворачиваем в IPv4 и коннектимся по нему
    assert ing._validated_connect_ip("::ffff:8.8.8.8") == "8.8.8.8"


@pytest.mark.parametrize(
    "bad",
    [
        "127.0.0.1",  # loopback
        "10.0.0.1",  # private
        "192.168.1.1",  # private
        "169.254.169.254",  # link-local (облачная метадата)
        "100.64.0.1",  # CGNAT — ловится `not is_global` (ужесточение аудита #2)
        "0.0.0.0",  # unspecified
        "224.0.0.1",  # multicast
        "::1",  # loopback v6
        "fe80::1",  # link-local v6
        "::ffff:127.0.0.1",  # mapped loopback — развернуть и заблокировать
        "::ffff:10.0.0.1",  # mapped private
        "64:ff9b::7f00:1",  # NAT64 loopback — не глобальный
        "not-an-ip",  # мусор из резолвера → fail-closed
    ],
)
def test_validated_connect_ip_blocks_internal(bad):
    with pytest.raises(SSRFBlocked):
        ing._validated_connect_ip(bad)


# ── резолв с пиннингом ──────────────────────────────────────────────────────────────────
def test_resolve_requires_all_addresses_public():
    # один приватный среди публичных ⇒ весь хост отклонён (политика all-public)
    with patched(socket, "getaddrinfo", _gai("8.8.8.8", "10.0.0.1")):
        with pytest.raises(SSRFBlocked):
            ing._resolve_pinned_ip("dual.host")


def test_resolve_deterministic_pick_keeps_keepalive():
    # min по (version, packed): IPv4 раньше IPv6, из IPv4 — численно меньший ⇒ 1.1.1.1.
    # Стабильный выбор держит keep-alive пула (транспорт резолвит на каждый запрос).
    order_a = _gai("2606:4700:4700::1111", "8.8.8.8", "1.1.1.1")
    order_b = _gai("1.1.1.1", "8.8.8.8", "2606:4700:4700::1111")
    with patched(socket, "getaddrinfo", order_a):
        assert ing._resolve_pinned_ip("multi.host") == "1.1.1.1"
    with patched(socket, "getaddrinfo", order_b):
        assert ing._resolve_pinned_ip("multi.host") == "1.1.1.1"  # порядок резолвера не влияет


def test_resolve_gaierror_fail_closed():
    def _boom(*a, **k):
        raise socket.gaierror("Name or service not known")

    with patched(socket, "getaddrinfo", _boom):
        with pytest.raises(SSRFBlocked):
            ing._resolve_pinned_ip("no-such.host")


def test_resolve_empty_host_blocked():
    with pytest.raises(SSRFBlocked):
        ing._resolve_pinned_ip("")


# ── транспорт: коннект к IP, TLS-идентичность по имени ───────────────────────────────────
async def _run_pinned(url: str, resolver, *, follow: bool = False, max_redirects: int = 3):
    """Прогнать реальный make_ssrf_safe_transport, подменив НИЖНИЙ уровень (super()) фейком,
    который просто фиксирует итоговый запрос и отдаёт 200/302 без сети. Возвращает список
    захваченных hop'ов [{host, sni, hdr, port}] и последний ответ (или проброшенное исключение)."""
    import httpx

    hops: list[dict] = []

    async def fake_parent(self, request):  # заменяет httpx.AsyncHTTPTransport.handle_async_request
        hops.append(
            {
                "host": request.url.host,  # == raw_host: куда РЕАЛЬНО коннектимся
                "sni": request.extensions.get("sni_hostname"),
                "hdr": request.headers.get("host"),
                "port": request.url.port,
                "scheme": request.url.scheme,
            }
        )
        loc = getattr(fake_parent, "redirect_to", None)
        if loc:
            return httpx.Response(302, headers={"location": loc}, request=request)
        return httpx.Response(200, content=b"ok", request=request)

    t = ing.make_ssrf_safe_transport()
    with (
        patched(socket, "getaddrinfo", resolver),
        patched(httpx.AsyncHTTPTransport, "handle_async_request", fake_parent),
    ):
        async with httpx.AsyncClient(
            transport=t, follow_redirects=follow, max_redirects=max_redirects
        ) as c:
            resp = await c.get(url)
    return hops, resp


async def test_transport_https_pins_ip_and_keeps_tls_identity():
    hops, resp = await _run_pinned("https://example.com:8443/p", _gai("93.184.216.34"))
    assert resp.status_code == 200
    h = hops[0]
    assert h["host"] == "93.184.216.34"  # TCP-коннект к запиненному IP
    assert h["sni"] == "example.com"  # SNI и проверка сертификата — по ИМЕНИ (без порта)
    assert h["hdr"] == "example.com:8443"  # Host — по ИМЕНИ (с портом, по HTTP), не по IP
    assert "93.184.216.34" not in h["hdr"]  # IP в Host не утёк
    assert h["port"] == 8443  # порт сохранён


async def test_transport_http_sets_no_sni():
    hops, _ = await _run_pinned("http://example.com/p", _gai("93.184.216.34"))
    assert hops[0]["host"] == "93.184.216.34"
    assert hops[0]["sni"] is None  # http — без sni_hostname


async def test_transport_blocks_host_resolving_to_private():
    with pytest.raises(SSRFBlocked):
        await _run_pinned("https://internal.corp/admin", _gai("10.0.0.5"))


async def test_toctou_private_on_connect_resolve_is_blocked():
    """Ядро правки: транспорт резолвит В МОМЕНТ коннекта. Если DNS отдаёт приватный IP — запрос
    отклонён, даже если гипотетическая ранняя проверка видела бы публичный (пиннинг, не первый gai)."""
    with pytest.raises(SSRFBlocked):
        await _run_pinned("https://rebind.evil/x", _gai("169.254.169.254"))


async def test_redirect_to_private_blocked_on_second_hop():
    """Редирект с публичного хоста на внутренний → 2-й hop проходит ТОТ ЖЕ пиннинг и отклоняется.
    Приватный хост до (фейкового) коннекта не доходит — валидация на каждый hop."""
    import httpx

    async def fake_parent(self, request):
        fake_parent.hops.append(request.url.host)
        if request.url.host == "8.8.8.8":  # публичный hop → редирект на приватный
            return httpx.Response(
                302, headers={"location": "http://private.internal/admin"}, request=request
            )
        return httpx.Response(200, request=request)

    fake_parent.hops = []
    resolver = _gai_by_host({"public.site": "8.8.8.8", "private.internal": "10.0.0.9"})
    t = ing.make_ssrf_safe_transport()
    with (
        patched(socket, "getaddrinfo", resolver),
        patched(httpx.AsyncHTTPTransport, "handle_async_request", fake_parent),
    ):
        async with httpx.AsyncClient(transport=t, follow_redirects=True, max_redirects=3) as c:
            with pytest.raises(SSRFBlocked):
                await c.get("http://public.site/start")
    assert fake_parent.hops == ["8.8.8.8"]  # приватный hop не дошёл до сети


async def test_relative_redirect_follows_real_host():
    """Регрессия (адвер. верификация #2): ОТНОСИТЕЛЬНЫЙ редирект `301 Location: /en/` на https
    обязан уехать на РЕАЛЬНЫЙ хост, а не на запиненный IP. httpx резолвит относительный Location
    через `request.url.join(...)` по ТОМУ ЖЕ объекту запроса — переписывай мы url→IP на нём, hop 2
    ушёл бы на `https://<IP>/en/` (имя потеряно → провал TLS по голому IP → ConnectError). Пинуем на
    копии ⇒ исходный url с ИМЕНЕМ цел, редирект резолвится на реальный хост, а тот заново пинуется."""
    import httpx

    hops: list[dict] = []

    async def fake_parent(self, request):
        hops.append(
            {
                "host": request.url.host,  # куда РЕАЛЬНО коннектимся (IP)
                "sni": request.extensions.get("sni_hostname"),  # TLS-идентичность (имя)
                "path": request.url.path,
            }
        )
        if request.url.path == "/":  # первый hop → относительный редирект
            return httpx.Response(301, headers={"location": "/en/"}, request=request)
        return httpx.Response(200, content=b"ok", request=request)

    t = ing.make_ssrf_safe_transport()
    with (
        patched(socket, "getaddrinfo", _gai("93.184.216.34")),
        patched(httpx.AsyncHTTPTransport, "handle_async_request", fake_parent),
    ):
        async with httpx.AsyncClient(transport=t, follow_redirects=True, max_redirects=3) as c:
            resp = await c.get("https://example.com/")
    assert resp.status_code == 200
    assert [h["path"] for h in hops] == ["/", "/en/"]  # редирект прошёл, оба hop'а состоялись
    assert all(h["host"] == "93.184.216.34" for h in hops)  # оба коннекта — к запиненному IP
    assert all(h["sni"] == "example.com" for h in hops)  # имя НЕ потеряно на 2-м hop (TLS цел)


async def test_relative_redirect_rebind_to_private_blocked_per_hop():
    """Пиннинг держится и на ОТНОСИТЕЛЬНОМ редиректе: тот же хост ре-резолвится на каждый hop. Если
    на 2-м резолве DNS ребайндит его в приватный IP — запрос отклонён (per-hop, не только 1-й gai)."""
    import httpx

    calls = {"n": 0}

    def _rebind(host, port=None, *a, **k):
        calls["n"] += 1
        ip = "93.184.216.34" if calls["n"] == 1 else "10.0.0.7"  # публичный → приватный
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0))]

    async def fake_parent(self, request):
        return httpx.Response(301, headers={"location": "/en/"}, request=request)

    t = ing.make_ssrf_safe_transport()
    with (
        patched(socket, "getaddrinfo", _rebind),
        patched(httpx.AsyncHTTPTransport, "handle_async_request", fake_parent),
    ):
        async with httpx.AsyncClient(transport=t, follow_redirects=True, max_redirects=3) as c:
            with pytest.raises(SSRFBlocked):
                await c.get("https://rebind.site/")
    assert calls["n"] == 2  # ре-резолв на 2-м hop состоялся и поймал ребайнд


# ── crawl_fetch: SSRF-блок считается blocked и НЕ роняет предохранитель ───────────────────
async def test_sitefetcher_ssrf_block_counts_blocked_not_breaker():
    from clients.crawl_fetch import BREAKER_THRESHOLD, SiteFetcher

    with patched(socket, "getaddrinfo", _gai("10.0.0.5")):  # любой хост → приватный
        async with SiteFetcher(concurrency=2) as f:
            for _ in range(BREAKER_THRESHOLD + 1):  # больше порога — доказываем, что не откроется
                with pytest.raises(SSRFBlocked):
                    await f.fetch("http://internal.host/x")
            assert f.stats.blocked == BREAKER_THRESHOLD + 1
            assert f.breaker_open is False  # блокировали МЫ, не сайт отказал → брейкер закрыт
            assert f.stats.failed == 0  # SSRF-блок не учитывается как отказ сайта


async def test_sitefetcher_allows_public_via_pinned_transport():
    """Публичный хост доходит до (фейкового) сетевого уровня и парсится — гард не ложно-срабатывает."""
    import httpx

    from clients.crawl_fetch import SiteFetcher

    async def fake_parent(self, request):
        assert request.url.host == "93.184.216.34"  # коннект к запиненному IP
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<html>ok</html>", request=request
        )

    with (
        patched(socket, "getaddrinfo", _gai("93.184.216.34")),
        patched(httpx.AsyncHTTPTransport, "handle_async_request", fake_parent),
    ):
        async with SiteFetcher(concurrency=1) as f:
            html = await f.fetch("https://example.com/page")
    assert "ok" in html and f.stats.ok == 1 and f.stats.blocked == 0


async def test_sitefetcher_relative_redirect_does_not_trip_breaker():
    """Регрессия #2 в краулере: страница с ОТНОСИТЕЛЬНЫМ редиректом (`/en/`) на https — частый
    легитимный кейс (нормализация слэша, i18n-префикс). Раньше url переписывался на исходном объекте
    ⇒ hop 2 уходил на голый IP ⇒ ConnectError ⇒ generic-except ⇒ by_error++ И _note_failure ⇒ порога
    таких страниц хватало, чтобы ЛОЖНО открыть предохранитель и оборвать весь обход. Теперь редирект
    следует на реальный хост и отдаёт 200 — stats.ok, брейкер не трогается."""
    import httpx

    from clients.crawl_fetch import BREAKER_THRESHOLD, SiteFetcher

    async def fake_parent(self, request):
        if request.url.path == "/":
            return httpx.Response(301, headers={"location": "/en/"}, request=request)
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<html>ok</html>", request=request
        )

    with (
        patched(socket, "getaddrinfo", _gai("93.184.216.34")),
        patched(httpx.AsyncHTTPTransport, "handle_async_request", fake_parent),
    ):
        async with SiteFetcher(concurrency=1) as f:
            for _ in range(BREAKER_THRESHOLD + 1):  # больше порога — доказываем, что не откроется
                html = await f.fetch("https://example.com/")
                assert "ok" in html
            assert f.stats.ok == BREAKER_THRESHOLD + 1
            assert f.breaker_open is False  # относительный редирект не считается отказом сайта
            assert not f.stats.by_error  # никаких ConnectError
