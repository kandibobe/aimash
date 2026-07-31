# Safe Restart / Drain Protocol for Hermes Gateway

## Purpose

Reduce the chance that `hermes gateway restart` or service restarts interrupt active agent turns.

## Problem statement

Current operational path is blunt:
- `hermes gateway restart`
- systemd/user service restarts
- `hermes update` auto-restarts gateway

This is acceptable for dead sessions, but risky during active work because the main issue is not crash-only behavior — it is SIGTERM during live turns.

## Scope

This document defines an operational protocol first. It does **not** claim that Hermes currently implements native drain mode.

## Safety levels

### Level 0 — blunt restart
Use only when:
- gateway is unhealthy
- model/provider config is broken and must be reloaded immediately
- no active operator work is in progress

### Level 1 — safe restart (runbook/wrapper)
Preferred default:
1. inspect whether active sessions/runs are in progress
2. if none — restart immediately
3. if active sessions exist — postpone or enter drain wait window
4. restart only after window expires or sessions finish

### Level 2 — native drain mode
Future desired behavior:
- gateway stops accepting new heavy turns
- existing turns are allowed to finish up to timeout N
- restart reason is logged explicitly
- post-restart health check runs automatically

## Minimum protocol now

### Step 1 — pre-restart checks
Before restart, verify:
- why restart is needed
- whether it can wait
- whether a P0 monitoring lane would be affected
- whether active user/operator sessions are currently running

### Step 2 — classify restart reason
Use one of:
- `manual_config_change`
- `deploy_reconnect`
- `scheduled_maintenance`
- `provider_reauth`
- `gateway_unhealthy`
- `host_event`
- `hermes_update`

### Step 3 — active-work gate
If active sessions are detected:
- prefer postpone
- else announce drain window in operator topic
- wait N seconds/minutes
- then restart

Suggested initial drain window:
- short config reload: 60–120s
- deploy reconnect: 120–300s
- gateway unhealthy: skip wait if service is already broken

### Step 4 — restart
Preferred command path remains:
```bash
hermes gateway restart && hermes gateway status
```
Fallback:
```bash
XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart hermes-gateway-<profile>.service
```

### Step 5 — post-restart verification
Always verify:
- gateway status = active
- MCP connectivity restored if relevant
- P0 cron coverage preserved
- operator-facing route still works

Suggested checks:
```bash
hermes gateway status
hermes mcp test aimash
hermes cron list
```

## Detection sources to implement next

The wrapper/script should eventually check one or more of:
- Hermes `state.db` running sessions
- recent gateway activity / lock files
- background process state if exposed by Hermes runtime
- optional operator override flag (`--force`)

## Wrapper requirements for future script

Planned script: `scripts/safe_gateway_restart.py`

It should:
1. accept `--reason <enum>`
2. inspect active work sources
3. print machine-readable decision:
   - `restart_now`
   - `wait_then_restart`
   - `blocked_requires_force`
4. support `--force`
5. log reason + decision + observed active count
6. run post-restart checks

## Non-goals

This protocol does not yet:
- modify Hermes upstream scheduler/gateway code
- guarantee session preservation during restart
- replace emergency restarts

## Immediate next engineering step

Implement `scripts/safe_gateway_restart.py` in wrapper mode first, then decide whether native drain support belongs upstream in Hermes.
