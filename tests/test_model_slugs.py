"""Слаги НАШИХ моделей сверяются с живым каталогом OpenRouter — как конфиг Hermes, но для бота.

Класс бага. `check_model_slugs` (`deploy/hermes/lint_config.py`) закрывает ровно один файл —
`~/.hermes/config.yaml`: `_model_specs(cfg)` читает `model`, `auxiliary`, `model_routing` и больше
ничего. Собственные роли бота (`llm_*` в `core/config.py`, `LLM_*` в `.env.example`, пресеты
`/model` в `agent/router.py`) не проверял НИКТО — при том, что симптом отказа тот же самый
(`HTTP 400 … is not a valid model ID`, non-retryable ⇒ бот молчит), а путь до прода короче: слаг
попадает туда обычным коммитом, без визарда и без ручной правки конфига на VPS.

Почему это не гипотеза. Комментарий `agent/router.py:116` дословно говорит «Slug'и сверены со
списком OpenRouter» — то есть сверка была, но разовая, руками и без даты. Каталог OpenRouter
меняется еженедельно (модели снимают: так умерла `gemini-2.0-flash-exp` в том же инциденте
27.07.2026), поэтому «сверено однажды» протухает ровно так же, как протух пример в докстринге
`agent/router.py`, из которого приехал мёртвый `deepseek/deepseek-v3`.

Перечисление ролей — через `Settings.model_fields`, а не списком: новая роль `llm_*` попадает под
гард самим фактом объявления. Иначе гард закрывал бы сегодняшний набор, а не класс.

Каталог недоступен ⇒ `skip` с явной причиной, НЕ «зелено»: тест, который краснеет от чужого
таймаута, отключат целиком — вместе с той частью, которая ловит настоящий дрейф. Ровно так же
деградирует и `check_model_slugs` (там — `warn`).

Отрицательный контроль обязателен: без него зелёный тест не отличает «все слаги живы» от
«собиратель ничего не нашёл и проверять было нечего».
"""

from __future__ import annotations

import importlib.util
import re
import sys

import pytest

from core.config import Settings
from tests._docs_paths import ROOT

# Каталог тянем функцией самого линта, а не второй копией запроса: один эндпоинт, один таймаут,
# одна деградация «нет сети → None». Модуль лежит вне пакета, поэтому грузим по пути.
_SPEC = importlib.util.spec_from_file_location(
    "hermes_lint_config_slugs", ROOT / "deploy" / "hermes" / "lint_config.py"
)
assert _SPEC and _SPEC.loader
lint_config = importlib.util.module_from_spec(_SPEC)
# Регистрация ДО exec_module обязательна — причина в tests/test_hermes_config_lint.py:41.
sys.modules[_SPEC.name] = lint_config
_SPEC.loader.exec_module(lint_config)


# `vendor/model` — ровно один слэш, без пробелов. Абсолютный путь (`/opt/...`) и URL под шаблон не
# подходят, поэтому эвристика по `.env.example` не собирает мусор (проверено на живом файле).
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._:-]*$")

_ENV_ASSIGN_RE = re.compile(
    r"^([A-Z][A-Z0-9_]*)=([a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._:-]*)\s*(?:#.*)?$", re.M
)


def collect_slugs() -> list[tuple[str, str]]:
    """`(источник, слаг)` для КАЖДОГО места, где репозиторий называет модель для своих вызовов.

    Мест четыре, и опасны все: дефолт роли уезжает в прод молча, `.env.example` копируют в боевой
    `.env` как есть, а пресеты `/model` оператор видит кнопкой — мёртвый слаг там отказывает уже
    после нажатия.
    """
    out: list[tuple[str, str]] = []

    for name, field in Settings.model_fields.items():
        if not name.startswith("llm_"):
            continue
        value = field.default
        if isinstance(value, str) and _SLUG_RE.match(value):
            out.append((f"core/config.py:{name}", value))

    env_example = ROOT / ".env.example"
    if env_example.exists():
        text = env_example.read_text(encoding="utf-8")
        for key, value in _ENV_ASSIGN_RE.findall(text):
            out.append((f".env.example:{key}", value))

    from llm import router

    for slug in router.AB_CANDIDATES:
        out.append(("agent/router.py:AB_CANDIDATES", slug))
    for slug in router._DEFAULT_CHOICES:
        out.append(("agent/router.py:_DEFAULT_CHOICES", slug))
    for slug in router.MODEL_LABELS:
        out.append(("agent/router.py:MODEL_LABELS", slug))

    return out


def dead_slugs(slugs: list[tuple[str, str]], catalog: set[str]) -> list[tuple[str, str]]:
    """Пары, которых в каталоге нет. Дубли схлопываются — один слаг в трёх списках = одна жалоба."""
    seen: dict[str, str] = {}
    for source, slug in slugs:
        if slug not in catalog and slug not in seen:
            seen[slug] = source
    return sorted((source, slug) for slug, source in seen.items())


def test_collector_sees_every_place_we_name_a_model():
    """Собиратель не пуст и покрывает все четыре источника — иначе проверка ниже холостая."""
    found = collect_slugs()
    sources = {source.split(":")[0] for source, _ in found}
    assert sources == {
        "core/config.py",
        ".env.example",
        "agent/router.py",
    }, f"источники разъехались: {sorted(sources)}"
    assert len(found) >= 10, f"собрано подозрительно мало слагов: {found}"


def test_dead_slug_is_caught():
    """Отрицательный контроль: слаг вне каталога обязан быть найден, с указанием источника."""
    catalog = {slug for _, slug in collect_slugs()}
    catalog.discard("deepseek/deepseek-chat")
    dead = dead_slugs(collect_slugs(), catalog)
    assert [slug for _, slug in dead] == ["deepseek/deepseek-chat"]


def test_all_our_model_slugs_exist_in_openrouter_catalog():
    """Живая сверка. Нет каталога ⇒ skip с причиной, а не молчаливое «зелено»."""
    catalog = lint_config.fetch_openrouter_catalog()
    if catalog is None:
        pytest.skip(
            f"каталог {lint_config._OPENROUTER_MODELS_URL} недоступен — слаги НЕ проверены; "
            "прогнать с сетью до деплоя"
        )

    dead = dead_slugs(collect_slugs(), catalog)
    assert not dead, (
        "слаги, которых нет у OpenRouter (HTTP 400 non-retryable ⇒ молчание бота): "
        + (", ".join(f"{source} → {slug!r}" for source, slug in dead))
    )
