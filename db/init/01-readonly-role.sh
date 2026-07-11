#!/bin/sh
# Read-only роль для Postgres MCP (Claude Code видит схему/данные, не может писать).
# Выполняется автоматически при ПЕРВОМ старте контейнера (docker-entrypoint-initdb.d).
# Defense-in-depth поверх `postgres-mcp --access-mode=restricted`.
#
# D3: был .sql с паролем ЛИТЕРАЛОМ в git ('aimash_ro'). Init-SQL не умеет подставлять env,
# поэтому шаг переехал в .sh: пароль приходит из AIMASH_RO_PASSWORD (compose → .env, untracked).
# Пусто ⇒ init ПАДАЕТ (fail-closed, правило #10), а не создаёт роль со всем известным паролем.
# Подставляем через psql-переменную (:'ro_pw'), а не в текст SQL: psql сам корректно квотит.
set -eu

: "${AIMASH_RO_PASSWORD:?AIMASH_RO_PASSWORD обязателен (POSTGRES_RO_PASSWORD в .env)}"

psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v ro_pw="$AIMASH_RO_PASSWORD" <<'EOSQL'
CREATE ROLE aimash_ro WITH LOGIN PASSWORD :'ro_pw';

GRANT CONNECT ON DATABASE aimash TO aimash_ro;
GRANT USAGE ON SCHEMA public TO aimash_ro;

-- Только SELECT на существующие и будущие таблицы/вьюхи.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO aimash_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO aimash_ro;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO aimash_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON SEQUENCES TO aimash_ro;
EOSQL
