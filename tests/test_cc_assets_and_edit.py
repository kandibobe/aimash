"""§19.7.2 автогенерация ассетов + §19.8 правка финала командой замены «X на Y» (офлайн)."""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import adcopy.assets_gen as AG  # noqa: E402
from agent.campaign_edit import apply_text_replace, parse_replace_edit  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


def _chat(content):
    async def _c(messages, **kw):
        return SimpleNamespace(content=content)

    return _c


# ── assets_gen ────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_generate_callouts_validates_length():
    content = json.dumps(["Гарантия 12 мес", "Trade-in", "С" * 30])  # третий >25 → отброшен
    with patched(AG, "chat", _chat(content)):
        spec = await AG.generate_asset("callouts", topic="авто")
    assert spec["family"] == "callouts"
    assert "С" * 30 not in spec["params"]["callouts"]
    assert "Trade-in" in spec["params"]["callouts"]


@pytest.mark.asyncio
async def test_generate_sitelinks_uses_campaign_url():
    content = json.dumps([{"link_text": "Каталог", "description1": "Все авто"}])
    with patched(AG, "chat", _chat(content)):
        spec = await AG.generate_asset("sitelinks", topic="авто", url="https://shop.example")
    sl = spec["params"]["sitelinks"][0]
    assert sl["link_text"] == "Каталог"
    assert sl["final_url"] == "https://shop.example"


@pytest.mark.asyncio
async def test_generate_snippets_rejects_bad_header():
    content = json.dumps({"header": "НеИзСписка", "values": ["Sedan", "SUV", "Hatchback"]})
    with patched(AG, "chat", _chat(content)):
        with pytest.raises(ValueError):
            await AG.generate_asset("structured_snippets", topic="авто")


@pytest.mark.asyncio
async def test_generate_snippets_ok():
    content = json.dumps({"header": "Models", "values": ["Sedan", "SUV", "Hatchback", "Pickup"]})
    with patched(AG, "chat", _chat(content)):
        spec = await AG.generate_asset("structured_snippets", topic="авто")
    assert spec["params"]["header"] == "Models"
    assert len(spec["params"]["values"]) >= 3


@pytest.mark.asyncio
async def test_generate_unsupported_family_raises():
    with pytest.raises(ValueError):
        await AG.generate_asset("call", topic="авто")


# ── campaign_edit: парс «X на Y» + применение с ре-валидацией ─────────────────────
def test_parse_replace_variants():
    assert parse_replace_edit("Смени телефон с +380128 на +380999") == ("+380128", "+380999")
    assert parse_replace_edit('смени описание "А" на "Б"') == ("А", "Б")
    assert parse_replace_edit("change Topdealer to Bestdealer") == ("Topdealer", "Bestdealer")
    assert parse_replace_edit("поставь бюджет 60") is None


def test_apply_replace_across_fields_and_revalidates():
    st = {
        "ad": {
            "headlines": ["Топовый автодилер", "Авто"],
            "descriptions": ["У нас Топовый автодилер рядом"],
            "final_url": "https://x",
            "path1": None,
            "path2": None,
        },
        "assets": {"new": [{"family": "callouts", "params": {"callouts": ["Топовый автодилер"]}}]},
        "url_options": {},
    }
    n = apply_text_replace(st, "Топовый автодилер", "Лучший автодилер")
    assert n == 3
    assert st["ad"]["headlines"][0] == "Лучший автодилер"
    assert st["assets"]["new"][0]["params"]["callouts"][0] == "Лучший автодилер"


def test_apply_replace_skips_overlong_result():
    # замена, дающая заголовок >30 (кириллица=1) — поле НЕ трогаем
    st = {"ad": {"headlines": ["Авто Кения"], "descriptions": []}, "assets": {}, "url_options": {}}
    n = apply_text_replace(st, "Авто", "А" * 28)  # → "А"*28+" Кения" = 34 > 30
    assert n == 0
    assert st["ad"]["headlines"][0] == "Авто Кения"


def test_apply_replace_revalidates_asset_length():
    # callout лимит 25: замена, выводящая за лимит, не применяется к ассету
    st = {
        "ad": {"headlines": [], "descriptions": []},
        "assets": {"new": [{"family": "callouts", "params": {"callouts": ["Гарантия"]}}]},
        "url_options": {},
    }
    n = apply_text_replace(st, "Гарантия", "Г" * 30)  # 30 > 25 → откат
    assert n == 0
    assert st["assets"]["new"][0]["params"]["callouts"][0] == "Гарантия"
    # в пределах лимита — применяется
    n2 = apply_text_replace(st, "Гарантия", "Гарантия 12 мес")  # 15 ≤ 25
    assert n2 == 1
    assert st["assets"]["new"][0]["params"]["callouts"][0] == "Гарантия 12 мес"
