from __future__ import annotations

import warnings
from pathlib import Path

import yaml
from alembic.config import Config
from alembic.script import ScriptDirectory


ROOT = Path(__file__).resolve().parents[1]


def test_alembic_config_uses_modern_os_path_separator() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        config = Config(str(ROOT / "alembic.ini"))
        ScriptDirectory.from_config(config)


def test_compose_has_bounded_production_runtime_defaults() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    for name in ("postgres", "scheduler", "backup"):
        assert services[name]["restart"] == "unless-stopped"

    for name in ("migrate", "mcp"):
        assert services[name]["restart"] == "no"

    for service in services.values():
        assert service["mem_limit"]
        assert service["logging"] == {
            "driver": "json-file",
            "options": {"max-size": "10m", "max-file": "3"},
        }

    assert services["postgres"]["healthcheck"]["test"][0] == "CMD-SHELL"
    assert services["mcp"]["healthcheck"]["test"][0] == "CMD"
    assert services["mcp"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert services["mcp"]["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )
