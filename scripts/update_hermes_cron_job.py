"""Update per-job inference fields through Hermes's locked cron API.

Hermes v0.19.0 stores provider/model on each job, but ``hermes cron edit`` does not expose
those two fields.  Calling ``cron.jobs.update_job`` preserves its validation, file lock,
snapshot refresh, and atomic save instead of patching ``jobs.json`` directly.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from typing import Any


def update_inference(
    job_id: str,
    provider: str,
    model: str,
    *,
    updater: Callable[[str, dict[str, Any]], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    if updater is None:
        from cron.jobs import update_job

        updater = update_job
    updated = updater(
        job_id,
        {"provider": provider.strip(), "model": model.strip()},
    )
    if updated is None:
        raise LookupError(f"Hermes cron job not found: {job_id}")
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()

    try:
        updated = update_inference(
            args.job_id,
            args.provider,
            args.model,
        )
    except LookupError as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            {
                "job_id": updated["id"],
                "provider": updated.get("provider"),
                "model": updated.get("model"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
