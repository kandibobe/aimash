"""Восстановить поверхность тулсетов Hermes на VPS по эталону deploy/hermes/config.yaml.

Харднинг конфига Hermes не переживает пересборку конфига: 24.07.2026 поверхность закрыли
руками, 27.07 конфиг развалился (авария со слагом модели) и был пересобран из дефолта —
terminal/file/code_execution/computer_use снова оказались включены на боевом gateway, и
заметили это только 29.07. Отсюда правило: харднинг накатывается ПРОЦЕДУРОЙ из репо-эталона,
а не руками один раз.

Список гасимого берётся из `agent.disabled_toolsets` в deploy/hermes/config.yaml — того же
файла, который проверяет `lint_config.check_toolset_allowlist`. Второго списка, способного
разъехаться с первым, здесь нет намеренно.

Результат проверяется по ФАКТУ рантайма (`hermes tools list --platform <p>`) и только так:
`hermes config get` врёт — `config set` пишет строку-скаляр, а `get` читает её обратно как
«мой список» (К10-капкан, deploy/hermes/OPERATIONS.md §14).

Использование:
    python scripts/hermes_restore_toolsets.py --dry-run
    python scripts/hermes_restore_toolsets.py
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _win_console import enable_utf8  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
REFERENCE = REPO / "deploy" / "hermes" / "config.yaml"
# Пара к _ALLOWED_ENABLED_TOOLSETS в deploy/hermes/lint_config.py.
ALLOWED_ENABLED = frozenset({"skills", "todo", "clarify"})
# Строки вида "  <значок> enabled  <slug>  <лейбл>"; значок берём как \S+, чтобы не зависеть
# от эмодзи и кодовой страницы консоли.
_STATUS_RE = re.compile(r"^\s*\S+\s+(enabled|disabled)\s+(\S+)")


def read_reference_slugs() -> list[str]:
    """Слаги из agent.disabled_toolsets независимо от YAML-стиля списка."""
    try:
        cfg = yaml.safe_load(REFERENCE.read_text(encoding="utf-8")) or {}
        raw_slugs = (cfg.get("agent") or {}).get("disabled_toolsets")
    except (OSError, yaml.YAMLError) as exc:
        raise SystemExit(f"Не удалось прочитать {REFERENCE}: {type(exc).__name__}") from exc
    if not isinstance(raw_slugs, list) or not all(isinstance(s, str) for s in raw_slugs):
        raise SystemExit(f"В {REFERENCE} agent.disabled_toolsets должен быть YAML-списком строк")
    slugs = [s.strip() for s in raw_slugs if s.strip()]
    if len(slugs) != len(set(slugs)):
        raise SystemExit(f"В {REFERENCE} agent.disabled_toolsets содержит дубли")
    if len(slugs) < 10:
        raise SystemExit(f"Из эталона разобрано всего {len(slugs)} слагов — похоже на сбой разбора")
    return slugs


def ssh(host: str, *argv: str) -> str:
    """Один вызов по ssh. Без shell=True: список аргументов, никакого экранирования."""
    proc = subprocess.run(
        ["ssh", host, *argv], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if proc.returncode != 0:
        raise SystemExit(f"ssh {' '.join(argv)} → код {proc.returncode}\n{proc.stderr.strip()}")
    return proc.stdout


def parse_enabled(listing: str) -> list[str]:
    enabled = []
    for line in listing.splitlines():
        m = _STATUS_RE.match(line)
        if m and m.group(1) == "enabled":
            enabled.append(m.group(2))
    if not enabled:
        raise SystemExit(
            "Не разобрал ни одной строки статуса — формат вывода изменился, проверь руками"
        )
    return enabled


def main() -> int:
    enable_utf8()
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--host", default="hermes-vps", help="ssh-алиас VPS (по умолчанию hermes-vps)")
    ap.add_argument(
        "--platform",
        default="telegram",
        help="платформа Hermes; поверхность тулов ПЕР-ПЛАТФОРМЕННАЯ, gateway работает как telegram",
    )
    ap.add_argument("--dry-run", action="store_true", help="показать команды, VPS не трогать")
    args = ap.parse_args()

    slugs = read_reference_slugs()
    print(
        f"Эталон {REFERENCE.relative_to(REPO)}: гасим {len(slugs)} тулсетов на платформе «{args.platform}»"
    )
    print("  " + ", ".join(slugs))

    disable_argv = ["hermes", "tools", "disable", "--platform", args.platform, *slugs]
    if args.dry_run:
        print(f"\n[dry-run] ssh {args.host} {' '.join(disable_argv)}")
        print(f"[dry-run] ssh {args.host} env XDG_RUNTIME_DIR=/run/user/0 hermes gateway restart")
        print(f"[dry-run] ssh {args.host} hermes tools list --platform {args.platform}")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = f"/root/.hermes/config.yaml.bak-{stamp}"
    rollback = (
        f'ssh {args.host} "cp -a {backup} /root/.hermes/config.yaml; '
        'XDG_RUNTIME_DIR=/run/user/0 hermes gateway restart"'
    )
    ssh(args.host, "cp", "-a", "/root/.hermes/config.yaml", backup)
    print(f"Бэкап: {backup}\nОткат: {rollback}")

    ssh(args.host, *disable_argv)
    ssh(args.host, "env", "XDG_RUNTIME_DIR=/run/user/0", "hermes", "gateway", "restart")

    listing = ssh(args.host, "hermes", "tools", "list", "--platform", args.platform)
    print(listing)
    leaked = sorted(set(parse_enabled(listing)) - ALLOWED_ENABLED)
    if leaked:
        print(f"ОТКАЗ: включёнными остались тулсеты вне разрешённого набора: {', '.join(leaked)}")
        print(f"Откат: {rollback}")
        return 1
    print(
        f"OK: включены только {', '.join(sorted(ALLOWED_ENABLED))} — совпадает с разрешённым набором"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
