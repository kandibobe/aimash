#!/bin/sh
# Общий entrypoint Aimash Core. Миграции выполняет одноразовый compose-сервис `migrate`,
# поэтому MCP и scheduler стартуют без скрытых side effects.
set -e

exec "$@"
