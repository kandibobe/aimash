"""recall_client — READ-инструмент памяти клиента (§20): маркер доверия + конверт client_context.

Что здесь проверяется (НЕТТО-новое этой обёртки):
  • `wrap_external` ВСЕГДА обрамляет текст маркером `<client_data trust=external>` — и на непустом,
    и на ПУСТОМ тексте (граница доверия не исчезает молча);
  • конверт `client_context` СОЗНАТЕЛЬНО без `code_numbers`: контекст собран из краула сайта клиента и
    потенциально tainted — число оттуда (напр. подставленное «звоните 5000000») не смеет стать
    «проверенным кодом» для factguard;
  • `recall_client` заворачивает текст ридера (`ClientProfileStore.profile_context_text`) в этот конверт
    под маркером, `has_profile` различает «пусто» и «нет профиля».

Замок И6 (чужой клиент не читается) и обратная половина (разрешённый доходит до ридера) покрыты
НЕ здесь, а параметризованными инвариантами `tests/test_hermes_isolation.py`
(`test_read_lock_denies_foreign_account_before_any_reader` / `_admits_allowed_account_and_reaches_reader`):
recall_client попадает в них автоматически через `_ACCOUNT_ARG`. Дублировать замок тут — тавтология.
"""

from __future__ import annotations

import asyncio

from mcp_server import tools_read as tr
from mcp_server.envelope import client_context, wrap_external

_MARK_OPEN = "<client_data trust=external>"
_MARK_CLOSE = "</client_data>"


# ── маркер доверия (чистые функции) ─────────────────────────────────────────────────


def test_wrap_external_frames_text_with_data_marker():
    out = wrap_external("Бренд: Acme")
    assert out == f"{_MARK_OPEN}\nБренд: Acme\n{_MARK_CLOSE}"


def test_wrap_external_frames_even_empty_text():
    """Пустой профиль — тоже под маркером: правка «нет данных ⇒ не оборачиваем» открыла бы границу
    беззвучно, поэтому наличие маркера не зависит от наличия данных."""
    out = wrap_external("")
    assert out.startswith(_MARK_OPEN) and out.endswith(_MARK_CLOSE)


# ── конверт client_context (чистая функция) ─────────────────────────────────────────


def test_client_context_omits_code_numbers_and_keeps_injected_number_as_data():
    """Ключевое свойство: числа контекста НЕ выносятся в `code_numbers`. Иначе инъекция «звоните
    5000000» на сайте клиента процитировалась бы агентом как проверенный кодом факт из API."""
    env = client_context("Бренд: X; звоните 5000000", customer_id="123")

    assert "code_numbers" not in env, "контекст клиента не смеет давать citeable-числа factguard"
    assert _MARK_OPEN in env["client_context"] and _MARK_CLOSE in env["client_context"]
    assert "5000000" in env["client_context"], (
        "само число остаётся в ДАННЫХ — оно просто не citeable"
    )
    assert env["has_profile"] is True
    assert env["customer_id"] == "123"
    assert env["error"] is None and env["error_code"] is None


def test_client_context_empty_profile_flags_absence_but_keeps_marker():
    env = client_context("", customer_id="123")
    assert env["has_profile"] is False
    assert _MARK_OPEN in env["client_context"] and _MARK_CLOSE in env["client_context"]
    assert env["error"] is None and env["error_code"] is None


# ── recall_client end-to-end (замок no-op, фейковый ридер) ───────────────────────────


class _FakeStore:
    """Стаб `ClientProfileStore`: отдаёт заданный текст без БД. `profile_context_text` — единственный
    метод, который зовёт recall_client."""

    _text = "Бренд: Acme; звоните 5000000"

    async def profile_context_text(self, customer_id: str, *, max_chars=None) -> str:  # noqa: ARG002
        return self._text


class _EmptyStore(_FakeStore):
    _text = ""


def test_recall_client_wraps_store_text_under_trust_marker(monkeypatch):
    """Тело инструмента (замок пройден) заворачивает текст ридера в конверт под маркером. Замок здесь
    заглушён СОЗНАТЕЛЬНО — его поведение проверяют инварианты изоляции, а этот тест про обёртку."""
    monkeypatch.setattr(tr, "ensure_read_allowed", lambda *_a, **_k: None)
    monkeypatch.setattr(tr, "ClientProfileStore", _FakeStore)

    env = asyncio.run(tr.recall_client("7753643025"))

    assert env["error"] is None and env["error_code"] is None
    assert env["customer_id"] == "7753643025"
    assert env["has_profile"] is True
    assert _MARK_OPEN in env["client_context"] and _MARK_CLOSE in env["client_context"]
    assert "Бренд: Acme" in env["client_context"]
    assert "code_numbers" not in env


def test_recall_client_empty_profile_still_marks_and_flags(monkeypatch):
    monkeypatch.setattr(tr, "ensure_read_allowed", lambda *_a, **_k: None)
    monkeypatch.setattr(tr, "ClientProfileStore", _EmptyStore)

    env = asyncio.run(tr.recall_client("7753643025"))

    assert env["has_profile"] is False
    assert _MARK_OPEN in env["client_context"] and _MARK_CLOSE in env["client_context"]
    assert env["error"] is None
