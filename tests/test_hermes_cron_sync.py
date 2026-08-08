from __future__ import annotations

import json
from pathlib import Path

import yaml

from scripts.sync_hermes_cron import apply_plan, build_plan
from scripts.update_hermes_cron_job import update_inference


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


def test_cron_cost_lanes_preserve_quality_for_complex_and_risk_jobs():
    registry = yaml.safe_load(_registry().read_text(encoding="utf-8"))
    jobs = {job["job_id"]: job for job in registry["jobs"]}

    assert (jobs["478eac21bfe6"]["model"], jobs["478eac21bfe6"]["reasoning_effort"]) == (
        "gpt-5.4-mini",
        "low",
    )
    assert (jobs["a0cff93f3a2b"]["model"], jobs["a0cff93f3a2b"]["reasoning_effort"]) == (
        "gpt-5.4-mini",
        "low",
    )
    for job_id in ("5db43f3b3d5d", "b44861829e51"):
        assert (jobs[job_id]["model"], jobs[job_id]["reasoning_effort"]) == (
            "gpt-5.4",
            "medium",
        )
    for job_id in ("e4f1b2ae179d", "2a402a32e5ec"):
        assert (jobs[job_id]["model"], jobs[job_id]["reasoning_effort"]) == (
            "gpt-5.6-sol",
            "high",
        )


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
                        "provider": "openai-codex",
                        "model": "gpt-5.4-mini",
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
                        "provider": "openai-codex",
                        "model": "gpt-5.4",
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
                "provider": "openai-codex",
                "model": "gpt-5.4-mini",
                "prompt": "new prompt",
            },
            "resume": True,
        }
    ]


def test_cron_apply_uses_locked_hermes_api_for_provider_and_model(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs.json"
    jobs.write_text('{"jobs": []}', encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "scripts.sync_hermes_cron.subprocess.run",
        lambda cmd, check: calls.append(cmd),
    )

    backup = apply_plan(
        [
            {
                "job_id": "managed",
                "changes": {
                    "provider": "openai-codex",
                    "model": "gpt-5.4-mini",
                },
                "resume": False,
            }
        ],
        jobs,
        "hermes",
        "hermes-python",
        Path("scripts/update_hermes_cron_job.py"),
    )

    assert backup is not None and backup.is_file()
    assert calls == [
        [
            "hermes-python",
            str(Path("scripts/update_hermes_cron_job.py")),
            "--job-id",
            "managed",
            "--provider",
            "openai-codex",
            "--model",
            "gpt-5.4-mini",
        ]
    ]


def test_locked_cron_updater_changes_both_inference_axes():
    calls: list[tuple[str, dict]] = []

    def fake_update(job_id, updates):
        calls.append((job_id, updates))
        return {"id": job_id, **updates}

    updated = update_inference(
        "managed",
        " openai-codex ",
        " gpt-5.4-mini ",
        updater=fake_update,
    )

    assert updated == {
        "id": "managed",
        "provider": "openai-codex",
        "model": "gpt-5.4-mini",
    }
    assert calls == [
        (
            "managed",
            {"provider": "openai-codex", "model": "gpt-5.4-mini"},
        )
    ]
