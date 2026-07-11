"""C1: кап сид-ключей до лимита Google (KeywordSeed/KeywordAndUrlSeed: «no more than 20 keywords»).

Раньше капа не было НИГДЕ: 30 фраз в /keywords → InvalidArgument на весь запрос → «не удалось
подобрать» (и fallback на Draft повторял тот же битый запрос). Теперь три рубежа: схема тула,
захват в UI (с честным сообщением об усечении) и граница SDK.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.handlers.keywords_flow as kwf  # noqa: E402
import bot.main as bm  # noqa: E402
from agent.tools.schemas import KeywordResearch  # noqa: E402
from ads.keyword_plan import MAX_SEEDS, generate_keyword_ideas  # noqa: E402
from core.config import settings  # noqa: E402

DRAFT = "7753643025"


class _NS(SimpleNamespace):
    def __getattr__(self, name):
        val = _NS()
        setattr(self, name, val)
        return val


def _client(captured: dict):
    class _Svc:
        def generate_keyword_ideas(self, request):
            captured["request"] = request
            return []

    class _Req(_NS):
        def __init__(self):
            super().__init__()
            self.geo_target_constants = []
            self.keyword_seed = SimpleNamespace(keywords=[])
            self.url_seed = SimpleNamespace(url="")
            self.keyword_and_url_seed = SimpleNamespace(url="", keywords=[])

    class _Client:
        enums = _NS()

        def get_service(self, name):
            return _Svc()

        def get_type(self, name):
            return _Req()

    return _Client()


class _Msg:
    def __init__(self, text: str = "") -> None:
        self.chat = SimpleNamespace(id=100)
        self.text = text
        self.answers: list[str] = []

    async def answer(self, text: str = "", **kw):
        self.answers.append(text)
        return self


class _State:
    def __init__(self, data: dict | None = None) -> None:
        self._data = dict(data or {})

    async def clear(self):
        self._data = {}

    async def set_state(self, s):
        pass

    async def update_data(self, **kw):
        self._data.update(kw)

    async def get_data(self):
        return dict(self._data)


@pytest.fixture(autouse=True)
def _allow_read(monkeypatch):
    monkeypatch.setattr(settings, "google_ads_allowed_customer_ids", DRAFT)


def test_sdk_boundary_caps_seeds_to_google_limit():
    """Последний рубеж: даже если сиды пришли мимо UI (агент/скрипт), в запрос уедет ≤20."""
    captured: dict = {}
    generate_keyword_ideas(
        _client(captured), DRAFT, seeds=[f"ключ {i}" for i in range(30)], language="ru"
    )
    assert len(captured["request"].keyword_seed.keywords) == MAX_SEEDS


def test_schema_rejects_more_seeds_than_google_accepts():
    KeywordResearch(seeds=[f"s{i}" for i in range(MAX_SEEDS)])  # ровно лимит — ок
    with pytest.raises(ValueError):  # 21-й сид ⇒ батч отвергается Google целиком
        KeywordResearch(seeds=[f"s{i}" for i in range(MAX_SEEDS + 1)])


@pytest.mark.asyncio
async def test_kw_seeds_handler_caps_and_says_so(monkeypatch):
    """Визард: 25 сид-слов → в подбор уходит 20, пользователю сказано, что список усечён."""
    shown: list = []
    monkeypatch.setattr(kwf, "_kw_present_params", lambda m, s: shown.append(1) or _noop())
    m = _Msg("\n".join(f"ключ {i}" for i in range(25)))
    state = _State()
    await kwf.kw_seeds(m, state)
    assert len((await state.get_data())["kw_seeds"]) == MAX_SEEDS
    assert any(str(MAX_SEEDS) in a and "25" in a for a in m.answers)


@pytest.mark.asyncio
async def test_kw_params_text_caps_merged_seeds(monkeypatch):
    """Досыл сидов на экране параметров: кап считается по СУММЕ (было kw_seeds + присланные)."""
    monkeypatch.setattr(kwf, "_kw_present_params", lambda m, s: _noop())
    m = _Msg("\n".join(f"новый {i}" for i in range(15)))
    state = _State({"kw_seeds": [f"старый {i}" for i in range(15)]})
    await kwf.kw_params_text(m, state)
    seeds = (await state.get_data())["kw_seeds"]
    assert len(seeds) == MAX_SEEDS
    assert seeds[0] == "старый 0"  # порядок сохранён: сначала уже принятые
    assert any("30" in a for a in m.answers)  # честно сказали, сколько было


@pytest.mark.asyncio
async def test_no_cap_message_when_within_limit(monkeypatch):
    monkeypatch.setattr(kwf, "_kw_present_params", lambda m, s: _noop())
    m = _Msg("ремонт ноутбуков\nчистка ноутбука")
    state = _State()
    await kwf.kw_seeds(m, state)
    assert (await state.get_data())["kw_seeds"] == ["ремонт ноутбуков", "чистка ноутбука"]
    assert not m.answers  # ничего не усекали — молчим


async def _noop() -> None:
    return None


def test_i18n_cap_key_present_in_both_langs():
    for lang in ("ru", "en"):
        txt = bm.i18n.CATALOG["kw_seeds_capped"][lang]
        assert "{n}" in txt and "{total}" in txt
