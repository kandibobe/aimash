"""§20.4: команда /crawl [url] — видимый вход в краулинг сайта клиента.

Инварианты (тот же контур, что и кнопка «Перекраулить», но из команды):
• URL из аргумента команды → нормализуется (bare-домен → https://) и уходит в _spawn_crawl;
• без аргумента берётся website профиля; нет ни того, ни другого → cli_no_website, краул НЕ запущен;
• аккаунт: выбранный в разделе «Клиенты» (FSM) имеет приоритет над активным read-аккаунтом;
• доступ fail-closed: _cli_check_access=False → cli_access_denied, краул НЕ запущен;
• обход уже идёт (_spawn_crawl→False) → cli_crawl_already, без второго запуска.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402
from bot.handlers.clients_kb import crawl_cmd  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


class FakeBot:
    def __init__(self):
        self.sent: list = []

    async def send_message(self, chat_id, text: str = "", **kw):
        self.sent.append((chat_id, text, kw))


class FakeMessage:
    def __init__(self, chat_id: int = 100):
        self.chat = SimpleNamespace(id=chat_id)
        self.bot = FakeBot()
        self.answers: list = []

    async def answer(self, text: str = "", **kw):
        self.answers.append(text)
        return self


class FakeState:
    def __init__(self, **data):
        self._data = dict(data)

    async def get_data(self):
        return dict(self._data)


def _cmd(arg: str | None):
    return SimpleNamespace(args=arg)


async def _run(
    arg: str | None,
    *,
    fsm_account: str | None = None,
    active_account: str = "7753643025",
    website: str | None = None,
    access: bool = True,
    spawn_ok: bool = True,
):
    """Прогнать crawl_cmd с замоканными доступом/профилем/краулом. Возвращает (msg, spawn_calls)."""
    spawn_calls: list = []

    def _fake_spawn(bot, chat_id, customer_id, url, *, mode="full"):
        spawn_calls.append((customer_id, url, mode))
        return spawn_ok

    async def _fake_access(_chat_id, _customer_id):
        return access

    async def _fake_active(_chat_id):
        return active_account

    async def _fake_get_by_account(_customer_id):
        return {"website": website} if website else None

    fake_clients = SimpleNamespace(get_by_account=_fake_get_by_account)
    msg = FakeMessage()
    state = FakeState(**({"cli_customer_id": fsm_account} if fsm_account else {}))
    with (
        patched(bm, "_spawn_crawl", _fake_spawn),
        patched(bm, "_cli_check_access", _fake_access),
        patched(bm, "_active_read_account", _fake_active),
        patched(bm, "CLIENTS", fake_clients),
    ):
        await crawl_cmd(msg, _cmd(arg), state)
    return msg, spawn_calls


@pytest.mark.asyncio
async def test_crawl_with_url_arg_spawns_normalized_url():
    msg, calls = await _run("example.com/promo")
    assert len(calls) == 1
    assert calls[0][1] == "https://example.com/promo"  # bare-домен нормализован
    assert msg.answers == [bm.i18n.t("cli_crawl_started", domain="example.com")]


@pytest.mark.asyncio
async def test_crawl_keeps_explicit_scheme():
    _msg, calls = await _run("http://insecure.example")
    assert calls[0][1] == "http://insecure.example"  # http:// не переписываем на https://


@pytest.mark.asyncio
async def test_crawl_without_arg_uses_profile_website():
    _msg, calls = await _run(None, website="client.co")
    assert len(calls) == 1
    assert calls[0][1] == "https://client.co"


@pytest.mark.asyncio
async def test_crawl_no_url_anywhere_is_no_website_and_no_spawn():
    msg, calls = await _run(None, website=None)
    assert calls == []  # краул НЕ запущен
    assert msg.answers == [bm.i18n.t("cli_no_website")]


@pytest.mark.asyncio
async def test_crawl_access_denied_fail_closed():
    msg, calls = await _run("example.com", access=False)
    assert calls == []  # доступа нет → краул НЕ запущен
    assert msg.answers == [bm.i18n.t("cli_access_denied")]


@pytest.mark.asyncio
async def test_crawl_already_running_reports_and_no_double_spawn():
    msg, calls = await _run("example.com", spawn_ok=False)
    assert len(calls) == 1  # запрошен ровно раз (дедуп внутри _spawn_crawl)
    assert msg.answers == [bm.i18n.t("cli_crawl_already", domain="example.com")]


@pytest.mark.asyncio
async def test_crawl_fsm_account_takes_priority_over_active():
    """Выбранный в «Клиенты» аккаунт (FSM) важнее активного read-аккаунта: краул уйдёт на него."""
    _msg, calls = await _run("x.com", fsm_account="1112223334", active_account="9998887776")
    assert calls[0][0] == "1112223334"


@pytest.mark.asyncio
async def test_crawl_falls_back_to_active_account():
    _msg, calls = await _run("x.com", fsm_account=None, active_account="9998887776")
    assert calls[0][0] == "9998887776"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
