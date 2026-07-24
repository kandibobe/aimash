"""core/guards.py — construction-time гарды, которые интерпретатор НЕ вырежет.

`assert` — не гард. Под `python -O` (и `PYTHONOPTIMIZE=1` в окружении) CPython выбрасывает
assert-инструкции из байткода целиком, вместе с сообщением. Для проверки, единственная задача
которой — не пустить мутационный инструмент в read-фазу, это значит: гард исчезает молча, импорт
проходит, денежный путь открыт. Признака нет ни одного — ни в логах, ни в поведении, — пока кто-то
не выставит наружу мутацию под видом чтения.

Сегодня это дыра ВЗВЕДЁННАЯ, а не активная: ни `Dockerfile`, ни `docker-compose.yml`, ни CI не
включают `-O`/`PYTHONOPTIMIZE`. Цена в том, что взводится она одной строкой в чужом коммите («ускорим
образ»), а снимает сразу два инварианта — И4 (MCP READ-слой без мутаций) и S4 (аналитический цикл
без мутаций). Правило 10 (fail-closed) требует, чтобы гард отказывал при любой конфигурации, а не
при удачной.

Держится тестами `tests/test_invariants_core.py`: (а) механизм роняет импорт под `-O` в подпроцессе;
(б) в продовых пакетах нет ни одного module-level `assert` — то есть класс бага закрыт, а не два его
экземпляра.
"""

from __future__ import annotations

from collections.abc import Collection


def require_no_mutations(
    names: Collection[str],
    mutation_names: Collection[str],
    *,
    rule: str,
    subject: str,
) -> None:
    """Пересечение с мутационными именами ⇒ `RuntimeError` на импорте модуля-вызывающего.

    Зовётся на уровне модуля (construction-time): падение видно немедленно при сборке/старте, а не
    на первом обращении к инструменту, когда рядом уже стоит живой аккаунт.

    Args:
        names: имена инструментов слоя, который обязан быть read-only.
        mutation_names: `agent.tools.schemas.MUTATION_TOOLS`.
        rule: код инварианта для сообщения («И4», «S4») — чтобы падение искалось по спеке.
        subject: чей это набор, человеко-читаемо.

    Raises:
        RuntimeError: если пересечение непусто.
    """
    overlap = frozenset(names) & frozenset(mutation_names)
    if not overlap:
        return
    raise RuntimeError(
        f"{rule} нарушен: {subject} пересекается с мутационными инструментами "
        f"(agent.tools.schemas.MUTATION_TOOLS): {sorted(overlap)}. "
        "Read-слой не смеет содержать мутации — это денежный путь."
    )
