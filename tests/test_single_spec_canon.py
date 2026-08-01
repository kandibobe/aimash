"""Only the three customer-supplied DOCX files form the normative product canon."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "docs" / "archive" / "pre-single-spec-2026-07"
ORIGINALS = (
    "Aimash_Technical_Specification.docx",
    "Aimash_Flow_Google_Search_4.docx",
    "Информация о клиентах_1.docx",
)


def test_spec_identifies_only_the_three_original_docx_as_canon():
    spec = (ROOT / "SPEC.md").read_text(encoding="utf-8")
    assert all(name in spec for name in ORIGINALS)
    assert "ненормативная инженерная интерпретация" in spec
    assert "Aimash_Unified_Technical_Specification_ACCEPTED.docx" not in spec


def test_developer_entrypoints_reference_the_original_contract_set():
    obsolete = ("HERMES_SPEC.md", "AGENTIC_VS_TZ.md", "TZ-Aimash-Hermes-Agent.md")
    for name in ("AGENTS.md", "CLAUDE.md", "README.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert all(original in text for original in ORIGINALS), name
        assert "SPEC.md" in text, name
        assert "Aimash_Unified_Technical_Specification_ACCEPTED.docx" not in text, name
        assert not any(old in text for old in obsolete), name


def test_unified_docx_builder_marks_the_output_non_normative_draft():
    builder = (ROOT / "scripts" / "_build_unified_spec_docx.py").read_text(encoding="utf-8")
    assert "Aimash_Unified_Technical_Specification_DRAFT.docx" in builder
    assert "СТАТУС: ЧЕРНОВИК" in builder
    assert "СТАТУС: ПРИНЯТО" not in builder


def test_previous_product_specs_are_non_normative_archive():
    expected = {
        "SPEC.pre-single-spec.md",
        "HERMES_SPEC.md",
        "AGENTIC_VS_TZ.md",
        "TZ-Aimash-Hermes-Agent.md",
    }
    if not ARCHIVE.exists():
        return
    assert expected == {path.name for path in ARCHIVE.iterdir() if path.is_file()}
    for name in expected:
        text = (ARCHIVE / name).read_text(encoding="utf-8")
        assert "NON-NORMATIVE" in text[:500], name
