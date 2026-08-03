"""Гард развязки фоновых контуров от `bot/` — разбором ИСХОДНИКА, а не sys.modules.

Почему отдельный тест, а не строка в `tests/test_headless_bootstrap.py`: тот зонд импортирует модуль
в чистом процессе и смотрит `sys.modules`. Функция-локальный transport-импорт туда
ничего не кладёт до первого ВЫЗОВА, поэтому import-зонд молчит на связности, которая живёт внутри
джобы (проверено отрицательным контролем 23.07.2026: `scheduler.jobs` в списке зонда → всё равно
"clean", при шести живых импортах `bot` в теле функций). Ровно такой ленивой связностью
`scheduler/jobs.py` и держался за кнопочный слой: цикл `bot.main` ↔ `scheduler.jobs` разрывали
поздним импортом, а не развязкой (SPEC.md §5.3, мина C4).

AST видит импорт независимо от того, где он написан, и не путает код со строками/комментариями
(`grep` спотыкался бы о каждое упоминание `bot.main` в докстрингах — их тут десятки).

Что стережём: перечисленные пакеты обязаны подниматься процессом БЕЗ legacy Telegram-слоя.
Планировщик — отдельный контейнер, `mcp_server/` — тул-слой Hermes, остальное — ядро,
которое переезжает без изменений. Одна строка `from bot import ux` в любом из них возвращает
зависимость от архивируемого пакета, и заметить это без гарда нельзя: в bot-процессе всё работает.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO_ROOT = Path(__file__).resolve().parents[1]

# Пакеты, которым legacy Telegram-слой не положен.
BOT_FREE_PACKAGES = (
    "scheduler",
    "advisor",
    "reports",
    "core",
    "ads",
    "confirm",
    "mcp_server",
    "clients",
    "adcopy",
    "keywords",
    "audit",
    "db",
    "app",
    "analysis",
    "llm",
)


# После v3 cleanup исключений нет: scheduler использует локальный httpx-транспорт Bot API.
AIOGRAM_ALLOWED: frozenset[str] = frozenset()


def _forbidden_imports(path: Path, *, aiogram_ok: bool) -> list[tuple[int, str]]:
    """Все импорты `bot`/`aiogram` в файле, включая написанные внутри функций. (строка, текст)."""
    banned = ("bot.",) if aiogram_ok else ("bot.", "aiogram")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "bot" or alias.name.startswith(banned):
                    bad.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            # level>0 — относительный импорт: внутри пакета, до `bot` не дотянется.
            if node.level == 0 and (mod == "bot" or mod.startswith(banned)):
                names = ", ".join(a.name for a in node.names)
                bad.append((node.lineno, f"from {mod} import {names}"))
    return bad


@pytest.mark.parametrize("package", BOT_FREE_PACKAGES)
def test_package_has_no_telegram_imports_anywhere(package: str) -> None:
    """Ни одного `import bot`/`import aiogram` — ни на уровне модуля, ни внутри функции."""
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / package).rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, text in _forbidden_imports(path, aiogram_ok=rel in AIOGRAM_ALLOWED):
            offenders.append(f"{rel}:{lineno}: {text}")
    assert not offenders, (
        f"пакет `{package}/` тянет Telegram-слой:\n  " + "\n  ".join(offenders) + "\n"
        "Кнопочный слой архивируется (SPEC.md §5.3) — фоновый контур обязан работать без него. "
        "Клавиатуры берутся через порт `scheduler/delivery.py`, тексты — из `core/texts.py`, "
        "локализация — из `core/i18n.py`, отправка вложений — из `scheduler/transport.py`."
    )


def test_aiogram_allowlist_is_not_stale() -> None:
    """Исключение из запрета aiogram обязано указывать на существующий файл, и сам этот файл —
    быть чист от `bot/`. Иначе переименование модуля молча превратило бы список в фикцию."""
    for rel in sorted(AIOGRAM_ALLOWED):
        path = REPO_ROOT / rel
        assert path.is_file(), f"в AIOGRAM_ALLOWED мёртвая запись: {rel}"
        assert not _forbidden_imports(path, aiogram_ok=True), (
            f"{rel} освобождён от запрета aiogram, но не от запрета `bot/` — "
            "исключение сделано для транспорта, а не для кнопочного слоя"
        )


def test_legacy_packages_and_dependency_are_absent() -> None:
    assert not (REPO_ROOT / "bot").exists()
    assert not (REPO_ROOT / "agent").exists()
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    constraints = (REPO_ROOT / "constraints.txt").read_text(encoding="utf-8")
    assert "aiogram" not in pyproject.lower()
    assert "aiogram" not in constraints.lower()


def test_compose_v3_has_no_legacy_bot_service() -> None:
    import yaml

    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert "bot" not in services
    assert {"postgres", "migrate", "scheduler", "mcp", "backup"} <= set(services)
    assert services["scheduler"]["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["mcp"]["command"] == ["python", "-m", "mcp_server"]


def test_runtime_image_defaults_to_mcp() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'CMD ["python", "-m", "mcp_server"]' in dockerfile
    assert 'CMD ["python", "-m", "bot.main"]' not in dockerfile


def test_standalone_scheduler_writes_heartbeat() -> None:
    """HEALTHCHECK в образе один на все сервисы и смотрит на свежесть heartbeat-файла. Писал его
    только `bot/main.py`, поэтому энтрипоинт планировщика без `heartbeat_loop()` дал бы вечно
    unhealthy контейнер — то есть сломанный `depends_on: service_healthy` и красный пост-деплойный
    гейт CI при полностью рабочих джобах."""
    src = (REPO_ROOT / "scheduler" / "__main__.py").read_text(encoding="utf-8")
    tree = ast.parse(src, filename="scheduler/__main__.py")
    called = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "heartbeat_loop" in called, (
        "scheduler/__main__.py не поднимает heartbeat_loop() — контейнер будет вечно unhealthy"
    )

    from scheduler.__main__ import _ROLE

    from db.session import _ROLE_LOCK_KEYS

    assert _ROLE == "scheduler" and _ROLE in _ROLE_LOCK_KEYS


def test_delivery_port_is_text_only_until_filled() -> None:
    """Незаполненный порт отдаёт None (standalone-планировщик шлёт текст), а не падает. Опечатка в
    имени — наоборот, падает сразу: «кнопок нет и непонятно почему» — самый дорогой исход."""
    from scheduler import delivery

    saved = dict(delivery._BUILDERS)
    delivery._BUILDERS.clear()
    try:
        assert delivery.markup(delivery.ADVISE_FEEDBACK, "uid", "ru") is None
        assert delivery.registered() == frozenset()
        with pytest.raises(ValueError):
            delivery.markup("advise_feedbak", "uid", "ru")  # опечатка
        with pytest.raises(ValueError):
            delivery.register("advise_feedbak", lambda *a, **kw: None)

        # Сбой построителя гасится: цифры в сообщении важнее кнопки под ними.
        def _boom(*_a, **_kw):
            raise RuntimeError("клавиатура не собралась")

        delivery.register(delivery.ADVISE_FEEDBACK, _boom)
        assert delivery.markup(delivery.ADVISE_FEEDBACK, "uid", "ru") is None
    finally:
        delivery._BUILDERS.clear()
        delivery._BUILDERS.update(saved)
