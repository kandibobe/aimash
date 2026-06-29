"""ingest: чтение ссылок/файлов → текст для задачи агенту. Офлайн (без сети — SSRF-гард блокирует
локальные адреса до коннекта; файлы строим в памяти). Проверяем извлечение URL, SSRF-гард, HTML→текст,
парс файлов (txt/csv/json/.docx/.xlsx + отказы), и проброс контента в handle_command как ДАННЫХ.
"""

from __future__ import annotations

import io
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import ingest  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


# ── extract_urls ────────────────────────────────────────────────────────────────────
def test_extract_urls_finds_and_trims():
    text = "Глянь https://shop.ua/landing, и ещё http://example.com/x)."
    urls = ingest.extract_urls(text)
    assert urls[0] == "https://shop.ua/landing"
    assert urls[1] == "http://example.com/x"
    assert ingest.extract_urls("без ссылок") == []


# ── SSRF-гард (IP-литералы → без DNS) ────────────────────────────────────────────────
def test_is_public_host_blocks_internal():
    assert ingest._is_public_host("127.0.0.1") is False  # loopback
    assert ingest._is_public_host("10.1.2.3") is False  # private
    assert ingest._is_public_host("169.254.1.1") is False  # link-local
    assert ingest._is_public_host("0.0.0.0") is False  # unspecified
    assert ingest._is_public_host("") is False
    assert ingest._is_public_host("8.8.8.8") is True  # публичный


# ── HTML → текст ──────────────────────────────────────────────────────────────────────
def test_html_to_text_strips_and_titles():
    html = "<html><head><title>Магазин</title><style>x{}</style></head><body><h1>Купить</h1><script>bad()</script><p>быстро и дёшево</p></body></html>"
    text = ingest._html_to_text(html)
    assert "Магазин" in text and "Купить" in text and "быстро" in text
    assert "bad()" not in text and "x{}" not in text  # script/style вырезаны


# ── extract_file_text ────────────────────────────────────────────────────────────────
def test_extract_txt_csv_json():
    assert "бриф" in ingest.extract_file_text("b.txt", "бриф кампании".encode())
    assert "ключ" in ingest.extract_file_text("k.csv", "ключ,объём\nкупить,100".encode())
    assert "topic" in ingest.extract_file_text("d.json", b'{"topic": "shoes"}')


def test_extract_xlsx():
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["ключ", "объём"])
    ws.append(["купить телефон", 1200])
    buf = io.BytesIO()
    wb.save(buf)
    text = ingest.extract_file_text("keywords.xlsx", buf.getvalue())
    assert "купить телефон" in text and "1200" in text


def test_extract_docx():
    from docx import Document

    d = Document()
    d.add_paragraph("Бриф кампании: продаём кроссовки")
    buf = io.BytesIO()
    d.save(buf)
    text = ingest.extract_file_text("brief.docx", buf.getvalue())
    assert "Бриф кампании" in text and "кроссовки" in text


def test_extract_pdf_soft_reject():
    with pytest.raises(ingest.IngestError):
        ingest.extract_file_text("doc.pdf", b"%PDF-1.4 ...")


def test_extract_unsupported_and_empty_and_toobig():
    with pytest.raises(ingest.IngestError):
        ingest.extract_file_text("a.exe", b"MZ...")
    with pytest.raises(ingest.IngestError):
        ingest.extract_file_text("empty.txt", b"   ")
    with pytest.raises(ingest.IngestError):
        ingest.extract_file_text("big.txt", b"x" * (ingest.MAX_FILE_BYTES + 1))


def test_extract_caps_text_length():
    big = ("a" * (ingest.MAX_TEXT_CHARS + 500)).encode()
    out = ingest.extract_file_text("big.txt", big)
    assert len(out) == ingest.MAX_TEXT_CHARS


# ── fetch_url_text: гарды (офлайн) ────────────────────────────────────────────────────
async def test_fetch_rejects_non_http_scheme():
    with pytest.raises(ingest.IngestError):
        await ingest.fetch_url_text("ftp://example.com/x")


async def test_fetch_blocks_loopback_without_network():
    # event-hook _guard блокирует 127.0.0.1 ДО коннекта → IngestError, без реального запроса.
    with pytest.raises(ingest.IngestError):
        await ingest.fetch_url_text("http://127.0.0.1:9/secret")


# ── handle_command: контент идёт как ДАННЫЕ (отдельным помеченным сообщением) ──────────
async def test_handle_command_injects_context_as_data():
    import json
    from types import SimpleNamespace

    import agent.loop as L

    captured = {}

    async def fake_chat(messages, **kw):
        captured["messages"] = messages
        tc = SimpleNamespace(
            function=SimpleNamespace(
                name="keyword_research", arguments=json.dumps({"seeds": ["обувь"]})
            )
        )
        return SimpleNamespace(tool_calls=[tc], content="")

    with patched(L, "chat", fake_chat):
        res = await L.handle_command(
            "подбери ключи по этому лендингу", context_text="Магазин кроссовок Nike, скидки"
        )
    assert res["type"] == "keywords_intent"
    # справочный контент — ОТДЕЛЬНОЕ сообщение, помеченное как данные, ПЕРЕД инструкцией
    msgs = captured["messages"]
    assert any(
        "СПРАВОЧНЫЙ КОНТЕНТ" in mm["content"] and "кроссовок" in mm["content"] for mm in msgs
    )
    assert msgs[-1]["content"] == "подбери ключи по этому лендингу"  # инструкция — последняя


async def test_handle_command_without_context_unchanged():
    import json
    from types import SimpleNamespace

    import agent.loop as L

    captured = {}

    async def fake_chat(messages, **kw):
        captured["messages"] = messages
        return SimpleNamespace(
            tool_calls=[
                SimpleNamespace(
                    function=SimpleNamespace(name="get_stats", arguments=json.dumps({}))
                )
            ],
            content="",
        )

    with patched(L, "chat", fake_chat):
        await L.handle_command("покажи статистику")
    # без context_text — ровно system + user (2 сообщения), без вставки блока контента
    msgs = captured["messages"]
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert msgs[1]["content"] == "покажи статистику"  # последняя — инструкция, контента нет


# ── бот: приём файла → задача (с подписью и без) ──────────────────────────────────────
from types import SimpleNamespace  # noqa: E402


class _Msg:
    def __init__(self, chat_id=80_000, text="", caption="", document=None):
        self.chat = type("C", (), {"id": chat_id})()
        self.text = text
        self.caption = caption
        self.document = document
        self.answers: list = []

    async def answer(self, t="", **kw):
        self.answers.append((t, kw))
        return self


class _Bot:
    def __init__(self, data: bytes):
        self._data = data

    async def download(self, file, destination):
        destination.write(self._data)


class _State:
    def __init__(self):
        self._d, self._s = {}, None

    async def clear(self):
        self._d, self._s = {}, None

    async def set_state(self, s):
        self._s = s

    async def update_data(self, **kw):
        self._d.update(kw)

    async def get_data(self):
        return dict(self._d)

    async def get_state(self):
        return self._s


async def test_on_document_with_caption_runs_task():
    import bot.main as bm

    captured = {}

    async def fake_handle(instruction, *, chat_id=0, context_text=None):
        captured.update(instruction=instruction, context_text=context_text)
        return {"type": "text", "text": "ok"}

    doc = SimpleNamespace(file_id="f", file_name="brief.txt", file_size=20)
    msg = _Msg(chat_id=80_001, caption="сделай кампанию по брифу", document=doc)
    with patched(bm, "handle_command", fake_handle):
        await bm.on_document(msg, _State(), _Bot("Бриф: кроссовки, скидки".encode()))
    assert captured["instruction"] == "сделай кампанию по брифу"
    assert "кроссовки" in captured["context_text"]  # контент файла проброшен как контекст


async def test_on_document_without_caption_asks_then_runs():
    import bot.main as bm

    captured = {}

    async def fake_handle(instruction, *, chat_id=0, context_text=None):
        captured.update(instruction=instruction, context_text=context_text)
        return {"type": "text", "text": "ok"}

    chat = 80_002
    bm._PENDING_CONTEXT.pop(chat, None)
    doc = SimpleNamespace(file_id="f", file_name="keys.csv", file_size=15)
    state = _State()
    # без подписи → запоминаем контент, спрашиваем задачу
    await bm.on_document(_Msg(chat_id=chat, document=doc), state, _Bot(b"kw,vol\x0aboots,99"))
    assert await state.get_state() == bm.IngestWizard.awaiting_task
    assert bm._PENDING_CONTEXT.get(chat)
    # задача пришла → агент с сохранённым контентом
    with patched(bm, "handle_command", fake_handle):
        await bm.ingest_task(_Msg(chat_id=chat, text="подбери похожие ключи"), state)
    assert captured["instruction"] == "подбери похожие ключи"
    assert "boots" in captured["context_text"]
    assert bm._PENDING_CONTEXT.get(chat) is None  # контент израсходован


async def test_on_document_too_big_rejected():
    import bot.main as bm

    doc = SimpleNamespace(file_id="f", file_name="big.txt", file_size=ingest.MAX_FILE_BYTES + 1)
    msg = _Msg(chat_id=80_003, document=doc)
    await bm.on_document(msg, _State(), _Bot(b"x"))
    assert msg.answers and bm._PENDING_CONTEXT.get(80_003) is None  # отказ, контент не сохранён
