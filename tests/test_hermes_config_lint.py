"""К10-гард: конфиг-линт Hermes жив и эталоны репо ему не противоречат.

Класс бага. `deploy/hermes/lint_config.py` объявлен в CLAUDE.md и SPEC §17 единственным
механизмом против К10 — «Hermes МОЛЧА игнорирует неизвестные ключи». По факту он **падал**
`TypeError` на `deploy/hermes/config.yaml`, главном охраняемом файле: `_get()` отдаёт истинный
сентинел `_MISSING`, `or {}` его не подменяет, итерация по `object()` роняет процесс. Крах шёл
ВТОРЫМ правилом из пяти, поэтому `check_telegram_gates`, `check_toolsets` и `check_hardening` не
исполнялись НИКОГДА — и за ними успели накопиться четыре реальных нарушения, включая отсутствие
`skills.inline_shell: false` (золотое правило 14 / К1: скил исполняет shell из своего markdown
в обход approvals).

Почему это тест, а не «не забыть запустить». Линт не звался ни из CI, ни из pre-commit, ни из
одного теста — grep по репо давал только упоминания в CLAUDE.md и README.md. Ровно поэтому его
смерть, отсутствие PIN.json и дрейф `inline_shell` прожили незамеченными одновременно.
`lint(cfg)` — чистая функция, явно спроектированная под вызов из теста («тест кормит словарём
напрямую», lint_config.py), то есть тест не был написан не из-за архитектуры.

Отрицательный контроль обязателен: без него зелёный тест не отличает «линт проверил и всё
хорошо» от «линт снова молчит».
"""

from __future__ import annotations

import copy
import importlib.util
import sys

import pytest
import yaml

from tests._docs_paths import ROOT

HERMES_DIR = ROOT / "deploy" / "hermes"
_SPEC = importlib.util.spec_from_file_location("hermes_lint_config", HERMES_DIR / "lint_config.py")


def _load_lint():
    if _SPEC is None or _SPEC.loader is None:  # pragma: no cover — файл на месте
        pytest.skip("deploy/hermes/lint_config.py недоступен")
    module = importlib.util.module_from_spec(_SPEC)
    # Регистрация ДО exec_module обязательна: `@dataclass` в линте лезет за неймспейсом через
    # `sys.modules[cls.__module__]`, и на незарегистрированном модуле это AttributeError.
    sys.modules[_SPEC.name] = module
    _SPEC.loader.exec_module(module)
    return module


_LINT = _load_lint()

# Эталон → профиль, который обязан вывестись из пути. Второй элемент дублирует ожидание
# намеренно: если `_infer_profile` начнёт врать, тесты ниже покраснеют не «где-то», а здесь.
_REFERENCE_CONFIGS = [
    (HERMES_DIR / "config.yaml", "vps-read"),
    (HERMES_DIR / "host-a" / "config.yaml", "host-a"),
]


def _load_cfg(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("path", "profile"), _REFERENCE_CONFIGS, ids=lambda v: getattr(v, "parent", v)
)
def test_reference_configs_pass_the_lint(path, profile):
    """Оба эталона репо линтуются без ERROR под своим профилем."""
    rep = _LINT.lint(_load_cfg(path), raw_text=path.read_text(encoding="utf-8"), profile=profile)
    assert rep.ok, "\n".join(
        [f"{path.relative_to(ROOT)} не проходит собственный К10-линт:", *map(str, rep.errors)]
    )


@pytest.mark.parametrize(("path", "profile"), _REFERENCE_CONFIGS, ids=lambda v: str(v)[-24:])
def test_profile_is_inferred_from_path(path, profile):
    """Профиль обязан выводиться из пути.

    Прежний дефолт «host-a для всего» давал на прод-эталоне ТРИ ложные ошибки
    `check_credential_boundary` — `command: docker`, `args: [… aimash-bot …]` и
    `get_change_history` штатны на Хосте B и запрещены на Хосте A. Линт, который врёт на
    главном охраняемом файле, перестают читать, и он снова становится мёртвым.
    """
    assert _LINT._infer_profile(path) == profile


def test_profile_is_not_guessed_outside_the_repo(tmp_path):
    """Fail-closed: для `~/.hermes/config.yaml` на живой ВМ профиль НЕ угадывается.

    Путь живого конфига Хоста A не содержит `host-a`, и молчаливый откат на `vps-read` снял бы
    ровно проверки границы креденшелов — то есть тот единственный набор правил, ради которого
    профиль и заведён. Не вывелось ⇒ CLI обязан потребовать `--profile` явно.
    """
    assert _LINT._infer_profile(tmp_path / "config.yaml") is None


def test_lint_survives_config_without_display_block():
    """Регресс на сам краш: конфиг без блока `display:` не роняет линт.

    Именно так он и умирал — на прод-эталоне, где `display` не задан. `host-a/config.yaml`
    линтовался чисто, поэтому падение никем не замечалось: эталон им ни разу не прогоняли.
    """
    cfg = {"model": {"provider": "openrouter", "default": "openai/gpt-5.6-terra"}}
    rep = _LINT.lint(cfg, profile="vps-read")
    assert not any("display" in f.path for f in rep.findings), (
        "отсутствие display: не должно давать находок — оно должно просто пропускаться"
    )


def test_lint_reports_display_tool_progress_typo():
    """Позитив на то самое правило, из-за которого линт и падал: значение, съедаемое молча.

    `gateway/display_config.py:256` приводит неизвестное значение к `all` без единой записи в
    лог. Для нас это потеря `~/.hermes/logs/tool_calls.log` — единственного следа, независимого
    от текста агента (К7).
    """
    rep = _LINT.lint({"display": {"tool_progress": "logs"}}, profile="vps-read")
    assert any(f.path == "display.tool_progress" for f in rep.errors), (
        f"опечатка в display.tool_progress не поймана: {[str(f) for f in rep.findings]}"
    )

    nested = {"display": {"platforms": {"telegram": {"tool_progress": "logs"}}}}
    rep = _LINT.lint(nested, profile="vps-read")
    assert any(f.path == "display.platforms.telegram.tool_progress" for f in rep.errors), (
        "пер-платформенный tool_progress не проверяется — а это ветка, на которой линт падал"
    )


# ── Отрицательные контроли: линт обязан КРАСНЕТЬ на реальных ослаблениях ──────


@pytest.mark.parametrize(
    ("dotted", "why"),
    [
        ("skills.inline_shell", "правило 14 / К1: RCE из SKILL.md мимо approvals"),
        ("memory.user_profile_enabled", "профиль пользователя один на всю команду"),
    ],
)
def test_lint_reddens_when_a_hardening_key_disappears(dotted, why):
    """Убрали ключ хардненинга из прод-эталона ⇒ линт обязан дать ERROR ровно по нему.

    Без этой проверки зелёный `test_reference_configs_pass_the_lint` не отличает «проверено»
    от «правило снова не исполняется»: оба состояния выглядят как отсутствие ошибок.
    """
    path, profile = _REFERENCE_CONFIGS[0]
    cfg = copy.deepcopy(_load_cfg(path))
    section, key = dotted.split(".")
    cfg.get(section, {}).pop(key, None)

    rep = _LINT.lint(cfg, profile=profile)
    assert any(f.path == dotted for f in rep.errors), (
        f"снятие {dotted} ({why}) прошло мимо линта — найдено: {[str(f) for f in rep.errors]}"
    )


def test_lint_reddens_when_a_must_disable_toolset_is_enabled():
    """Включение любого тулсета из `_MUST_DISABLE` обязано давать ERROR по каждому.

    Список не пуст по построению и держит `cronjob`/`delegation`: первый даёт мутацию без
    команды человека (правило 3), второй — субагента, который наследует MCP родителя и никогда
    не спрашивает человека. Снятие любого из них требует письменного решения (Р2), а не правки
    рабочего дерева.
    """
    path, profile = _REFERENCE_CONFIGS[0]
    cfg = copy.deepcopy(_load_cfg(path))
    assert _LINT._MUST_DISABLE, "_MUST_DISABLE опустел — гард выродился в тождество"

    cfg["agent"]["disabled_toolsets"] = []
    rep = _LINT.lint(cfg, profile=profile)
    reported = {f.path for f in rep.errors}
    assert "agent.disabled_toolsets" in reported, (
        f"пустой disabled_toolsets не дал ошибки — найдено: {[str(f) for f in rep.errors]}"
    )
    missed = [t for t in _LINT._MUST_DISABLE if not any(t in str(f) for f in rep.errors)]
    assert not missed, f"тулсеты из _MUST_DISABLE не названы поимённо: {missed}"
