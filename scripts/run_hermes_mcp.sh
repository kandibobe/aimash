#!/bin/sh
# Serialize the named MCP artifact container. If a gateway process is still using it, flock fails
# and this invocation leaves it untouched. If the previous stdio owner died and left an orphan,
# the released lock makes that exact stateless container safe to replace before reconnecting.
set -eu

LOCK_FILE=/run/lock/aimash-hermes-mcp.lock
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "Aimash MCP is already owned by an active Hermes session" >&2
    exit 75
fi

if docker inspect aimash-mcp >/dev/null 2>&1; then
    docker rm -f aimash-mcp >/dev/null
fi

exec docker compose \
    --project-directory /opt/aimash \
    --profile mcp \
    run --rm --no-deps -T --name aimash-mcp mcp
