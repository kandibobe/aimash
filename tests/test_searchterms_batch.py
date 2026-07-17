"""3.2а: батч минус-слов чекбоксами в /searchterms.

Что стережём (страховка UI поверх денежного периметра, сами гейты — в test_write_layer):
- тогглы/циклы (чекбокс, тип соответствия, уровень, пикер списка) НЕ минтят черновиков — только
  server-side состояние + перерисовка markup;
- «Минусовать выбранные» минтит черновик(и) ТОЛЬКО через _build_proposal (валидация схемой) и
  показывает через _present_proposal с customer_id аккаунта, с которого термины ПРОЧИТАНЫ
  («мутируем то, что видим») — не активного на момент клика;
- группировка пакетов: кампания/группа — по одному черновику на кампанию (схема привязана к
  одной), общий список — ОДИН черновик на весь пакет (add_negatives_to_shared_set);
- анти-stale: старый gen (включая легаси-кнопки action='neg') → alert, ноль черновиков.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402
from bot.callbacks import SearchTermsCB  # noqa: E402
from bot.keyboards import searchterms_kb, searchterms_sharedset_kb  # noqa: E402

ACCT = "7753643025"


class FakeMessage:
    def __init__(self, chat_id: int = 100):
        self.chat = type("C", (), {"id": chat_id})()
        self.answers: list = []
        self.markups: list = []

    async def answer(self, text: str = "", **kw):
        self.answers.append((text, kw))
        return self

    async def edit_text(self, text: str = "", **kw):
        return self

    async def edit_reply_markup(self, reply_markup=None):
        self.markups.append(reply_markup)
        return self


class FakeCallbackQuery:
    def __init__(self, message, uid: int = 100):
        self.message = message
        self.from_user = type("U", (), {"id": uid})()
        self.answers: list = []

    async def answer(self, text: str = "", show_alert: bool = False, **kw):
        self.answers.append((text, show_alert))


def _items() -> list[dict]:
    return [
        {
            "term": "бесплатно скачать",
            "campaign": "Окна",
            "ad_group": "Общие",
            "cost": 5.0,
            "clicks": 7,
        },
        {
            "term": "своими руками",
            "campaign": "Окна",
            "ad_group": "Бренд",
            "cost": 3.0,
            "clicks": 4,
        },
        {
            "term": "вакансии монтажник",
            "campaign": "Двери",
            "ad_group": "Общие",
            "cost": 2.0,
            "clicks": 3,
        },
    ]


def _flat(markup) -> list:
    return [b for row in markup.inline_keyboard for b in row]


def _setup(chat_id: int, *, acct: str = ACCT) -> int:
    return bm._searchterms_store(chat_id, _items(), None, acct=acct)


# ── клавиатуры ───────────────────────────────────────────────────────────────────
def test_searchterms_kb_marks_selection_and_counts():
    kb = searchterms_kb(_items(), 1, selected={0, 2}, opts={"mt": "exact", "lvl": "campaign"})
    texts = [b.text for b in _flat(kb)]
    assert texts[0].startswith("✅") and texts[1].startswith("⬜") and texts[2].startswith("✅")
    assert any("(2)" in t for t in texts), "кнопка «Минусовать выбранные» без счётчика выбора"
    assert any("точное" in t for t in texts)  # цикл типа соответствия
    assert not any("📋" in t for t in texts)  # строка списка — только на уровне 'shared'
    # тап по строке — toggle, НЕ мутационный экшен
    cbs = [b.callback_data for b in _flat(kb)]
    assert cbs[0] == SearchTermsCB(action="toggle", idx=0, gen=1).pack()


def test_searchterms_kb_shared_level_adds_list_row():
    kb = searchterms_kb(
        _items(), 1, selected=set(), opts={"mt": "broad", "lvl": "shared", "ss": "Мой список"}
    )
    texts = [b.text for b in _flat(kb)]
    assert any("📋" in t and "Мой список" in t for t in texts)
    assert any("широкое" in t for t in texts)


def test_sharedset_kb_lists_choices_new_and_back():
    kb = searchterms_sharedset_kb([{"name": "Брендозащита", "member_count": 12}], 3)
    flat = _flat(kb)
    assert "Брендозащита" in flat[0].text and "(12)" in flat[0].text
    assert flat[0].callback_data == SearchTermsCB(action="ss_pick", idx=0, gen=3).pack()
    assert flat[1].callback_data == SearchTermsCB(action="ss_pick", idx=-1, gen=3).pack()  # новый
    assert flat[-1].callback_data == SearchTermsCB(action="ss_back", gen=3).pack()


# ── тогглы: только состояние + markup, никаких черновиков ────────────────────────
async def test_toggle_flips_selection_and_redraws():
    from bot.handlers.reports import on_searchterms_toggle

    chat_id = 401
    gen = _setup(chat_id)
    cq = FakeCallbackQuery(FakeMessage(chat_id))
    await on_searchterms_toggle(cq, SearchTermsCB(action="toggle", idx=1, gen=gen))
    assert bm._SEARCH_TERMS_SEL[chat_id] == {1}
    assert cq.message.markups, "markup не перерисован"
    await on_searchterms_toggle(cq, SearchTermsCB(action="toggle", idx=1, gen=gen))
    assert bm._SEARCH_TERMS_SEL[chat_id] == set()


async def test_toggle_stale_gen_alerts_and_keeps_state():
    from bot.handlers.reports import on_searchterms_toggle

    chat_id = 402
    gen = _setup(chat_id)
    cq = FakeCallbackQuery(FakeMessage(chat_id))
    # легаси-кнопка action='neg' из старого сообщения: gen прошлого поколения
    await on_searchterms_toggle(cq, SearchTermsCB(action="neg", idx=0, gen=gen - 1))
    assert cq.answers and cq.answers[-1][1] is True  # show_alert «список устарел»
    assert bm._SEARCH_TERMS_SEL[chat_id] == set()
    assert not cq.message.markups


async def test_mt_and_lvl_cycles():
    from bot.handlers.reports import on_searchterms_lvl, on_searchterms_mt

    chat_id = 403
    gen = _setup(chat_id)
    cq = FakeCallbackQuery(FakeMessage(chat_id))
    assert bm._SEARCH_TERMS_OPTS[chat_id]["mt"] == "exact"  # дефолт прежней per-term кнопки
    await on_searchterms_mt(cq, SearchTermsCB(action="mt", gen=gen))
    assert bm._SEARCH_TERMS_OPTS[chat_id]["mt"] == "phrase"
    await on_searchterms_mt(cq, SearchTermsCB(action="mt", gen=gen))
    await on_searchterms_mt(cq, SearchTermsCB(action="mt", gen=gen))
    assert bm._SEARCH_TERMS_OPTS[chat_id]["mt"] == "exact"  # полный круг

    await on_searchterms_lvl(cq, SearchTermsCB(action="lvl", gen=gen))
    assert bm._SEARCH_TERMS_OPTS[chat_id]["lvl"] == "adgroup"
    await on_searchterms_lvl(cq, SearchTermsCB(action="lvl", gen=gen))
    o = bm._SEARCH_TERMS_OPTS[chat_id]
    assert o["lvl"] == "shared" and o["ss"], "вход в 'shared' обязан подставить дефолтное имя"


async def test_ss_pick_existing_and_new():
    from bot.handlers.reports import on_searchterms_ss_pick

    chat_id = 404
    gen = _setup(chat_id)
    bm._SEARCH_TERMS_OPTS[chat_id]["ss_choices"] = [{"name": "Брендозащита", "member_count": 2}]
    cq = FakeCallbackQuery(FakeMessage(chat_id))
    await on_searchterms_ss_pick(cq, SearchTermsCB(action="ss_pick", idx=0, gen=gen))
    o = bm._SEARCH_TERMS_OPTS[chat_id]
    assert o["ss"] == "Брендозащита" and o["lvl"] == "shared"
    await on_searchterms_ss_pick(cq, SearchTermsCB(action="ss_pick", idx=-1, gen=gen))
    assert o["ss"] == bm.i18n.t("searchterms_ss_default_name")


async def test_ss_open_fetches_choices_and_renders_picker(monkeypatch):
    from bot.handlers.reports import on_searchterms_ss_open

    chat_id = 405
    gen = _setup(chat_id)
    lists = [{"id": "77", "name": "Брендозащита", "status": "ENABLED", "member_count": 2}]

    async def _fake_read(fn, client, acct, label=""):
        assert acct == ACCT  # читаем списки ТОГО аккаунта, с которого собраны термины
        return lists

    import ads.client as ads_client

    async def _fake_client(acct):
        return object()

    monkeypatch.setattr(ads_client, "build_client_async", _fake_client)
    monkeypatch.setattr(bm, "run_ads_read_call", _fake_read)
    cq = FakeCallbackQuery(FakeMessage(chat_id))
    await on_searchterms_ss_open(cq, SearchTermsCB(action="ss_open", gen=gen))
    assert bm._SEARCH_TERMS_OPTS[chat_id]["ss_choices"] == lists
    assert cq.message.markups, "пикер не показан"


# ── apply: черновики только через _build_proposal + _present_proposal ────────────
async def _run_apply(chat_id: int, sel: set[int], opts_patch: dict, monkeypatch) -> list[dict]:
    from bot.handlers.reports import on_searchterms_apply

    gen = _setup(chat_id)
    bm._SEARCH_TERMS_SEL[chat_id] = set(sel)
    bm._SEARCH_TERMS_OPTS[chat_id].update(opts_patch)
    presented: list[dict] = []

    async def _fake_present(message, **kw):
        presented.append(kw)

    async def _fail_active(*a, **kw):
        raise AssertionError("батч с известным аккаунтом чтения не должен гадать активный")

    monkeypatch.setattr(bm, "_present_proposal", _fake_present)
    monkeypatch.setattr(bm, "_present_proposal_active", _fail_active)
    cq = FakeCallbackQuery(FakeMessage(chat_id))
    await on_searchterms_apply(cq, SearchTermsCB(action="apply", gen=gen))
    return presented


async def test_apply_empty_selection_alerts(monkeypatch):
    presented = await _run_apply(406, set(), {}, monkeypatch)
    assert presented == []


async def test_apply_campaign_level_one_proposal_per_campaign(monkeypatch):
    presented = await _run_apply(407, {0, 1, 2}, {"mt": "phrase", "lvl": "campaign"}, monkeypatch)
    assert len(presented) == 2  # «Окна» (2 запроса) + «Двери» (1)
    by_camp = {p["params"]["campaign"]: p for p in presented}
    assert by_camp["Окна"]["operation"] == "add_negative_keywords"
    assert by_camp["Окна"]["params"]["keywords"] == ["бесплатно скачать", "своими руками"]
    assert by_camp["Окна"]["params"]["ad_group"] is None  # уровень кампании — без сужения
    assert by_camp["Двери"]["params"]["keywords"] == ["вакансии монтажник"]
    assert all(p["params"]["match_type"] == "phrase" for p in presented)
    assert all(p["customer_id"] == ACCT for p in presented)  # «мутируем то, что видим»
    assert bm._SEARCH_TERMS_SEL[407] == set()  # выбор израсходован


async def test_apply_adgroup_level_groups_by_group(monkeypatch):
    presented = await _run_apply(408, {0, 1}, {"lvl": "adgroup"}, monkeypatch)
    assert len(presented) == 2  # одна кампания, но РАЗНЫЕ группы → по черновику на группу
    groups = {p["params"]["ad_group"] for p in presented}
    assert groups == {"Общие", "Бренд"}
    assert all(p["params"]["campaign"] == "Окна" for p in presented)


async def test_apply_shared_level_single_proposal(monkeypatch):
    presented = await _run_apply(
        409, {0, 1, 2}, {"lvl": "shared", "ss": "Брендозащита", "mt": "broad"}, monkeypatch
    )
    assert len(presented) == 1  # общий список аккаунта — ОДИН черновик на весь пакет
    p = presented[0]
    assert p["operation"] == "add_negatives_to_shared_set"
    assert p["params"]["shared_set"] == "Брендозащита"
    assert p["params"]["keywords"] == [
        "бесплатно скачать",
        "своими руками",
        "вакансии монтажник",
    ]
    assert p["params"]["match_type"] == "broad"
    assert p["customer_id"] == ACCT


async def test_apply_stale_gen_mints_nothing(monkeypatch):
    from bot.handlers.reports import on_searchterms_apply

    chat_id = 410
    gen = _setup(chat_id)
    bm._SEARCH_TERMS_SEL[chat_id] = {0}
    presented: list = []

    async def _fake_present(message, **kw):
        presented.append(kw)

    monkeypatch.setattr(bm, "_present_proposal", _fake_present)
    cq = FakeCallbackQuery(FakeMessage(chat_id))
    await on_searchterms_apply(cq, SearchTermsCB(action="apply", gen=gen - 1))
    assert presented == [] and cq.answers[-1][1] is True
