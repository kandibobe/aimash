"""Гард класса багов: любое обращение `bm.<name>` в модулях bot/handlers/* должно
резолвиться на реальный атрибут bot.main (позднее связывание, CLAUDE.md «Структура»).

Prod-инцидент 2026-07: `kw_params_kb`/`kw_geo_kb` были определены в bot/keyboards.py, но
ЗАБЫТЫ в блоке `from bot.keyboards import (...)` в bot/main.py. Обращение `bm.kw_params_kb`
падало `AttributeError: module '__main__' has no attribute 'kw_params_kb'` только В РАНТАЙМЕ
(экран параметров подбора ключей был мёртв) — офлайн-тесты, зовущие хендлеры напрямую, этого
не ловили. Этот тест статически (AST) собирает ВСЕ обращения `bm.<attr>` по каждому
хендлер-модулю и проверяет, что атрибут существует на bot.main — забытый импорт краснит CI.
"""

from __future__ import annotations

import ast
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot.main as bm  # noqa: E402  (импорт запускает register_all → ре-экспорт имён)

_HANDLERS_DIR = Path(bm.__file__).resolve().parent / "handlers"


def _main_alias(tree: ast.Module) -> str | None:
    """Имя, под которым модуль импортировал bot.main (обычно `bm`). None — если не импортит."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "bot.main":
                    return alias.asname or "bot.main"
    return None


def _referenced_attrs(tree: ast.Module, alias: str) -> set[tuple[str, int]]:
    """Все `alias.<attr>` в модуле → множество (attr, lineno)."""
    out: set[tuple[str, int]] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == alias
        ):
            out.add((node.attr, node.lineno))
    return out


def test_all_bm_references_resolve_on_main():
    """Каждое `bm.<attr>` во всех bot/handlers/*.py существует на bot.main."""
    missing: list[str] = []
    modules = sorted(_HANDLERS_DIR.glob("*.py"))
    assert modules, "не найдено ни одного хендлер-модуля — сломан путь bot/handlers?"

    for path in modules:
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        alias = _main_alias(tree)
        if alias is None:
            continue  # модуль не завязан на bot.main через late-binding
        for attr, lineno in sorted(_referenced_attrs(tree, alias)):
            if not hasattr(bm, attr):
                missing.append(f"{path.name}:{lineno} → bm.{attr}")

    assert not missing, (
        "Обращение bm.<name> не резолвится на bot.main (забыт импорт/ре-экспорт в bot/main.py):\n  "
        + "\n  ".join(missing)
    )


def _fake_module(name: str, attr: str) -> types.ModuleType:
    mod = types.ModuleType(name)

    def handler():  # публичное имя хендлера, «определённое» в этом модуле
        return name

    handler.__module__ = name
    setattr(mod, attr, handler)
    return mod


def test_reexport_raises_on_shadowed_name():
    """Волна 3: тень имени при ре-экспорте — ОШИБКА, а не warning.

    Раньше коллизия логировалась и имя пропускалось: bm.<name> отдавал объект bot.main, а не
    хендлер, — monkeypatch тестов бил мимо, сам хендлер оставался недостижим по имени. Молча.
    Теперь падаем на импорте: CI/старт бота видят конфликт сразу."""
    assert hasattr(bm, "settings")  # любое существующее публичное имя main
    with pytest.raises(RuntimeError, match="затеняет"):
        bm._reexport_handlers([_fake_module("fake_handlers_shadow", "settings")])


def test_reexport_binds_new_name():
    """Позитивная ветка: неконфликтующее имя действительно появляется на bot.main."""
    mod = _fake_module("fake_handlers_ok", "aimash_probe_handler")
    try:
        bm._reexport_handlers([mod])
        assert bm.aimash_probe_handler() == "fake_handlers_ok"
    finally:
        vars(bm).pop("aimash_probe_handler", None)
