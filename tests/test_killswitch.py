"""BZ-1: аварийный рубильник мутаций (core.killswitch) — env + файл-флаг, оба fail-closed.

Три свойства, ради которых рубильник существует (и которые здесь закреплены):
  1. Срабатывает БЕЗ рестарта процесса: env и файл читаются на КАЖДОМ вызове, мимо
     settings-снимка (`touch KILL_SWITCH` в живом контейнере = мгновенный стоп).
  2. Fail-closed на опечатке: env выключает рубильник ТОЛЬКО известными «выключенными»
     значениями; любое другое непустое значение («flase», «disable») = ВКЛЮЧЁН.
  3. Не сжигает одноразовое подтверждение: проверка стоит ДО claim — после снятия рубильника
     тот же черновик исполняется тем же «да».

Реальный ConfirmStore на temp SQLite (conftest) для интеграционной половины.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.client import ensure_allowed  # noqa: E402
from ads.mutations import _require_confirmation  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
from conftest import attested  # noqa: E402
from core.config import settings  # noqa: E402
from core.killswitch import (  # noqa: E402
    ENV_VAR,
    GateRefusal,
    ensure_mutations_enabled,
    mutations_disabled,
)
from db.session import init_db  # noqa: E402

DRAFT = "7753643025"
OWNER = 100
OP = "update_budget"


# ── Канал env: семантика fail-closed ────────────────────────────────────────────────
def test_env_known_off_values_keep_mutations_enabled(monkeypatch):
    """Рубильник ВЫКЛЮЧЕН только явно «выключенными» значениями (регистр/пробелы — нормализуются)."""
    for off in ("", "0", "false", "no", "off", " OFF ", "False"):
        monkeypatch.setenv(ENV_VAR, off)
        assert mutations_disabled() is None, f"{off!r} должно оставлять мутации разрешёнными"
        ensure_mutations_enabled()  # не бросает


def test_env_set_disables_mutations(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")
    assert mutations_disabled() is not None
    with pytest.raises(PermissionError, match="рубильник"):
        ensure_mutations_enabled()


def test_env_typo_is_fail_closed(monkeypatch):
    """Опечатка в значении = рубильник ВКЛЮЧЁН, а не молча выключен: «flase» при попытке
    выключить мутации должно останавливать, «disable» при попытке включить — тоже останавливать.
    Рубильник, который от опечатки остаётся выключенным, — не рубильник (правило 10)."""
    for typo in ("flase", "disable", "yes", "true", "on", "стоп"):
        monkeypatch.setenv(ENV_VAR, typo)
        with pytest.raises(PermissionError):
            ensure_mutations_enabled()


def test_refusal_is_gate_refusal_and_hides_env_value(monkeypatch):
    """Ревью 2026-07-30: (а) отказ рубильника — GateRefusal (внешняя временная причина ⇒
    живой путь возвращает черновик в pending, не жжёт); (б) сырое ЗНАЧЕНИЕ env в текст не
    попадает (правило 5: значение может оказаться чем угодно, вплоть до вставленного секрета) —
    оператору достаточно ИМЕНИ переменной."""
    monkeypatch.setenv(ENV_VAR, "hunter2-secret-value")
    with pytest.raises(GateRefusal) as ei:
        ensure_mutations_enabled()
    text = str(ei.value)
    assert "hunter2" not in text  # значение скрыто
    assert ENV_VAR in text  # имя — есть: понятно, что снимать


# ── Канал файл-флага: стоп без рестарта ─────────────────────────────────────────────
def test_file_flag_toggles_without_restart(monkeypatch, tmp_path):
    """Появление файла останавливает мутации НА СЛЕДУЮЩЕМ ЖЕ вызове (кэша нет), удаление —
    возвращает. Это и есть общий host safety flag без рестарта процесса."""
    flag = tmp_path / "KILL_SWITCH"
    monkeypatch.setattr(settings, "kill_switch_file", str(flag))
    assert mutations_disabled() is None  # файла нет — работаем

    flag.touch()
    with pytest.raises(PermissionError, match="файл-флаг"):
        ensure_mutations_enabled()

    flag.unlink()
    assert mutations_disabled() is None  # снятие тоже без рестарта


def test_empty_file_setting_disables_file_channel(monkeypatch):
    """Пустой kill_switch_file = файл-канал выключен ОСОЗНАННО (env-канал остаётся). Это не
    fail-open: «канала нет» задаёт владелец конфигом, а не сбой среды."""
    monkeypatch.setattr(settings, "kill_switch_file", "")
    assert mutations_disabled() is None


def test_compose_uses_one_persistent_read_only_safety_flag():
    import yaml

    root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    expected_path = "/run/aimash-safety/KILL_SWITCH"
    expected_mount = "./runtime/safety:/run/aimash-safety:ro"
    for service in ("scheduler", "mcp"):
        cfg = compose["services"][service]
        assert cfg["environment"]["KILL_SWITCH_FILE"] == expected_path
        assert expected_mount in cfg["volumes"]
    assert (root / "runtime" / "safety" / ".gitkeep").is_file()


# ── Врезка в замок аккаунта: проверка №0 ────────────────────────────────────────────
def test_ensure_allowed_blocked_before_any_allow_logic(monkeypatch):
    """Рубильник стоит РАНЬШЕ разбора allow-list: даже валидно разрешённый Draft отклоняется,
    и причина в ошибке — рубильник, а не список (иначе оператор чинил бы не то)."""
    monkeypatch.setattr(settings, "google_ads_allowed_customer_ids", DRAFT)
    ensure_allowed(DRAFT)  # контроль: без рубильника Draft разрешён

    monkeypatch.setenv(ENV_VAR, "1")
    with pytest.raises(PermissionError, match="рубильник"):
        ensure_allowed(DRAFT)


# ── Врезка в confirm-гейт: ДО claim, подтверждение не сжигается ─────────────────────
async def _confirmed_draft(store: ConfirmStore) -> str:
    cid = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid,
        operation=OP,
        customer_id=DRAFT,
        params=attested(
            {"campaign": "X", "mode": "increase_by_percent", "value": 10.0},
            {"kind": "budget", "before_micros": 10_000_000},
        ),
        summary="s",
        chat_id=OWNER,
        user_initiated=True,
    )
    assert await store.confirm(cid, chat_id=OWNER) is True
    return cid


async def test_gate_refuses_before_claim_and_keeps_confirmation_alive(monkeypatch):
    """Отказ рубильника НЕ столбит черновик: статус остаётся confirmed, и после снятия
    рубильника ТО ЖЕ подтверждение исполняется — человек не остаётся и без мутации, и без
    карточки (та же гарантия, что у freshness-гейта)."""
    await init_db()
    store = ConfirmStore()
    cid = await _confirmed_draft(store)

    monkeypatch.setenv(ENV_VAR, "1")
    with pytest.raises(PermissionError, match="рубильник"):
        await _require_confirmation(store, cid, OP)
    snap = await store.get_confirmed(cid)
    assert snap is not None and snap.status == "confirmed"  # не сожжено

    monkeypatch.delenv(ENV_VAR)
    claimed = await _require_confirmation(store, cid, OP)
    assert claimed.status == "executing"  # рубильник снят — то же «да» исполнимо
