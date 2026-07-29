"""12 READ-инструментов над дашбордом Hermes.

Слой тонкий по правилу 6: валидация входа → один-два GET через `HermesReadClient` → конверт. Логики
здесь нет и быть не должно; появилась потребность — её место в `deploy/hermes/` (операционные
процедуры) или в самом Hermes.

Каждый инструмент ловит ЛЮБОЕ исключение и возвращает редактированный error-конверт: FastMCP иначе
кладёт сырой `str(e)` в `ToolError`, а исключение httpx несёт URL и заголовки (правило 5).

Параметр `file` у `/api/logs` намеренно НЕ выставлен в сигнатуру `hermes_logs`. Он принимает имя
файла, то есть был бы вторым путём к чтению произвольного файла на VPS — мимо того самого allow-list,
который закрывает `/api/fs/read-text`. Чего нет в схеме инструмента, того модель не подставит.
"""

from __future__ import annotations

import json
from typing import Any, Final

from hermes_ops.client import HermesReadClient
from mcp_server.redact import redact_error

# Потолок объёма ответа. `/api/logs` и `.../messages` на живой сессии отдают сотни килобайт — один
# такой вызов съедает контекст целиком. Усечение сигналим явно, а не молча режем.
MAX_RESPONSE_CHARS: Final[int] = 20_000


def _cap(data: Any) -> tuple[Any, bool, str | None]:
    """(данные, усечено, пояснение). При превышении отдаём префикс JSON-текста, а не обрезок структуры."""
    text = json.dumps(data, ensure_ascii=False, default=str)
    if len(text) <= MAX_RESPONSE_CHARS:
        return data, False, None
    return (
        text[:MAX_RESPONSE_CHARS],
        True,
        f"ответ усечён: {len(text)} символов при потолке {MAX_RESPONSE_CHARS}. "
        "Сузь запрос (lines/limit/offset) — данные не потеряны, они просто не влезли в один ответ.",
    )


def _ok(endpoint: str, data: Any, *, note: str | None = None) -> dict[str, Any]:
    payload, truncated, cap_note = _cap(data)
    return {
        "ok": True,
        "endpoint": endpoint,
        "data": payload,
        "truncated": truncated,
        "note": cap_note or note,
        "error": None,
    }


def _err(endpoint: str, exc: BaseException) -> dict[str, Any]:
    """Отказ. `data=None` — fail-closed: частичных данных инструмент не отдаёт."""
    return {
        "ok": False,
        "endpoint": endpoint,
        "data": None,
        "truncated": False,
        "note": None,
        "error": redact_error(exc),
    }


async def _fetch(
    endpoint: str,
    *,
    path_params: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Один GET в конверте. Клиент строится на вызов — база/таймаут перечитываются из окружения."""
    try:
        data = await HermesReadClient().get(endpoint, path_params=path_params, query=query)
    except Exception as exc:  # noqa: BLE001 — наружу идёт редактированный конверт, не исключение
        return _err(endpoint, exc)
    return _ok(endpoint, data, note=note)


# ── Инструменты ─────────────────────────────────────────────────────────────────────────


async def hermes_status(profile: str | None = None) -> dict:
    """Состояние Hermes: версия, дата релиза, жив ли gateway, подключённые платформы, число сессий.

    Первое, что смотреть при «бот молчит»: `gateway_running` и `gateway_platforms.telegram.state`.
    """
    return await _fetch("/api/status", query={"profile": profile})


async def hermes_system_stats() -> dict:
    """Ресурсы машины Hermes: память, диск, нагрузка.

    VPS — 3.7 Gi с историей oom-kill (OPERATIONS.md §14.1). При 502 дашборда или пропавшем gateway
    смотреть сюда ДО того, как разбирать туннель Tailscale/Caddy: обычно падает не цепочка, а бэкенд.
    """
    return await _fetch("/api/system/stats")


async def hermes_toolsets(profile: str | None = None) -> dict:
    """Живые тулсеты агента — ГЛОБАЛЬНАЯ раскладка, не платформенная.

    Проверка по факту рантайма, а не по репо-конфигу: Hermes молча игнорирует неизвестные ключи
    (К10), так что «выключено в config.yaml» и «выключено в рантайме» — два разных утверждения.

    ⚠️ У ручки есть только параметр `profile`; `platform` она не принимает, а тулсеты в Hermes
    задаются ПЕР-ПЛАТФОРМЕННО. Замер 29.07.2026: здесь `video`/`context_engine`/`yuanbao`
    показаны включёнными, а на telegram они выключены — и наоборот, telegram может держать
    включённым то, чего тут нет. Поэтому для К-инвариантов (`terminal`/`web`/`session_search`
    у телеграм-агента) этот вызов — ориентир, а решающий ответ даёт
    `hermes tools list --platform telegram` на VPS.
    """
    return await _fetch("/api/tools/toolsets", query={"profile": profile})


async def hermes_mcp_servers(profile: str | None = None) -> dict:
    """Зарегистрированные MCP-серверы. Наш READ-путь в Google Ads жив, если здесь есть `aimash`."""
    return await _fetch("/api/mcp/servers", query={"profile": profile})


async def hermes_model_info(profile: str | None = None) -> dict:
    """Активные модели и раскладка по ролям.

    Молчание бота 2026-07-27 было ровно здесь: слаг `deepseek/deepseek-v3` не существует → HTTP 400
    на каждом ходу с инструментами. Конфиг-линт ловит ИМЕНА ключей, не ЗНАЧЕНИЯ — слаг проверяется
    только живым чтением.
    """
    return await _fetch("/api/model/info", query={"profile": profile})


async def hermes_config_show(profile: str | None = None) -> dict:
    """Эффективный конфиг Hermes (дрейф К10: опечатка в ключе = настройка молча сброшена).

    Только чтение: `/api/config/raw` (умеет PUT, то есть перезапись конфига целиком) в allow-list
    не входит и войти не может.
    """
    return await _fetch("/api/config", query={"profile": profile})


async def hermes_sessions_list(
    limit: int = 20,
    offset: int = 0,
    order: str | None = None,
    profile: str | None = None,
) -> dict:
    """Список сессий агента — что Hermes делал и когда."""
    return await _fetch(
        "/api/sessions",
        query={"limit": limit, "offset": offset, "order": order, "profile": profile},
    )


async def hermes_session_messages(
    session_id: str,
    limit: int = 50,
    offset: int = 0,
    profile: str | None = None,
) -> dict:
    """Сообщения одной сессии — разбор конкретного хода агента (какие инструменты звал, что вернули)."""
    return await _fetch(
        "/api/sessions/{session_id}/messages",
        path_params={"session_id": session_id},
        query={"limit": limit, "offset": offset, "profile": profile},
    )


async def hermes_sessions_search(q: str, limit: int = 20, profile: str | None = None) -> dict:
    """Полнотекстовый поиск по истории сессий."""
    return await _fetch("/api/sessions/search", query={"q": q, "limit": limit, "profile": profile})


async def hermes_logs(
    lines: int = 200,
    level: str | None = None,
    component: str | None = None,
    search: str | None = None,
) -> dict:
    """Логи Hermes без ssh: хвост `lines` строк, опционально по уровню/компоненту/подстроке.

    Выбор файла недоступен намеренно (см. докстринг модуля) — читается штатный лог, не произвольный
    путь на диске.
    """
    return await _fetch(
        "/api/logs",
        query={"lines": lines, "level": level, "component": component, "search": search},
    )


async def hermes_cron_jobs(
    job_id: str | None = None,
    limit: int = 20,
    profile: str | None = None,
) -> dict:
    """Расписания агента. Без `job_id` — список джоб; с `job_id` — прогоны конкретной.

    Cron агент ставит себе сам; §12 требует проверяемой обратимости — отсюда видно, что реально
    зарегистрировано и как отрабатывало.
    """
    if job_id is None:
        return await _fetch("/api/cron/jobs", query={"profile": profile})
    return await _fetch(
        "/api/cron/jobs/{job_id}/runs",
        path_params={"job_id": job_id},
        query={"limit": limit, "profile": profile},
    )


async def hermes_usage(days: int = 7, profile: str | None = None) -> dict:
    """Расход токенов за `days` дней — суммарно и в разрезе моделей (два GET в одном конверте)."""
    endpoint = "/api/analytics/usage"
    try:
        client = HermesReadClient()
        usage = await client.get(endpoint, query={"days": days, "profile": profile})
        by_model = await client.get(
            "/api/analytics/models", query={"days": days, "profile": profile}
        )
    except Exception as exc:  # noqa: BLE001 — редактированный конверт вместо исключения
        return _err(endpoint, exc)
    return _ok(endpoint, {"usage": usage, "by_model": by_model})


# ── Реестр ──────────────────────────────────────────────────────────────────────────────

HERMES_TOOL_FUNCS: Final[dict[str, Any]] = {
    "hermes_status": hermes_status,
    "hermes_system_stats": hermes_system_stats,
    "hermes_toolsets": hermes_toolsets,
    "hermes_mcp_servers": hermes_mcp_servers,
    "hermes_model_info": hermes_model_info,
    "hermes_config_show": hermes_config_show,
    "hermes_sessions_list": hermes_sessions_list,
    "hermes_session_messages": hermes_session_messages,
    "hermes_sessions_search": hermes_sessions_search,
    "hermes_logs": hermes_logs,
    "hermes_cron_jobs": hermes_cron_jobs,
    "hermes_usage": hermes_usage,
}

HERMES_READ_TOOLS: Final[frozenset[str]] = frozenset(HERMES_TOOL_FUNCS)
