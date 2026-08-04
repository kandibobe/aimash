# Hermes capability pilot and rollback

This rollout keeps every new autonomous surface fail-closed by default.

## What changes automatically

- `tools.tool_search` is explicit and remains `enabled: off` after the supervised OFF scenarios
  missed required tool/order checks and OFF-02 artifact delivery. It may change only how
  already-allowed MCP schemas are disclosed; it cannot discover a tool outside the session allowlist.
- Kanban remains installed for CLI inspection, but `kanban.dispatch_in_gateway: false` and
  `kanban.auto_decompose: false` prevent the production gateway from spawning workers.
- `composite_change` exposes the existing rollbackable proposal builder. It creates one pending
  proposal and cannot execute it; execution remains `execute_confirmed()` with trusted reply CAS,
  audit and post-verify.
- The host watcher reads only P0 cron ids and health states from `jobs.json`. It never stores prompts,
  delivery destinations or client data.

`/goal` is present in pinned Hermes v0.19.0, but this wave does not activate a standing production
goal or add a `goal_judge` policy. It stays behind a separate Telegram acceptance test.

## Before apply

1. Run the focused suite listed below.
2. Confirm `hermes version` is exactly the repository pin from `PIN.json`.
3. Run `sync_aimash_surface.py` only through the SHA-pinned deploy job. The sync writes
   `config.yaml.aimash-prev`, `SOUL.md.aimash-prev`, plugin backups and per-skill `.aimash-prev` files
   before atomic replacement.
4. Do not enable Kanban dispatch until a separate READ-only profile exists and its trace passes both
   exact Telegram UAT scenarios.

## Tool Search A/B gate

Run the same 22 golden Telegram scenarios twice: first with `tools.tool_search.enabled: off`, then
with a temporary host-local `enabled: auto`. Keep `auto` only if it has no additional safety violations, no lost required
tool/order/readback/artifact checks, and does not reduce the passed-scenario count. Compare median tool
calls, latency and model cost separately; a cheaper run is not accepted when delivery or readback is
missing. Store both raw trace sets so the decision can be reproduced. Restore `off` after the test;
only a later reviewed commit may make `auto` the deploy-time default.

## Fast feature rollback

No repository rollback is needed to disable progressive disclosure:

```yaml
tools:
  tool_search:
    enabled: "off"
```

Kanban rollback is its safe repository state:

```yaml
kanban:
  dispatch_in_gateway: false
  auto_decompose: false
```

After a config-only rollback, use the normal drain-aware gateway restart and verify MCP discovery,
Telegram single-poller state and the two UAT traces.

## Full release rollback

Use the normal SHA-pinned release procedure to deploy the previous verified commit. For an immediate
host-local recovery before a Git deploy finishes, restore the `.aimash-prev` files created by the
surface sync, then perform the same drain-aware restart. Never run two Telegram pollers and never
replace `/root/.hermes` wholesale: it contains sessions, cron state and the delivery ledger.

The database migration set is unchanged by this wave. `composite_change` uses the existing proposal,
CAS, audit and compensation tables, so code rollback does not require a database downgrade.

## Verification

```text
python -m pytest tests/test_mcp_plan_surface.py tests/test_composite.py
python -m pytest tests/test_agent_benchmark.py tests/test_ops_alert.py
python -m pytest tests/test_hermes_config_lint.py tests/test_hermes_surface_sync.py
python deploy/hermes/lint_config.py deploy/hermes/config.yaml
```

Passing these checks proves the local contract only. Production acceptance additionally requires the
deployed SHA, gateway health, MCP discovery, no Telegram `409 Conflict`, actual XLSX delivery and real
audit/readback evidence.
