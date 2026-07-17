"""§8: быстрые пути при НЕвыбранном аккаунте и нескольких живых показывают ПИКЕР, а не пустой Draft.

_require_read_account — единая развилка быстрых путей (/report N, /export, /sheets, /campaigns,
NL-статистика): пин Draft / один живой (авто-дефолт) / ноль живых → прежний одношаговый путь;
NULL-выбор + >1 живого → подсказка pick_live_account_first + flow-пикер, чтение НЕ выполняется.
Мутационный замок не затрагивается (тесты замка — tests/test_safety_core.py).
"""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402


class _FakeMsg:
    def __init__(self, chat_id: int = 778001):
        self.answers: list = []
        self.chat = SimpleNamespace(id=chat_id)

    async def answer(self, text="", **kw):
        self.answers.append((text, kw))


async def test_require_returns_active_nondraft_without_picker(monkeypatch):
    async def _fake_active(_chat):
        return "1112223334"

    monkeypatch.setattr(bm, "_active_read_account", _fake_active)
    msg = _FakeMsg()
    assert await bm._require_read_account(msg, "report") == "1112223334"
    assert msg.answers == []  # одношаговый путь, никакого пикера


async def test_require_draft_not_pending_returns_draft(monkeypatch):
    """Пин Draft/ноль живых (pending=False) → прежнее поведение: Draft без пикера."""
    import core.access as ca

    async def _fake_active(_chat):
        return bm.DRAFT_ACCOUNT_ID

    async def _fake_pending(_chat):
        return False

    monkeypatch.setattr(bm, "_active_read_account", _fake_active)
    monkeypatch.setattr(ca, "account_choice_pending", _fake_pending)
    msg = _FakeMsg()
    assert await bm._require_read_account(msg, "report") == bm.DRAFT_ACCOUNT_ID
    assert msg.answers == []


async def test_require_pending_shows_report_picker_and_stops(monkeypatch):
    import core.access as ca

    async def _fake_active(_chat):
        return bm.DRAFT_ACCOUNT_ID

    async def _fake_pending(_chat):
        return True

    started: list = []

    async def _fake_picker(m, target):
        started.append(target)

    monkeypatch.setattr(bm, "_active_read_account", _fake_active)
    monkeypatch.setattr(ca, "account_choice_pending", _fake_pending)
    monkeypatch.setattr(bm, "_start_report_picker", _fake_picker)
    msg = _FakeMsg()
    assert await bm._require_read_account(msg, "report") is None  # чтение НЕ выполняется
    assert started == ["report"]
    assert msg.answers, "должна уйти подсказка pick_live_account_first"


async def test_require_pending_campaigns_flow_uses_campaigns_picker(monkeypatch):
    import core.access as ca

    async def _fake_active(_chat):
        return bm.DRAFT_ACCOUNT_ID

    async def _fake_pending(_chat):
        return True

    started: list = []

    async def _fake_camp_picker(m):
        started.append("campaigns")

    monkeypatch.setattr(bm, "_active_read_account", _fake_active)
    monkeypatch.setattr(ca, "account_choice_pending", _fake_pending)
    monkeypatch.setattr(bm, "_start_campaigns_picker", _fake_camp_picker)
    msg = _FakeMsg()
    assert await bm._require_read_account(msg, "campaigns") is None
    assert started == ["campaigns"]


async def test_send_campaigns_stops_on_pending_picker(monkeypatch):
    """/campaigns при pending: список НЕ читается (нет SDK-вызова), якорь не трогается."""
    import core.access as ca

    chat = 778002

    async def _fake_active(_chat):
        return bm.DRAFT_ACCOUNT_ID

    async def _fake_pending(_chat):
        return True

    async def _fake_camp_picker(m):
        pass

    async def _boom(*a, **kw):  # чтение кампаний не должно случиться
        raise AssertionError("list_campaigns не должен вызываться при pending-пикере")

    monkeypatch.setattr(bm, "_active_read_account", _fake_active)
    monkeypatch.setattr(ca, "account_choice_pending", _fake_pending)
    monkeypatch.setattr(bm, "_start_campaigns_picker", _fake_camp_picker)
    monkeypatch.setattr(bm, "run_ads_read_call", _boom)
    bm._CAMP_ACCT.pop(chat, None)
    msg = _FakeMsg(chat)
    await bm._send_campaigns(msg, chat)
    assert chat not in bm._CAMP_ACCT  # якорь не выставлен — чтения не было


async def test_dispatch_need_account_shows_status_picker(monkeypatch):
    """Ветка need_account из agent-loop: одно сообщение с подсказкой + клавиатурой (target=status)."""
    rows = [
        SimpleNamespace(id=bm.DRAFT_ACCOUNT_ID, name="Draft", currency="", status="ENABLED"),
        SimpleNamespace(id="1112223334", name="Башня", currency="UAH", status="ENABLED"),
        SimpleNamespace(id="9998887776", name="DARIAL", currency="USD", status="ENABLED"),
    ]

    async def _fake_rows(_chat):
        return rows

    async def _fake_last(_chat):
        return None

    monkeypatch.setattr(bm, "_read_account_rows", _fake_rows)
    monkeypatch.setattr(bm, "_last_account", _fake_last)
    msg = _FakeMsg()
    await bm._dispatch_command_result(msg, {"type": "need_account"}, None)
    assert len(msg.answers) == 1
    text, kw = msg.answers[0]
    assert kw.get("reply_markup") is not None  # пикер приложен к самой подсказке
    assert "status" in str(kw["reply_markup"])  # target пикера — статистика после тапа


async def test_do_read_returns_need_account_when_pending(monkeypatch):
    """NL get_stats БЕЗ аккаунта при pending → {'type': 'need_account'} (бот-слой рисует пикер)."""
    import agent.loop as al
    import core.access as ca

    async def _pending(_chat):
        return True

    monkeypatch.setattr(ca, "account_choice_pending", _pending)
    res = await al._do_read("get_stats", {}, chat_id=1)
    assert res == {"type": "need_account"}


async def test_do_read_with_explicit_account_skips_pending(monkeypatch):
    """Аккаунт назван в запросе → pending НЕ проверяется, обычный резолв (тут — not found)."""
    import agent.loop as al
    import core.access as ca

    async def _pending(_chat):
        raise AssertionError("account_choice_pending не должен вызываться при явном аккаунте")

    async def _resolve(_chat, _arg):
        raise LookupError("нет такого")

    monkeypatch.setattr(ca, "account_choice_pending", _pending)
    monkeypatch.setattr(ca, "resolve_read_account", _resolve)
    res = await al._do_read("get_stats", {"account": "Башня"}, chat_id=1)
    assert res["type"] == "text"


# ── AD.3: форс-пикер аккаунта на МУТАЦИОННЫХ мятах (не угадываем чужие деньги) ──────────
async def test_present_proposal_active_forces_pick_when_pending(monkeypatch):
    """Активный не закреплён (Draft) + живых >1 → стэш черновика + пикер (target='mutate'),
    сам _present_proposal НЕ вызывается (мутация не минтится, пока аккаунт не выбран)."""
    import core.access as ca

    chat = 779001
    rows = [
        SimpleNamespace(id=bm.DRAFT_ACCOUNT_ID, name="Draft", currency="", status="ENABLED"),
        SimpleNamespace(id="5437782039", name="Башня", currency="UAH", status="ENABLED"),
    ]

    async def _fake_active(_chat):
        return bm.DRAFT_ACCOUNT_ID

    async def _fake_pending(_chat):
        return True

    async def _fake_rows(_chat):
        return rows

    async def _fake_last(_chat):
        return None

    async def _fake_freq(_chat, *a, **kw):
        return []

    async def _boom(*a, **kw):
        raise AssertionError("_present_proposal не должен минтиться при неоднозначном аккаунте")

    monkeypatch.setattr(bm, "_active_read_account", _fake_active)
    monkeypatch.setattr(ca, "account_choice_pending", _fake_pending)
    monkeypatch.setattr(bm, "_read_account_rows", _fake_rows)
    monkeypatch.setattr(bm, "_last_account", _fake_last)
    monkeypatch.setattr(bm, "_frequent_accounts", _fake_freq)
    monkeypatch.setattr(bm, "_present_proposal", _boom)
    bm._PENDING_MUT.pop(chat, None)
    msg = _FakeMsg(chat)
    await bm._present_proposal_active(
        msg,
        chat_id=chat,
        operation="pause_campaign",
        params={"campaign": "X"},
        summary="s",
        cid="cid779001",
    )
    assert chat in bm._PENDING_MUT and bm._PENDING_MUT[chat]["cid"] == "cid779001"
    assert len(msg.answers) == 1
    _text, kw = msg.answers[0]
    assert "mutate" in str(kw.get("reply_markup"))  # пикер целится в доигрывание мутации
    assert bm._pop_pending_mut(chat)["cid"] == "cid779001"  # одноразово
    assert bm._pop_pending_mut(chat) is None
    bm._PENDING_MUT.pop(chat, None)


async def test_present_proposal_active_mints_directly_when_pinned(monkeypatch):
    """Аккаунт закреплён (не-Draft) → сразу _present_proposal с этим customer_id, без пикера."""
    chat = 779002
    seen: dict = {}

    async def _fake_active(_chat):
        return "5437782039"

    async def _fake_present(message, **kw):
        seen.update(kw)

    monkeypatch.setattr(bm, "_active_read_account", _fake_active)
    monkeypatch.setattr(bm, "_present_proposal", _fake_present)
    bm._PENDING_MUT.pop(chat, None)
    msg = _FakeMsg(chat)
    await bm._present_proposal_active(
        msg,
        chat_id=chat,
        operation="pause_campaign",
        params={"campaign": "X"},
        summary="s",
        cid="cid779002",
    )
    assert seen.get("customer_id") == "5437782039"
    assert chat not in bm._PENDING_MUT  # ничего не отложено


# ── AD.2: баннер аккаунта на КАЖДОЙ карточке (включая Draft) ────────────────────────────
async def _capture_present_summary(monkeypatch, customer_id):
    """Прогнать _present_proposal с заглушками, вернуть summary, ушедший в save_proposal."""
    captured: dict = {}

    async def _fake_before(*a, **kw):
        return None

    async def _fake_save(**kw):
        captured.update(kw)

    monkeypatch.setattr(bm, "read_before", _fake_before)
    monkeypatch.setattr(bm.STORE, "save_proposal", _fake_save)
    monkeypatch.setattr(bm, "ensure_allowed", lambda cid: None)  # не-Draft проходит замок
    monkeypatch.setattr(bm.ux, "proposal_fits", lambda *_a, **_k: True)
    msg = _FakeMsg(779003)
    await bm._present_proposal(
        msg,
        chat_id=779003,
        operation="pause_campaign",
        params={"campaign": "X"},
        summary="s",
        cid="cidbanner",
        customer_id=customer_id,
    )
    return captured.get("summary", "")


async def test_confirm_card_shows_account_banner_for_draft(monkeypatch):
    """Draft теперь ТОЖЕ несёт баннер аккаунта (тихий фолбэк на Draft больше не невидим)."""
    summary = await _capture_present_summary(monkeypatch, bm.DRAFT_ACCOUNT_ID)
    assert bm.DRAFT_ACCOUNT_ID in summary  # ярлык «… · 7753643025» в сводке


async def test_confirm_card_shows_account_banner_for_child(monkeypatch):
    """Боевой аккаунт несёт баннер с его id (менеджер видит, на чьи деньги правка, до ✅)."""
    summary = await _capture_present_summary(monkeypatch, "5437782039")
    assert "5437782039" in summary


# ── AD.3 (волна 2.8): пошаговые мут-флоу — /newsearch и GDN — единственные были без пикера ─
def _patch_pending(monkeypatch, pending: bool):
    import core.access as ca

    async def _fake_active(_chat):
        return bm.DRAFT_ACCOUNT_ID

    async def _fake_pending(_chat):
        return pending

    async def _fake_rows(_chat):
        return [
            SimpleNamespace(id=bm.DRAFT_ACCOUNT_ID, name="Draft", currency="", status="ENABLED"),
            SimpleNamespace(id="5437782039", name="Башня", currency="UAH", status="ENABLED"),
        ]

    async def _fake_last(_chat):
        return None

    async def _fake_freq(_chat, *a, **kw):
        return []

    monkeypatch.setattr(bm, "_active_read_account", _fake_active)
    monkeypatch.setattr(ca, "account_choice_pending", _fake_pending)
    monkeypatch.setattr(bm, "_read_account_rows", _fake_rows)
    monkeypatch.setattr(bm, "_last_account", _fake_last)
    monkeypatch.setattr(bm, "_frequent_accounts", _fake_freq)


async def test_prompt_account_if_ambiguous_shows_setacct_picker(monkeypatch):
    """Не закреплён + живых >1 → пикер с target='setacct' (тап ПИНИТ аккаунт: следующий шаг флоу
    резолвит уже выбранный). Черновика ещё нет — стэшить, в отличие от AD.3-мяты, нечего."""
    _patch_pending(monkeypatch, True)
    msg = _FakeMsg(779010)
    assert await bm.prompt_account_if_ambiguous(msg, 779010) is True
    assert len(msg.answers) == 1
    _text, kw = msg.answers[0]
    assert "setacct" in str(kw.get("reply_markup"))


async def test_prompt_account_if_ambiguous_silent_when_not_pending(monkeypatch):
    """Пин Draft / один живой / ноль живых → молчим (прежнее поведение, лишний вопрос не задаём)."""
    _patch_pending(monkeypatch, False)
    msg = _FakeMsg(779011)
    assert await bm.prompt_account_if_ambiguous(msg, 779011) is False
    assert msg.answers == []


async def test_newsearch_cmd_asks_account_before_brief(monkeypatch):
    """/newsearch: пикер уходит ДО брифа, состояние визарда при этом ставится (бриф ждём следом)."""
    asked: list = []

    async def _fake_prompt(_m, chat_id):
        asked.append(chat_id)
        return True

    monkeypatch.setattr(bm, "prompt_account_if_ambiguous", _fake_prompt)

    class _State:
        def __init__(self):
            self._s = None

        async def clear(self):
            self._s = None

        async def set_state(self, s):
            self._s = s

    st = _State()
    msg = _FakeMsg(779012)
    await bm.newsearch_cmd(msg, st)
    assert asked == [779012]
    assert st._s == bm.SearchWizard.awaiting_brief.state
    assert msg.answers, "бриф всё равно спрашиваем (пикер не отменяет флоу)"


def test_gdn_entry_points_ask_account():
    """Оба входа в GDN-визард (фото и изображение-ДОКУМЕНТОМ) обязаны звать пикер: иначе бриф
    соберётся, а черновик уедет на Draft. Гард источника — офлайн-вызовы хендлеров это не ловят."""
    import inspect

    for fn in (bm.on_photo, bm.on_document):
        assert "prompt_account_if_ambiguous" in inspect.getsource(fn), (
            f"{fn.__name__}: GDN-ветка стартует визард без пикера аккаунта (AD.3)"
        )


# ── Замечание 4 (2026-07-17): «Кампании залочены на Draft» — прогрев discovery + выход ──
async def test_require_draft_warms_discovery_before_pending(monkeypatch):
    """Фикс B: решение «показывать ли пикер» (account_choice_pending) принималось по набору
    discovery, который на fail-quiet старте ПУСТ, а само-починка жила только ВНУТРИ пикера —
    замкнутый круг (пикер не показан → починка не вызвана). Прогрев обязан идти ПЕРЕД pending."""
    import ads.client as ac
    import core.access as ca

    order: list[str] = []

    async def _fake_active(_chat):
        return bm.DRAFT_ACCOUNT_ID

    async def _fake_discover(*a, **kw):
        order.append("discover")

    async def _fake_pending(_chat):
        order.append("pending")
        return False

    monkeypatch.setattr(bm, "_active_read_account", _fake_active)
    monkeypatch.setattr(ac, "ensure_read_children_discovered", _fake_discover)
    monkeypatch.setattr(ca, "account_choice_pending", _fake_pending)
    msg = _FakeMsg()
    assert await bm._require_read_account(msg, "campaigns") == bm.DRAFT_ACCOUNT_ID
    assert order == ["discover", "pending"], "прогрев discovery обязан идти ДО решения о пикере"


def _patch_campaign_read(monkeypatch, camps: list[dict]) -> None:
    """Заглушки SDK-чтения для _send_campaigns_for: клиент не строится, список — заданный."""
    import ads.client as ac

    async def _fake_client(_acct):
        return object()

    async def _fake_read(*a, **kw):
        return camps

    monkeypatch.setattr(ac, "build_client_async", _fake_client)
    monkeypatch.setattr(bm, "run_ads_read_call", _fake_read)


async def test_send_campaigns_draft_with_live_offers_switch(monkeypatch):
    """Фикс A: Draft (в т.ч. авто-пин _heal_if_stuck_global) при живых аккаунтах — не тупик:
    после списка уходит баннер + кнопка «Сменить аккаунт» (MoreCB action='campaigns_pick')."""
    chat = 778003
    _patch_campaign_read(monkeypatch, [{"id": 1, "name": "Test", "status": "ENABLED"}])
    monkeypatch.setattr(bm, "_live_account_hint", lambda _a: "есть живые аккаунты")
    msg = _FakeMsg(chat)
    try:
        await bm._send_campaigns_for(msg, chat, bm.DRAFT_ACCOUNT_ID)
        assert len(msg.answers) == 2  # список + баннер
        text, kw = msg.answers[1]
        assert "есть живые аккаунты" in text
        assert "campaigns_pick" in str(kw.get("reply_markup"))
    finally:
        bm._CAMP_CACHE.pop(chat, None)
        bm._CAMP_ACCT.pop(chat, None)


async def test_send_campaigns_draft_empty_merges_hint_with_switch(monkeypatch):
    """Пустой Draft: подсказка и кнопка «Сменить аккаунт» в ОДНОМ сообщении с «нет кампаний»."""
    chat = 778004
    _patch_campaign_read(monkeypatch, [])
    monkeypatch.setattr(bm, "_live_account_hint", lambda _a: "есть живые аккаунты")
    msg = _FakeMsg(chat)
    await bm._send_campaigns_for(msg, chat, bm.DRAFT_ACCOUNT_ID)
    assert len(msg.answers) == 1
    text, kw = msg.answers[0]
    assert "есть живые аккаунты" in text
    assert "campaigns_pick" in str(kw.get("reply_markup"))


async def test_send_campaigns_live_account_no_banner(monkeypatch):
    """На живом аккаунте баннера нет (_live_account_hint='' для не-Draft) — не шумим."""
    chat = 778005
    _patch_campaign_read(monkeypatch, [{"id": 1, "name": "Test", "status": "ENABLED"}])
    msg = _FakeMsg(chat)
    try:
        await bm._send_campaigns_for(msg, chat, "5437782039")
        assert len(msg.answers) == 1  # только список, без баннера
    finally:
        bm._CAMP_CACHE.pop(chat, None)
        bm._CAMP_ACCT.pop(chat, None)


async def test_more_campaigns_pick_routes_to_picker(monkeypatch):
    """Кнопка «🔄 Сменить аккаунт» (campaigns_switch_kb) маршрутизируется в _start_campaigns_picker:
    вне хаб-клавиатур test_hub_buttons_have_on_more_route её не покрывает — гардим точечно."""
    from bot.handlers import commands as cmd

    started: list = []

    async def _fake_picker(_m):
        started.append("campaigns")

    monkeypatch.setattr(bm, "_start_campaigns_picker", _fake_picker)
    msg = _FakeMsg(778006)
    cq = SimpleNamespace(message=msg, from_user=SimpleNamespace(id=778006, username="t"))

    async def _cq_answer(*a, **kw):
        return None

    cq.answer = _cq_answer
    await cmd.on_more(cq, bm.MoreCB(action="campaigns_pick"), None)
    assert started == ["campaigns"]
