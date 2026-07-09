"""B13: fetch_url_html не тянет/не декодирует НЕ-HTML (PDF/картинки/архивы) — иначе бинарь
уходит в LLM-сведение профиля как мусорный «текст». Content-Type проверяется ДО чтения тела.

httpx.AsyncClient подменяется фейком (SSRF-гард event_hook при этом не вызывается — сеть/DNS
не трогаем). Проверяем: text/html проходит, application/pdf и image/* отвергаются.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clients.crawler as cr  # noqa: E402


class _FakeResp:
    def __init__(self, ctype: str, body: bytes = b"<html>ok</html>"):
        self.headers = {"content-type": ctype}
        self._body = body
        self.encoding = "utf-8"

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        yield self._body


class _StreamCM:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, resp, **kw):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url):
        return _StreamCM(self._resp)


def _patch_httpx(monkeypatch, resp):
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(resp, **kw))


async def test_html_content_type_passes(monkeypatch):
    _patch_httpx(monkeypatch, _FakeResp("text/html; charset=utf-8", b"<title>Hi</title>"))
    out = await cr.fetch_url_html("https://example.com/")
    assert "Hi" in out


async def test_plain_text_passes(monkeypatch):
    _patch_httpx(monkeypatch, _FakeResp("text/plain", b"just text"))
    out = await cr.fetch_url_html("https://example.com/robots.txt")
    assert "just text" in out


@pytest.mark.parametrize("ctype", ["application/pdf", "image/png", "application/zip", "video/mp4"])
async def test_binary_content_type_rejected(monkeypatch, ctype):
    _patch_httpx(monkeypatch, _FakeResp(ctype, b"\x00\x01binary"))
    with pytest.raises(ValueError, match="тип контента"):
        await cr.fetch_url_html("https://example.com/file")
