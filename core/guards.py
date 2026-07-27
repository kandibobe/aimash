"""core/guards.py — construction-time гарды + runtime kill-switch на мутации.

`assert` — не гард. Под `python -O` (и `PYTHONOPTIMIZE=1` в окружении) CPython выбрасывает
assert-инструкции из байткода целиком, вместе с сообщением. Для проверки, единственная задача
которой — не пустить мутационный инструмент в read-фазу, это значит: гард исчезает молча, импорт
проходит, денежный путь открыт. Признака нет ни одного — ни в логах, ни в поведении, — пока кто-то
не выставит наружу мутацию под видом чтения.

Сегодня это дыра ВЗВЕДЁННАЯ, а не активная: ни `Dockerfile`, ни `docker-compose.yml`, ни CI не
включают `-O`/`PYTHONOPTIMIZE`. Цена в том, что взводится она одной строкой в чужом коммите («ускорим
образ»), а снимает сразу два инварианта — И4 (MCP READ-слой без мутаций) и S4 (аналитический цикл
без мутаций). Правило 10 (fail-closed) требует, чтобы гард отказывал при любой конфигурации, а не
при удачной.

Держится тестами `tests/test_invariants_core.py`: (а) механизм роняет импорт под `-O` в подпроцессе;
(б) в продовых пакетах нет ни одного module-level `assert` — то есть класс бага закрыт, а не два его
экземпляра.

## Emergency Kill-Switch

`DISABLE_ALL_MUTATIONS=true` (env) — **глобальная заморозка ВСЕХ WRITE-операций**. 
Проверяется на самом верхнем уровне `ensure_allowed()` в `ads/client.py`.
Не влияет на READ-инструменты — бот продолжает читать и отвечать.

Два уровня:
1. **Env-флаг** — `DISABLE_ALL_MUTATIONS=true` в .env или export. Мгновенно блокирует все мутации.
2. **Redis-флаг** (планируется) — ключ `admaster:killswitch` в Redis для remote-управления без рестарта.
   Пока fallback: файл `~/.hermes/killswitch.flag`.

Дополнительный уровень: cron-задания с `approvals.cron_mode: deny` в Hermes config — 
WRITE-операции из кронов заблокированы на уровне фреймворка.
"""

from __future__ import annotations

from collections.abc import Collection
from core.logging import log


def require_no_mutations(
    names: Collection[str],
    mutation_names: Collection[str],
    *,
    rule: str,
    subject: str,
) -> None:
    """Пересечение с мутационными именами ⇒ `RuntimeError` на импорте модуля-вызывающего.

    Зовётся на уровне модуля (construction-time): падение видно немедленно при сборке/старте, а не
    на первом обращении к инструменту, когда рядом уже стоит живой аккаунт.

    Args:
        names: имена инструментов слоя, который обязан быть read-only.
        mutation_names: `agent.tools.schemas.MUTATION_TOOLS`.
        rule: код инварианта для сообщения («И4», «S4») — чтобы падение искалось по спеке.
        subject: чей это набор, человеко-читаемо.

    Raises:
        RuntimeError: если пересечение непусто.
    """
    overlap = frozenset(names) & frozenset(mutation_names)
    if not overlap:
        return
    raise RuntimeError(
        f"{rule} нарушен: {subject} пересекается с мутационными инструментами "
        f"(agent.tools.schemas.MUTATION_TOOLS): {sorted(overlap)}. "
        "Read-слой не смеет содержать мутации — это денежный путь."
    )


def require_registered_surface(
    registered: Collection[str],
    approved: Collection[str],
    *,
    subject: str,
) -> None:
    """Живая MCP-поверхность обязана совпадать с одобренным набором инструментов — РОВНО, не «⊆».

    Зовётся при сборке сервера (`build_server`), после регистрации и ДО отдачи сервера наружу: любое
    расхождение роняет старт (fail-fast, fail-closed, правило 10), а не всплывает на первом вызове.
    `registered` берётся из ФАКТИЧЕСКОГО реестра FastMCP, а не из исходного словаря, который итерирует
    цикл, — иначе проверка тавтологична: она обязана поймать `mcp.tool()`-регистрацию мимо READ-набора
    (напр. кто-то добавил цикл по `PROPOSE_TOOL_FUNCS` или выставил `execute_confirmed`).

    Ловит ОБА направления дрейфа §15.2:
      • лишнее (`extra`) — confirm/execute/propose просочились на живую поверхность до подтверждения
        канала доставки/якоря. Это и есть охраняемая граница: WRITE-слой в прод не выходит;
      • нехватка (`missing`) — READ-инструмент выключен, а эталон не обновлён. Не про безопасность, но
        про честность эталона: разошёлся `approved` с реальностью — гард перестаёт что-либо
        гарантировать. Поэтому равенство, а не вхождение.

    Args:
        registered: имена, которые сервер ДЕЙСТВИТЕЛЬНО зарегистрировал (из реестра FastMCP).
        approved: одобренный к выставлению набор (сегодня — только READ; WRITE расширит его осознанно).
        subject: чья это поверхность, человеко-читаемо.

    Raises:
        RuntimeError: registered != approved.
    """
    reg = frozenset(registered)
    app = frozenset(approved)
    if reg == app:
        return
    extra = sorted(reg - app)
    missing = sorted(app - reg)
    raise RuntimeError(
        f"MCP-поверхность разошлась с одобренным набором ({subject}): "
        f"лишние={extra or '—'}, недостающие={missing or '—'}. "
        "На живой MCP-поверхности — ТОЛЬКО одобренные инструменты (§15.2): confirm/execute/propose "
        "не выходят в прод, пока не подтверждён канал доставки/якоря."
    )


# ── Runtime Kill-Switch ────────────────────────────────────────────────────
import os
import pathlib

_KILLSWITCH_FILE = pathlib.Path(os.getenv("HERMES_HOME", "~/.hermes")) / "killswitch.flag"


def mutations_allowed() -> bool:
    """Глобальная проверка: разрешены ли WRITE-операции сейчас?

    Два уровня проверки (fail-closed — отказ при любой ошибке чтения):
    1. Env-флаг `DISABLE_ALL_MUTATIONS=true` — самый быстрый, не требует FS.
    2. Файл `~/.hermes/killswitch.flag` — для remote-управления без рестарта контейнера.
       Файл существует → мутации ЗАБЛОКИРОВАНЫ (touch-файл = kill).
       Файл отсутствует → мутации разрешены.

    В будущем: Redis-ключ `admaster:killswitch` (проверка с низким TTL).

    Returns:
        True если мутации разрешены, False если глобально заблокированы.
    """
    # Уровень 1: env-флаг (fast path)
    if os.environ.get("DISABLE_ALL_MUTATIONS", "").strip().lower() in ("true", "1", "yes"):
        log.warning("KILL-SWITCH: DISABLE_ALL_MUTATIONS=true в env — мутации заблокированы")
        return False

    # Уровень 2: файловый флаг
    try:
        kf = _KILLSWITCH_FILE.expanduser()
        if kf.exists():
            log.warning("KILL-SWITCH: файл %s существует — мутации заблокированы", kf)
            return False
    except OSError:
        # fail-closed: ошибка чтения = мутации запрещены
        log.error("KILL-SWITCH: ошибка проверки файла %s — мутации ЗАПРЕЩЕНЫ", _KILLSWITCH_FILE)
        return False

    return True


def require_mutations_allowed() -> None:
    """Runtime-проверка перед входом в `ensure_allowed()`. Бросает PermissionError если блокировано.

    Это самый верхний уровень — вызывается ДО всех остальных проверок в ensure_allowed().
    """
    if not mutations_allowed():
        raise PermissionError(
            "Глобальный kill-switch активен: WRITE-операции заблокированы. "
            "Проверь DISABLE_ALL_MUTATIONS в .env или удали killswitch.flag. "
            "READ-инструменты продолжают работать."
        )
