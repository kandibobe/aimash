"""N1.4: fuzzy-подсказка при опечатке имени кампании (/pause, /resume, /addkeys).

Инварианты (fail-closed, GR8): подсказка — ТОЛЬКО кнопки с ТОЧНЫМИ именами, никогда не исполняем
на угаданном имени; клик идёт штатным пикером (D3/D4) → обычный confirm-гейт; сбой загрузки
списка кампаний → старое поведение (черновик минтится, ошибку честно покажет исполнение).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402

_CAMPS = [
    {"name": "Летняя распродажа", "id": "10", "status": "ENABLED"},
    {"name": "Зимняя коллекция", "id": "20", "status": "PAUSED"},
    {"name": "Brand-Search", "id": "30", "status": "ENABLED"},
]


class _Msg:
    def __init__(self, chat_id, text=""):
        self.chat = SimpleNamespace(id=chat_id)
        self.text = text
        self.sent: list = []

    async def answer(self, text, **kw):
        self.sent.append((text, kw))


class _State:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.state = None
        self.cleared = False

    async def clear(self):
        self.cleared = True

    async def set_state(self, s):
        self.state = s

    async def get_data(self):
        return dict(self.data)


# ── чистый скорер кандидатов ──────────────────────────────────────────────────────────
def test_fuzzy_candidates_typo_case_substring_and_miss():
    # опечатка (пропущенная буква)
    c1 = bm._fuzzy_campaign_candidates(_CAMPS, "Летня распродажа")
    assert [c["name"] for c in c1][:1] == ["Летняя распродажа"]
    # регистр (GAQL-матч точный и регистрозависимый — предлагаем точное имя)
    c2 = bm._fuzzy_campaign_candidates(_CAMPS, "brand-search")
    assert [c["name"] for c in c2] == ["Brand-Search"]
    # подстрока (difflib слеп к коротким запросам)
    c3 = bm._fuzzy_campaign_candidates(_CAMPS, "коллекция")
    assert any(c["name"] == "Зимняя коллекция" for c in c3)
    # мимо — пусто (никаких «лучших догадок» из ничего)
    assert bm._fuzzy_campaign_candidates(_CAMPS, "qqqqzzzz") == []
    assert bm._fuzzy_campaign_candidates(_CAMPS, "  ") == []


def _pin_account(monkeypatch, acct: str = "1234567890"):
    """Детерминизировать N1.4-гейты: закреплённый аккаунт (fuzzy активен), без похода в БД."""

    async def fake_acct(chat_id):
        return acct

    monkeypatch.setattr(bm, "_active_read_account", fake_acct)


# ── /pause /resume: опечатка → подсказка, точное имя → старый путь ────────────────────
async def test_slash_mutate_typo_suggests_and_never_mints(monkeypatch):
    calls = []

    async def fake_load(chat_id):
        return list(_CAMPS)

    async def fake_present(m, chat_id, operation, name):
        calls.append((operation, name))

    _pin_account(monkeypatch)
    monkeypatch.setattr(bm, "_kw_add_load_campaigns", fake_load)
    monkeypatch.setattr(bm, "_slash_mutate_present", fake_present)
    m = _Msg(911)
    await bm._slash_mutate(m, SimpleNamespace(args="Летня распродажа"), "pause_campaign")
    assert calls == []  # черновик НЕ минтится на несуществующее имя
    text, kw = m.sent[-1]
    assert "не найдена" in text or "not found" in text
    markup = kw.get("reply_markup")
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("Летняя распродажа" in t for t in labels)
    # кандидаты положены в кэш D4 — клик доигрывает штатный on_slash_mutate_pick → confirm-гейт
    assert bm._SLASH_MUT_CACHE[911][0]["name"] == "Летняя распродажа"
    # поколение списка записано — старые клавиатуры этим кликом не резолвятся (гонка кэша)
    assert bm._SLASH_MUT_GEN[911] >= 1


async def test_slash_mutate_typo_of_wrong_status_campaign_keeps_old_path(monkeypatch):
    """Опечатка имени PAUSED-кампании в /pause: кандидаты фильтруются целевым статусом (паузить
    приостановленную бессмысленно) → подсказки нет, старый честный путь (ошибку скажет исполнение)."""
    calls = []

    async def fake_load(chat_id):
        return list(_CAMPS)

    async def fake_present(m, chat_id, operation, name):
        calls.append(name)

    _pin_account(monkeypatch)
    monkeypatch.setattr(bm, "_kw_add_load_campaigns", fake_load)
    monkeypatch.setattr(bm, "_slash_mutate_present", fake_present)
    m = _Msg(916)
    await bm._slash_mutate(m, SimpleNamespace(args="Зимня коллекция"), "pause_campaign")
    assert calls == ["Зимня коллекция"]  # «Зимняя коллекция» PAUSED → не кандидат для /pause


async def test_slash_mutate_fuzzy_skipped_when_account_ambiguous(monkeypatch):
    """AD.3: аккаунт не закреплён + живых >1 → мутация уйдёт на аккаунт из форс-пикера; сверять имя
    со списком АКТИВНОГО аккаунта было бы сверкой не с тем аккаунтом — fuzzy пропускается."""
    calls = []

    async def fake_load(chat_id):
        raise AssertionError("список кампаний не должен грузиться при неоднозначном аккаунте")

    async def fake_present(m, chat_id, operation, name):
        calls.append(name)

    async def pending(chat_id):
        return True

    _pin_account(monkeypatch, bm.DRAFT_ACCOUNT_ID)
    monkeypatch.setattr("core.access.account_choice_pending", pending)
    monkeypatch.setattr(bm, "_kw_add_load_campaigns", fake_load)
    monkeypatch.setattr(bm, "_slash_mutate_present", fake_present)
    m = _Msg(917)
    await bm._slash_mutate(m, SimpleNamespace(args="Летня распродажа"), "pause_campaign")
    assert calls == ["Летня распродажа"]  # старый путь: форс-пикер аккаунта внутри present


async def test_stale_keyboard_click_rejected_after_cache_overwrite(monkeypatch):
    """Гонка кэша (ревью N1.4): кнопка СТАРОЙ клавиатуры после перезаписи кэша (fuzzy) обязана
    дать «список устарел», а не отрезолвить idx в ДРУГУЮ кампанию (подмена интента клика)."""
    from bot.handlers.campaigns_menu import on_slash_mutate_pick

    chat_id = 918
    presented = []

    async def fake_present(m, cid, op, name):
        presented.append(name)

    monkeypatch.setattr(bm, "_slash_mutate_present", fake_present)
    alerts = []

    class _Cq:
        def __init__(self):
            self.message = _Msg(chat_id)

        async def answer(self, text=None, show_alert=False, **kw):
            if text:
                alerts.append(text)

    gen_old = bm._slash_mut_store(chat_id, list(_CAMPS))  # D4-пикер: Летняя/Зимняя/Brand
    bm._slash_mut_store(chat_id, [_CAMPS[2]])  # fuzzy перезаписал кэш кандидатами
    await on_slash_mutate_pick(_Cq(), bm.SlashMutCB(op="pause_campaign", idx=0, gen=gen_old))
    assert presented == []  # клик по «Летняя…» НЕ превратился в «Brand-Search»
    assert alerts  # stale-алерт показан
    # свежая клавиатура (актуальный gen) работает как раньше
    gen_new = bm._slash_mut_store(chat_id, list(_CAMPS))
    await on_slash_mutate_pick(_Cq(), bm.SlashMutCB(op="pause_campaign", idx=0, gen=gen_new))
    assert presented == ["Летняя распродажа"]


async def test_slash_mutate_exact_name_goes_straight(monkeypatch):
    calls = []

    async def fake_load(chat_id):
        return list(_CAMPS)

    async def fake_present(m, chat_id, operation, name):
        calls.append((operation, name))

    _pin_account(monkeypatch)
    monkeypatch.setattr(bm, "_kw_add_load_campaigns", fake_load)
    monkeypatch.setattr(bm, "_slash_mutate_present", fake_present)
    m = _Msg(912)
    await bm._slash_mutate(m, SimpleNamespace(args="Летняя распродажа"), "pause_campaign")
    assert calls == [("pause_campaign", "Летняя распродажа")]


async def test_slash_mutate_load_failure_keeps_old_behavior(monkeypatch):
    """Сбой чтения списка ([]) → НЕ блокируем команду: минтим как раньше (fail-open только на
    ПОДСКАЗКУ; сама мутация всё равно за confirm-гейтом + ошибку покажет исполнение)."""
    calls = []

    async def fake_load(chat_id):
        return []

    async def fake_present(m, chat_id, operation, name):
        calls.append(name)

    _pin_account(monkeypatch)
    monkeypatch.setattr(bm, "_kw_add_load_campaigns", fake_load)
    monkeypatch.setattr(bm, "_slash_mutate_present", fake_present)
    m = _Msg(913)
    await bm._slash_mutate(m, SimpleNamespace(args="Летня распродажа"), "resume_campaign")
    assert calls == ["Летня распродажа"]


# ── /addkeys: текст-ввод имени с опечаткой → подсказка, остаёмся в состоянии ──────────
async def test_kw_add_campaign_typo_suggests_buttons(monkeypatch):
    from bot.handlers.keywords_flow import kw_add_campaign

    async def fake_load(chat_id):
        return list(_CAMPS)

    _pin_account(monkeypatch)
    monkeypatch.setattr(bm, "_kw_add_load_campaigns", fake_load)
    m = _Msg(914, text="Зимня коллекция")
    st = _State({"kw_add_token": "tok1"})
    await kw_add_campaign(m, st)
    text, kw = m.sent[-1]
    assert "не найдена" in text or "not found" in text
    labels = [b.text for row in kw["reply_markup"].inline_keyboard for b in row]
    assert any("Зимняя коллекция" in t for t in labels)
    assert bm._KW_ADD_CAMP_CACHE[914][0]["name"] == "Зимняя коллекция"
    assert st.state is None  # остались в awaiting_campaign — выбор за оператором
