"""§20 (Фаза C): маршрутизация UI раздела «Информация про клиентов».

Реальный ConfirmStore/ClientProfileStore на temp SQLite (conftest); LLM-извлечение подменяем.
Проверяем: выбор аккаунта → карточка; add → накопление текста → save создаёт ТОЛЬКО черновик
profile_save (исполнение по ✅); наличие профиля → save даёт profile_update; clear даёт черновик
profile_clear; пустой ввод/пустой буфер не создают черновик.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from bot.callbacks import ClientCB  # noqa: E402
from clients.profile_extract import ClientProfileExtract  # noqa: E402
from clients.store import ClientProfileStore  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
from db.session import init_db  # noqa: E402


@pytest.fixture(autouse=True)
def _grant_access(monkeypatch):
    """Эти тесты проверяют РОУТИНГ визарда «Клиенты», а не замок аккаунта. Гейт доступа делаем
    детерминированно True: в CI env allow-list пуст → `_cli_check_access` вернул бы False, и
    хендлеры ушли бы в ранний отказ (не создав ни состояния, ни черновика). Замок покрыт отдельно
    (ensure_allowed/read_allowed + cross-domain тесты). Draft в проде всегда разрешён — это и
    моделируем. Без env-зависимости тесты одинаково зелёные локально и в CI."""

    async def _ok(_chat_id, _customer_id):
        return True

    monkeypatch.setattr(bm, "_cli_check_access", _ok)


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


class FakeMessage:
    def __init__(self, chat_id: int = 100, text: str = ""):
        self.chat = type("C", (), {"id": chat_id})()
        self.text = text
        self.caption = None
        self.answers: list = []

    async def answer(self, text: str = "", **kw):
        self.answers.append((text, kw))
        return self

    async def edit_text(self, text: str = "", **kw):
        self.answers.append((text, kw))
        return self


class FakeCB:
    def __init__(self, chat_id: int = 100, uid: int = 100):
        self.message = FakeMessage(chat_id=chat_id)
        self.from_user = type("U", (), {"id": uid})()
        self.answers: list = []

    async def answer(self, text: str = "", show_alert: bool = False, **kw):
        self.answers.append((text, show_alert))


class FakeState:
    def __init__(self):
        self._data: dict = {}
        self._state = None

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kw):
        self._data.update(kw)

    async def set_state(self, s):
        self._state = s

    async def get_state(self):
        return self._state

    async def clear(self):
        self._data = {}
        self._state = None


def _fake_extract(result: ClientProfileExtract):
    async def _f(text, **kwargs):
        return result

    return _f


@pytest.mark.asyncio
async def test_account_select_shows_card():
    await init_db()
    chat_id = 301
    bm._CLI_ACCT_CACHE[chat_id] = [
        SimpleNamespace(id=DRAFT_ACCOUNT_ID, name="Draft", manager=False)
    ]
    state = FakeState()
    cq = FakeCB(chat_id=chat_id)
    await bm.cli_account_cb(cq, ClientCB(action="acct", idx=0), state)
    assert (await state.get_data())["cli_customer_id"] == DRAFT_ACCOUNT_ID
    assert cq.message.answers  # карточка показана


@pytest.mark.asyncio
async def test_add_accumulate_save_creates_profile_save_proposal():
    await init_db()
    chat_id = 302
    bm._CLI_TEXT_BUF.pop(chat_id, None)
    bm._LAST_PENDING.pop(chat_id, None)
    state = FakeState()
    await state.update_data(cli_customer_id=DRAFT_ACCOUNT_ID)

    # add → режим приёма текста
    cq = FakeCB(chat_id=chat_id)
    await bm.cli_add_update_cb(cq, ClientCB(action="add"), state)
    assert await state.get_state() == bm.ClientInfoWizard.awaiting_text

    # два сообщения подряд накапливаются
    await bm.cli_accumulate_text(FakeMessage(chat_id, "Kasi Motors — автодилер"), state)
    await bm.cli_accumulate_text(FakeMessage(chat_id, "телефон +254 712 345 678"), state)
    assert len(bm._CLI_TEXT_BUF[chat_id]) == 2

    # save (LLM подменён) → черновик profile_save (pending, НЕ исполнен)
    extract = ClientProfileExtract(brand="Kasi Motors", business_desc="автодилер")
    with patched(bm, "extract_profile", _fake_extract(extract)):
        await bm.cli_save_cb(FakeCB(chat_id=chat_id), state)

    cid = bm._LAST_PENDING[chat_id]
    snap = await ConfirmStore().get_confirmed(cid)
    assert snap.operation == "profile_save"
    assert snap.status == "pending"
    assert snap.user_initiated is True
    assert snap.params["patch"]["brand"] == "Kasi Motors"
    assert snap.customer_id == DRAFT_ACCOUNT_ID
    assert bm._CLI_TEXT_BUF.get(chat_id) is None  # буфер очищен после показа черновика


@pytest.mark.asyncio
async def test_save_with_existing_profile_is_update():
    await init_db()
    chat_id = 303
    await ClientProfileStore().apply_upsert(
        DRAFT_ACCOUNT_ID, {"brand": "Old"}, operation="profile_save"
    )
    bm._CLI_TEXT_BUF[chat_id] = ["новая инфа"]
    state = FakeState()
    await state.update_data(cli_customer_id=DRAFT_ACCOUNT_ID)
    extract = ClientProfileExtract(business_desc="обновление")
    with patched(bm, "extract_profile", _fake_extract(extract)):
        await bm.cli_save_cb(FakeCB(chat_id=chat_id), state)
    snap = await ConfirmStore().get_confirmed(bm._LAST_PENDING[chat_id])
    assert snap.operation == "profile_update"


@pytest.mark.asyncio
async def test_save_empty_buffer_no_proposal():
    await init_db()
    chat_id = 304
    bm._CLI_TEXT_BUF.pop(chat_id, None)
    bm._LAST_PENDING.pop(chat_id, None)
    state = FakeState()
    await state.update_data(cli_customer_id=DRAFT_ACCOUNT_ID)
    cq = FakeCB(chat_id=chat_id)
    await bm.cli_save_cb(cq, state)
    assert cq.answers and cq.answers[-1][1] is True  # show_alert: нечего сохранять
    assert bm._LAST_PENDING.get(chat_id) is None


@pytest.mark.asyncio
async def test_clear_creates_profile_clear_proposal():
    await init_db()
    chat_id = 305
    await ClientProfileStore().apply_upsert(
        DRAFT_ACCOUNT_ID, {"brand": "ToClear"}, operation="profile_save"
    )
    state = FakeState()
    await state.update_data(cli_customer_id=DRAFT_ACCOUNT_ID)
    await bm.cli_clear_cb(FakeCB(chat_id=chat_id), state)
    snap = await ConfirmStore().get_confirmed(bm._LAST_PENDING[chat_id])
    assert snap.operation == "profile_clear"
    assert snap.status == "pending"


@pytest.mark.asyncio
async def test_clear_without_profile_alerts():
    await init_db()
    chat_id = 306
    bm._LAST_PENDING.pop(chat_id, None)
    # используем аккаунт без профиля (Draft мог быть очищен предыдущими тестами, но пере-создадим чисто)
    await ClientProfileStore().apply_clear(DRAFT_ACCOUNT_ID)
    state = FakeState()
    await state.update_data(cli_customer_id=DRAFT_ACCOUNT_ID)
    cq = FakeCB(chat_id=chat_id)
    await bm.cli_clear_cb(cq, state)
    assert cq.answers and cq.answers[-1][1] is True  # show_alert: нет профиля
    assert bm._LAST_PENDING.get(chat_id) is None
