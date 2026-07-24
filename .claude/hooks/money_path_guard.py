#!/usr/bin/env python3
"""Хук строгого режима на «денежных» путях (Claude Code PreToolUse/PostToolUse).

Читает JSON события из stdin, сопоставляет редактируемый файл с глоб-реестром
`.claude/hooks/money_paths.json` и:
- PreToolUse, критичный путь (ads/mutations.py, confirm/**, ...) → permissionDecision "ask":
  Claude обязан получить подтверждение пользователя ПЕРЕД правкой + показывает напоминание об
  инвариантах золотых правил;
- PreToolUse, широкий путь (scheduler/agent/bot/валидация) → additionalContext (напоминание,
  без блокировки);
- PostToolUse, любой из них → additionalContext: напоминание прогнать safety-тесты.

Fail-OPEN by design: этот хук — УДОБНОЕ напоминание, а НЕ настоящий гард (реальная защита —
в коде: ensure_allowed/confirm-гейт + тесты). Любая ошибка скрипта → ничего не выводим и exit 0,
чтобы баг хука не заблокировал редактирование. Никакого сетевого/тяжёлого кода.
"""

from __future__ import annotations

import fnmatch
import json
import os
import sys


def _find_root(start: str) -> str:
    """Каталог проекта — ближайший вверх, содержащий .claude (иначе — start)."""
    cur = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(cur, ".claude")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return os.path.abspath(start)
        cur = parent


def _rel(path: str, root: str) -> str:
    """Путь относительно корня проекта, прямые слэши, без ведущего './'."""
    try:
        rp = os.path.relpath(path, root)
    except ValueError:  # разные диски на Windows и т.п.
        rp = path
    rp = rp.replace("\\", "/")
    return rp[2:] if rp.startswith("./") else rp


def _matches(rel: str, pattern: str) -> bool:
    pattern = pattern.replace("\\", "/")
    if pattern.endswith("/**"):  # рекурсивный каталог
        base = pattern[:-3]
        return rel == base or rel.startswith(base + "/")
    return fnmatch.fnmatch(rel, pattern) or rel == pattern or rel.endswith("/" + pattern)


def _emit(
    *,
    event: str,
    decision: str | None = None,
    reason: str | None = None,
    context: str | None = None,
) -> None:
    out: dict = {"hookSpecificOutput": {"hookEventName": event}}
    if decision:
        out["hookSpecificOutput"]["permissionDecision"] = decision
    if reason:
        out["hookSpecificOutput"]["permissionDecisionReason"] = reason
    if context:
        out["hookSpecificOutput"]["additionalContext"] = context
    print(json.dumps(out, ensure_ascii=False))


def main() -> None:
    raw = sys.stdin.read()
    if not raw.strip():
        return
    data = json.loads(raw)

    tool_input = data.get("tool_input") or {}
    file_path = tool_input.get("file_path") or tool_input.get("path") or ""
    if not file_path:
        return

    event = data.get("hook_event_name") or "PreToolUse"
    root = _find_root(data.get("cwd") or os.getcwd())

    cfg_path = os.path.join(root, ".claude", "hooks", "money_paths.json")
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)

    rel = _rel(file_path, root)
    is_critical = any(_matches(rel, p) for p in cfg.get("critical", []))
    is_broad = any(_matches(rel, p) for p in cfg.get("broad", []))

    if event == "PostToolUse":
        if is_critical or is_broad:
            _emit(event="PostToolUse", context=cfg.get("post_reminder"))
        return

    # PreToolUse
    if is_critical:
        _emit(event="PreToolUse", decision="ask", reason=cfg.get("critical_reminder"))
    elif is_broad:
        _emit(event="PreToolUse", context=cfg.get("broad_reminder"))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 — fail-open: баг хука не должен блокировать редактирование
        pass
