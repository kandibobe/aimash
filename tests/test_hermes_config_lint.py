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


# Слаги, которые эталоны репо называют сегодня. Каталог подменяется ВСЕГДА (autouse): тест не
# имеет права зависеть от сети — иначе он краснеет по погоде, а не по конфигу. Живая сверка с
# `/api/v1/models` остаётся на CI-шаге и pre-commit, где линт запускают процессом.
_CATALOG_STUB = frozenset(
    {
        "openai/gpt-5.6-terra",
        "google/gemini-3.1-flash-lite",
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
        "deepseek/deepseek-chat",
    }
)

# Наборы `supported_parameters` по эндпоинтам — списаны с живого ответа OpenRouter 27.07.2026.
# Важна ровно одна асимметрия: у `deepseek-chat` НИ ОДИН провайдер не берёт `reasoning`, и
# именно на ней прод отвечал 404, хотя слаг в каталоге есть.
_ENDPOINTS_STUB = {
    "openai/gpt-5.6-terra": [{"tools", "reasoning", "reasoning_effort", "include_reasoning"}],
    "deepseek/deepseek-v4-flash": [{"tools", "reasoning", "reasoning_effort"}],
    "deepseek/deepseek-v4-pro": [{"tools", "reasoning", "reasoning_effort"}],
    "google/gemini-3.1-flash-lite": [{"tools", "reasoning"}],
    "deepseek/deepseek-chat": [{"tools", "tool_choice"}, {"tools", "response_format"}],
}


@pytest.fixture(autouse=True)
def _offline_model_catalog(monkeypatch):
    monkeypatch.setattr(_LINT, "fetch_openrouter_catalog", lambda *_a, **_kw: set(_CATALOG_STUB))
    monkeypatch.setattr(
        _LINT,
        "fetch_endpoint_params",
        lambda slug, *_a, **_kw: [set(s) for s in _ENDPOINTS_STUB.get(slug, [])] or None,
    )


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
def test_group_plain_text_is_enabled_but_sender_allowlist_remains(path, profile):
    cfg = _load_cfg(path)
    telegram = cfg["gateway"]["platforms"]["telegram"]
    assert telegram["require_mention"] is False
    assert telegram["group_allow_from"]

    telegram["require_mention"] = True
    rep = _LINT.lint(cfg, profile=profile)
    assert any(f.path == "gateway.platforms.telegram.require_mention" for f in rep.errors)


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


def test_unknown_nested_leaf_does_not_hide_behind_a_known_section():
    """`agent` существует, но опечатка его дочернего ключа всё равно блокирует strict lint."""
    rep = _LINT.Report()
    _LINT.check_unknown_keys({"agent": {"max_turnz": 20}}, rep)
    assert any(f.path == "agent.max_turnz" for f in rep.warnings), [str(f) for f in rep.findings]


def test_platform_disabled_accepts_dynamic_platform_but_not_a_typo():
    """Pinned Hermes supports per-platform skill disables; only the platform name is dynamic."""
    rep = _LINT.Report()
    _LINT.check_unknown_keys(
        {"skills": {"platform_disabled": {"telegram": ["aimash-development"]}}}, rep
    )
    assert not rep.findings

    typo = _LINT.Report()
    _LINT.check_unknown_keys(
        {"skills": {"platform_disable": {"telegram": ["aimash-development"]}}}, typo
    )
    assert any(f.path == "skills.platform_disable.telegram" for f in typo.warnings)


def test_runtime_registry_matches_deploy_model_and_fallbacks():
    """Два источника модели не могут снова тихо объявить разные canonical runtime."""
    registry = _load_cfg(HERMES_DIR / "runtime_registry.yaml")
    deploy = _load_cfg(HERMES_DIR / "config.yaml")
    assert (
        registry["primary_runtime"]["provider"],
        registry["primary_runtime"]["model"],
    ) == (deploy["model"]["provider"], deploy["model"]["default"])
    configured = [(item["provider"], item["model"]) for item in deploy["fallback_providers"]]
    registered = [
        (registry["routing_lanes"][name]["provider"], registry["routing_lanes"][name]["model"])
        for name in ("primary_fallback", "lightweight_background")
    ]
    assert registered == configured


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
        ("provider_routing.require_parameters", "правило 13: tools/reasoning срежутся МОЛЧА"),
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

    Список не пуст по построению и фиксирует ровно ту неактивную поверхность, которую утвердил владелец
    31.07.2026. `cronjob`, `delegation`, memory и native work tools для private-profile разрешены.
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


def test_lint_reddens_when_a_known_toolset_is_merely_unmentioned():
    """Регресс 27.07–30.07.2026: неупомянутый тулсет ВКЛЮЧЁН, а линт молчал.

    `check_toolsets` смотрит только на перечисленное в `disabled_toolsets` — значит слаг, которого
    там нет, проходит без единого слова, хотя по дефолту он включён. Ровно так `computer_use`,
    `x_search`, `video_gen`, `homeassistant`, `spotify`, `yuanbao` не попадали в поле зрения линта
    вовсе, а когда живой конфиг на VPS пересобрался из дефолта, на боевом telegram-gateway двое
    суток стояли включёнными `terminal`/`file`/`code_execution` — при зелёном линте.

    Проверка ведётся ОТ РАЗРЕШЁННОГО (`_ALLOWED_ENABLED_TOOLSETS`), поэтому краснеет и новый
    тулсет, приехавший с апгрейдом Hermes: его нет ни в разрешённых, ни в погашенных.
    """
    path, profile = _REFERENCE_CONFIGS[0]
    assert profile == "vps-read", "тест написан под профиль агента, а не под машину владельца"
    allowed = _LINT._ALLOWED_ENABLED_TOOLSETS
    assert allowed < _LINT._KNOWN_TOOLSETS, (
        "_ALLOWED_ENABLED_TOOLSETS покрыл все тулсеты — проверка выродилась в тождество"
    )

    cfg = copy.deepcopy(_load_cfg(path))
    victim = sorted(set(cfg["agent"]["disabled_toolsets"]) - allowed)[0]
    cfg["agent"]["disabled_toolsets"] = [
        t for t in cfg["agent"]["disabled_toolsets"] if t != victim
    ]

    rep = _LINT.lint(cfg, profile=profile)
    assert any(victim in str(f) for f in rep.errors), (
        f"молча выпавший из disabled_toolsets {victim!r} не дал ошибки — "
        f"найдено: {[str(f) for f in rep.errors]}"
    )


def test_the_pinned_toolset_list_covers_the_live_surface():
    """`_KNOWN_TOOLSETS` — пин поверхности версии, и проверка от разрешённого стоит на нём.

    Отстал пин — «неупомянутых» слагов для линта не существует, и он снова молчит про включённое.
    Список сверен живым `hermes tools list --platform telegram` на v0.19.0 (30.07.2026); шесть
    слагов ниже в редакции 21.07 отсутствовали.
    """
    added_2026_07_30 = {
        "computer_use",
        "x_search",
        "video_gen",
        "homeassistant",
        "spotify",
        "yuanbao",
    }
    missing = sorted(added_2026_07_30 - _LINT._KNOWN_TOOLSETS)
    assert not missing, f"пин тулсетов отстал от замера живой поверхности: {missing}"


def test_vps_lint_rejects_extra_or_unbounded_mcp_servers():
    """An MCP server without include exposes every tool, including future mutations."""
    path, profile = _REFERENCE_CONFIGS[0]
    assert profile == "vps-read"
    cfg = copy.deepcopy(_load_cfg(path))
    cfg["mcp_servers"]["github"] = {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
    }

    rep = _LINT.lint(cfg, profile=profile)
    assert any(f.path == "mcp_servers.github" for f in rep.errors), [str(f) for f in rep.errors]

    cfg = copy.deepcopy(_load_cfg(path))
    cfg["mcp_servers"]["tavily"].pop("tools")
    rep = _LINT.lint(cfg, profile=profile)
    assert any(f.path == "mcp_servers.tavily.tools.include" for f in rep.errors), [
        str(f) for f in rep.errors
    ]


# ── Слаги моделей: тот же класс, что К10 — «рабочий на вид и не работает» ─────


@pytest.mark.parametrize(
    ("extra", "path"),
    [
        (
            {"fallback_providers": [{"provider": "openrouter", "model": "deepseek/deepseek-v3"}]},
            "fallback_providers[0]",
        ),
        (
            {
                "auxiliary": {
                    "compression": {"provider": "openrouter", "model": "deepseek/deepseek-v3"}
                }
            },
            "auxiliary.compression",
        ),
    ],
)
def test_lint_catches_a_dead_model_slug(extra, path):
    """Регресс инцидента 27.07.2026: прод молчал сутки на несуществующем слаге.

    Скрытые модели auxiliary/fallback не видны в `hermes model`; мёртвый слаг проявится только
    при компрессии или после отказа основной модели. Единственный шанс поймать раньше — линт.
    """
    cfg = {
        "model": {"provider": "openrouter", "default": "deepseek/deepseek-v4-flash"},
        **extra,
    }
    rep = _LINT.lint(cfg, profile="vps-read")
    assert any(f.path == path for f in rep.errors), (
        f"мёртвый слаг в {path} прошёл мимо линта: {[str(f) for f in rep.findings]}"
    )


def test_lint_accepts_live_slugs_everywhere():
    """Отрицательный контроль: живые main/aux/fallback/delegation слаги проходят.

    Без него предыдущий тест не отличает «поймало мёртвый слаг» от «ругается на любой».
    """
    cfg = {
        "model": {"provider": "openrouter", "default": "openai/gpt-5.6-terra"},
        "auxiliary": {
            "transient_retries": 2,  # скаляр среди mapping'ов — не должен ломать разбор
            "compression": {"provider": "openrouter", "model": "google/gemini-3.1-flash-lite"},
        },
        "fallback_providers": [{"model": "deepseek/deepseek-v4-pro"}],
        "delegation": {"model": "google/gemini-3.1-flash-lite"},
    }
    # Правило зовём напрямую: на минимальном cfg штатно ругаются соседние правила (хардненинг,
    # тулсеты, неаттестованные ключи), и через `lint()` их шум утопил бы предмет проверки.
    rep = _LINT.Report()
    _LINT.check_model_slugs(cfg, rep)
    assert not rep.findings, (
        f"ложные срабатывания на живых слагах: {[str(f) for f in rep.findings]}"
    )


def test_lint_accepts_pinned_codex_slug_and_rejects_unknown_one():
    """Codex OAuth has its own pinned catalog; it must not be treated as unchecked OpenRouter."""
    good = {"model": {"provider": "openai-codex", "default": "gpt-5.6-terra"}}
    good_rep = _LINT.Report()
    _LINT.check_model_slugs(good, good_rep)
    assert not good_rep.findings

    bad = {"model": {"provider": "openai-codex", "default": "gpt-5.6-terra-typo"}}
    bad_rep = _LINT.Report()
    _LINT.check_model_slugs(bad, bad_rep)
    assert any(f.path == "model" for f in bad_rep.errors)


def test_lint_warns_but_does_not_redden_without_network(monkeypatch):
    """Каталог не достался ⇒ WARN, не ERROR: линт зовут из pre-commit и из CI без гарантии сети.

    Fail-closed здесь был бы вредителем — красный линт по причине «нет интернета» научит
    прогонять его с `|| true`, и правило умрёт целиком, как уже умирал сам линт.
    """
    cfg = {"model": {"provider": "openrouter", "default": "deepseek/deepseek-v3"}}
    rep = _LINT.lint(cfg, profile="vps-read")
    assert any(f.path == "model" for f in rep.errors), "с каталогом мёртвый слаг обязан быть ERROR"

    monkeypatch.setattr(_LINT, "fetch_openrouter_catalog", lambda *_a, **_kw: None)
    rep_offline = _LINT.lint(cfg, profile="vps-read")
    offline_model_errors = [f for f in rep_offline.errors if f.path == "model"]
    assert not offline_model_errors, f"офлайн дал ERROR: {[str(f) for f in offline_model_errors]}"
    assert any("НЕ проверены" in f.message for f in rep_offline.warnings), (
        "офлайн обязан сказать вслух, что слаги не проверены — молчание читается как «проверено»"
    )


def test_lint_reddens_when_a_tool_turn_model_has_no_reasoning_provider():
    """Второй инцидент 27.07.2026: слаг ЖИВОЙ, а вызвать модель нельзя.

    `deepseek/deepseek-chat` есть в каталоге — поэтому `check_model_slugs` его пропускает и
    поймать обязано отдельное правило. Ни один из трёх его провайдеров не берёт `reasoning`, а
    `agent.reasoning_effort` задан ⇒ Hermes шлёт параметр всегда ⇒ `require_parameters: true`
    отбрасывает всех ⇒ `HTTP 404 No endpoints found` на каждом ходу. Замерено живым ключом:
    `chat+tools` → 200, `chat+tools+reasoning` → 404.
    """
    cfg = {
        "model": {"provider": "openrouter", "default": "deepseek/deepseek-chat"},
        "agent": {"reasoning_effort": "medium"},
    }
    rep = _LINT.Report()
    _LINT.check_tool_turn_model_endpoints(cfg, rep)
    assert any(f.path == "model.default" for f in rep.errors), (
        f"модель без reasoning-провайдера прошла мимо линта: {[str(f) for f in rep.findings]}"
    )

    # Отрицательный контроль ДВОЙНОЙ: правило обязано молчать и на модели с reasoning, и на той
    # же `deepseek-chat`, когда reasoning не запрашивается — иначе оно ругается на всё подряд.
    for cfg_ok in (
        {
            "model": {"provider": "openrouter", "default": "deepseek/deepseek-v4-flash"},
            "agent": {"reasoning_effort": "medium"},
        },
        {
            "model": {"provider": "openrouter", "default": "deepseek/deepseek-chat"},
            "agent": {"reasoning_effort": "none"},
        },
    ):
        rep_ok = _LINT.Report()
        _LINT.check_tool_turn_model_endpoints(cfg_ok, rep_ok)
        assert not rep_ok.findings, (
            f"ложное срабатывание на {cfg_ok}: {[str(f) for f in rep_ok.findings]}"
        )


def test_lint_checks_fallbacks_by_the_same_yardstick_as_the_default():
    """Fallback продолжает tool-turn и обязан поддерживать те же tools/reasoning параметры."""
    cfg = {
        "model": {"provider": "openrouter", "default": "deepseek/deepseek-v4-flash"},
        "agent": {"reasoning_effort": "high"},
        "fallback_providers": [{"provider": "openrouter", "model": "deepseek/deepseek-chat"}],
    }
    rep = _LINT.Report()
    _LINT.check_tool_turn_model_endpoints(cfg, rep)
    assert any(f.path == "fallback_providers[0]" for f in rep.errors), (
        f"негодный fallback прошёл мимо: {[str(f) for f in rep.findings]}"
    )
    assert not any(f.path == "model.default" for f in rep.errors), (
        "дефолт исправен — правило не должно ругаться заодно и на него"
    )

    # Отрицательный контроль: рабочая fallback-цепь молчит.
    rep_ok = _LINT.Report()
    _LINT.check_tool_turn_model_endpoints(
        {
            **cfg,
            "fallback_providers": [
                {"provider": "openrouter", "model": "deepseek/deepseek-v4-flash"},
                {"provider": "openrouter", "model": "google/gemini-3.1-flash-lite"},
            ],
        },
        rep_ok,
    )
    assert not rep_ok.findings, (
        f"ложное срабатывание на рабочих fallback: {[str(f) for f in rep_ok.findings]}"
    )


@pytest.mark.parametrize("path", _LINT._INERT_CONFIG_PATHS)
def test_lint_rejects_inert_pinned_config_keys(path):
    """Каждый доказанно мёртвый ключ краснит линт, а не остаётся advisory warning."""
    cfg: dict = {}
    cursor = cfg
    parts = path.split(".")
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = True
    rep = _LINT.Report()
    _LINT.check_inert_keys(cfg, rep)
    assert any(f.path == path for f in rep.errors), [str(f) for f in rep.findings]


def test_lint_warns_on_a_second_provider_in_the_gateway():
    """Чужой провайдер в `auxiliary` — это второй секрет в процессе, который знает один ключ.

    Так и было до 27.07: весь `auxiliary:` стоял на `provider: google` со снятой
    `gemini-2.0-flash-exp` и валил компрессию с генерацией заголовков 404-м.
    """
    cfg = {
        "model": {"provider": "openrouter", "default": "openai/gpt-5.6-terra"},
        "auxiliary": {"title_generation": {"provider": "google", "model": "gemini-2.0-flash-exp"}},
    }
    rep = _LINT.lint(cfg, profile="vps-read")
    assert any(f.path == "auxiliary.title_generation" for f in rep.warnings), (
        f"второй провайдер не отмечен: {[str(f) for f in rep.findings]}"
    )
