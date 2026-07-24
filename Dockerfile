# Многостадийная сборка: компактный образ, non-root, БЕЗ секретов внутри (секреты — только
# в рантайме через env/секрет-менеджер). Бот — long-poll (HTTP-порта нет).
FROM python:3.12-slim AS builder
WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY . .
# A11: устанавливаем с ПИНАМИ (constraints.txt) — воспроизводимый образ. Без -c каждый деплой
# ресолвил бы новейшие версии заново (прод менялся без изменения кода). Бамп версий — отдельным
# PR (перегенерация constraints.txt через uv pip compile).
RUN pip install -e . -c constraints.txt    # только рантайм-зависимости (без [dev]), запиннены

FROM python:3.12-slim AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
# Непривилегированный пользователь (никаких root-процессов в контейнере).
RUN useradd --create-home --uid 10001 aimash
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app
# Entrypoint применяет миграции (alembic upgrade head) перед стартом бота. chmod до USER (root).
RUN chmod +x /app/docker-entrypoint.sh
USER aimash
# B9: healthcheck по СВЕЖЕСТИ heartbeat (живость event-loop бота), а не только импорт модулей —
# раньше зависший polling / крэш-луп оставался «healthy». Бот пишет heartbeat каждые 10с; протух
# (>60с) ⇒ unhealthy ⇒ оркестратор перезапускает. start-period покрывает первый запуск.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "scripts/healthcheck.py"]
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "-m", "bot.main"]
