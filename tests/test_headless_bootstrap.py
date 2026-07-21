"""Гард РАЗВЯЗКИ headless-контура (пивот Hermes/MCP): ads+confirm поднимаются БЕЗ bot/aiogram.

`app/bootstrap.py` сеет модульные глобалы `ads/client.py` для MCP-сервера (Контур A), который
`bot` НЕ импортирует. Инвариант, на котором стоит вся форма `mcp_server/` как «тонкой обёртки»:
ни `app.bootstrap`, ни `ads.read/client/mutations`, ни `confirm.gate/store` не тянут `bot.*` /
`aiogram.*` в `sys.modules`. Ломается ТИХО — кто-то добавил `from bot ...` в `ads/`/`confirm/`, и
MCP-процесс молча потащит весь Telegram-слой (в пределе — второй event loop бота). Draft этого не
покажет: там всё в одном процессе.

Проверяется в ОТДЕЛЬНОМ интерпретаторе: под pytest `conftest` уже импортирует `bot`, поэтому
in-process проверка `sys.modules` была бы ложноположительной. subprocess даёт чистый старт.

Родственно будущему `tests/test_hermes_isolation.py` (И4/И5): недоступность bot-путей из
headless-контура — предпосылка изоляции инструментов.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Тело зонда: импортируется в чистом процессе, падает assert'ом при утечке bot/aiogram.
_PROBE = """
import sys
import app.bootstrap
import db.session, ads.client, ads.read, ads.mutations, confirm.gate, confirm.store

leaked = sorted(
    m for m in sys.modules
    if m == "bot" or m.startswith("bot.") or m == "aiogram" or m.startswith("aiogram.")
)
assert not leaked, "headless-слой подтянул: " + ", ".join(leaked)
assert callable(app.bootstrap.bootstrap_ads_layer)
print("clean")
"""


def test_headless_ads_layer_imports_without_bot() -> None:
    # ENV=dev: тестируем чистоту импорта, не prod-конфиг (в prod core.config fail-fast на пустом
    # whitelist/ключе). PYTHONPATH: subprocess стартует не из pytest-контекста.
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "ENV": "dev", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",  # Windows: иначе cp1251 ломает кириллицу в сообщении assert'а
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"headless-импорт не чист:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "clean" in proc.stdout


# ── Поведение самого bootstrap: что критично, что fail-soft ────────────────────────
# `callable(bootstrap_ads_layer)` (выше, в зонде) доказывает только существование имени. Ниже —
# контракт, ради которого функция и существует: ОДНА точка старта ads-слоя для бота и MCP.


async def test_bootstrap_seeds_all_three_globals_in_order(monkeypatch):
    """Порядок: сперва БД, потом сидеры `ads.client`. Обратный порядок означал бы `build_client`
    без расшифрованных per-account токенов — на Draft (единый .env-токен) это НЕ видно, а на
    боевом мультиаккаунте даёт `PERMISSION_DENIED` на дочернем аккаунте."""
    import ads.client as ac

    from app.bootstrap import bootstrap_ads_layer
    from db import session as dbs

    calls: list[str] = []

    async def _fake_init_db():
        calls.append("init_db")

    async def _fake_schema_gate():
        calls.append("schema_gate")

    async def _fake_oauth():
        calls.append("load_oauth_cache")

    def _fake_build():  # синхронная — bootstrap зовёт её через asyncio.to_thread
        calls.append("build_client")

    async def _fake_discover():
        calls.append("discover_read_children")

    monkeypatch.setattr(dbs, "init_db", _fake_init_db)
    monkeypatch.setattr(dbs, "assert_schema_at_head", _fake_schema_gate)
    monkeypatch.setattr(ac, "load_oauth_cache", _fake_oauth)
    monkeypatch.setattr(ac, "build_client", _fake_build)
    monkeypatch.setattr(ac, "discover_read_children", _fake_discover)

    await bootstrap_ads_layer()

    assert calls == [
        "init_db",
        "schema_gate",
        "load_oauth_cache",
        "build_client",
        "discover_read_children",
    ]


async def test_bootstrap_raises_on_db_but_survives_seeder_failures(monkeypatch):
    """БД критична (fail-closed, правило 10), три сидера — нет. Если сделать сидеры критичными,
    бот перестанет стартовать на любом мигании Google Ads API; если сделать БД некритичной —
    процесс поднимется без `ConfirmStore`, то есть без денежного гейта.

    Заодно правило 5: наружу идёт ТИП исключения, а не `str(e)` — в тексте ошибки asyncpg лежит DSN
    с паролем."""
    import ads.client as ac

    from app.bootstrap import bootstrap_ads_layer
    from db import session as dbs

    async def _boom_db():
        # Форма как у настоящей ошибки asyncpg — с DSN и паролем. gitleaks:allow (пароль выдуман).
        raise RuntimeError("connection to server at 10.0.0.1, password=hunter2 failed")

    async def _boom_async():
        raise RuntimeError("ads api hiccup")

    def _boom_sync():
        raise RuntimeError("ads api hiccup")

    monkeypatch.setattr(dbs, "init_db", _boom_db)
    with pytest.raises(RuntimeError) as ei:
        await bootstrap_ads_layer()
    assert "RuntimeError" in str(ei.value)
    assert "hunter2" not in str(ei.value) and "10.0.0.1" not in str(ei.value)
    assert ei.value.__cause__ is None  # `from None`: цепочка с DSN не всплывает у вызывающего

    # БД поднялась — падение любого из трёх сидеров старт не роняет.
    async def _ok():
        return None

    monkeypatch.setattr(dbs, "init_db", _ok)
    monkeypatch.setattr(dbs, "assert_schema_at_head", _ok)
    monkeypatch.setattr(ac, "load_oauth_cache", _boom_async)
    monkeypatch.setattr(ac, "build_client", _boom_sync)
    monkeypatch.setattr(ac, "discover_read_children", _boom_async)
    await bootstrap_ads_layer()  # не должно бросить


async def test_bootstrap_propagates_schema_gate_refusal(monkeypatch):
    """Схема-гейт — не декорация: его отказ обязан дойти до вызывающего. Иначе MCP поднимется на
    отставшей схеме и будет отдавать агенту данные, форму которых код понимает неправильно."""
    from app.bootstrap import bootstrap_ads_layer
    from db import session as dbs

    async def _ok():
        return None

    async def _stale():
        raise RuntimeError("схема БД отстала от кода: alembic_version=['0001'], head=['0042']")

    monkeypatch.setattr(dbs, "init_db", _ok)
    monkeypatch.setattr(dbs, "assert_schema_at_head", _stale)

    with pytest.raises(RuntimeError, match="отстала"):
        await bootstrap_ads_layer()
