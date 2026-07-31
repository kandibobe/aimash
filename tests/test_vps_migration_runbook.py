"""Гард: ранбук переезда VPS (OPERATIONS.md §15–§16) и его скрипты не расходятся.

Почему это тест, а не «прочитать глазами перед переездом». Ранбук исполняется РЕДКО и в стрессе:
прод погашен, окно тикает. Ровно в этот момент выясняется, что упомянутого в доке скрипта нет
(переименовали), что скрипт синтаксически битый (правили без запуска), или что защита, ради которой
он написан, вычищена рефакторингом. Дешёвая проверка на каждом прогоне CI стоит меньше, чем один
такой сюрприз в окне даунтайма.

Проверяются три свойства:
  1. скрипты существуют, POSIX-совместимы и синтаксически валидны (`sh -n`);
  2. защиты, ради которых они написаны, на месте (правило 5, fail-closed, «один поллер Telegram»);
  3. дока и файлы синхронны: каждый `scripts/*.sh`, упомянутый в §15–§16, реально существует.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
OPERATIONS = ROOT / "deploy" / "hermes" / "OPERATIONS.md"

MIGRATION_SCRIPTS = (
    "vps_migrate_export.sh",
    "vps_migrate_import.sh",
    "vps_migrate_verify.sh",
)


def _read(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", MIGRATION_SCRIPTS)
def test_script_exists_and_is_posix_sh(name: str) -> None:
    path = SCRIPTS / name
    assert path.is_file(), f"{name} упомянут в ранбуке §16, но файла нет"
    # `#!/bin/sh`, а не bash: скрипты гоняются на свежем сервере, где bash есть, но полагаться на
    # bash-измы в ранбуке восстановления — лишняя переменная (busybox/dash в rescue-режиме).
    assert _read(name).startswith("#!/bin/sh"), f"{name}: ожидается shebang #!/bin/sh"


@pytest.mark.parametrize("name", MIGRATION_SCRIPTS)
def test_script_syntax_is_valid(name: str) -> None:
    sh = shutil.which("sh")
    if not sh:
        pytest.skip("нет sh в PATH (Windows без Git Bash) — синтаксис проверит CI на ubuntu")
    proc = subprocess.run([sh, "-n", str(SCRIPTS / name)], capture_output=True, text=True)
    assert proc.returncode == 0, f"{name}: синтаксическая ошибка\n{proc.stderr}"


@pytest.mark.parametrize("name", MIGRATION_SCRIPTS)
def test_no_secret_file_is_ever_printed(name: str) -> None:
    """Правило 5: `.env` не выводится в stdout/лог ни одним из скриптов.

    Скрипты обязаны сообщать НАЛИЧИЕ ключа (`grep -q`), а не значение. `cat`/`head`/`tail` над
    `.env` — прямой путь секрета в лог CI, журнал systemd или скроллбек SSH-сессии.
    """
    body = _read(name)
    for bad in re.finditer(r"\b(cat|head|tail|less|more)\b[^\n|;]*\.env\b", body):
        pytest.fail(f"{name}: печать .env запрещена (правило 5): {bad.group(0).strip()!r}")


def test_export_refuses_to_write_into_the_repo() -> None:
    """Архив экспорта несёт два `.env` открытым текстом — внутри git-дерева ему нельзя (правило 5)."""
    body = _read("vps_migrate_export.sh")
    assert 'OUT_DIR="${MIGRATE_OUT_DIR:-/root/vps-migration}"' in body, (
        "дефолтный каталог экспорта должен быть вне /opt/aimash"
    )
    assert "OUT_DIR внутри репозитория" in body, (
        "нет активной проверки «OUT_DIR внутри репозитория»"
    )
    assert 'chmod 600 "$OUT"' in body, "архив с секретами обязан создаваться с правами 600"


def test_export_verifies_dump_and_hermes_backup() -> None:
    """«Файл создался» ≠ «бэкап годен» — обе проверки целостности должны остаться в скрипте."""
    body = _read("vps_migrate_export.sh")
    assert "PGDMP" in body, "нет проверки магии PGDMP — битый/пустой дамп пройдёт незамеченным"
    assert "state\\.db" in body or "state.db" in body, (
        "нет проверки, что архив Hermes содержит state.db (история сессий)"
    )


def test_import_is_fail_closed_and_never_starts_pollers_silently() -> None:
    body = _read("vps_migrate_import.sh")
    assert body.count("set -e") >= 1, "import обязан быть fail-closed (set -e)"
    # Защита от pg_restore --clean по живой боевой БД.
    assert "--allow-same-host" in body and "машиной-источником" in body, (
        "потеряна защита от импорта на машину-источник"
    )
    # M1: один токен Telegram = один поллер. Подъём только по явному флагу.
    assert "START_APP=0" in body and "START_GW=0" in body, (
        "поллеры Telegram (bot/gateway) не должны подниматься по умолчанию — только по флагу"
    )
    assert "SECRETS_ENCRYPTION_KEY" in body, (
        "import обязан проверять ключ шифрования: без него oauth_tokens из дампа мертвы"
    )
    assert "sha256sum -c" in body, "нет сверки контрольных сумм архива"


def test_verify_is_a_gate_not_a_report() -> None:
    """verify обязан валить exit-кодом, иначе это просто текст на экране."""
    body = _read("vps_migrate_verify.sh")
    assert "exit 1" in body, "verify без ненулевого выхода не гейт"
    assert 'if [ "$F" -gt 0 ]' in body, "нет итогового решения по счётчику FAIL"
    # Осознанное отсутствие `set -e` — часть контракта: нужен ПОЛНЫЙ список проблем, не первая.
    assert not re.search(r"^set -e\s*$", body, re.MULTILINE), (
        "в verify не должно быть set -e — он обрубит проверку на первой находке"
    )
    # Мины переезда, которые ловятся только здесь.
    for marker, why in (
        ("409", "двойной поллер Telegram (M1)"),
        ("funnel", "публичный доступ к пульту — К3"),
        ("oom-kill", "возврат к OOM после потери drop-in MemoryMax (M5)"),
        ("Linger", "user-gateway умрёт при logout (§2)"),
        ("alembic_version", "схема БД после restore"),
    ):
        assert marker in body, f"verify перестал проверять {why}"

    # BusyBox/POSIX grep -E не понимает PCRE-классы \s/\w. Проверка должна сравнивать
    # официальный счётчик pinned Hermes с фактическим registry live-контейнера.
    assert "Tools discovered:" in body
    assert "expected_tool_names" in body
    assert '"$DISCOVERED" = "$EXPECTED"' in body
    assert "grep -coE 'mcp__aimash__|^\\s*-\\s+\\w+'" not in body


def test_runbook_mentions_every_migration_script() -> None:
    text = OPERATIONS.read_text(encoding="utf-8")
    assert "## 16. Переезд на ДРУГУЮ машину" in text, "раздел §16 исчез из ранбука"
    for name in MIGRATION_SCRIPTS:
        assert name in text, f"§16 не упоминает {name} — ранбук и скрипты разошлись"
    # §15 (rescale) обязан указывать на §16: иначе «переезд» ищут по всему файлу в окне даунтайма.
    rescale = text.split("## 15. Апгрейд VPS", 1)[1].split("## 16.", 1)[0]
    assert "§16" in rescale, "§15 не ссылается на §16 (переезд vs rescale — первый же вопрос)"
    assert "vps_migrate_verify.sh" in rescale, "§15 не зовёт verify после rescale"


def test_every_script_referenced_by_the_runbook_exists() -> None:
    """Антидрейф: переименовали скрипт — тест падает здесь, а не на погашенном проде."""
    text = OPERATIONS.read_text(encoding="utf-8")
    referenced = set(re.findall(r"scripts/([a-z0-9_]+\.sh)", text))
    missing = sorted(n for n in referenced if not (SCRIPTS / n).is_file())
    assert not missing, f"OPERATIONS.md ссылается на несуществующие скрипты: {missing}"
