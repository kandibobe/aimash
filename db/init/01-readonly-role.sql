-- Read-only роль для Postgres MCP (Claude Code видит схему/данные, не может писать).
-- Выполняется автоматически при первом старте контейнера (docker-entrypoint-initdb.d).
-- Defense-in-depth поверх `postgres-mcp --access-mode=restricted`.

CREATE ROLE aimash_ro WITH LOGIN PASSWORD 'aimash_ro';

GRANT CONNECT ON DATABASE aimash TO aimash_ro;
GRANT USAGE ON SCHEMA public TO aimash_ro;

-- Только SELECT на существующие и будущие таблицы/вьюхи.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO aimash_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO aimash_ro;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO aimash_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON SEQUENCES TO aimash_ro;
