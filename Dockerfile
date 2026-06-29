# Многостадийная сборка: компактный образ, non-root, БЕЗ секретов внутри (секреты — только
# в рантайме через env/секрет-менеджер). Бот — long-poll (HTTP-порта нет).
FROM python:3.12-slim AS builder
WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY . .
RUN pip install -e .            # только рантайм-зависимости (без [dev])

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
# Healthcheck — только импорт конфигурации/сессии БД (дёшево, без секретов в выводе).
# В prod импорт core.config также проверит SECRETS_ENCRYPTION_KEY (fail-fast).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import core.config, db.session"]
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "-m", "bot.main"]
