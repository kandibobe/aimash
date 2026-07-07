"""P4 (живой тест заказчика 2026-07-06): рантайм-админы (env ∪ БД, зеркало рантайм-whitelist).

Раньше админка = только env ADMIN_CHAT_IDS (правка .env + рестарт VPS). Теперь is_admin =
env ∪ таблица admins; /addadmin//removeadmin — без рестарта. Fail-closed: сбой БД ⇒ только env;
пустые оба ⇒ админов нет. Гарды от самоблокировки: env-админ неснимаем, нельзя снять себя,
нельзя снять последнего. Класс-гарды: (1) каждый вызов _is_admin в хендлерах — с await
(неawait-нутая корутина truthy = fail-open); (2) прямые settings.admin_ids вне core/ — только
разрешённые env-guard места.
"""

from __future__ import annotations

import re
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.access import (  # noqa: E402
    _invalidate_admin_cache,
    add_admin,
    admin_ids_all,
    ensure_account_allowed_for_user,
    is_admin,
    list_admins,
    remove_admin,
)
from core.config import settings  # noqa: E402
from db.session import init_db  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NEW_ADMIN = 800800800


@contextmanager
def _env_admins(value: str):
    orig = settings.admin_chat_ids
    settings.admin_chat_ids = value
    _invalidate_admin_cache()
    try:
        yield
    finally:
        settings.admin_chat_ids = orig
        _invalidate_admin_cache()


@pytest.fixture(autouse=True)
async def _clean():
    await init_db()
    await remove_admin(NEW_ADMIN)
    _invalidate_admin_cache()
    yield
    await remove_admin(NEW_ADMIN)
    _invalidate_admin_cache()


async def test_env_admin_passes_without_db():
    with _env_admins("111,222"):
        assert await is_admin(111) is True
        assert await is_admin(222) is True


async def test_nobody_admin_when_both_empty_fail_closed():
    with _env_admins(""):
        assert await is_admin(NEW_ADMIN) is False
        assert await is_admin(None) is False


async def test_addadmin_effective_without_restart_and_idempotent():
    with _env_admins("111"):
        assert await is_admin(NEW_ADMIN) is False
        assert await add_admin(NEW_ADMIN, added_by=111, note="Anton") is True
        assert await is_admin(NEW_ADMIN) is True  # кэш инвалидирован сразу
        assert await add_admin(NEW_ADMIN, added_by=111) is False  # идемпотентно
        rows = await list_admins()
        assert sum(1 for r in rows if r.chat_id == NEW_ADMIN) == 1


async def test_removeadmin_revokes():
    with _env_admins("111"):
        await add_admin(NEW_ADMIN)
        assert await is_admin(NEW_ADMIN) is True
        await remove_admin(NEW_ADMIN)
        assert await is_admin(NEW_ADMIN) is False


async def test_admin_ids_all_union_env_and_db():
    """Рассылки (алерты/дайджест/старт-пинг/баг-форвард) идут на env ∪ БД — рантайм-админ
    получает их БЕЗ рестарта."""
    with _env_admins("111"):
        await add_admin(NEW_ADMIN, added_by=111)
        ids = await admin_ids_all()
        assert 111 in ids and NEW_ADMIN in ids


async def test_db_failure_degrades_to_env_only(monkeypatch):
    """Fail-closed: сбой БД ⇒ рантайм-админы НЕ работают, env-админ продолжает работать."""
    import core.access as ca

    with _env_admins("111"):
        await add_admin(NEW_ADMIN)
        _invalidate_admin_cache()

        class _BoomSession:
            def __call__(self, *a, **k):
                raise RuntimeError("db down")

        monkeypatch.setattr(ca, "Session", _BoomSession())
        assert await is_admin(111) is True  # env работает
        assert await is_admin(NEW_ADMIN) is False  # рантайм-админ — отказ (не fail-open)
        assert await admin_ids_all() == {111}


async def test_runtime_admin_bypasses_per_user_read_grant(monkeypatch):
    """Read-bypass (core.access:170) работает и для РАНТАЙМ-админа: enforced-режим, гранта нет —
    ensure_account_allowed_for_user пропускает. Мутации этим не открываются (отдельный замок)."""
    monkeypatch.setattr(settings, "account_access_mode", "enforced")
    with _env_admins(""):
        await add_admin(NEW_ADMIN)
        await ensure_account_allowed_for_user(NEW_ADMIN, "1234567890")  # не бросает
        with pytest.raises(PermissionError):
            await ensure_account_allowed_for_user(999999999, "1234567890")  # не-админ — отказ


async def test_runtime_admin_does_not_open_mutations():
    """Golden rule 9: админка (чтение/управление доступом) НЕ открывает мутации — ensure_allowed
    (Draft-потолок) отдельный чокпойнт."""
    from ads.client import ensure_allowed

    await add_admin(NEW_ADMIN)
    with pytest.raises(PermissionError):
        ensure_allowed("1234567890")


# ── Класс-гарды на уровне исходников ────────────────────────────────────────────────
def test_every_handler_is_admin_call_is_awaited():
    """_is_admin стал async: неawait-нутый вызов — truthy-корутина = FAIL-OPEN. Каждый вызов
    в bot/handlers/ обязан быть `await _is_admin(...)` (или определение/докстринга)."""
    bad: list[str] = []
    for p in (ROOT / "bot" / "handlers").glob("*.py"):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            s = line.strip()
            if "_is_admin(" not in s or s.startswith("#"):
                continue
            if "def _is_admin(" in s or "`" in s:  # определение / докстрока с примером
                continue
            if "await _is_admin(" not in s:
                bad.append(f"{p.name}:{i}: {s}")
    assert not bad, "неawait-нутые вызовы _is_admin (fail-open):\n" + "\n".join(bad)


def test_no_direct_admin_ids_authorization_outside_core():
    """Единственная точка проверки админства — core.access.is_admin. Прямые обращения к
    settings.admin_ids вне core/ разрешены ТОЛЬКО в env-guard местах /removeadmin и /admins
    (проверка «это env-админ» / список env-админов), иначе рантайм-админы молча игнорируются."""
    allowed = {
        ("bot/handlers/commands.py", "in bm.settings.admin_ids"),  # removeadmin: env неснимаем
        ("bot/handlers/commands.py", "sorted(bm.settings.admin_ids)"),  # /admins: показать env
    }
    offenders: list[str] = []
    for sub in ("bot", "scheduler", "agent", "ads", "reports", "keywords", "clients"):
        base = ROOT / sub
        if not base.exists():
            continue
        for p in base.rglob("*.py"):
            rel = p.relative_to(ROOT).as_posix()
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if ".admin_ids" not in line or line.strip().startswith("#"):
                    continue
                if any(rel == f and pat in line for f, pat in allowed):
                    continue
                offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, "прямой admin_ids вне core/ (рантайм-админы игнорируются):\n" + "\n".join(
        offenders
    )


def test_admins_migration_chain_is_linear():
    """0021 продолжает цепочку от 0020 (один head)."""
    src = (ROOT / "migrations" / "versions" / "0021_admins_runtime.py").read_text(encoding="utf-8")
    assert re.search(r'revision: str = "0021_admins_runtime"', src)
    assert re.search(r'down_revision: str \| None = "0020_audit_chat_index"', src)
