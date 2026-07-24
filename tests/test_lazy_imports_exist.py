"""Гард класса: ленивый импорт ВНУТРИ функции не должен ссылаться на несуществующее имя.

Пойман на живом дефекте: advisor/service.py импортировал `from clients.store import ClientStore`
(класс называется ClientProfileStore) внутри try/except Exception → ImportError глотался, и §20.6
(профиль клиента как контекст advisor) + брендозащита минус-слов НЕ работали НИКОГДА и МОЛЧА.
mypy это видел, но в CI он continue-on-error → защиты не было.

Локальные импорты в этом проекте распространены осознанно (разрыв циклов, ленивая загрузка SDK),
и почти все они живут в try/except — то есть опечатка в имени = тихо мёртвая фича, а не падение.
Здесь проверяем ВСЕ импорты внутренних пакетов: имя обязано существовать (символ или подмодуль).
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]

# Внутренние пакеты (сторонние не трогаем — их импортируемость проверяет сборка окружения).
PACKAGES = {
    "adcopy",
    "ads",
    "advisor",
    "agent",
    "audit",
    "bot",
    "clients",
    "confirm",
    "core",
    "db",
    "keywords",
    "reports",
    "scheduler",
}


def _iter_sources():
    for pkg in sorted(PACKAGES):
        yield from sorted((ROOT / pkg).rglob("*.py"))


def _exists(module: str, name: str) -> bool:
    mod = importlib.import_module(module)
    if hasattr(mod, name):
        return True
    try:  # `from pkg import submodule` — символа в __init__ нет, но подмодуль существует
        return importlib.util.find_spec(f"{module}.{name}") is not None
    except (ImportError, AttributeError, ValueError):
        return False  # module — не пакет ⇒ имени просто нет


def test_internal_imports_resolve():
    broken: list[str] = []
    for path in _iter_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
                continue
            if node.module.split(".")[0] not in PACKAGES:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                try:
                    ok = _exists(node.module, alias.name)
                except Exception as e:  # noqa: BLE001 — сам модуль не импортируется → это тоже дефект
                    broken.append(
                        f"{path.relative_to(ROOT)}:{node.lineno} — "
                        f"{node.module} не импортируется ({type(e).__name__})"
                    )
                    continue
                if not ok:
                    broken.append(
                        f"{path.relative_to(ROOT)}:{node.lineno} — "
                        f"в {node.module} нет имени {alias.name!r}"
                    )
    assert not broken, (
        "ленивые импорты ссылаются на несуществующие имена (except их глотает):\n"
        + "\n".join(broken)
    )
