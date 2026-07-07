"""2.5 (аудит 2026-07-06): /mutready — чек-лист готовности к включению мутаций (только админ).

Инварианты: команда — ЧИСТАЯ ДИАГНОСТИКА: не меняет settings/allowed-list, не пишет oauth-рантайм,
не зовёт ensure_allowed «на пропуск»; не-админ получает отказ; финальный шаг всегда «владелец,
руками» (текст чек-листа это говорит).
"""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import ads.client as ac  # noqa: E402

# ВАЖНО: bot.main — ПЕРВЫМ (его хвост сам импортирует handlers; импорт commands раньше main дал бы
# частично инициализированный модуль в цикле и потерю re-export'ов bm.<handler>).
import bot.main as bm  # noqa: E402
import bot.handlers.commands as bc  # noqa: E402
from core.config import settings  # noqa: E402


class _FakeMsg:
    def __init__(self, chat_id: int = 778101):
        self.answers: list = []
        self.chat = SimpleNamespace(id=chat_id)

    async def answer(self, text="", **kw):
        self.answers.append((text, kw))


def _cmd(args: str | None = None):
    return SimpleNamespace(args=args)


async def test_mutready_denied_for_non_admin(monkeypatch):
    monkeypatch.setattr(bc, "_is_admin", lambda cid: False)
    msg = _FakeMsg()
    await bc.mutready_cmd(msg, _cmd("7753643025"))
    assert msg.answers and "ADMIN_CHAT_IDS" in msg.answers[0][0]


async def test_mutready_checklist_readonly_invariant(monkeypatch):
    """Чек-лист собирается БЕЗ каких-либо изменений: oauth-рантайм не трогается, allowed-list
    settings идентичен до/после, ensure_allowed не вызывается вовсе."""

    def _boom_oauth(*a, **kw):
        raise AssertionError("/mutready не должен писать oauth-рантайм")

    def _boom_allow(*a, **kw):
        raise AssertionError("/mutready не должен звать ensure_allowed (даже на проверку)")

    monkeypatch.setattr(ac, "set_oauth_runtime", _boom_oauth)
    monkeypatch.setattr(ac, "ensure_allowed", _boom_allow)

    async def _fake_client(cid=None):
        return object()

    async def _fake_read(fn, client, cid, *a, **kw):
        return "AUD"

    monkeypatch.setattr(ac, "build_client_async", _fake_client)
    monkeypatch.setattr(bm, "run_ads_read_call", _fake_read)

    before = settings.google_ads_allowed_customer_ids
    r = await bm._mutready_check(bm.DRAFT_ACCOUNT_ID)
    assert settings.google_ads_allowed_customer_ids == before  # конфиг не тронут
    assert r["cid"] == bm.DRAFT_ACCOUNT_ID
    assert r["visible"] is True  # Draft всегда в потолке (ALLOWED_CEILING)
    assert r["probe"] is True and r["currency"] == "AUD"
    assert isinstance(r["operators"], list)

    text = bm.texts.fmt_mutready(r, "ru")
    assert "Готовность к мутациям" in text
    # финальный шаг — всегда за владельцем: либо «уже включены», либо инструкция руками
    assert ("УЖЕ включены" in text) or ("ВЛАДЕЛЕЦ" in text and "НЕ меняет" in text)


async def test_mutready_probe_failure_is_honest(monkeypatch):
    async def _fake_client(cid=None):
        return object()

    async def _boom_read(fn, client, cid, *a, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(ac, "build_client_async", _fake_client)
    monkeypatch.setattr(bm, "run_ads_read_call", _boom_read)
    r = await bm._mutready_check("9990001112")  # невидимый id: диагностика всё равно собирается
    assert r["probe"] is False and r["probe_error"]
    assert r["visible"] is False and r["mutations_enabled"] is False
    text = bm.texts.fmt_mutready(r, "ru")
    assert "❌" in text  # честные кресты, не падение


async def test_mutready_usage_on_garbage(monkeypatch):
    monkeypatch.setattr(bc, "_is_admin", lambda cid: True)

    async def _resolve_fail(chat_id, raw):
        raise LookupError("нет")

    import core.access as ca

    monkeypatch.setattr(ca, "resolve_read_account", _resolve_fail)
    msg = _FakeMsg()
    await bc.mutready_cmd(msg, _cmd("совсем не аккаунт"))
    assert msg.answers and "/mutready" in msg.answers[0][0]
