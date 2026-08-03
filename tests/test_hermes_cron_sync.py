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
            assert "[Кнопка:" in prompt
    by_schedule: dict[str, list[str]] = {}
    for job in managed:
        by_schedule.setdefault(job["schedule"], []).append(job["job_id"])
    assert all(len(ids) == 1 for ids in by_schedule.values())
    assert next(j for j in managed if j["job_id"] == "031080f7bfac")["schedule"] == "30 18 * * *"


def test_cron_policy_is_advisory_only_and_evening_jobs_are_ordered():
    registry = yaml.safe_load(_registry().read_text(encoding="utf-8"))
    rules = registry["default_rules"]
    assert rules["execution_policy"] == "read_only_notify_then_wait"
    assert rules["mutations_from_cron"] == "forbidden"
    assert rules["proposals_from_cron"] == "forbidden"
    assert rules["memory_writes_from_cron"] == "forbidden"
    assert rules["artifacts_from_cron"] == "human_command_required"

    jobs = {job["job_id"]: job for job in registry["jobs"]}
    assert jobs["5db43f3b3d5d"]["schedule"] == "30 17 * * *"
    assert jobs["b44861829e51"]["schedule"] == "50 17 * * *"
    assert jobs["a0cff93f3a2b"]["schedule"] == "10 18 * * *"
    assert jobs["a0cff93f3a2b"]["depends_on"] == ["5db43f3b3d5d", "b44861829e51"]
    assert jobs["031080f7bfac"]["schedule"] == "30 18 * * *"
    assert "a0cff93f3a2b" in jobs["031080f7bfac"]["depends_on"]


def test_cron_prompts_cover_requested_review_cycles_without_automatic_actions():
    prompt_dir = _registry().parent / "cron_prompts"
    prompts = {
        name: (prompt_dir / f"{name}.txt").read_text(encoding="utf-8").casefold()
        for name in (
            "hourly_watchdog",
            "daily_health",
            "daily_budget",
            "drift_detection",
            "context_summary",
            "shadow_daily",
            "weekly_review",
            "monthly_review",
        )
    }

    for prompt in prompts.values():
        assert "[кнопка:" in prompt
    assert all(
        marker in prompts["hourly_watchdog"]
        for marker in ("cpc", "конверс", "нулевых кликах", "disapproved", "рекомендации google")
    )
    assert all(
        marker in prompts["daily_health"]
        for marker in (
            "search terms",
            "явный мусор",
            "сформировать sheets",
            "create_search_term_review",
        )
    )
    assert all(
        marker in prompts["weekly_review"]
        for marker in (
            "предыдущих 7 дней",
            "quality",
            "auction insights",
            "competitor intelligence",
            "tavily",
            "wow sheets",
        )
    )
    assert all(
        marker in prompts["monthly_review"]
        for marker in (
            "pinning",
            "landing page",
            "attribution",
            "сформировать pdf",
            "build_monthly_pdf",
        )
    )
    assert "ничего не записывай и не удаляй" in prompts["context_summary"]
    assert "не создавай proposal" in prompts["shadow_daily"]
    assert all(
        marker in prompts["daily_budget"]
        for marker in ("месячный медиаплан", "расход mtd", "месячный план не задан")
    )


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
