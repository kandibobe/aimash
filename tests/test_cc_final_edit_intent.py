"""3B: footgun финальной правки §19.8 — «смени бюджет с 40 на 60» больше НЕ портит тексты.

Раньше literal replace шёл первым: «40»→«60» било по заголовку «Скидка 40%», а бюджет не менялся.
Теперь settings-маркеры/числовые пары → сначала LLM-извлечение; чистый текст → literal replace
без LLM; пусто+пара → фолбэк literal; оба мимо → честное «не понял».
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402
from agent.campaign_edit import is_numeric_pair, is_settings_edit  # noqa: E402
from agent.campaign_settings import CampaignSettings  # noqa: E402
from db.session import init_db  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


class FakeMessage:
    def __init__(self, chat_id: int, text: str):
        self.chat = SimpleNamespace(id=chat_id)
        self.text = text
        self.answers: list = []

    async def answer(self, text: str = "", **kw):
        self.answers.append(text)
        return self


class FakeState:
    def __init__(self, data=None):
        self._data = dict(data or {})

    async def get_data(self):
        return dict(self._data)

    async def clear(self):
        self._data = {}


async def _mk_draft(chat_id: int) -> str:
    sid = await bm.CDRAFTS.create(chat_id=chat_id, customer_id=bm.DRAFT_ACCOUNT_ID)
    await bm.CDRAFTS.patch(
        sid,
        lambda st: (
            st["settings"].update({"campaign_name": "Тест", "budget_daily_micros": 40_000_000}),
            st["ad"].update(
                {
                    "final_url": "https://ex.com",
                    "headlines": ["Скидка 40%", "Быстро и надёжно", "Купить авто"],
                    "descriptions": ["Описание один", "Описание два"],
                }
            ),
        ),
        expected_chat_id=chat_id,
    )
    return sid


async def _resummarize_noop(m, sid, chat_id):
    return None


def test_markers_and_numeric_pair():
    assert is_settings_edit("смени бюджет с 40 на 60")
    assert is_settings_edit("change the budget to 60")
    assert is_settings_edit("добавь город Найроби")
    assert not is_settings_edit("смени „Быстро и надёжно“ на „Надёжно и быстро“")
    assert is_numeric_pair("40", "60") and is_numeric_pair("40.5$", "60,0")
    assert not is_numeric_pair("v2.0", "v3.0") and not is_numeric_pair("40%", "60")


async def test_budget_edit_goes_to_settings_not_literal(monkeypatch):
    """«смени бюджет с 40 на 60» → бюджет обновлён через settings, заголовок «Скидка 40%» ЦЕЛ."""
    await init_db()
    chat_id = 92_001
    sid = await _mk_draft(chat_id)

    async def _fake_extract(text, language="ru"):
        return CampaignSettings(budget_daily_units=60.0)

    m = FakeMessage(chat_id, "смени бюджет с 40 на 60")
    with (
        patched(bm, "extract_campaign_settings", _fake_extract),
        patched(bm, "_cc_resummarize", _resummarize_noop),
    ):
        await bm.cc_final_edit(m, FakeState({"cc_session": sid}))

    snap = await bm.CDRAFTS.get(sid, expected_chat_id=chat_id)
    assert snap.wizard_state["ad"]["headlines"][0] == "Скидка 40%"  # текст НЕ тронут
    assert snap.wizard_state["settings"]["budget_daily_micros"] == 60_000_000  # бюджет обновлён


async def test_pure_text_replace_skips_llm(monkeypatch):
    """Чистая текстовая замена идёт literal-путём БЕЗ вызова LLM."""
    await init_db()
    chat_id = 92_002
    sid = await _mk_draft(chat_id)

    async def _boom(text, language="ru"):
        raise AssertionError("LLM не должен вызываться для чистой текстовой замены")

    m = FakeMessage(chat_id, "смени «Быстро и надёжно» на «Надёжно и быстро»")
    with (
        patched(bm, "extract_campaign_settings", _boom),
        patched(bm, "_cc_resummarize", _resummarize_noop),
    ):
        await bm.cc_final_edit(m, FakeState({"cc_session": sid}))

    snap = await bm.CDRAFTS.get(sid, expected_chat_id=chat_id)
    assert "Надёжно и быстро" in snap.wizard_state["ad"]["headlines"]


async def test_empty_patch_with_marker_falls_back_to_literal(monkeypatch):
    """Settings-маркер, но извлечение пустое → фолбэк на literal («смени цену с 40 на 60» в
    price-контексте текста)."""
    await init_db()
    chat_id = 92_003
    sid = await _mk_draft(chat_id)
    await bm.CDRAFTS.patch(
        sid,
        lambda st: st["ad"]["descriptions"].__setitem__(0, "Цена от 40 долларов"),
        expected_chat_id=chat_id,
    )

    async def _empty(text, language="ru"):
        return CampaignSettings()

    m = FakeMessage(chat_id, "смени цену с 40 на 60")
    with (
        patched(bm, "extract_campaign_settings", _empty),
        patched(bm, "_cc_resummarize", _resummarize_noop),
    ):
        await bm.cc_final_edit(m, FakeState({"cc_session": sid}))

    snap = await bm.CDRAFTS.get(sid, expected_chat_id=chat_id)
    assert snap.wizard_state["ad"]["descriptions"][0] == "Цена от 60 долларов"


async def test_both_paths_miss_says_not_understood(monkeypatch):
    """Ни настройки, ни замена → честное «не понял» (не пере-сводка с иллюзией правки)."""
    await init_db()
    chat_id = 92_004
    sid = await _mk_draft(chat_id)

    async def _empty(text, language="ru"):
        return CampaignSettings()

    called = {"n": 0}

    async def _resum(m, s, c):
        called["n"] += 1

    m = FakeMessage(chat_id, "сделай красиво пожалуйста")
    with (
        patched(bm, "extract_campaign_settings", _empty),
        patched(bm, "_cc_resummarize", _resum),
    ):
        await bm.cc_final_edit(m, FakeState({"cc_session": sid}))

    assert called["n"] == 0  # пере-сводки не было
    assert any("Не понял" in a or "Couldn" in a for a in m.answers)
