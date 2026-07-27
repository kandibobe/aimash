#!/usr/bin/env python3
"""Конфиг-линт К10 для Hermes: ловит то, что Hermes проглатывает молча.

**Зачем.** Hermes игнорирует неизвестные ключи без единого предупреждения (К10), а некоторые
значения ещё и молча приводит к дефолту: `gateway/display_config.py:256` —
``return val if val in {"off","new","all","verbose","log"} else "all"``. Значит опечатка не
роняет старт и не пишет в лог — она даёт конфиг, который выглядит рабочим и не работает.
`hermes config check` этот класс НЕ ловит (он про missing/stale в `.env`).

**Чем этот линт НЕ является.** Границей безопасности. Граница в этой схеме одна — отсутствие
полномочия (роль Google-пользователя `READ_ONLY` на Хосте A). Линт — прибор: делает молчаливое
расхождение громким до деплоя. Агент с терминалом перепишет и конфиг, и этот файл.

**Главный урок, зашитый в правила ниже.** Пример апстрима `cli-config.yaml.example` — НИЖНЯЯ
ГРАНИЦА, а не whitelist: в нём ровно 19 корневых ключей и среди них НЕТ `approvals`, `gateway`,
`mcp_servers`, `security`, `privacy`, `provider_routing`, `auxiliary` — при том, что все они
реальны и задокументированы. Поэтому неизвестный ключ здесь — **предупреждение**, никогда не
ошибка: «ключа нет в моей таблице» ≠ «ключа не существует». Один раз это чуть не стоило нам
`skills.write_approval` и `security.redact_secrets`.

Запуск (для эталонов репо профиль выводится из пути, флаг не нужен)::

    python deploy/hermes/lint_config.py deploy/hermes/config.yaml            # → vps-read
    python deploy/hermes/lint_config.py deploy/hermes/{config,host-a/config}.yaml   # оба разом
    python deploy/hermes/lint_config.py ~/.hermes/config.yaml --profile host-a   # на живой ВМ

Код возврата: 1 — есть ERROR хотя бы в одном файле; 0 — только WARN или чисто.

Автоматика (иначе линт снова умрёт незамеченным — именно так он и прожил сломанным):
`tests/test_hermes_config_lint.py` в общем прогоне, хук `hermes-config-lint` в pre-commit,
шаг «Hermes config lint (К10)» в `.github/workflows/ci.yml`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_HERE = Path(__file__).resolve().parent
PIN_PATH = _HERE / "PIN.json"

# Каталог слагов OpenRouter — публичный, без ключа. Таймаут короткий: линт зовут из pre-commit,
# и сеть здесь удобство, а не условие (нет каталога ⇒ предупреждение, см. check_model_slugs).
_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_ENDPOINTS_URL_TMPL = "https://openrouter.ai/api/v1/models/{slug}/endpoints"
_CATALOG_TIMEOUT = 6.0

# ── Аттестации ────────────────────────────────────────────────────────────────
# Дословная цитата + откуда взята. Таблица НЕ читает эталонные конфиги этого репозитория:
# оракул, сверяющий файл сам с собой, доказывает только собственную непротиворечивость.
# Версия апстрима, против которой цитаты сняты, лежит в PIN.json рядом.


@dataclass(frozen=True)
class Attested:
    src: str
    quote: str


_ATTESTED: dict[str, Attested] = {
    "model": Attested("user-guide/configuration.md", "model:\n  provider: openrouter"),
    "provider_routing.require_parameters": Attested(
        "cli-config.yaml.example:163",
        "#   # require_parameters: true",
    ),
    "provider_routing.data_collection": Attested(
        "cli-config.yaml.example:166",
        '#   # data_collection: "deny"',
    ),
    "agent.disabled_toolsets": Attested("user-guide/configuration.md:695", "  disabled_toolsets:"),
    "agent.tool_use_enforcement": Attested(
        "user-guide/configuration.md:1374",
        '  tool_use_enforcement: "auto"   # "auto" | true | false | ["model-substring", ...]',
    ),
    "agent.max_turns": Attested("cli-config.yaml.example:700", "  max_turns: 60"),
    "agent.reasoning_effort": Attested(
        "cli-config.yaml.example:758", '  reasoning_effort: "medium"'
    ),
    "approvals.mode": Attested("user-guide/security.md", "approvals:\n  mode: manual"),
    "approvals.deny": Attested(
        "user-guide/security.md",
        "block matching terminal commands unconditionally — before `--yolo`, `/yolo`, "
        "and `approvals.mode: off`",
    ),
    "approvals.cron_mode": Attested("user-guide/security.md", "cron_mode: deny | approve"),
    "approvals.timeout": Attested(
        "user-guide/security.md", "approvals: timeout (молчание = отказ)"
    ),
    "approvals.mcp_reload_confirm": Attested("user-guide/security.md", "mcp_reload_confirm"),
    "approvals.destructive_slash_confirm": Attested(
        "user-guide/security.md", "destructive_slash_confirm"
    ),
    "terminal.backend": Attested("cli-config.yaml.example:214", '  backend: "local"'),
    "terminal.cwd": Attested("cli-config.yaml.example:215", '  cwd: "."'),
    "terminal.timeout": Attested("cli-config.yaml.example:216", "  timeout: 180"),
    "terminal.lifetime_seconds": Attested("cli-config.yaml.example:223", "  lifetime_seconds: 300"),
    "tool_loop_guardrails.warnings_enabled": Attested(
        "cli-config.yaml.example:377", "  warnings_enabled: true"
    ),
    "tool_loop_guardrails.hard_stop_enabled": Attested(
        "cli-config.yaml.example:378", "  hard_stop_enabled: false"
    ),
    "tool_loop_guardrails.hard_stop_after": Attested(
        "cli-config.yaml.example:383", "  hard_stop_after:"
    ),
    "memory.memory_enabled": Attested("cli-config.yaml.example:564", "  memory_enabled: true"),
    "memory.user_profile_enabled": Attested(
        "cli-config.yaml.example:567", "  user_profile_enabled: true"
    ),
    "privacy.redact_pii": Attested(
        "user-guide/configuration.md:1604",
        "  redact_pii: false  # Strip PII from LLM context (gateway only)",
    ),
    "skills.write_approval": Attested(
        "user-guide/configuration.md:607",
        "  write_approval: false   # false = write freely (default) | true = stage every write "
        "for review",
    ),
    "skills.guard_agent_created": Attested(
        "user-guide/configuration.md:596", "  guard_agent_created: true   # default: false"
    ),
    "skills.inline_shell": Attested(
        "user-guide/features/creating-skills.md:314", "  inline_shell: true"
    ),
    "security.redact_secrets": Attested(
        "user-guide/configuration.md:1914",
        "  redact_secrets: true           # Redact API key patterns in tool output and logs "
        "(on by default)",
    ),
    "security.tirith_fail_open": Attested(
        "user-guide/configuration.md:1918",
        "  tirith_fail_open: true         # Allow command execution if tirith is unavailable",
    ),
    "display.tool_progress": Attested(
        "gateway/display_config.py:256",
        'return val if val in {"off", "new", "all", "verbose", "log"} else "all"',
    ),
    "display.platforms": Attested(
        "user-guide/configuration.md:1592",
        "Valid platform keys: `telegram`, `discord`, `slack`, …",
    ),
    # Каталог ~/.hermes/plugins/<name>/ — НЕ активация: «General plugins and user-installed
    # backends are disabled by default». Пока имени нет в `enabled`, плагин виден в
    # `hermes plugins` и не подписан НИ НА ЧТО — самая дорогая форма К10 (лог пуст, и пустота
    # читается как «метаданные не доходят»). Аттестовано заранее: на время прогона V1–V22
    # блок раскомментируется, и линт не должен краснеть на ожидаемом ключе.
    "plugins.enabled": Attested(
        "user-guide/features/plugins.md",
        "plugins:\n  enabled:\n    - my-tool-plugin",
    ),
    "plugins.disabled": Attested(
        "user-guide/features/plugins.md",
        "plugins:\n  disabled:\n    - noisy-plugin",
    ),
    "mcp_servers": Attested("user-guide/configuration.md", "mcp_servers:"),
    "gateway.platforms": Attested("user-guide/messaging/telegram.md", "gateway:\n  platforms:"),
    "auxiliary.compression": Attested(
        "user-guide/configuration.md:753", "auxiliary:\n  compression:"
    ),
}

# Ключи, читаемые С УРОВНЯ БЛОКА платформы. Именно они конвертируются в env
# (`plugins/platforms/telegram/adapter.py:9391/9396/9401`), а решение о доступе принимает
# `gateway/authz_mixin.py::_is_user_authorized`, который смотрит ИСКЛЮЧИТЕЛЬНО env
# (TELEGRAM_ALLOWED_USERS :381, TELEGRAM_GROUP_ALLOWED_USERS :401, TELEGRAM_GROUP_ALLOWED_CHATS :404).
# Тот же ключ, положенный в `extra:`, не читает никто — это К10 в чистом виде.
_AUTH_KEYS_BLOCK_LEVEL = ("allow_from", "group_allow_from", "group_allowed_chats")

# Ключи, у которых потребитель ровно один и он читает `config.extra` (adapter.py:9023).
# На уровне блока они, наоборот, съедаются молча.
_EXTRA_ONLY_KEYS = ("group_topics",)

_TOOL_PROGRESS_VALUES = frozenset({"off", "new", "all", "verbose", "log"})

# Слаги тулсетов установленной версии (`platform_toolsets.cli`, сверено 21.07).
_KNOWN_TOOLSETS = frozenset(
    {
        "browser",
        "clarify",
        "code_execution",
        "context_engine",
        "cronjob",
        "delegation",
        "file",
        "image_gen",
        "memory",
        "session_search",
        "skills",
        "terminal",
        "todo",
        "tts",
        "video",
        "vision",
        "web",
    }
)

# Тулсеты, гашение которых — решение архитектуры, а не вкус. Снятие любого требует письменного
# решения (Р2), поэтому линт держит их списком, а не комментарием.
_MUST_DISABLE = {
    "cronjob": "агент заводит расписание сам ⇒ мутация без команды человека (правило 3)",
    "delegation": "субагент наследует MCP родителя и никогда не спрашивает человека",
    "memory": "MEMORY.md/USER.md — один комплект на инстанс, а топик = клиент (Р2)",
    "session_search": "FTS5 по всей state.db = по переписке всех клиентов (К9/И6)",
    "context_engine": "индексация ФС втягивает в контекст всё, до чего дотянется, включая .env",
}

# Похоже на утёкший секрет. Правило 5: в конфиге допустимы только ${VAR}.
_SECRET_SHAPES = (
    (re.compile(r"sk-or-v?\d?-[A-Za-z0-9]{16,}"), "ключ OpenRouter"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"), "токен Telegram-бота"),
    (re.compile(r"\b1//[A-Za-z0-9_-]{20,}\b"), "refresh-токен Google OAuth"),
    (re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}\b"), "access-токен Google"),
    (re.compile(r"postgres(?:ql)?://[^\s\"']*:[^\s\"'@]+@"), "DSN с паролем"),
)

_LEVELS = ("error", "warn")


@dataclass
class Finding:
    level: str
    path: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - косметика
        mark = "ОШИБКА" if self.level == "error" else "ВНИМАНИЕ"
        return f"[{mark}] {self.path}: {self.message}"


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def error(self, path: str, message: str) -> None:
        self.findings.append(Finding("error", path, message))

    def warn(self, path: str, message: str) -> None:
        self.findings.append(Finding("warn", path, message))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warn"]

    @property
    def ok(self) -> bool:
        return not self.errors


_MISSING = object()


def _get(cfg: Any, dotted: str) -> Any:
    """Достаёт значение по пути. Возвращает `_MISSING`, а не None: у половины ключей здесь
    `false` — осмысленное значение, и путать его с отсутствием нельзя."""
    cur: Any = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _as_list(value: Any) -> list:
    """`_MISSING`/None → []. Отдельная функция потому, что `_MISSING` истинен, и `x or []`
    на нём молча возвращает сам сентинел — ровно та ошибка, которую линт и ловит у других."""
    if value is _MISSING or value is None:
        return []
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


def _as_dict(value: Any) -> dict:
    """Парная к `_as_list` для mapping'ов. Заведена по той же причине и после того, как
    `_get(...) or {}` на строке 300 уронил весь линт TypeError'ом на главном охраняемом файле:
    `_MISSING` истинен ⇒ `or {}` его не подменяет, а итерация по `object()` падает. Крах шёл
    ВТОРЫМ правилом из пяти, поэтому check_telegram_gates/check_toolsets/check_hardening не
    исполнялись НИКОГДА — прибор К10 по прод-эталону не отрабатывал ни разу."""
    return value if isinstance(value, dict) else {}


def _flatten(cfg: Any, prefix: str = "") -> list[str]:
    out: list[str] = []
    if not isinstance(cfg, dict):
        return out
    for key, val in cfg.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        out.append(path)
        if isinstance(val, dict):
            out.extend(_flatten(val, path))
    return out


def _is_attested(path: str) -> bool:
    """Аттестован сам ключ или любой его предок (поддеревья вроде `mcp_servers.*` и
    `gateway.platforms.telegram.*` разбираются отдельными правилами, а не таблицей)."""
    parts = path.split(".")
    return any(".".join(parts[: i + 1]) in _ATTESTED for i in range(len(parts)))


# ── Правила ───────────────────────────────────────────────────────────────────


def check_unknown_keys(cfg: dict, rep: Report) -> None:
    """К10 — но только предупреждением. См. шапку модуля: отсутствие в таблице не доказывает
    отсутствие ключа в Hermes, обратная ошибка нам уже дорого обходилась."""
    for path in _flatten(cfg):
        if not _is_attested(path):
            rep.warn(
                path,
                "[НЕ АТТЕСТОВАН] ключ не сверен первичным источником — проверить "
                "по докам пиновой версии и дописать в _ATTESTED либо убрать",
            )


def check_enum_values(cfg: dict, rep: Report) -> None:
    """Значения, которые Hermes приводит к дефолту МОЛЧА. Опаснее опечатки в имени ключа:
    имя хотя бы видно глазами в diff, а подмена значения не оставляет следа нигде."""
    for path in (
        "display.tool_progress",
        *(
            f"display.platforms.{name}.tool_progress"
            for name in _as_dict(_get(cfg, "display.platforms"))
        ),
    ):
        val = _get(cfg, path)
        if val is _MISSING:
            continue
        if isinstance(val, bool) or str(val).strip().lower() not in _TOOL_PROGRESS_VALUES:
            rep.error(
                path,
                f"значение {val!r} не входит в {sorted(_TOOL_PROGRESS_VALUES)} ⇒ "
                "gateway/display_config.py:256 молча подставит 'all'. Для нас это потеря "
                "~/.hermes/logs/tool_calls.log — единственного следа, независимого от текста "
                "агента (К7)",
            )

    mode = _get(cfg, "approvals.mode")
    if mode is not _MISSING and mode not in ("manual", "smart", "off"):
        rep.error("approvals.mode", f"{mode!r} — допустимы manual | smart | off")
    if mode == "off":
        rep.error("approvals.mode", "`off` снимает подтверждения полностью")

    cron_mode = _get(cfg, "approvals.cron_mode")
    if cron_mode is not _MISSING and cron_mode not in ("deny", "approve"):
        rep.error(
            "approvals.cron_mode",
            f"{cron_mode!r} невалидно (допустимы deny | approve) ⇒ будет съедено молча",
        )


def check_telegram_gates(cfg: dict, rep: Report) -> None:
    """Самое дорогое правило файла: три ортогональных гейта, которые легко перепутать так,
    что доступ окажется открыт, а конфиг — на вид строгим."""
    tg = _get(cfg, "gateway.platforms.telegram")
    if tg is _MISSING:
        return
    if not isinstance(tg, dict):
        rep.error("gateway.platforms.telegram", "ожидался mapping")
        return

    base = "gateway.platforms.telegram"
    extra = tg.get("extra") if isinstance(tg.get("extra"), dict) else {}

    # 1. Гейты доступа обязаны лежать на уровне блока: только он конвертируется в env,
    #    а `_is_user_authorized` читает исключительно env.
    for key in _AUTH_KEYS_BLOCK_LEVEL:
        if key in extra:
            rep.error(
                f"{base}.extra.{key}",
                "гейт доступа в `extra:` не читает никто (adapter.py:9391/9396/9401 конвертируют "
                "в env только уровень блока) — поднять на уровень блока платформы",
            )

    # 2. `group_allowed_chats` не сужает доступ, а отменяет перечисление людей.
    for path in (f"{base}.group_allowed_chats", f"{base}.extra.group_allowed_chats"):
        if _get(cfg, path) is not _MISSING:
            rep.error(
                path,
                "чат-лист авторизует группу ЦЕЛИКОМ и проверяется ПЕРВЫМ — authz_mixin.py:344-358 "
                "делает `return True` до проверки личности отправителя, то есть отменяет "
                "group_allow_from, а не дополняет его. Это принятие Р2 технически",
            )

    # 3. Fail-closed (правило 10): пустой список = «может кто угодно» на префильтре адаптера
    #    (adapter.py:1058-1060 — env-фолбэк там fail-open), поэтому пустота здесь запрещена.
    for key in ("allow_from", "group_allow_from"):
        val = tg.get(key, _MISSING)
        if val is _MISSING or not _as_list(val):
            rep.error(
                f"{base}.{key}",
                "пуст или отсутствует. Правило 10: гейт без конфигурации обязан ОТКАЗЫВАТЬ, "
                "но префильтр адаптера на пустом env fail-open (adapter.py:1058-1060)",
            )

    # 4. `group_topics` — зеркальная ошибка: на уровне блока его не прочитает никто.
    for key in _EXTRA_ONLY_KEYS:
        if key in tg:
            rep.error(
                f"{base}.{key}",
                f"`{key}` читается ТОЛЬКО из `extra` (adapter.py:9023) — на уровне блока "
                "будет съеден молча",
            )

    if tg.get("require_mention") is not True:
        rep.error(
            f"{base}.require_mention",
            "обязан быть true (К4): иначе бот в группе реагирует на любое сообщение",
        )
    if tg.get("guest_mode") is not False:
        rep.error(f"{base}.guest_mode", "обязан быть false: гостевой режим обходит allow-list")


def check_toolsets(cfg: dict, rep: Report) -> None:
    disabled = set(_as_list(_get(cfg, "agent.disabled_toolsets")))
    for slug in sorted(disabled - _KNOWN_TOOLSETS):
        rep.warn(
            "agent.disabled_toolsets",
            f"{slug!r} нет в списке тулсетов установленной версии — строка гасит НИЧТО "
            "(проверить по `platform_toolsets.cli` живого конфига)",
        )
    for slug, why in _MUST_DISABLE.items():
        if slug not in disabled:
            rep.error("agent.disabled_toolsets", f"{slug} обязан быть погашен: {why}")


def check_hardening(cfg: dict, rep: Report) -> None:
    """Ключи, у которых дефолт апстрима работает ПРОТИВ нас. Все обязаны стоять явно —
    дефолт в 0.x не обещание (Р7), и его смена не даст ни ошибки, ни строки в логе."""
    expected = {
        "memory.memory_enabled": (False, "память — один комплект на инстанс, топик = клиент (Р2)"),
        "memory.user_profile_enabled": (False, "«профиль пользователя» один на всю команду"),
        "skills.write_approval": (True, "дефолт false = агент правит свои постоянные инструкции"),
        "skills.guard_agent_created": (True, "дефолт false"),
        "skills.inline_shell": (False, "скил исполняет shell из своего markdown в обход approvals"),
        "provider_routing.require_parameters": (
            True,
            "правило 13: без него OpenRouter МОЛЧА срежет tools/reasoning у провайдера, который "
            "их не умеет — агент «отвечает текстом» вместо вызова MCP, и это читается как "
            "деградация модели, а не как сбой конфига",
        ),
    }
    for path, (want, why) in expected.items():
        val = _get(cfg, path)
        if val is _MISSING:
            rep.error(path, f"не задан явно; нужен {str(want).lower()} — {why}")
        elif val is not want:
            rep.error(path, f"{val!r}, ожидалось {str(want).lower()} — {why}")


def fetch_openrouter_catalog(timeout: float = _CATALOG_TIMEOUT) -> set[str] | None:
    """Живые слаги OpenRouter или `None`, если каталог не достался (нет сети, таймаут, мусор).

    Эндпоинт публичный и ключа не требует — секрет в линт не попадает (правило 5). Отдельная
    функция, а не инлайн: тест подменяет её и проверяет правило офлайн и детерминированно.
    """
    try:
        with urllib.request.urlopen(_OPENROUTER_MODELS_URL, timeout=timeout) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None
    slugs = {str(item.get("id", "")).strip() for item in data if isinstance(item, dict)}
    slugs.discard("")
    return slugs or None


def _model_specs(cfg: dict) -> list[tuple[str, str, str]]:
    """`(путь, provider, model)` для КАЖДОГО места, где конфиг называет модель.

    Мест три, и опасны именно два последних: глобальный `model:` виден в `hermes model` и в
    дашборде, а `auxiliary.*` и `model_routing.*` не показывает никто.
    """
    root = _as_dict(_get(cfg, "model"))
    default_provider = str(root.get("provider") or "").strip()
    out: list[tuple[str, str, str]] = []
    if root:
        out.append(("model", default_provider, str(root.get("default") or "").strip()))
    for section in ("auxiliary", "model_routing"):
        for role, spec in _as_dict(_get(cfg, section)).items():
            if not isinstance(spec, dict) or "model" not in spec:
                continue
            provider = str(spec.get("provider") or "").strip() or default_provider
            out.append((f"{section}.{role}", provider, str(spec.get("model") or "").strip()))
    return out


def check_model_slugs(cfg: dict, rep: Report) -> None:
    """Слаг несуществующей модели — тот же класс, что К10: конфиг рабочий на вид и не работает.

    Инцидент 27.07.2026, прод стоял до вмешательства: в `~/.hermes/config.yaml` лежал блок
    `model_routing`, дословно скопированный из докстринга `agent/router.py:264` установленного
    Hermes, а в том примере — `deepseek/deepseek-v3`, слага которого у OpenRouter нет.
    `classify_turn()` шлёт в тир `v3` ЛЮБОЙ ход, где агенту доступны инструменты (то есть у нас
    каждый), поэтому дефолт перебивался на мёртвую модель → `HTTP 400 … is not a valid model ID`
    → non-retryable → бот молчал вообще. Тем же заходом весь `auxiliary:` смотрел на снятую
    `gemini-2.0-flash-exp` и валил компрессию с генерацией заголовков.

    Проверяется против ЖИВОГО каталога, а не против таблицы в этом файле: список моделей меняется
    еженедельно, а зашитая копия протухла бы ровно так же, как протух пример в докстринге.
    """
    specs = _model_specs(cfg)
    if not specs:
        return

    catalog: set[str] | None = None
    if any(provider.lower() == "openrouter" for _, provider, _ in specs):
        catalog = fetch_openrouter_catalog()
        if catalog is None:
            rep.warn(
                "model",
                f"каталог {_OPENROUTER_MODELS_URL} недоступен — слаги моделей НЕ проверены; "
                "прогнать линт с сетью до деплоя",
            )

    for path, provider, model in specs:
        if not model:
            rep.error(path, "модель не задана — Hermes молча уедет на свой дефолт")
            continue
        if provider.lower() != "openrouter":
            rep.warn(
                path,
                f"провайдер {provider!r}: по топологии (CLAUDE.md) gateway знает ровно один ключ, "
                "OPENROUTER_API_KEY — чужой провайдер тянет второй секрет в тот же процесс, а его "
                f"слаг {model!r} линт сверить не может",
            )
            continue
        if model.count("/") != 1:
            rep.error(
                path,
                f"{model!r} — не слаг OpenRouter: формат `vendor/model` "
                "(в 400-х уже ловили и голое `deepseek-chat`)",
            )
            continue
        if catalog is not None and model not in catalog:
            rep.error(
                path,
                f"{model!r} нет в каталоге OpenRouter ⇒ HTTP 400 на КАЖДОМ вызове этой роли; "
                "проверить живое имя в /api/v1/models",
            )


def fetch_endpoint_params(slug: str, timeout: float = _CATALOG_TIMEOUT) -> list[set[str]] | None:
    """`supported_parameters` по каждому эндпоинту модели, или `None`, если список не достался.

    Тот же публичный эндпоинт без ключа, что и каталог. Отдельная функция по той же причине:
    тест её подменяет и проверяет правило офлайн.
    """
    url = _ENDPOINTS_URL_TMPL.format(slug=slug)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    endpoints = data.get("endpoints") if isinstance(data, dict) else None
    if not isinstance(endpoints, list):
        return None
    return [
        {str(p) for p in (ep.get("supported_parameters") or [])}
        for ep in endpoints
        if isinstance(ep, dict)
    ]


def check_default_model_endpoints(cfg: dict, rep: Report) -> None:
    """Слаг живой, а вызвать модель всё равно нельзя: ни один её провайдер не берёт наши параметры.

    Второй инцидент 27.07.2026, тем же вечером и с тем же симптомом «бот молчит», но другой
    причиной — почему `check_model_slugs` его и не поймал: `deepseek/deepseek-chat` в каталоге
    ЕСТЬ. Не оказалось эндпоинта, умеющего `reasoning`: `agent.reasoning_effort` задан, Hermes
    шлёт параметр всегда, а `provider_routing.require_parameters: true` (правило 13) велит
    OpenRouter отбрасывать провайдера, не поддерживающего ЛЮБОЙ переданный параметр. Отбросив
    всех троих, он отвечает `HTTP 404 No endpoints found that can handle the requested
    parameters`. Замерено живым ключом: `chat+tools` → 200, `chat+tools+reasoning` → 404,
    `v4-flash+tools+reasoning` → 200.

    Проверяется только глобальный дефолт: `auxiliary.*` инструментов не получают, и требовать
    от них `tools` значит краснеть на ролях, которым это не нужно.
    """
    root = _as_dict(_get(cfg, "model"))
    model = str(root.get("default") or "").strip()
    provider = str(root.get("provider") or "").strip().lower()
    if not model or provider != "openrouter" or model.count("/") != 1:
        return  # эти случаи разбирает check_model_slugs — второй жалобы на то же не нужно

    required = {"tools"}
    effort = _get(cfg, "agent.reasoning_effort")
    if effort is not _MISSING and str(effort).strip().lower() not in ("", "none"):
        required.add("reasoning")

    endpoints = fetch_endpoint_params(model)
    if endpoints is None:
        rep.warn(
            "model.default",
            f"список эндпоинтов {model!r} не достался — совместимость с {sorted(required)} "
            "НЕ проверена; прогнать линт с сетью до деплоя",
        )
        return
    if any(required <= params for params in endpoints):
        return

    have = set().union(*endpoints) if endpoints else set()
    rep.error(
        "model.default",
        f"{model!r}: ни один из {len(endpoints)} провайдеров OpenRouter не поддерживает "
        f"{sorted(required - have) or sorted(required)} ⇒ при require_parameters: true это "
        "HTTP 404 «No endpoints found» на КАЖДОМ ходу с инструментами, а не деградация качества",
    )


def check_no_model_routing(cfg: dict, rep: Report) -> None:
    """`model_routing` в нашем конфиге запрещён — не по вкусу, а потому что он ломает прод.

    Смарт-роутинг Hermes (`agent/router.py`) уводит в тир `v3` ЛЮБОЙ ход, где агенту доступны
    инструменты, то есть у нас каждый. Тиры зашиты на старые модели DeepSeek, а у них нет
    провайдера с `reasoning` — при обязательном `require_parameters: true` это HTTP 404 на всё.
    27.07.2026 блок клал прод дважды за день: сперва со слагом `deepseek/deepseek-v3` из
    докстринга (HTTP 400), потом с валидным `deepseek/deepseek-chat` (HTTP 404).

    Выключателя у роутера нет: `smart_model_routing.enabled` пишет только setup-wizard, а
    рантайм смотрит исключительно на наличие ключа `model_routing` (`agent/router.py:287`:
    `routing = config.get("model_routing", {})` → пусто ⇒ `(None, None)` ⇒ дефолт). Поэтому
    единственная рабочая форма «выключено» — отсутствие ключа, и проверять надо именно её.
    """
    if _get(cfg, "model_routing") is _MISSING:
        return
    rep.error(
        "model_routing",
        "блок присутствует: Hermes перебьёт дефолт на свои тиры (v3/r1) на каждом ходу с "
        "инструментами. Выключателя нет — `smart_model_routing.enabled` рантайм не читает; "
        "снять ключ целиком",
    )


def check_no_secrets(raw_text: str, rep: Report) -> None:
    """Правило 5. Линт печатает ТОЛЬКО имя формы, никогда — совпавший текст."""
    for pattern, what in _SECRET_SHAPES:
        if pattern.search(raw_text):
            rep.error(
                "<файл>",
                f"похоже на {what} прямо в конфиге. Секреты только в ~/.hermes/.env, сюда — ${{VAR}}",
            )


def check_credential_boundary(cfg: dict, rep: Report) -> None:
    """Хост A — вольный, и держится он ровно на одном: полноправных кредов на нём НЕТ.
    `docker exec aimash-bot …` из репо-эталона тянет туда окружение денежного контейнера
    (GOOGLE_ADS_*, SECRETS_ENCRYPTION_KEY, DATABASE_URL) и обнуляет схему целиком."""
    servers = _get(cfg, "mcp_servers")
    if not isinstance(servers, dict):
        return
    for name, srv in servers.items():
        if not isinstance(srv, dict):
            continue
        cmd = str(srv.get("command", ""))
        args = [str(a) for a in _as_list(srv.get("args"))]
        blob = " ".join([cmd, *args])
        if cmd.endswith("docker") or cmd == "docker":
            rep.error(
                f"mcp_servers.{name}.command",
                "на Хосте A запуск через docker запрещён: он наследует окружение денежного "
                "контейнера (полноправный OAuth, ключ расшифровки refresh-токенов, DSN) — "
                "read-only потолок после этого не значит ничего",
            )
        if "aimash-bot" in blob:
            rep.error(
                f"mcp_servers.{name}.args",
                "ссылка на контейнер денежного контура (`aimash-bot`) — этого контейнера на "
                "Хосте A быть не должно вовсе",
            )
        include = _as_list(
            (srv.get("tools") or {}).get("include") if isinstance(srv.get("tools"), dict) else None
        )
        if "get_change_history" in include:
            rep.error(
                f"mcp_servers.{name}.tools.include",
                "get_change_history читает НАШ audit-trail из таблицы `proposals` "
                "(mcp_server/tools_read.py:377-379), а эта БД живёт на Хосте B. С Хоста A он "
                "вернёт пустой ответ, который агент прочитает как «изменений не было»",
            )


def check_terminal_backend(cfg: dict, rep: Report) -> None:
    backend = _get(cfg, "terminal.backend")
    if backend is not _MISSING and backend != "local":
        rep.error(
            "terminal.backend",
            f"{backend!r}: при контейнерном бэкенде guard stack пропускается целиком "
            "(«container bypass») — перестают срабатывать даже декоративные апрувы",
        )


_PROFILE_CHECKS = {
    "host-a": (check_credential_boundary, check_terminal_backend),
    "vps-read": (),
}


def _infer_profile(path: Path) -> str | None:
    """Профиль по пути — ТОЛЬКО для эталонов внутри `deploy/hermes/`, для остальных `None`.

    Прежний дефолт «host-a для всего» давал на прод-эталоне три ЛОЖНЫЕ ошибки
    `check_credential_boundary` (docker / `aimash-bot` / `get_change_history` запрещены на
    Хосте A и штатны на Хосте B) — а линт, который врёт на главном охраняемом файле, перестают
    читать. Угадывать по внешним путям нельзя в обратную сторону: `~/.hermes/config.yaml` на
    живой ВМ Хоста A не содержит в пути `host-a`, и молчаливый откат на `vps-read` снял бы
    ровно проверки границы креденшелов. Не вывелось ⇒ требуем `--profile` явно (fail-closed).
    """
    try:
        rel = path.resolve().relative_to(_HERE)
    except ValueError:
        return None
    return "host-a" if "host-a" in rel.parts else "vps-read"


def lint(cfg: dict, raw_text: str = "", profile: str = "host-a") -> Report:
    """Единственная точка входа. `cfg` — уже разобранный YAML (тест кормит словарём напрямую,
    чтобы проверять правила без файлов на диске)."""
    rep = Report()
    if not isinstance(cfg, dict):
        rep.error("<файл>", "корень конфига не mapping")
        return rep
    for check in (
        check_unknown_keys,
        check_enum_values,
        check_telegram_gates,
        check_toolsets,
        check_hardening,
        check_model_slugs,
        check_default_model_endpoints,
        check_no_model_routing,
    ):
        check(cfg, rep)
    for check in _PROFILE_CHECKS.get(profile, ()):
        check(cfg, rep)
    if raw_text:
        check_no_secrets(raw_text, rep)
    return rep


def _pin_banner() -> str:
    """Аттестации сняты против конкретной версии апстрима. Без этой строки отчёт линта
    выглядел бы вечно актуальным, а у Hermes 0.x ключи переименовываются между релизами (Р7)."""
    try:
        pin = json.loads(PIN_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "ПИН НЕИЗВЕСТЕН: deploy/hermes/PIN.json не прочитан — аттестации не к чему привязать"
    return (
        f"аттестации сняты против {pin.get('upstream')} @ {pin.get('ref')} "
        f"({pin.get('attested_at')}); на хосте обязан стоять он же — сверить `hermes version`"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="К10-линт конфига Hermes")
    # `nargs="+"` — не удобство: pre-commit передаёт хуку СПИСОК затронутых файлов, а профиль у
    # каждого свой (`_infer_profile`). Одиночный аргумент заставил бы либо звать линт дважды
    # мимо списка от pre-commit, либо гонять оба эталона под одним профилем — а это ровно те
    # три ложные ошибки `check_credential_boundary`, из-за которых линт и перестают читать.
    ap.add_argument(
        "config", type=Path, nargs="+", help="пути к config.yaml (эталоны или ~/.hermes/)"
    )
    ap.add_argument(
        "--profile",
        choices=sorted(_PROFILE_CHECKS),
        default=None,
        help="по умолчанию выводится из пути внутри deploy/hermes/ "
        "(.../host-a/... → host-a, иначе vps-read); для путей вне репо обязателен",
    )
    ap.add_argument("--strict", action="store_true", help="считать предупреждения ошибками")
    args = ap.parse_args(argv)

    print(f"# {_pin_banner()}")
    rc = 0
    for path in args.config:
        profile = args.profile or _infer_profile(path)
        if profile is None:
            ap.error(
                f"{path} лежит вне deploy/hermes/ — профиль не выводится, укажите --profile "
                f"явно ({', '.join(sorted(_PROFILE_CHECKS))})"
            )
        origin = "задан явно" if args.profile else "выведен из пути"

        raw = path.read_text(encoding="utf-8")
        rep = lint(yaml.safe_load(raw), raw_text=raw, profile=profile)

        print(f"# {path} (профиль {profile}, {origin})")
        for finding in sorted(rep.findings, key=lambda f: (_LEVELS.index(f.level), f.path)):
            print(finding)
        print(f"# итого: ошибок {len(rep.errors)}, предупреждений {len(rep.warnings)}")
        if not rep.errors and not rep.warnings:
            print("# чисто — но это значит «не разошлось с таблицей», а не «безопасно»")
        if rep.errors or (args.strict and rep.warnings):
            rc = 1
    return rc


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
