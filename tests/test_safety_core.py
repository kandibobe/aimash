"""Офлайн-тесты ядра безопасности (без внешних сервисов/ключей).

Проверяют золотые правила:
- RSA-длина: кириллица = 1 символ, лимиты 30/90/15.
- Замок аккаунта: операции разрешены ТОЛЬКО на Aimash Draft (7753643025); fail-closed; потолок в коде.
- Confirm-гейт: мутация без валидного confirmation_id отклоняется; бюджет — только user_initiated.

Запуск: pytest -q   (или python tests/test_safety_core.py для быстрой проверки без pytest)
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adcopy.validate import rsa_len, validate  # noqa: E402
from ads.client import (  # noqa: E402
    DRAFT_ACCOUNT_ID,
    ensure_allowed,
    ensure_manager_allowed,
    ensure_read_allowed,
)
from ads.mutations import apply_update_budget  # noqa: E402
from core.config import settings  # noqa: E402


@contextmanager
def allowed_ids(value: str):
    """Временно задать GOOGLE_ADS_ALLOWED_CUSTOMER_IDS (allow-list) и вернуть как было."""
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


@contextmanager
def read_ids(value: str):
    """Временно задать GOOGLE_ADS_READ_CUSTOMER_IDS (read-allow-list, §8) и вернуть как было."""
    prev = settings.google_ads_read_customer_ids
    settings.google_ads_read_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_read_customer_ids = prev


@contextmanager
def login_customer_id(value: str):
    """Временно задать настроенный MCC (login_customer_id, для ensure_manager_allowed)."""
    prev = settings.google_ads_login_customer_id
    settings.google_ads_login_customer_id = value
    try:
        yield
    finally:
        settings.google_ads_login_customer_id = prev


# ── RSA длина: кириллица = 1 символ ────────────────────────────────────────────
def test_cyrillic_counts_as_one():
    head = "Доставка цветов Киев"  # 20 символов
    assert rsa_len(head) == 20
    ok, n = validate("А" * 30, "headline")
    assert ok and n == 30  # 30 кириллических укладывается в лимит 30
    ok, n = validate("А" * 31, "headline")
    assert not ok and n == 31


def test_cjk_counts_as_two():
    assert rsa_len("你好") == 4  # CJK = 2 каждый


# ── Замок единственного аккаунта (Aimash Draft 7753643025) ─────────────────────
def test_ensure_allowed_fail_closed_when_empty():
    # Пустой allow-list = ОТКАЗ (а не «разрешено всё»).
    with allowed_ids(""):
        try:
            ensure_allowed(DRAFT_ACCOUNT_ID)
            raise AssertionError("ожидался PermissionError (fail-closed)")
        except PermissionError:
            pass


def test_ensure_allowed_accepts_draft_normalized():
    # И '7753643025', и человекочитаемый '775-364-3025' разрешены (нормализация).
    with allowed_ids(DRAFT_ACCOUNT_ID):
        ensure_allowed(DRAFT_ACCOUNT_ID)
        ensure_allowed("775-364-3025")


def test_ensure_allowed_rejects_other_account():
    # Аккаунт не из списка — отказ, даже если список непустой.
    with allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            ensure_allowed("1234567890")
            raise AssertionError("ожидался PermissionError (чужой аккаунт)")
        except PermissionError:
            pass


def test_ensure_allowed_ceiling_blocks_foreign_env():
    # env пытается разрешить чужой (боевой) аккаунт → выход за код-потолок → отказ.
    with allowed_ids("1234567890"):
        try:
            ensure_allowed("1234567890")
            raise AssertionError("ожидался PermissionError (выход за потолок)")
        except PermissionError:
            pass


# ── Раздельный замок ЧТЕНИЯ (§8: сводка по дочерним MCC) ───────────────────────
_CHILD = "1112223334"  # условный дочерний аккаунт MCC


def test_ensure_read_allowed_fail_closed_when_empty():
    # Оба списка пусты → чтение запрещено (fail-closed), как и мутации.
    with allowed_ids(""), read_ids(""):
        try:
            ensure_read_allowed(DRAFT_ACCOUNT_ID)
            raise AssertionError("ожидался PermissionError (fail-closed)")
        except PermissionError:
            pass


def test_ensure_read_allowed_defaults_to_mutation_scope():
    # read-list пуст → множество чтения = мутационный список (поведение не меняется).
    with allowed_ids(DRAFT_ACCOUNT_ID), read_ids(""):
        ensure_read_allowed(DRAFT_ACCOUNT_ID)  # разрешённый аккаунт читается
        ensure_read_allowed("775-364-3025")  # нормализация
        try:
            ensure_read_allowed(_CHILD)  # дочерний не в списках → отказ
            raise AssertionError("ожидался PermissionError (дочерний не перечислен)")
        except PermissionError:
            pass


def test_ensure_read_allowed_permits_listed_child():
    # Дочерний в read-list → читать можно (расширение под §8).
    with allowed_ids(DRAFT_ACCOUNT_ID), read_ids(_CHILD):
        ensure_read_allowed(_CHILD)
        ensure_read_allowed(DRAFT_ACCOUNT_ID)  # мутационный аккаунт тоже остаётся читаемым


def test_mutation_lock_unchanged_by_read_allowlist():
    # ⚠️ КЛЮЧЕВОЙ инвариант разделения замков: дочерний в read-list НЕ открывает мутации на нём.
    # ensure_allowed (деньги) обязан отклонить его — даже когда чтение разрешено. G1: read-list лишь
    # добавляет аккаунт в ПОТОЛОК; мутация требует ещё и членства в allowed_customer_ids (opt-in).
    with allowed_ids(DRAFT_ACCOUNT_ID), read_ids(_CHILD):
        ensure_read_allowed(_CHILD)  # читать — можно
        try:
            ensure_allowed(_CHILD)  # менять — НЕЛЬЗЯ (не в allowed_customer_ids)
            raise AssertionError("замок мутаций пробит read-list — критическая регрессия!")
        except PermissionError:
            pass


# ── G1 (управляемый список): включение мутаций на ВИДИМОМ аккаунте через allow-list ──
def test_mutation_enabled_for_visible_account_in_allowlist():
    # G: аккаунт, ВИДИМЫЙ боту (в read-list) И явно добавленный в allowed_customer_ids — мутируем.
    with allowed_ids(f"{DRAFT_ACCOUNT_ID},{_CHILD}"), read_ids(_CHILD):
        ensure_allowed(DRAFT_ACCOUNT_ID)  # Draft — по-прежнему
        ensure_allowed(_CHILD)  # включённый видимый дочерний — теперь тоже (opt-in владельца)


def test_mutation_blocked_for_invisible_account_even_in_allowlist():
    # Защита от опечатки: чужой/невидимый id в allowed_customer_ids, но НЕ в read/discovered →
    # выходит за эффективный потолок allowed_ceiling() → отказ (env не откроет невидимый аккаунт).
    with allowed_ids(f"{DRAFT_ACCOUNT_ID},1234567890"), read_ids(_CHILD):
        try:
            ensure_allowed("1234567890")
            raise AssertionError("невидимый аккаунт из allow-list прошёл — регрессия потолка!")
        except PermissionError:
            pass


def test_allowed_ceiling_reflects_visible_accounts():
    from ads.client import allowed_ceiling

    with allowed_ids(DRAFT_ACCOUNT_ID), read_ids(_CHILD):
        ceiling = allowed_ceiling()
        assert DRAFT_ACCOUNT_ID in ceiling and _CHILD in ceiling  # Draft + видимый read-child
        assert "1234567890" not in ceiling  # невидимый чужой id — не в потолке


# ── Сентинел «all» (решение владельца 2026-07: Draft-only доктрина снята, prod-дефолт) ──
def test_all_sentinel_allows_every_visible_account():
    # «all» ⇒ мутационный набор = ВЕСЬ видимый потолок (Draft ∪ read-list ∪ discovered).
    with allowed_ids("all"), read_ids(_CHILD):
        ensure_allowed(DRAFT_ACCOUNT_ID)
        ensure_allowed(_CHILD)  # видимый дочерний мутируем без явного перечисления


def test_all_sentinel_still_blocks_invisible_account():
    # «all» — это ПОТОЛОК, а не «всё на свете»: аккаунт вне видимости бота — по-прежнему отказ.
    with allowed_ids("all"), read_ids(_CHILD):
        try:
            ensure_allowed("1234567890")
            raise AssertionError("«all» открыл невидимый аккаунт — регрессия потолка!")
        except PermissionError:
            pass


def test_all_sentinel_star_alias_degrades_to_draft_floor():
    # Алиас «*»; при пустой видимости (read-list пуст, discovery не бегал) набор = пол потолка
    # {Draft} — безопасная деградация, не эскалация.
    with allowed_ids("*"), read_ids(""):
        ensure_allowed(DRAFT_ACCOUNT_ID)
        try:
            ensure_allowed(_CHILD)  # невидимый (нет в read/discovered) — отказ даже при «*»
            raise AssertionError("«*» открыл невидимый дочерний — регрессия потолка!")
        except PermissionError:
            pass


# ── Замок обхода MCC (ensure_manager_allowed): прямой юнит (раньше — лишь косвенно) ──
def test_ensure_manager_allowed_fail_closed_when_empty():
    # Пустой login_customer_id → обход MCC запрещён (fail-closed), а не «разрешено всё».
    with login_customer_id(""):
        try:
            ensure_manager_allowed(DRAFT_ACCOUNT_ID)
            raise AssertionError("ожидался PermissionError (fail-closed)")
        except PermissionError:
            pass


def test_ensure_manager_allowed_accepts_configured_normalized():
    # Настроенный MCC разрешён; нормализация: '775-364-3025' ≡ '7753643025'.
    with login_customer_id(DRAFT_ACCOUNT_ID):
        ensure_manager_allowed(DRAFT_ACCOUNT_ID)
        ensure_manager_allowed("775-364-3025")


def test_ensure_manager_allowed_rejects_unconfigured():
    # manager_id вне настроенного набора MCC → отказ.
    with login_customer_id(DRAFT_ACCOUNT_ID):
        try:
            ensure_manager_allowed("1234567890")
            raise AssertionError("ожидался PermissionError (MCC не настроен)")
        except PermissionError:
            pass


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
        self._claimed = False

    async def claim(self, confirmation_id, *, operation):
        # Зеркало ConfirmStore.claim: confirmed + совпавшая операция, одноразово.
        p = self._p
        if p is None or p.status != "confirmed" or p.operation != operation or self._claimed:
            return None
        self._claimed = True
        return p

    async def finalize(self, confirmation_id, *, result):
        self.finalized = True


def _run(coro):
    return asyncio.run(coro)


def test_mutation_rejected_without_confirmation():
    store = FakeStore(proposal=None)  # нет подтверждённого proposal
    with allowed_ids(DRAFT_ACCOUNT_ID):  # аккаунт ок → проверяем именно confirm-гейт
        try:
            _run(
                apply_update_budget(
                    customer_id=DRAFT_ACCOUNT_ID,
                    campaign_id="1",
                    new_budget_micros=50_000_000,
                    confirmation_id="bogus",
                    confirm_store=store,
                    ads_client=object(),
                )
            )
            raise AssertionError("ожидался PermissionError")
        except PermissionError:
            pass


def test_mutation_rejected_for_foreign_account():
    # Даже с валидным confirmed proposal — мутация чужого аккаунта отклоняется замком.
    store = FakeStore(FakeProposal("update_budget", "confirmed", user_initiated=True))
    with allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            _run(
                apply_update_budget(
                    customer_id="1234567890",
                    campaign_id="1",
                    new_budget_micros=50_000_000,
                    confirmation_id="ok",
                    confirm_store=store,
                    ads_client=object(),
                )
            )
            raise AssertionError("ожидался PermissionError (чужой аккаунт)")
        except PermissionError:
            pass
    assert store.finalized is False


def test_budget_blocked_when_not_user_initiated():
    # подтверждение есть, но не прямая команда пользователя (напр. scheduler) → отказ
    store = FakeStore(FakeProposal("update_budget", "confirmed", user_initiated=False))
    with allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            _run(
                apply_update_budget(
                    customer_id=DRAFT_ACCOUNT_ID,
                    campaign_id="1",
                    new_budget_micros=50_000_000,
                    confirmation_id="x",
                    confirm_store=store,
                    ads_client=object(),
                )
            )
            raise AssertionError("ожидался PermissionError (бюджет не по команде)")
        except PermissionError:
            pass


def test_budget_applies_when_confirmed_and_user_initiated():
    # SDK-исполнитель подменяем (офлайн, без живого аккаунта) — проверяем, что оба гейта
    # пройдены, исполнитель вызван с правильным customer_id и proposal финализирован.
    import ads.mutations as mut

    def fake_sdk(client, customer_id, campaign_id, new_budget_micros, disclosed_shared_scope=False):
        return {
            "customer_id": customer_id,
            "campaign_id": str(campaign_id),
            "new_budget_micros": int(new_budget_micros),
            "applied": True,
        }

    store = FakeStore(FakeProposal("update_budget", "confirmed", user_initiated=True))
    orig = mut._apply_budget_via_sdk
    mut._apply_budget_via_sdk = fake_sdk
    try:
        with allowed_ids(DRAFT_ACCOUNT_ID):
            result = _run(
                apply_update_budget(
                    customer_id=DRAFT_ACCOUNT_ID,
                    campaign_id="42",
                    new_budget_micros=50_000_000,
                    confirmation_id="ok",
                    confirm_store=store,
                    ads_client=object(),
                )
            )
    finally:
        mut._apply_budget_via_sdk = orig
    assert result["applied"] is True
    assert result["customer_id"] == DRAFT_ACCOUNT_ID
    assert store.finalized is True


if __name__ == "__main__":
    # Быстрый прогон без pytest
    test_cyrillic_counts_as_one()
    test_cjk_counts_as_two()
    test_ensure_allowed_fail_closed_when_empty()
    test_ensure_allowed_accepts_draft_normalized()
    test_ensure_allowed_rejects_other_account()
    test_ensure_allowed_ceiling_blocks_foreign_env()
    test_ensure_read_allowed_fail_closed_when_empty()
    test_ensure_read_allowed_defaults_to_mutation_scope()
    test_ensure_read_allowed_permits_listed_child()
    test_mutation_lock_unchanged_by_read_allowlist()
    test_ensure_manager_allowed_fail_closed_when_empty()
    test_ensure_manager_allowed_accepts_configured_normalized()
    test_ensure_manager_allowed_rejects_unconfigured()
    test_mutation_rejected_without_confirmation()
    test_mutation_rejected_for_foreign_account()
    test_budget_blocked_when_not_user_initiated()
    test_budget_applies_when_confirmed_and_user_initiated()
    print("OK: все офлайн-тесты ядра безопасности прошли")
