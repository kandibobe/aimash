"""Инварианты указателей документации: ссылка, ведущая в пустоту, — это молчаливый отказ.

Класс бага. `CLAUDE.md` объявлял источником истины `ТЗ.md`, которого не было ни в рабочем дереве,
ни в истории git; `docs/ACCEPTANCE.md` и `docs/README.md` ссылались на тот же `../ТЗ.md`. Ничто
не падало: markdown-ссылки никто не резолвит, а вопрос «где ТЗ?» упирался в несуществующий файл.
Тот же класс — переезд спеки Hermes из `docs/` в `deploy/hermes/`, после которого ссылки на
старый путь остались в семи местах.

Тест закрывает класс, а не два случая: любая относительная markdown-ссылка из ключевых документов
обязана резолвиться в существующий путь. Второй заход того же класса — упоминания переехавших
путей ВНЕ markdown-ссылок (бэктики, комментарии): `test_no_references_to_moved_paths` ниже.

Третий заход, обратная сторона того же (2026-07-30): ссылка ведёт куда надо, но её НЕТ.
`docs/REPO_GUARDRAILS.md` (ruleset на ветку == право снять confirm-гейт) прожил с нулём входящих
ссылок; `deploy/hermes/SOUL.md` — слот №1 системного промпта — не значился в индексе вовсе. Дока,
которую нельзя найти из индекса, не существует для читающего, и никакая проверка ссылок этого не
видит: битых ссылок нет ровно потому, что ссылок нет. `test_docs_index_covers_every_doc` требует
обратного включения — каждый документ достижим из `docs/README.md` ССЫЛКОЙ, не упоминанием.

Границы. Проверяются только markdown-ссылки `[текст](путь)` — там ложных срабатываний нет
(130 ссылок, 0 битых на момент написания). Пути в бэктиках намеренно НЕ проверяются: спека
`deploy/hermes/HERMES_SPEC.md` осознанно упоминает несуществующие пути — файлы вышестоящего
проекта Hermes (`gateway/platforms/base.py`, `tools/skills_hub.py`), будущие модули
(`tiktok/mutations.py`) и файлы, отсутствие которых она прямо констатирует («отдельных
`proposal.py` / `audit.py` НЕТ»). Гард, который краснеет на осознанных упоминаниях, отключают —
и тогда он не ловит ничего.
"""

from __future__ import annotations

import re

import pytest

from tests._docs_paths import (
    LEDGER_FILE,
    MOVED_PATHS,
    ROOT,
    doc_files,
    is_local_only,
    source_and_doc_files,
)

# [текст](путь) — берём путь до якоря `#`
_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _internal_targets(text: str) -> list[str]:
    out = []
    for m in _LINK.finditer(text):
        target = m.group(1).split("#")[0].strip()
        if not target or "://" in target or target.startswith("mailto:"):
            continue
        out.append(target)
    return out


@pytest.mark.parametrize("doc", doc_files(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_markdown_links_resolve(doc):
    """Каждая относительная ссылка ведёт в существующий файл или каталог.

    Исключение — цели, намеренно не попадающие в репозиторий (`is_local_only`): в CI и в свежем
    клоне их нет по политике `.gitignore`, а не из-за опечатки."""
    broken = sorted(
        {
            target
            for target in _internal_targets(doc.read_text(encoding="utf-8"))
            if not (doc.parent / target).exists()
            and not (ROOT / target).exists()
            and not is_local_only(target)
        }
    )
    assert not broken, f"{doc.relative_to(ROOT).as_posix()}: ссылки в пустоту: {broken}"


def test_docs_index_covers_every_doc():
    """Каждый документ `docs/` и `deploy/hermes/` достижим ССЫЛКОЙ из индекса `docs/README.md`.

    Обратное включение к `test_markdown_links_resolve`: тот ловит ссылку в пустоту, этот — пустоту
    вместо ссылки. Второе тише: битых ссылок нет ровно потому, что документ никто не упомянул.

    Достижимость считается по РЕЗОЛВУ ссылки, не по вхождению имени в текст: «см. SOUL.md» в prose
    находится подстрокой и никуда не ведёт, а требование индекса — чтобы вело.

    Вне требования: сам индекс и `docs/archive/**` (архив вне зоны инвариантов, `_SKIP_DIRS`).
    """
    index = ROOT / "docs" / "README.md"
    linked = set()
    for target in _internal_targets(index.read_text(encoding="utf-8")):
        for base in (index.parent, ROOT):
            resolved = (base / target).resolve()
            if resolved.exists():
                linked.add(resolved)

    expected = sorted((ROOT / "docs").glob("*.md")) + sorted(
        (ROOT / "deploy" / "hermes").rglob("*.md")
    )
    historical_redirects = {
        "deploy/hermes/AGENTIC_VS_TZ.md",
        "deploy/hermes/HERMES_SPEC.md",
        "docs/TZ-Aimash-Hermes-Agent.md",
    }
    missing = sorted(
        p.relative_to(ROOT).as_posix()
        for p in expected
        if p != index
        and "archive" not in p.parts
        and p.relative_to(ROOT).as_posix() not in historical_redirects
        and p.resolve() not in linked
    )
    assert not missing, (
        "не сослан из индекса docs/README.md: "
        + ", ".join(missing)
        + ". Дока, которой нет в индексе, не существует для читающего — добавь строку "
        "«- [имя](путь) — о чём» в подходящий раздел. Если документ намеренно не для индекса, "
        "переезд ему в docs/archive/ (там инварианты не действуют), а не молчание."
    )


@pytest.mark.parametrize("old,new", sorted(MOVED_PATHS.items()))
def test_no_references_to_moved_paths(old, new):
    """Ни один `.md`/`.py` не ссылается на путь, которого больше нет.

    Шире markdown-теста выше: ловит бэктики, комментарии и docstring'и — там переезд и остаётся
    незамеченным. Реестр переездов — `MOVED_PATHS` в `tests/_docs_paths.py`."""
    variants = {old, old.replace("/", "\\")}
    offenders = []
    for path in source_and_doc_files():
        if path == LEDGER_FILE:  # реестр обязан содержать старое имя — он про него и есть
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), 1):
            if any(v in line for v in variants):
                offenders.append(f"{path.relative_to(ROOT).as_posix()}:{lineno}")
    assert not offenders, f"ссылки на переехавший `{old}` (теперь `{new}`): {offenders}"


@pytest.mark.parametrize("rulebook", ["CLAUDE.md", "AGENTS.md"])
def test_agent_rulebook_declares_the_tz(rulebook):
    """Оба агентских rulebook объявляют источник истины — проверка работает без самого файла.

    Отдельно от параметризованного теста: тот зелёный и в случае, если ссылку на ТЗ просто
    удалить, а это ровно та «починка», которую делать нельзя."""
    text = (ROOT / rulebook).read_text(encoding="utf-8")
    assert "ТЗ.md" in text, f"{rulebook} больше не указывает на ТЗ — источник истины потерян"


def test_tz_is_generated_where_sources_are_available():
    """Есть `.docx` заказчика ⇒ обязан быть и сводный `ТЗ.md`.

    `ТЗ.md` и `*.docx` не коммитятся (`.gitignore:64-65`), поэтому в CI проверять нечего — но на
    машине с документами «оригиналы лежат, а сводного текста нет» это тот самый провал, из-за
    которого `CLAUDE.md` годами указывал в пустоту."""
    sources = sorted(ROOT.glob("*.docx"))
    if not sources:
        pytest.skip("документы заказчика (*.docx) локальные — в этом окружении их нет")
    assert (ROOT / "ТЗ.md").exists(), (
        f"оригиналы на месте ({len(sources)} .docx), а ТЗ.md нет — запусти `python scripts/docx_to_tz.py`"
    )
