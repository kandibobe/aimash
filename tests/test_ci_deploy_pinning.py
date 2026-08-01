from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_production_deploy_is_pinned_to_the_tested_workflow_sha() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert 'DEPLOY_SHA="${{ github.sha }}"' in workflow
    assert 'git reset --hard "$DEPLOY_SHA"' in workflow
    assert 'test "$(git rev-parse HEAD)" = "$DEPLOY_SHA"' in workflow
    assert "git reset --hard origin/master" not in workflow
