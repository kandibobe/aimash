"""§20.6 + §7: профиль клиента доезжает до advisor, брендозащита минус-слов получает токены.

Оба пути импортировали НЕсуществующий `clients.store.ClientStore` (класс — ClientProfileStore),
ImportError глотался `except Exception` → advisor всегда работал с пустым контекстом и пустым
набором защищённых терминов: бот мог посоветовать заминусовать бренд самого клиента. Молча.
Класс бага закрыт гардом tests/test_lazy_imports_exist.py; здесь — что провода реально соединены.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clients.store as store  # noqa: E402
from advisor import service as S  # noqa: E402


class _FakeStore:
    async def profile_context_text(self, customer_id: str, *, max_chars: int = 1500) -> str:
        return f"профиль:{customer_id}"[:max_chars]

    async def protected_negative_terms(self, customer_id: str) -> set[str]:
        return {"aimash", "мойка"}


def test_client_profile_reaches_advisor(monkeypatch):
    monkeypatch.setattr(store, "ClientProfileStore", _FakeStore)
    assert asyncio.run(S._client_profile_context("7753643025")) == "профиль:7753643025"


def test_protected_brand_terms_reach_advisor(monkeypatch):
    monkeypatch.setattr(store, "ClientProfileStore", _FakeStore)
    assert asyncio.run(S._protected_terms("7753643025")) == {"aimash", "мойка"}


def test_failure_is_still_best_effort(monkeypatch):
    """Сбой БД/нет профиля → advisor работает как раньше (пустой контекст), а не падает."""

    class _Boom:
        async def profile_context_text(self, *a, **kw):
            raise RuntimeError("db down")

        async def protected_negative_terms(self, *a, **kw):
            raise RuntimeError("db down")

    monkeypatch.setattr(store, "ClientProfileStore", _Boom)
    assert asyncio.run(S._client_profile_context("1")) == ""
    assert asyncio.run(S._protected_terms("1")) == set()
