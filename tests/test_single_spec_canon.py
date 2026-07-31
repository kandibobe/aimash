"""The accepted 2026-07-31 product spec must remain the only normative product canon."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "docs" / "archive" / "pre-single-spec-2026-07"


def test_single_product_spec_is_accepted_canon():
    spec = (ROOT / "SPEC.md").read_text(encoding="utf-8")
    assert "принято заказчиком 31.07.2026" in spec
    assert "единственный нормативный продуктовый источник истины" in spec


def test_developer_entrypoints_reference_only_single_product_canon():
    obsolete = ("HERMES_SPEC.md", "AGENTIC_VS_TZ.md", "TZ-Aimash-Hermes-Agent.md")
    for name in ("AGENTS.md", "CLAUDE.md", "README.md", "docs/README.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "SPEC.md" in text, name
        assert not any(old in text for old in obsolete), name


def test_previous_product_specs_are_non_normative_archive():
    expected = {
        "SPEC.pre-single-spec.md",
        "HERMES_SPEC.md",
        "AGENTIC_VS_TZ.md",
        "TZ-Aimash-Hermes-Agent.md",
    }
    assert expected == {path.name for path in ARCHIVE.iterdir() if path.is_file()}
    for name in expected:
        text = (ARCHIVE / name).read_text(encoding="utf-8")
        assert "NON-NORMATIVE" in text[:500], name
