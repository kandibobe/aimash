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

Код возврата: 1 — есть ERROR хотя бы в одном файле (или WARN с `--strict`); 0 — чисто.

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

# OAuth-backed Codex provider does not use OpenRouter's catalog. These are the pinned Hermes
# v0.19.0 curated fallbacks from hermes_cli/codex_models.py at ref v2026.7.20. Keep the list tied
# to PIN.json: a Hermes upgrade must re-attest it instead of silently accepting a guessed slug.
_OPENAI_CODEX_MODELS = frozenset(
    {
        "gpt-5.6-sol",
        "gpt-5.6-sol-pro",
        "gpt-5.6-terra",
        "gpt-5.6-terra-pro",
        "gpt-5.6-luna",
        "gpt-5.6-luna-pro",
        "gpt-5.5",
        "gpt-5.4-mini",
        "gpt-5.4",
        "gpt-5.3-codex",
        "gpt-5.3-codex-spark",
    }
)

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
    "fallback_providers": Attested("hermes_cli/config.py:910-914", '"fallback_providers": []'),
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
    "agent.restart_drain_timeout": Attested(
        "hermes_cli/config.py:1020-1033", '"restart_drain_timeout": 0'
    ),
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
    "skills.platform_disabled": Attested(
        "hermes_cli/skills_config.py:10,58-61",
        "platform_disabled:                    # per-platform overrides",
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
    "delegation": Attested("hermes_cli/config.py:2364", '"delegation": {'),
    "_config_version": Attested("hermes_cli/config.py", '"_config_version"'),
    "agent.reasoning_overrides": Attested("hermes_cli/config.py:1193", '"reasoning_overrides": {}'),
    "browser.cloud_provider": Attested(
        "hermes_cli/tools_config.py:3057", 'cfg_get(config, "browser", "cloud_provider")'
    ),
    "command_allowlist": Attested("hermes_cli/config.py", '"command_allowlist": []'),
    "compression.enabled": Attested("hermes_cli/config.py", '"enabled": True'),
    "compression.threshold": Attested("hermes_cli/config.py", '"threshold"'),
    "compression.target_ratio": Attested("hermes_cli/config.py", '"target_ratio"'),
    "compression.protect_last_n": Attested("hermes_cli/config.py", '"protect_last_n"'),
    "compression.protect_first_n": Attested("hermes_cli/config.py", '"protect_first_n"'),
    "session_reset.mode": Attested(
        "gateway/config.py:456-473", 'mode: str = "none"  # "daily", "idle", "both", or "none"'
    ),
    "session_reset.idle_minutes": Attested("gateway/config.py:456-473", "idle_minutes: int = 1440"),
    "session_reset.notify": Attested("gateway/config.py:456-474", "notify: bool = True"),
    "sessions.auto_prune": Attested("hermes_cli/config.py:3174-3188", '"auto_prune": False'),
    "sessions.retention_days": Attested("hermes_cli/config.py:3174-3188", '"retention_days": 90'),
    "sessions.vacuum_after_prune": Attested(
        "hermes_cli/config.py:3189-3195", '"vacuum_after_prune": True'
    ),
    "sessions.min_interval_hours": Attested(
        "hermes_cli/config.py:3196-3199", '"min_interval_hours": 24'
    ),
    "display.show_cost": Attested("hermes_cli/config.py", '"show_cost": False'),
    "display.runtime_footer": Attested("hermes_cli/config.py", '"runtime_footer": {'),
    "auxiliary.transient_retries": Attested("hermes_cli/config.py", '"transient_retries"'),
    "auxiliary.vision": Attested("hermes_cli/config.py", '"vision": {'),
    "auxiliary.web_extract": Attested("hermes_cli/config.py", '"web_extract": {'),
    "auxiliary.approval": Attested("hermes_cli/config.py", '"approval": {'),
    "auxiliary.title_generation": Attested("hermes_cli/config.py", '"title_generation": {'),
    "auxiliary.memory_query_rewrite": Attested("hermes_cli/config.py", '"memory_query_rewrite": {'),
    "auxiliary.curator": Attested("hermes_cli/config.py", '"curator": {'),
    "dashboard.basic_auth": Attested("hermes_cli/config.py", '"basic_auth": {'),
    "onboarding.seen": Attested("hermes_cli/config.py", '"seen": {'),
}

# В этих поддеревьях имена ниже точки динамические (имя MCP-сервера, платформа, model slug)
# либо валидируются отдельным профильным правилом. Для остальных путей аттестация только точная:
# опечатка `agent.max_turn` не должна пройти потому, что `agent` как раздел существует.
_ATTESTED_SUBTREES = frozenset(
    {
        "model",
        "mcp_servers",
        "gateway.platforms",
        "display.platforms",
        "platform_toolsets",
        "known_plugin_toolsets",
        "skills.platform_disabled",
        "agent.reasoning_overrides",
        "display.runtime_footer",
        "dashboard.basic_auth",
        "onboarding.seen",
        "auxiliary.vision",
        "auxiliary.web_extract",
        "auxiliary.approval",
        "auxiliary.title_generation",
        "auxiliary.memory_query_rewrite",
        "auxiliary.curator",
        "auxiliary.compression",
        "delegation",
        "tool_loop_guardrails.hard_stop_after",
    }
)

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

# Слаги тулсетов установленной версии. Пересверено 30.07.2026 живым
# `hermes tools list --platform telegram` на v0.19.0 — предыдущий список (21.07) не знал шести
# слагов, и ровно через эту дыру `computer_use` два дня стоял включённым на боевом gateway.
_KNOWN_TOOLSETS = frozenset(
    {
        "browser",
        "clarify",
        "code_execution",
        "computer_use",
        "context_engine",
        "cronjob",
        "delegation",
        "file",
        "homeassistant",
        "image_gen",
        "memory",
        "session_search",
        "skills",
        "spotify",
        "terminal",
        "todo",
        "tts",
        "video",
        "video_gen",
        "vision",
        "web",
        "x_search",
        "yuanbao",
    }
)

# Private trusted-operator profile approved by the owner on 2026-07-31. Keep this as an allow-list:
# a new toolset shipped by a future Hermes version must still require an explicit decision.
_ALLOWED_ENABLED_TOOLSETS = frozenset(
    {
        "browser",
        "clarify",
        "code_execution",
        "computer_use",
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
        "video",
        "vision",
        "web",
    }
)

# Production Telegram gateway may see only our bounded Ads READ server plus the explicitly
# allow-listed research server. Every server must declare tools.include: an unbounded MCP server
# silently exposes all current and future tools, including mutations (observed with GitHub MCP on
# the live gateway 2026-07-31).
_ALLOWED_VPS_MCP_SERVERS = frozenset({"aimash", "tavily"})

# Toolsets explicitly kept inactive in the same fixed profile.
_MUST_DISABLE = {
    "homeassistant": "в проекте нет Home Assistant",
    "spotify": "в проекте нет Spotify-сценариев",
    "video_gen": "генерация видео не входит в текущий контур",
    "x_search": "X Search не настроен",
    "yuanbao": "Yuanbao не используется",
    "tts": "text-to-speech не входит в текущий контур",
}

# Эти пути похожи на реальные настройки, но у пина v2026.7.20 нет ни одного runtime-read.
# Именно такие ключи опаснее синтаксической ошибки: YAML валиден, Hermes стартует, оператор
# видит «настроенный» блок и принимает дефолтное поведение за задуманное.
_INERT_CONFIG_PATHS = {
    "model_routing": (
        "в Hermes v0.19.0 нет runtime model router; используйте явную main-модель, "
        "fallback_providers и delegation.model"
    ),
    "smart_model_routing": (
        "setup-wizard записывает этот маркер, но пиновый runtime его не читает"
    ),
    "browser.use_gateway": "browser читает cloud_provider; use_gateway относится к web/image/video",
    "openrouter.extra_headers": (
        "этот блок не читается; для заголовков поддержаны model.default_headers или providers.*"
    ),
    "tool_loop_guardrails.exact_failure": "порог должен быть внутри hard_stop_after/warn_after",
    "tool_loop_guardrails.same_tool_failure": "порог должен быть внутри hard_stop_after/warn_after",
    "tool_loop_guardrails.idempotent_no_progress": (
        "порог должен быть внутри hard_stop_after/warn_after"
    ),
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
    """Return effective leaf settings, not structural YAML containers."""
    out: list[str] = []
    if not isinstance(cfg, dict):
        return out
    for key, val in cfg.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(val, dict) and val:
            out.extend(_flatten(val, path))
        else:
            out.append(path)
    return out


def _is_attested(path: str) -> bool:
    """Exact by default; only explicitly dynamic subtrees accept descendants."""
    if path in _ATTESTED:
        return True
    return any(path.startswith(f"{prefix}.") for prefix in _ATTESTED_SUBTREES)


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


def check_inert_keys(cfg: dict, rep: Report) -> None:
    """Reject settings proven to have no consumer in the pinned runtime."""
    for path, explanation in _INERT_CONFIG_PATHS.items():
        if _get(cfg, path) is not _MISSING:
            rep.error(path, f"ключ не читается пиновым runtime: {explanation}")


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

    if tg.get("require_mention") is not False:
        rep.error(
            f"{base}.require_mention",
            "обязан быть false: обычный текст allowlisted sender в супергруппе должен будить "
            "агента без reply/@mention; доступ по-прежнему ограничивает group_allow_from",
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


def check_vps_trusted_operator_policy(cfg: dict, rep: Report) -> None:
    """Exact owner-approved policy for the private production Telegram gateway."""
    disabled = set(_as_list(_get(cfg, "agent.disabled_toolsets")))
    for slug, why in _MUST_DISABLE.items():
        if slug not in disabled:
            rep.error("agent.disabled_toolsets", f"{slug} обязан быть погашен: {why}")
    expected = {
        "memory.memory_enabled": True,
        "memory.user_profile_enabled": True,
        "compression.enabled": True,
        "compression.threshold": 0.15,
        "compression.target_ratio": 0.10,
        "compression.protect_last_n": 12,
        "compression.protect_first_n": 0,
        "session_reset.mode": "idle",
        "session_reset.idle_minutes": 1440,
        "session_reset.notify": True,
        "sessions.auto_prune": True,
        "sessions.retention_days": 30,
        "sessions.vacuum_after_prune": True,
        "sessions.min_interval_hours": 24,
    }
    for path, want in expected.items():
        value = _get(cfg, path)
        if value is _MISSING:
            rep.error(path, f"не задан явно; private trusted-operator profile требует {want!r}")
        elif type(value) is not type(want) or value != want:
            rep.error(path, f"{value!r}, ожидалось {want!r} для private trusted-operator profile")


def check_delegation(cfg: dict, rep: Report) -> None:
    """A configured-but-disabled orchestrator is a silent no-op; an enabled one is explicit."""
    disabled = set(_as_list(_get(cfg, "agent.disabled_toolsets")))
    delegation = _get(cfg, "delegation")
    if "delegation" in disabled:
        if isinstance(delegation, dict) and delegation:
            rep.error(
                "delegation",
                "блок настроен, но toolset delegation выключен в agent.disabled_toolsets",
            )
        return

    if not isinstance(delegation, dict) or not delegation:
        rep.error("delegation", "toolset включён, но его модель и лимиты не заданы явно")
        return
    for key in ("provider", "model"):
        if not str(delegation.get(key) or "").strip():
            rep.error(f"delegation.{key}", "обязателен для явной модели субагентов")
    if delegation.get("orchestrator_enabled") is not True:
        rep.error("delegation.orchestrator_enabled", "должен быть true для agent orchestration")
    if delegation.get("subagent_auto_approve") is not True:
        rep.error(
            "delegation.subagent_auto_approve",
            "должен быть true: READ-аналитик вызывается Hermes как бесшовный tool",
        )


def check_toolset_allowlist(cfg: dict, rep: Report) -> None:
    """Поверхность агента — от РАЗРЕШЁННОГО, а не от запрещённого (профиль `vps-read`).

    `check_toolsets` смотрит только на то, что перечислено в `disabled_toolsets`, поэтому тулсет,
    которого там нет, проходит молча — а по дефолту он ВКЛЮЧЁН. Через эту дыру `computer_use`,
    `x_search`, `video_gen`, `homeassistant`, `spotify`, `yuanbao` вообще не попадали в поле зрения
    линта, а 27.07.2026 живой конфиг пересобрался из дефолта и вернул `terminal`/`file`/
    `code_execution` на боевой telegram-gateway — линт при этом молчал.

    Только для `vps-read`: там агент ходит в чужие деньги и обязан иметь ровно наш набор. Профиль
    `host-a` — рабочая машина владельца, её включённая поверхность (терминал и т.п.) — отдельное
    осознанное решение, у неё свои проверки (`check_terminal_backend`, `check_credential_boundary`).
    """
    disabled = set(_as_list(_get(cfg, "agent.disabled_toolsets")))
    for slug in sorted(_KNOWN_TOOLSETS - _ALLOWED_ENABLED_TOOLSETS - disabled):
        rep.error(
            "agent.disabled_toolsets",
            f"{slug} не погашен и не входит в разрешённые "
            f"{sorted(_ALLOWED_ENABLED_TOOLSETS)} ⇒ ВКЛЮЧЁН по дефолту. Либо дописать его в "
            "disabled_toolsets, либо (осознанно, письменным решением) — в "
            "_ALLOWED_ENABLED_TOOLSETS линта",
        )


def check_mcp_server_allowlists(cfg: dict, rep: Report) -> None:
    """Fail closed on extra or unbounded MCP servers in the production Telegram gateway."""
    servers = _get(cfg, "mcp_servers")
    if not isinstance(servers, dict):
        rep.error("mcp_servers", "production gateway must declare an explicit MCP server mapping")
        return
    for name, server in servers.items():
        path = f"mcp_servers.{name}"
        if name not in _ALLOWED_VPS_MCP_SERVERS:
            rep.error(
                path,
                f"server is outside the production allow-list {sorted(_ALLOWED_VPS_MCP_SERVERS)}",
            )
            continue
        tools = server.get("tools") if isinstance(server, dict) else None
        include = tools.get("include") if isinstance(tools, dict) else None
        if (
            not isinstance(include, list)
            or not include
            or not all(isinstance(tool, str) and tool.strip() for tool in include)
        ):
            rep.error(
                f"{path}.tools.include",
                "must be a non-empty list; absent include exposes every MCP tool by default",
            )


def check_hardening(cfg: dict, rep: Report) -> None:
    """Ключи, у которых дефолт апстрима работает ПРОТИВ нас. Все обязаны стоять явно —
    дефолт в 0.x не обещание (Р7), и его смена не даст ни ошибки, ни строки в логе."""
    expected = {
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

    Глобальный `model:` виден в `hermes model`; auxiliary/delegation/fallback-цепь легко
    протухают незаметно, поэтому проверяются тем же живым каталогом.
    """
    root = _as_dict(_get(cfg, "model"))
    default_provider = str(root.get("provider") or "").strip()
    out: list[tuple[str, str, str]] = []
    if root:
        out.append(("model", default_provider, str(root.get("default") or "").strip()))
    for role, spec in _as_dict(_get(cfg, "auxiliary")).items():
        if isinstance(spec, dict) and "model" in spec:
            provider = str(spec.get("provider") or "").strip() or default_provider
            out.append((f"auxiliary.{role}", provider, str(spec.get("model") or "").strip()))
    delegation = _as_dict(_get(cfg, "delegation"))
    if "model" in delegation:
        provider = str(delegation.get("provider") or "").strip() or default_provider
        out.append(("delegation", provider, str(delegation.get("model") or "").strip()))

    fallbacks = _as_list(_get(cfg, "fallback_providers"))
    legacy = _get(cfg, "fallback_model")
    if legacy is not _MISSING:
        fallbacks.extend(_as_list(legacy))
    for index, spec in enumerate(fallbacks):
        if not isinstance(spec, dict):
            out.append((f"fallback_providers[{index}]", "", ""))
            continue
        provider = str(spec.get("provider") or "").strip() or default_provider
        out.append(
            (
                f"fallback_providers[{index}]",
                provider,
                str(spec.get("model") or "").strip(),
            )
        )
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
        normalized_provider = provider.lower()
        if normalized_provider == "openai-codex":
            if model not in _OPENAI_CODEX_MODELS:
                rep.error(
                    path,
                    f"{model!r} нет в curated catalog openai-codex пинового Hermes; "
                    "сверить hermes_cli/codex_models.py и обновить PIN/аттестацию",
                )
            continue
        if normalized_provider != "openrouter":
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


def _tool_turn_models(cfg: dict) -> list[tuple[str, str]]:
    """`(путь, слаг)` каждой модели, которой достанется ход С ИНСТРУМЕНТАМИ.

    Это глобальный дефолт и fallback-цепь: после отказа основной модели fallback продолжает
    тот же агентский ход с теми же инструментами. `auxiliary.*` сюда не входит.
    """
    root = _as_dict(_get(cfg, "model"))
    default_provider = str(root.get("provider") or "").strip()
    specs = [("model.default", default_provider, str(root.get("default") or "").strip())]
    for index, spec in enumerate(_as_list(_get(cfg, "fallback_providers"))):
        if not isinstance(spec, dict):
            continue
        provider = str(spec.get("provider") or "").strip() or default_provider
        specs.append(
            (
                f"fallback_providers[{index}]",
                provider,
                str(spec.get("model") or "").strip(),
            )
        )
    # чужой провайдер и кривой формат слага — забота check_model_slugs, второй жалобы не нужно
    return [
        (path, model)
        for path, provider, model in specs
        if model and provider.lower() == "openrouter" and model.count("/") == 1
    ]


def check_tool_turn_model_endpoints(cfg: dict, rep: Report) -> None:
    """Слаг живой, а вызвать модель всё равно нельзя: ни один её провайдер не берёт наши параметры.

    Второй инцидент 27.07.2026, тем же вечером и с тем же симптомом «бот молчит», но другой
    причиной — почему `check_model_slugs` его и не поймал: `deepseek/deepseek-chat` в каталоге
    ЕСТЬ. Не оказалось эндпоинта, умеющего `reasoning`: `agent.reasoning_effort` задан, Hermes
    шлёт параметр всегда, а `provider_routing.require_parameters: true` (правило 13) велит
    OpenRouter отбрасывать провайдера, не поддерживающего ЛЮБОЙ переданный параметр. Отбросив
    всех троих, он отвечает `HTTP 404 No endpoints found that can handle the requested
    parameters`. Замерено живым ключом: `chat+tools` → 200, `chat+tools+reasoning` → 404,
    `v4-flash+tools+reasoning` → 200.

    Тиры `model_routing` проверяются наравне с дефолтом и по той же мерке: третий за день
    завал прода (12:01) пришёл именно оттуда — визард «умного роутинга» разложил тиры на
    `deepseek-chat` (0 эндпоинтов с reasoning) и `deepseek-r1` (1 из 2), а заодно перебил
    `model.default`. Тир `r1` включается по словам «аудит», «стратегия», «оптимизация» —
    то есть краснеть он обязан ДО деплоя, а не на первом же профильном вопросе менеджера.
    """
    required = {"tools"}
    effort = _get(cfg, "agent.reasoning_effort")
    if effort is not _MISSING and str(effort).strip().lower() not in ("", "none"):
        required.add("reasoning")

    for path, model in _tool_turn_models(cfg):
        endpoints = fetch_endpoint_params(model)
        if endpoints is None:
            rep.warn(
                path,
                f"список эндпоинтов {model!r} не достался — совместимость с {sorted(required)} "
                "НЕ проверена; прогнать линт с сетью до деплоя",
            )
            continue
        if any(required <= params for params in endpoints):
            continue
        have = set().union(*endpoints) if endpoints else set()
        rep.error(
            path,
            f"{model!r}: ни один из {len(endpoints)} провайдеров OpenRouter не поддерживает "
            f"{sorted(required - have) or sorted(required)} ⇒ при require_parameters: true это "
            "HTTP 404 «No endpoints found» на КАЖДОМ ходу с инструментами, а не деградация "
            "качества",
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
    "vps-read": (
        check_vps_trusted_operator_policy,
        check_toolset_allowlist,
        check_mcp_server_allowlists,
    ),
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
        check_inert_keys,
        check_enum_values,
        check_telegram_gates,
        check_toolsets,
        check_delegation,
        check_hardening,
        check_model_slugs,
        check_tool_turn_model_endpoints,
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
