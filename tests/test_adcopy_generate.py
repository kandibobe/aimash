"""Офлайн-тесты Фазы 2.B: генерация RSA-текстов. Роутер (LLM) подменяется заглушкой.

Проверяем главное (golden rule #4): длину считает КОД — слишком длинные элементы
ОТбрасываются, кириллица = 1 символ; дедуп; догенерация при нехватке; устойчивый парсинг.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import adcopy.generate as G  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


def _msg(content: str):
    return SimpleNamespace(content=content)


def _fake_chat_returning(*contents: str):
    """Заглушка router.chat: на i-й вызов отдаёт contents[i] (последний повторяется)."""
    calls = {"n": 0}

    async def _chat(messages, **kwargs):
        i = min(calls["n"], len(contents) - 1)
        calls["n"] += 1
        return _msg(contents[i])

    return _chat, calls


def _json(headlines, descriptions):
    return json.dumps({"headlines": headlines, "descriptions": descriptions}, ensure_ascii=False)


# ── happy path ────────────────────────────────────────────────────────────────────
async def test_generate_happy_path():
    content = _json(
        ["Купить цветы", "Доставка цветов", "Букеты недорого"], ["Описание раз", "Описание два"]
    )
    fake, calls = _fake_chat_returning(content)
    with patched(G, "chat", fake):
        draft = await G.generate_rsa(G.CopyBrief(topic="цветы", n_headlines=3, n_descriptions=2))
    assert draft.headlines == ["Купить цветы", "Доставка цветов", "Букеты недорого"]
    assert draft.descriptions == ["Описание раз", "Описание два"]
    assert draft.dropped_headlines == 0 and draft.attempts == 1
    assert calls["n"] == 1  # хватило одного вызова


# ── длину считает КОД: слишком длинный отброшен (кириллица = 1) ──────────────────
async def test_overlong_headline_dropped_cyrillic_one():
    too_long = "а" * 31  # 31 кириллический символ > 30 → код отбросит
    ok30 = "а" * 30  # ровно 30 → ок
    content = _json([too_long, ok30, "Короткий"], ["Норм описание"])
    fake, _ = _fake_chat_returning(content)
    with patched(G, "chat", fake):
        draft = await G.generate_rsa(G.CopyBrief(topic="x", n_headlines=2, n_descriptions=1))
    assert too_long not in draft.headlines
    assert ok30 in draft.headlines and "Короткий" in draft.headlines
    assert draft.dropped_headlines == 1 and draft.attempts == 1


async def test_overlong_description_dropped():
    long_desc = "б" * 91  # > 90
    content = _json(["H"], [long_desc, "Ок описание"])
    fake, _ = _fake_chat_returning(content)
    with patched(G, "chat", fake):
        draft = await G.generate_rsa(G.CopyBrief(topic="x", n_headlines=1, n_descriptions=1))
    assert long_desc not in draft.descriptions and "Ок описание" in draft.descriptions
    assert draft.dropped_descriptions == 1 and draft.attempts == 1


# ── дедуп (без учёта регистра) ──────────────────────────────────────────────────
async def test_dedup_case_insensitive():
    content = _json(["Доставка цветов", "доставка цветов", "Букеты"], ["d"])
    fake, _ = _fake_chat_returning(content)
    with patched(G, "chat", fake):
        draft = await G.generate_rsa(G.CopyBrief(topic="x", n_headlines=5, n_descriptions=1))
    assert draft.headlines == ["Доставка цветов", "Букеты"]  # дубль убран


# ── догенерация при нехватке (несколько вызовов) ────────────────────────────────
async def test_regenerates_until_enough():
    c1 = _json(["Заголовок 1"], [])  # первый вызов даёт 1 заголовок
    c2 = _json(["Заголовок 2"], ["Описание"])  # второй — добивает
    fake, calls = _fake_chat_returning(c1, c2)
    with patched(G, "chat", fake):
        draft = await G.generate_rsa(G.CopyBrief(topic="x", n_headlines=2, n_descriptions=1))
    assert draft.headlines == ["Заголовок 1", "Заголовок 2"]
    assert draft.attempts == 2 and calls["n"] == 2


async def test_stops_on_unparsable_output():
    fake, calls = _fake_chat_returning("извините, не могу")  # нет JSON
    with patched(G, "chat", fake):
        draft = await G.generate_rsa(G.CopyBrief(topic="x", n_headlines=3, n_descriptions=2))
    assert draft.headlines == [] and draft.descriptions == []
    assert calls["n"] == 1  # без смысла повторять — остановились


# ── B1: слишком длинные описания восстанавливаются (ремонт LLM / детерминированный трим) ──
async def test_overlong_descriptions_recovered_by_trim():
    """Модель упорно отдаёт описания сверх 90 → добор детерминированным тримом до минимума
    (раньше флоу сдавался: «1/4 описание» → «не удалось сгенерировать»). Ремонт-LLM тут не
    помогает (тот же контент без ключа items) — работает именно трим."""
    over = ["А" * 100, "Б" * 100]  # оба > 90 → отброшены, но восстановимы тримом (разные буквы)
    content = _json(["Заголовок"], over)
    fake, _ = _fake_chat_returning(content)
    with patched(G, "chat", fake):
        draft = await G.generate_rsa(G.CopyBrief(topic="x", n_headlines=1, n_descriptions=2))
    assert len(draft.descriptions) == 2  # добито тримом до цели (не сдались)
    assert all(G.rsa_len(d) <= 90 for d in draft.descriptions)  # каждый уложен в лимит
    assert draft.dropped_descriptions >= 2  # честно: модель дала слишком длинные


async def test_overlong_descriptions_recovered_by_repair():
    """Если ремонт-LLM возвращает укороченные варианты — берём их (качество лучше трима)."""
    over = ["слово " * 20, "текст " * 20]  # >90 каждое
    gen = _json(["H1"], over)
    repaired = json.dumps(
        {"items": ["Короткое описание один", "Короткое описание два"]}, ensure_ascii=False
    )
    fake, _ = _fake_chat_returning(gen, gen, repaired)  # gen на генерацию, repaired на ремонт
    with patched(G, "chat", fake):
        draft = await G.generate_rsa(G.CopyBrief(topic="x", n_headlines=1, n_descriptions=2))
    assert "Короткое описание один" in draft.descriptions
    assert "Короткое описание два" in draft.descriptions
    assert all(G.rsa_len(d) <= 90 for d in draft.descriptions)


def test_hard_trim_respects_limit_and_word_boundary():
    long = "Незабываемый отдых на Байкале экскурсии трекинг и чистейший воздух бронируйте сейчас же"
    out = G._hard_trim(long, 90)
    assert G.rsa_len(out) <= 90
    assert out and not out.endswith(" ")
    # одно слово длиннее лимита — режем по символам, но всё равно ≤ лимита
    assert G.rsa_len(G._hard_trim("Ц" * 50, 30)) <= 30


# ── устойчивый парсинг JSON в обёртке ───────────────────────────────────────────
def test_parse_tolerates_fences_and_text():
    fenced = "```json\n" + _json(["A"], ["B"]) + "\n```"
    assert G._parse(fenced) == (["A"], ["B"])
    assert G._parse("тут JSON: " + _json(["X"], []) + " — конец") == (["X"], [])
    assert G._parse("совсем не json") == ([], [])
    assert G._parse("") == ([], [])


# ── валидация брифа в коде ───────────────────────────────────────────────────────
def test_brief_bounds():
    from pydantic import ValidationError

    G.CopyBrief(topic="x", n_headlines=30, n_descriptions=10)  # верхние границы — ок
    for bad in ({"n_headlines": 31}, {"n_descriptions": 0}):
        try:
            G.CopyBrief(topic="x", **bad)
            raise AssertionError(f"должно было упасть: {bad}")
        except ValidationError:
            pass
