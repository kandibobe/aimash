"""Офлайн-тесты ядра безопасности (без внешних сервисов/ключей).

Проверяют золотые правила:
- RSA-длина: кириллица = 1 символ, лимиты 30/90/15.
- Confirm-гейт: мутация без валидного confirmation_id отклоняется; бюджет — только user_initiated.

Запуск: pytest -q   (или python tests/test_safety_core.py для быстрой проверки без pytest)
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adcopy.validate import rsa_len, validate  # noqa: E402
from ads.mutations import apply_update_budget  # noqa: E402


# ── RSA длина: кириллица = 1 символ ────────────────────────────────────────────
def test_cyrillic_counts_as_one():
    head = "Доставка цветов Киев"  # 20 символов
    assert rsa_len(head) == 20
    ok, n = validate("А" * 30, "headline")
    assert ok and n == 30           # 30 кириллических укладывается в лимит 30
    ok, n = validate("А" * 31, "headline")
    assert not ok and n == 31


def test_cjk_counts_as_two():
    assert rsa_len("你好") == 4      # CJK = 2 каждый


# ── Confirm-гейт ───────────────────────────────────────────────────────────────
@dataclass
class FakeProposal:
    operation: str
    status: str
    user_initiated: bool


class FakeStore:
    def __init__(self, proposal=None):
        self._p = proposal
        self.finalized = False

    async def get_confirmed(self, confirmation_id):
        return self._p

    async def finalize(self, confirmation_id, *, result):
        self.finalized = True


def _run(coro):
    return asyncio.run(coro)


def test_mutation_rejected_without_confirmation():
    store = FakeStore(proposal=None)  # нет подтверждённого proposal
    try:
        _run(apply_update_budget(
            campaign_id="1", new_budget_micros=50_000_000,
            confirmation_id="bogus", confirm_store=store, ads_client=object(),
        ))
        raise AssertionError("ожидался PermissionError")
    except PermissionError:
        pass


def test_budget_blocked_when_not_user_initiated():
    # подтверждение есть, но не прямая команда пользователя (напр. scheduler) → отказ
    store = FakeStore(FakeProposal("update_budget", "confirmed", user_initiated=False))
    try:
        _run(apply_update_budget(
            campaign_id="1", new_budget_micros=50_000_000,
            confirmation_id="x", confirm_store=store, ads_client=object(),
        ))
        raise AssertionError("ожидался PermissionError (бюджет не по команде)")
    except PermissionError:
        pass


def test_budget_applies_when_confirmed_and_user_initiated():
    store = FakeStore(FakeProposal("update_budget", "confirmed", user_initiated=True))
    result = _run(apply_update_budget(
        campaign_id="42", new_budget_micros=50_000_000,
        confirmation_id="ok", confirm_store=store, ads_client=object(),
    ))
    assert result["applied"] is True
    assert store.finalized is True


if __name__ == "__main__":
    # Быстрый прогон без pytest
    test_cyrillic_counts_as_one()
    test_cjk_counts_as_two()
    test_mutation_rejected_without_confirmation()
    test_budget_blocked_when_not_user_initiated()
    test_budget_applies_when_confirmed_and_user_initiated()
    print("OK: все офлайн-тесты ядра безопасности прошли")
