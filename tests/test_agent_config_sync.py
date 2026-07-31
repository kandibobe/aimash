"""Гарды от дрейфа проектных инструкций Claude Code и Codex.

Обе среды требуют собственные точки входа (`CLAUDE.md`/`AGENTS.md`, `.claude/`/`.agents/`),
поэтому физическое дублирование неизбежно. Без этого теста общий safety-контракт уже разошёлся:
Codex-rulebook получил несуществующие `.Codex/commands` и модель `Codex-sonnet-5`, а копия
`gads-version` сохранила старую дату сансета.
"""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _shared_rulebook(text: str) -> str:
    """Убрать единственный намеренно платформенный раздел из rulebook."""
    start = text.index("## Работа в ")
    end = text.index("## Что НЕ делать", start)
    return text[:start] + text[end:]


def test_agent_rulebooks_share_the_same_core():
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    codex = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert _shared_rulebook(claude) == _shared_rulebook(codex), (
        "CLAUDE.md и AGENTS.md разошлись вне платформенного раздела «Работа в …». "
        "Общее safety/architecture-ядро правится в обоих файлах одним изменением."
    )


def test_shared_skills_are_identical():
    claude_root = ROOT / ".claude" / "skills"
    codex_root = ROOT / ".agents" / "skills"
    claude = {p.parent.name: p for p in claude_root.glob("*/SKILL.md")}
    codex = {p.parent.name: p for p in codex_root.glob("*/SKILL.md")}

    missing = sorted(set(claude) - set(codex))
    assert not missing, f"в .agents/skills нет общих Claude-скилов: {missing}"

    drift = sorted(
        name
        for name, path in claude.items()
        if path.read_text(encoding="utf-8") != codex[name].read_text(encoding="utf-8")
    )
    assert not drift, f"общие .claude/.agents скилы разошлись: {drift}"


def test_money_path_policy_is_shared():
    claude = json.loads((ROOT / ".claude/hooks/money_paths.json").read_text(encoding="utf-8"))
    codex = json.loads((ROOT / ".codex/hooks/money_paths.json").read_text(encoding="utf-8"))
    assert claude == codex, "Claude/Codex hooks охраняют разные денежные пути"
