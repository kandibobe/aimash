"""Инвариант упаковки: каждый top-level пакет репозитория попадает в дистрибутив.

Класс бага, который тест закрывает: `[tool.setuptools.packages.find] include` в `pyproject.toml`
перечисляет пакеты ВРУЧНУЮ (плоская раскладка, авто-определение setuptools падает на «Multiple
top-level packages discovered»), и новый пакет туда добавить забывают. Промах невидим на обоих
штатных путях — в Docker идёт `COPY . .`, а pytest запускается из корня репозитория, поэтому
недостающий пакет всё равно импортируется из CWD.

Всплывает он только там, где установка честная: `pip install -e .` в отдельный venv и запуск из
другой директории. Именно так ставится MCP-READ на отдельном хосте под Hermes — и там отсутствие
`app` и `audit` роняло `python -m mcp_server` на импорте `app.bootstrap` / `audit.collect`,
то есть инструмент выглядел настроенным и не работал.
"""

from __future__ import annotations

import pathlib
import tomllib

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _declared_prefixes() -> set[str]:
    """Имена пакетов из `include`, без хвостового `*`."""
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = data["tool"]["setuptools"]["packages"]["find"]["include"]
    return {p.removesuffix("*") for p in patterns}


def _actual_packages() -> set[str]:
    """Top-level каталоги с `__init__.py` — то, что setuptools обязан установить."""
    return {p.parent.name for p in _ROOT.glob("*/__init__.py")}


def test_every_toplevel_package_is_declared():
    missing = _actual_packages() - _declared_prefixes()
    assert not missing, (
        f"пакеты {sorted(missing)} есть в репозитории, но не перечислены в "
        f"[tool.setuptools.packages.find] include — `pip install -e .` их не поставит, "
        f"и импорт упадёт при запуске из другой директории"
    )


def test_no_stale_declarations():
    """Обратная сторона: перечисленного пакета больше нет (переименовали/удалили).

    Не ошибка сборки, но верный признак, что список разошёлся с деревом — а разойдясь в одну
    сторону, он разойдётся и в другую."""
    stale = _declared_prefixes() - _actual_packages()
    assert not stale, (
        f"в include перечислены {sorted(stale)}, но таких пакетов в репозитории нет — "
        f"список разошёлся с деревом"
    )
