from __future__ import annotations

import importlib.util
import sys

import pytest
import yaml

from tests._docs_paths import ROOT


def _load_module():
    path = ROOT / "scripts" / "hermes_restore_toolsets.py"
    spec = importlib.util.spec_from_file_location("hermes_restore_toolsets", path)
    if spec is None or spec.loader is None:  # pragma: no cover
        pytest.skip("hermes_restore_toolsets.py unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_reference_toolsets_accepts_block_yaml(monkeypatch, tmp_path):
    module = _load_module()
    slugs = [f"toolset-{index}" for index in range(12)]
    reference = tmp_path / "config.yaml"
    reference.write_text(
        yaml.safe_dump({"agent": {"disabled_toolsets": slugs}}, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "REFERENCE", reference)

    assert module.read_reference_slugs() == slugs


def test_reference_toolsets_rejects_duplicates(monkeypatch, tmp_path):
    module = _load_module()
    reference = tmp_path / "config.yaml"
    reference.write_text(
        yaml.safe_dump({"agent": {"disabled_toolsets": ["same"] * 12}}, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "REFERENCE", reference)

    with pytest.raises(SystemExit, match="дубли"):
        module.read_reference_slugs()
