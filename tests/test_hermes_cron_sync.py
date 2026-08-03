from __future__ import annotations

import json
from pathlib import Path

import yaml

from scripts.sync_hermes_cron import build_plan


def _registry() -> Path:
    return Path(__file__).resolve().parents[1] / "deploy/hermes/cron_registry.yaml"


def test_managed_cron_prompts_exist_and_schedules_do_not_collide():
    registry = yaml.safe_load(_registry().read_text(encoding="utf-8"))
    managed = [job for job in registry["jobs"] if job.get("schedule")]
    assert managed
    for job in managed:
        if job.get("prompt_file"):
            prompt_path = _registry().parent / job["prompt_file"]
            assert prompt_path.is_file()
            prompt = prompt_path.read_text(encoding="utf-8")
            assert (
                "не создавай proposal" in prompt.casefold()
                or "никаких автоматических изменений" in prompt.casefold()
                or "не записывай автоматически" in prompt.casefold()
            )
            if job["job_id"] != "a0cff93f3a2b":
                assert "[Кнопка:" in prompt
    by_schedule: dict[str, list[str]] = {}
    for job in managed:
        by_schedule.setdefault(job["schedule"], []).append(job["job_id"])
    assert all(len(ids) == 1 for ids in by_schedule.values())
    assert next(j for j in managed if j["job_id"] == "031080f7bfac")["schedule"] == "30 18 * * *"


def test_cron_sync_is_scoped_and_detects_schedule_prompt_and_resume(tmp_path):
    registry = tmp_path / "cron_registry.yaml"
    prompts = tmp_path / "cron_prompts"
    prompts.mkdir()
    (prompts / "one.md").write_text("new prompt\n", encoding="utf-8")
    registry.write_text(
        yaml.safe_dump(
            {
                "jobs": [
                    {
                        "job_id": "managed",
                        "name": "New name",
                        "schedule": "30 17 * * *",
                        "prompt_file": "cron_prompts/one.md",
                        "auto_resume": True,
                    },
                    {"job_id": "documented-only", "name": "No mutation"},
                ]
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    home = tmp_path / "home" / "cron"
    home.mkdir(parents=True)
    jobs = home / "jobs.json"
    jobs.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "managed",
                        "name": "Old name",
                        "prompt": "old prompt",
                        "enabled": False,
                        "schedule": {"expr": "0 18 * * *"},
                    },
                    {"id": "unrelated", "name": "Keep me", "enabled": True},
                ]
            }
        ),
        encoding="utf-8",
    )
    assert build_plan(registry, jobs) == [
        {
            "job_id": "managed",
            "name": "New name",
            "changes": {
                "schedule": "30 17 * * *",
                "name": "New name",
                "prompt": "new prompt",
            },
            "resume": True,
        }
    ]
