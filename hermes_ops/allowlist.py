"""Allow-list путей дашборда Hermes — единственный источник того, куда слой вправе постучать.

Allow-list, а не deny-list: список запрещённого защищает ровно до следующего релиза Hermes, который
добавит эндпоинт, никем не запрещённый (241 путь на пине 0.19.0 — уже сегодня). Набор ниже закрыт
по построению: путь не отсюда → `RuntimeError` в транспорте (`client.py`), инструмент до сети не
доходит.

Сверяется с пином `deploy/hermes/openapi-0.19.0.paths.json` тестом
`tests/test_hermes_ops_surface.py` — каждый путь обязан существовать в схеме пиновой версии И иметь
метод GET. Это дисциплина К10, перенесённая с конфига на API: обновили Hermes и эндпоинт исчез —
тест краснеет, а не инструмент молча бьёт в 404.
"""

from __future__ import annotations

from typing import Final

# ── Разрешённые GET-пути (шаблоны с {placeholder} подставляет `client.get`) ──────────────
#
# 14 путей на 12 инструментов. Расширять — только вместе с гардом: новый путь без записи в пин и без
# прохода по FORBIDDEN_PREFIXES не пройдёт `tests/test_hermes_ops_surface.py`.
READ_ENDPOINTS: Final[frozenset[str]] = frozenset(
    {
        "/api/status",  # версия, живость gateway, платформы, auth_required
        "/api/system/stats",  # RAM/диск VPS — прямая проверка боли 3.7 Gi (§14.1)
        "/api/tools/toolsets",  # К-инварианты: terminal/web/session_search обязаны отсутствовать
        "/api/mcp/servers",  # жив ли наш `aimash` MCP (READ-путь в Google Ads)
        "/api/model/info",  # слаги моделей — авария 2026-07-27 была ровно здесь
        "/api/config",  # эффективный конфиг (дрейф К10). ⛔ НЕ `/api/config/raw` — тот умеет PUT
        "/api/sessions",  # список сессий агента
        "/api/sessions/{session_id}/messages",  # разбор конкретного хода
        "/api/sessions/search",  # поиск по истории
        "/api/logs",  # логи без ssh
        "/api/cron/jobs",  # расписания, что агент поставил себе сам
        "/api/cron/jobs/{job_id}/runs",  # прогоны джобы (обратимость cron, §12)
        "/api/analytics/usage",  # расход токенов
        "/api/analytics/models",  # расход в разрезе моделей
    }
)

# ── Поверхность, которую нельзя выставлять агенту ни при каких условиях ──────────────────
#
# Это НЕ механизм защиты (защита — сам allow-list выше), а якорь для теста: он падает, если кто-то
# добавил в `READ_ENDPOINTS` путь под одним из этих префиксов. Формулировка «почему» — рядом, чтобы
# правка не выглядела безобидной.
FORBIDDEN_PREFIXES: Final[frozenset[str]] = frozenset(
    {
        "/api/env",  # GET/PUT/DELETE + POST /api/env/reveal — выдача секретов открытым текстом
        "/api/credentials",  # пул API-ключей провайдеров
        "/api/fs",  # POST /api/fs/write-text — запись любого файла на VPS под root
        "/api/files",  # upload/download/mkdir/delete — файловый менеджер по всей машине
        "/api/skills",  # hub/install|uninstall — установка community-скилов (К14, §21)
        "/api/git",  # push / create-pr / revert / worktree — запись в репозиторий
        "/api/plugins",  # kanban/dispatch — запуск агентских воркеров
        "/api/providers",  # OAuth-сессии и кастомные эндпоинты провайдеров
        "/api/auth",  # выпуск сессий дашборда
        "/api/pairing",  # привязка мессенджер-аккаунтов
        "/api/webhooks",  # исходящие каналы
        "/api/chat",  # POST — говорить от лица агента мимо гейта
        "/api/actions",  # запуск действий
        "/api/ops",  # /api/ops/backup/download — бэкап целиком, включая секреты
        "/api/config/raw",  # PUT — перезапись конфига целиком (вернуть тулсет terminal, К14)
        "/api/gateway",  # рестарт/дренаж живого gateway
        "/api/memory",  # артефактная память клиентов (И6 — изоляция клиентов)
        "/api/messaging",  # отправка сообщений в Telegram/WhatsApp от имени бота
        "/api/profiles",  # смена активного профиля агента
        "/api/curator",  # правка поведения куратора
        "/api/hermes",  # /api/hermes/update — обновление платформы мимо пина
    }
)


def _forbidden_hits(endpoints: frozenset[str] = READ_ENDPOINTS) -> list[str]:
    """Пути allow-list, попадающие под запретный префикс. Вынесено ради прямой проверки в тесте."""
    return sorted(p for p in endpoints if p.startswith(tuple(FORBIDDEN_PREFIXES)))


# ── Construction-time гард ──────────────────────────────────────────────────────────────
#
# Не `assert`: под `-O` CPython вырезает assert из байткода, и гард исчезает МОЛЧА — ровно та
# аргументация, что в `core/guards.py`. Падаем на импорте: расхождение видно при сборке сервера, а
# не на первом вызове инструмента, уже подключённого к живому VPS без аутентификации.
_hits = _forbidden_hits()
if _hits:
    raise RuntimeError(
        f"allow-list дашборда Hermes пересекается с запретной поверхностью: {_hits}. "
        "Эти префиксы дают запись файлов под root, выдачу секретов и установку скилов — выставлять "
        "их агенту нельзя (правило 8: опасное не регистрируется вовсе; И7: контекст агента "
        "недоверенный). Если путь действительно безопасен — сузь FORBIDDEN_PREFIXES осознанно, "
        "отдельным коммитом, с обоснованием."
    )
