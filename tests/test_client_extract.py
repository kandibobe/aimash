"""§20.3 (Фаза B): офлайн-тесты извлечения профиля клиента из свободного текста (LLM-заглушка).

Модель подменяется без сети. Проверяем: полный разбор, пустой ввод без вызова LLM, мусор
безопасно, fallback при сбое LLM, field-salvage, to_patch (не кладёт пустые — мердж не затирает).
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clients.profile_extract as PE  # noqa: E402
from clients.profile_extract import ClientProfileExtract, extract_profile, structure_crawl  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


def _fake_chat(content: str | None = None, *, raises: bool = False):
    async def _chat(messages, **kwargs):
        if raises:
            raise RuntimeError("LLM down")
        return SimpleNamespace(content=content)

    return _chat


def _kasi_json() -> str:
    return json.dumps(
        {
            "brand": "Kasi Motors",
            "business_desc": "автодилер, поддержанные авто, Найроби, Кения",
            "geo": "Кения, Найроби",
            "language": "English, Swahili",
            "website": "kasimotors.co.ke",
            "socials": {"instagram": "@kasimotors"},
            "notes": "упор на гарантию 12 мес",
            "services": [{"name": "Седаны", "price": "от $4000", "category": "авто"}],
            "contacts": [{"kind": "phone", "value": "+254 712 345 678"}],
        }
    )


@pytest.mark.asyncio
async def test_extract_parses_full_text():
    with patched(PE, "chat", _fake_chat(_kasi_json())):
        p = await extract_profile("Клиент Kasi Motors — автодилер в Найроби…")
    assert p.brand == "Kasi Motors"
    assert p.socials["instagram"] == "@kasimotors"
    assert p.services[0].name == "Седаны"
    assert p.contacts[0].kind == "phone"
    assert not p.is_empty()


@pytest.mark.asyncio
async def test_empty_input_skips_llm():
    called = {"n": 0}

    async def _boom(messages, **kwargs):
        called["n"] += 1
        raise AssertionError("LLM не должна вызываться на пустом вводе")

    with patched(PE, "chat", _boom):
        p = await extract_profile("   ")
    assert p.is_empty()
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_garbage_is_safe():
    with patched(PE, "chat", _fake_chat("не json вообще")):
        p = await extract_profile("что-то")
    assert p.is_empty()  # мусор → пустой объект, без исключений


@pytest.mark.asyncio
async def test_llm_failure_fallbacks_to_empty():
    with patched(PE, "chat", _fake_chat(raises=True)):
        p = await extract_profile("Клиент X")
    assert p.is_empty()


@pytest.mark.asyncio
async def test_field_salvage_on_partial_garbage():
    # валиден brand, но services — мусорной формы (число): salvage оставит валидные ключи
    content = json.dumps({"brand": "Acme", "services": "не список"})
    with patched(PE, "chat", _fake_chat(content)):
        p = await extract_profile("текст")
    assert p.brand == "Acme"
    assert p.services == []


def test_to_patch_omits_empty():
    p = ClientProfileExtract(brand="Acme", business_desc=None, services=[])
    patch = p.to_patch()
    assert patch == {"brand": "Acme"}  # пустые/None не кладём — мердж §20.5 не затрёт


@pytest.mark.asyncio
async def test_structure_crawl_injects_website():
    # модель не вернула website → structure_crawl подставит переданный
    content = json.dumps({"brand": "Kasi Motors", "services": []})
    with patched(PE, "chat", _fake_chat(content)):
        p = await structure_crawl(pages_text="страницы сайта…", website="kasimotors.com")
    assert p.website == "kasimotors.com"
