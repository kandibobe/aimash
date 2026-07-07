"""2.3 (аудит 2026-07-06): ЯВНОЕ чтение неактивных (CANCELED/SUSPENDED) дочерних наших MCC.

Инварианты безопасности: неактивные НЕ в _READ_DISCOVERED ⇒ не в авто-пикерах/scheduler и НЕ
расширяют allowed_ceiling (потолок мутаций, golden rule 9); ensure_read_allowed без explicit —
отказ (байт-в-байт старое поведение); мутация на неактивный — ВСЕГДА отказ (ensure_allowed).
"""

from __future__ import annotations

import pathlib
import sys
from contextlib import contextmanager

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import ads.client as ac  # noqa: E402
from ads.read import ChildAccount  # noqa: E402
from core.config import settings  # noqa: E402

DRAFT = "7753643025"
INACTIVE = "3334445556"


@contextmanager
def _cfg(read_ids: str = "", allowed: str = DRAFT):
    pa, pr = settings.google_ads_allowed_customer_ids, settings.google_ads_read_customer_ids
    settings.google_ads_allowed_customer_ids = allowed
    settings.google_ads_read_customer_ids = read_ids
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = pa
        settings.google_ads_read_customer_ids = pr


@pytest.fixture(autouse=True)
def _seed_inactive():
    ac.set_discovered_read_children([])
    ac.set_discovered_inactive_children_meta(
        [
            ChildAccount(
                id=INACTIVE,
                name="Старый",
                currency="UAH",
                manager=False,
                level=1,
                status="CANCELED",
            )
        ]
    )
    yield
    ac.set_discovered_inactive_children_meta([])
    ac.set_discovered_read_children([])


def test_inactive_not_in_discovered_and_not_in_ceiling():
    assert INACTIVE not in ac.discovered_read_children()  # авто-поверхность не расширена
    with _cfg():
        assert INACTIVE not in ac.allowed_ceiling()  # потолок мутаций не расширен (golden rule 9)


def test_ensure_read_allowed_explicit_only():
    with _cfg(read_ids=DRAFT):
        with pytest.raises(PermissionError):
            ac.ensure_read_allowed(INACTIVE)  # без explicit — старое поведение (отказ)
        ac.ensure_read_allowed(INACTIVE, explicit=True)  # явный запрос оператора — ок


def test_mutation_on_inactive_always_denied():
    with _cfg(read_ids=DRAFT, allowed=DRAFT):
        with pytest.raises(PermissionError):
            ac.ensure_allowed(INACTIVE)  # мутационный замок не знает про explicit вообще


def test_explicit_does_not_open_foreign_ids():
    with _cfg(read_ids=DRAFT):
        with pytest.raises(PermissionError):
            # чужой id (не в meta неактивных, не в read-наборе) — отказ даже с explicit
            ac.ensure_read_allowed("9990001112", explicit=True)


async def test_resolve_by_name_finds_inactive_with_grant(monkeypatch):
    from core.access import grant_account_access, resolve_read_account, revoke_account_access
    from db.session import init_db

    await init_db()
    monkeypatch.setattr(settings, "account_access_mode", "enforced")
    chat = 6401
    await grant_account_access(chat, INACTIVE)
    try:
        with _cfg(read_ids=DRAFT):
            assert await resolve_read_account(chat, "Старый") == INACTIVE  # имя из meta неактивных
            assert await resolve_read_account(chat, INACTIVE) == INACTIVE  # и по id
    finally:
        await revoke_account_access(chat, INACTIVE)
