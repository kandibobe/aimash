"""Ш1: рендер «было → станет» живёт в `confirm/render.py` и одинаков во всех контурах.

Пока рендер сидел внутри `bot/texts.py`, headless-подтверждение (`confirm.gate.build_summary`,
дальше MCP-WRITE) показывало человеку сырой dict параметров — то есть человек жал «да» не под тем
текстом, который читал. Тесты здесь держат три инварианта переноса:

* модуль headless — импорт `confirm.render` НЕ тянет `bot`/`aiogram`/`sqlalchemy`;
* `lang` обязателен — contextvar-язык не может втихую вернуться в чистый слой;
* RU и EN покрывают ОДИН И ТОТ ЖЕ набор операций (расхождение = молчаливый пустой summary
  на одном из языков).
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.service import SUPPORTED_OPERATIONS  # noqa: E402
from core import texts  # noqa: E402
from confirm import render  # noqa: E402
from confirm.gate import build_summary  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
# Операции со СВОЕЙ богатой карточкой (тексты RSA поэлементно, превью GDN/Video): рендер для них
# честно отдаёт '', а вызывающий оставляет собственный summary.
_OWN_CARD = {
    "create_rsa",
    "create_search_campaign",
    "create_gdn_campaign",
    "create_video_campaign",
    "create_demand_gen_campaign",
}


def test_render_is_headless():
    """Импорт рендера в чистом процессе не должен тянуть Telegram/ORM/SDK.

    Проверяем подпроцессом, а не `sys.modules` текущего: pytest к этому моменту уже импортировал
    пол-проекта, и проверка в общем процессе была бы вечнозелёной."""
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "import confirm.render;"
        "print(','.join(sorted({m.split('.')[0] for m in sys.modules}"
        " & {'bot', 'aiogram', 'sqlalchemy', 'aiohttp', 'openai'})))" % _ROOT
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=_ROOT)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "", f"confirm.render притащил: {out.stdout.strip()}"


def test_render_requires_explicit_lang():
    """Язык — аргумент, а не contextvar: в контуре без Telegram-запроса его брать неоткуда,
    а молчаливый «ru» на английской карточке — тихий дефект. Держим отсутствие дефолта."""
    with pytest.raises(TypeError):
        render.fmt_mutation_summary("pause_campaign", {"campaign": "X"})


def _operations_of(fn_name: str) -> set[str]:
    """Множество операций, которые ветвление функции разбирает явно (`operation == "..."`)."""
    tree = ast.parse((_ROOT / "confirm" / "render.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == fn_name)
    ops: set[str] = set()
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)):
            continue
        if node.left.id != "operation":
            continue
        for cmp_ in node.comparators:
            if isinstance(cmp_, ast.Constant) and isinstance(cmp_.value, str):
                ops.add(cmp_.value)
            elif isinstance(cmp_, ast.Tuple | ast.Set | ast.List):
                ops |= {e.value for e in cmp_.elts if isinstance(e, ast.Constant)}
    return ops


def test_ru_en_operation_parity():
    """RU-ветка и EN-ветка обязаны разбирать один набор операций: расхождение не падает, а молча
    отдаёт пустую сводку на одном языке — и карточка показывает служебный текст вместо diff."""
    ru, en = _operations_of("fmt_mutation_summary"), _operations_of("_mutation_summary_en")
    assert ru == en, f"только RU: {sorted(ru - en)}; только EN: {sorted(en - ru)}"


def test_render_covers_every_supported_operation():
    """Каждая исполняемая операция обязана иметь человеческий текст — кроме тех, у кого своя
    карточка. Новая операция в SUPPORTED_OPERATIONS без рендера уронит этот тест, а не покажет
    пользователю сырой dict на подтверждении."""
    ru = _operations_of("fmt_mutation_summary")
    assert set(SUPPORTED_OPERATIONS) - ru == _OWN_CARD
    assert ru - set(SUPPORTED_OPERATIONS) == set()


def test_texts_wrapper_delegates_to_render():
    """`core.texts` остался тонкой обёрткой: тот же текст, что и в headless-контуре."""
    params = {"campaign": "Search", "mode": "set_to", "value": 50}
    for lang in ("ru", "en"):
        assert texts.fmt_mutation_summary("update_budget", params, lang) == (
            render.fmt_mutation_summary("update_budget", params, lang)
        )
    assert texts.status_human("PAUSED", "en") == render.status_human("PAUSED", "en")
    assert texts.match_type_human("phrase", "en") == render.match_type_human("phrase", "en")


def test_build_summary_shows_human_text_not_raw_dict():
    """Headless-подтверждение показывает то же «было → станет», что и Telegram-карточка."""
    params = {"campaign": "Search", "mode": "set_to", "value": 50}
    s = build_summary("update_budget", before="[текущее значение из Google Ads]", after=params)
    assert s == render.fmt_mutation_summary("update_budget", params, "ru")
    assert "{" not in s and "mode" not in s  # сырого dict в тексте больше нет
    assert build_summary("update_budget", before="x", after=params, lang="en").startswith(
        "Campaign"
    )


def test_build_summary_falls_back_for_own_card_operations():
    """У create_rsa своя карточка — рендер отдаёт '', и запасной сырой формат обязан остаться,
    иначе подтверждение уехало бы с ПУСТОЙ сводкой (хуже сырого dict)."""
    s = build_summary("create_rsa", before="—", after={"headlines": ["a"]})
    assert s.startswith("create_rsa: ")
    assert build_summary("pause_campaign", before="—", after=None) == "pause_campaign: — → None"
