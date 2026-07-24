"""§19.4 — качество подбора ключей (волна 2.4, 2026-07). Офлайн: LLM и SDK подменены заглушками.

Классы багов, которые здесь закрыты (каждый ронял ФУНКЦИЮ, за которую менеджер платит):
1. Фильтр релевантности был fail-open И МОЛЧА: эхо-JSON по 120 ключам не влезал в max_tokens →
   обрыв → `{}` → все ключи «релевантны», ни строки лога. Контракт инвертирован (модель шлёт
   только НЕРЕЛЕВАНТНЫЕ), обрыв виден по finish_reason и пишется WARNING.
2. Кластеризация по интенту на 200 ключах — тот же обрыв → одна группа «Все ключи», колонка
   «Интент» пуста. Теперь батчи по 70 параллельно + склейка; ключ не теряется даже при сбое батча.
3. Табличный XLSX/CSV («ключ | тип соответствия») резался по запятой → заголовки и вторая колонка
   становились «ключами» (файл 200×2 → ~401 ключ).
4. /addkeys не снимал маркеры `[…]`/«…» → KEYWORD_HAS_INVALID_CHARS уже ПОСЛЕ ✅.
5. common_match_type считал УДАЛЁННЫЕ ключи и ключи чужих каналов.
6. Двойной тап по «🔎 Генерация ключевых слов» (колбэки не троттлятся) → две таблицы.
"""

from __future__ import annotations

import json
import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import keywords.cluster as C  # noqa: E402
import keywords.filter as F  # noqa: E402
from bot import ux  # noqa: E402
from keywords.ingest import parse_keywords_text, strip_match_markers  # noqa: E402


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


def _chat(content: str, *, finish: str = "stop"):
    async def _c(messages, **kw):
        return SimpleNamespace(content=content, finish_reason=finish)

    return _c


# ── 1. Фильтр релевантности: обрыв больше не «всё релевантно» МОЛЧА ────────────────
@pytest.mark.asyncio
async def test_truncated_answer_is_failopen_but_logged(caplog):
    """Обрыв по max_tokens (нет закрывающей «]») → fail-open (ключи не теряем), но WARNING
    с finish_reason: раньше это была полностью немая деградация фильтра §19.4.2 в no-op."""
    texts = [f"key {i}" for i in range(20)]
    truncated = '["key 1", "key 2", "key 3'  # ответ оборван на полуслове
    with caplog.at_level(logging.WARNING, logger="aimash.keywords"):
        with patched(F, "chat", _chat(truncated, finish="length")):
            v = await F.filter_relevance(texts=texts, topic="t")
    assert all(v[t] is True for t in texts)  # fail-open: ни один ключ не потерян
    assert any("length" in r.getMessage() for r in caplog.records)  # и это ВИДНО в логах


@pytest.mark.asyncio
async def test_all_marked_irrelevant_is_distrusted(caplog):
    """Модель пометила нерелевантным ВЕСЬ батч ⇒ она не поняла тему. Верить нельзя: в визарде это
    дало бы кампанию с нулём ключей. Fail-open + WARNING."""
    texts = [f"key {i}" for i in range(12)]
    with caplog.at_level(logging.WARNING, logger="aimash.keywords"):
        with patched(F, "chat", _chat(json.dumps(texts))):
            v = await F.filter_relevance(texts=texts, topic="t")
    assert all(v[t] is True for t in texts)
    assert any("не доверяем" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_partial_irrelevant_is_honoured():
    """Нормальный ответ (меньше порога недоверия) — вердикты применяются как есть."""
    texts = [f"key {i}" for i in range(20)]
    with patched(F, "chat", _chat(json.dumps(["key 0", "key 5"]))):
        v = await F.filter_relevance(texts=texts, topic="t")
    assert v["key 0"] is False and v["key 5"] is False
    assert sum(1 for t in texts if v[t] is False) == 2


@pytest.mark.asyncio
async def test_llm_failure_is_failopen_and_logged(caplog):
    async def _boom(messages, **kw):
        raise RuntimeError("LLM down")

    with caplog.at_level(logging.WARNING, logger="aimash.keywords"):
        with patched(F, "chat", _boom):
            v = await F.filter_relevance(texts=["a", "b"], topic="t")
    assert v == {"a": True, "b": True}
    assert caplog.records  # сбой модели тоже больше не немой


def test_relevance_batch_fits_generation_ceiling():
    """Батч фильтра ≤ 80: запрос длинный, ответ — только нерелевантные (O(#нерелевантных))."""
    assert F._BATCH <= 80


# ── 2. Кластеризация: батчинг, склейка, ни один ключ не теряется ───────────────────
@pytest.mark.asyncio
async def test_clustering_batches_and_merges():
    """150 ключей → 3 запроса по ≤70 (раньше один на 200 → обрыв → «Все ключи»). Одинаковые
    (имя, интент) из разных батчей склеиваются в ОДНУ группу, все ключи покрыты."""
    keys = [f"k{i}" for i in range(150)]
    calls: list[int] = []

    async def _c(messages, **kw):
        sent = [ln for ln in messages[-1]["content"].splitlines() if ln.startswith("k")]
        calls.append(len(sent))
        half = len(sent) // 2
        return SimpleNamespace(
            content=json.dumps(
                [
                    {"name": "Покупка", "intent": "транзакционный", "keywords": sent[:half]},
                    {"name": "Инфо", "intent": "информационный", "keywords": sent[half:]},
                ]
            ),
            finish_reason="stop",
        )

    with patched(C, "chat", _c):
        clusters = await C.cluster_keywords(keys)
    assert len(calls) == 3 and max(calls) <= C._CLUSTER_BATCH  # батчами, а не одним запросом
    names = [c.name for c in clusters]
    assert names.count("Покупка") == 1 and names.count("Инфо") == 1  # склеены, а не 3×2 группы
    covered = [k for c in clusters for k in c.keywords]
    assert sorted(covered) == sorted(keys) and len(covered) == len(keys)  # без потерь и дублей
    assert {c.intent for c in clusters} == {"транзакционный", "информационный"}  # интент есть


@pytest.mark.asyncio
async def test_clustering_failed_batch_keys_land_in_misc():
    """Сбой ОДНОГО батча не должен терять его ключи из сводки/экспорта — они уходят в «Прочее»."""
    keys = [f"k{i}" for i in range(100)]
    seen: list[int] = []

    async def _c(messages, **kw):
        seen.append(1)
        if len(seen) == 2:  # второй батч падает
            raise RuntimeError("LLM down")
        sent = [ln for ln in messages[-1]["content"].splitlines() if ln.startswith("k")]
        return SimpleNamespace(
            content=json.dumps([{"name": "A", "intent": "коммерческий", "keywords": sent}]),
            finish_reason="stop",
        )

    with patched(C, "chat", _c):
        clusters = await C.cluster_keywords(keys)
    covered = [k for c in clusters for k in c.keywords]
    assert sorted(covered) == sorted(keys)  # ключи упавшего батча не исчезли
    assert any(c.name == "Прочее" for c in clusters)


@pytest.mark.asyncio
async def test_clustering_total_failure_falls_back_to_single_group():
    async def _boom(messages, **kw):
        raise RuntimeError("LLM down")

    with patched(C, "chat", _boom):
        clusters = await C.cluster_keywords(["a", "b"])
    assert len(clusters) == 1 and clusters[0].name == "Все ключи"


# ── 3. Табличный ввод (XLSX/CSV через core.ingest) ────────────────────────────────
def test_xlsx_table_with_match_type_column():
    """Файл «ключ | тип соответствия» (лист → «# Sheet1», строка → «ячейка, ячейка»): тип берётся
    ИЗ КОЛОНКИ, заголовки и имя листа в ключи не превращаются. Раньше 3 строки давали 7 «ключей»."""
    text = (
        "# Keywords\nKeyword, Match type\nбуксир киев, exact\nэвакуатор киев, broad\nтакси, phrase"
    )
    out = parse_keywords_text(text, default_match_type="phrase")
    by = {k.text: k.match_type for k in out}
    assert by == {"буксир киев": "exact", "эвакуатор киев": "broad", "такси": "phrase"}
    assert "match type" not in {k.text.casefold() for k in out}  # заголовок не ключ
    assert not any(k.text.casefold() in ("exact", "broad", "phrase") for k in out)  # и не значения


def test_xlsx_table_ru_headers_and_metric_columns():
    """Экспорт таблицы бота («ключ, объём, конкуренция») — ключ только первая ячейка, числа/метрики
    отбрасываются. Русские заголовки тоже."""
    text = "# Лист1\nКлючевое слово, Объём, Конкуренция\nремонт ноутбуков, 1200, HIGH\nсервис, 300, LOW"
    out = parse_keywords_text(text)
    assert [k.text for k in out] == ["ремонт ноутбуков", "сервис"]


def test_plain_comma_list_still_splits_into_keywords():
    """Регресс: обычный список через запятую (все ячейки — текст) по-прежнему = несколько ключей."""
    out = parse_keywords_text("аренда авто, прокат машин, авто напрокат")
    assert [k.text for k in out] == ["аренда авто", "прокат машин", "авто напрокат"]


def test_keyword_with_digits_is_not_mistaken_for_table():
    """«24 часа» — ячейка с цифрами, но НЕ число: список не должен схлопнуться в табличную строку."""
    out = parse_keywords_text("эвакуатор 24 часа, буксир недорого")
    assert [k.text for k in out] == ["эвакуатор 24 часа", "буксир недорого"]


# ── 4. /addkeys: маркеры снимаются тем же парсером, что в файловом пути ────────────
def test_addkeys_strips_markers():
    """`[ремонт]` / «"буксир"» уходили в Google буквально → KEYWORD_HAS_INVALID_CHARS уже после ✅."""
    assert strip_match_markers("[ремонт окон]") == ("ремонт окон", "exact")
    assert strip_match_markers('"буксир киев"') == ("буксир киев", "phrase")
    out = parse_keywords_text('[ремонт окон]\n"буксир киев"\nэвакуатор')
    assert [k.text for k in out] == ["ремонт окон", "буксир киев", "эвакуатор"]
    assert not any("[" in k.text or '"' in k.text for k in out)


def test_addkeys_text_path_uses_ingest_parser():
    """Гард: текстовый путь /addkeys обязан идти через parse_keywords_text (маркеры + валидация),
    а не резать text.split(',') — иначе класс бага возвращается."""
    src = (Path(__file__).resolve().parents[1] / "bot/handlers/keywords_flow.py").read_text(
        encoding="utf-8"
    )
    body = src[src.index("async def kw_add_keywords") :]
    body = body[: body.index("\n@bm.dp")]
    assert "parse_keywords_text" in body
    assert 'text.replace("\\n", ",").split(",")' not in body


# ── 5. common_match_type: без REMOVED и без чужих каналов ─────────────────────────
def test_common_match_type_query_excludes_removed_and_other_channels():
    """500 давно удалённых BROAD перевешивали 20 живых PHRASE → визард §19.3 предлагал «по
    аналогии» тип, которым аккаунт уже не пользуется."""
    import ads.read as R

    seen: list[str] = []

    class _GA:
        def search(self, *, customer_id, query):
            seen.append(query)
            raise RuntimeError("stop")  # метрики не нужны: проверяем сам GAQL

    class _Client:
        def get_service(self, name):
            return _GA()

    with patched(R, "ensure_read_allowed", lambda cid: None):
        R.search_campaign_medians(_Client(), "123")
    q = next(q for q in seen if "ad_group_criterion.keyword.match_type" in q)
    assert "ad_group_criterion.status != 'REMOVED'" in q
    assert "campaign.advertising_channel_type = 'SEARCH'" in q


# ── 6. Двойной тап по долгой кнопке ───────────────────────────────────────────────
def test_single_flight_blocks_second_tap():
    key = ("chat", "sess-1")
    assert ux.acquire_flight("cc_kw_gen", key) is True
    assert ux.acquire_flight("cc_kw_gen", key) is False  # второй тап — отказ
    assert (
        ux.acquire_flight("cc_kw_gen", ("chat", "sess-2")) is True
    )  # другая сессия не блокируется
    ux.release_flight("cc_kw_gen", key)
    assert ux.acquire_flight("cc_kw_gen", key) is True  # после релиза кнопка снова живая
    ux.release_flight("cc_kw_gen", key)
    ux.release_flight("cc_kw_gen", ("chat", "sess-2"))


def test_kw_generate_handler_is_guarded():
    """Гард класса: обработчик кнопки генерации ОБЯЗАН держать single-flight (иначе двойной тап
    снова наплодит две Google-таблицы, и первая — уже выверенная — будет отвергнута как «чужая»)."""
    src = (Path(__file__).resolve().parents[1] / "bot/handlers/campaign_wizard.py").read_text(
        encoding="utf-8"
    )
    head = src[src.index('bm.F.action == "kw_generate"') :]
    head = head[: head.index("async def cc_kw_generate_run")]
    assert "acquire_flight" in head and "release_flight" in head


# ── 7. Брендозащита минус-слов подключена во ВСЕХ call-site ───────────────────────
def test_all_negative_keyword_callsites_pass_protected():
    """Докстринг suggest_negative_keywords обещал брендозащиту, но `protected=` передавал только
    /keywords: визард и /advise могли посоветовать заминусовать бренд самого клиента."""
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for rel in ("bot/main.py", "bot/handlers/campaign_wizard.py", "advisor/service.py"):
        src = (root / rel).read_text(encoding="utf-8")
        idx = 0
        while (idx := src.find("suggest_negative_keywords(", idx)) != -1:
            call = src[idx : src.find(")", idx) + 1]
            if "protected" not in call:
                offenders.append(f"{rel}: {call[:60]}…")
            idx += 1
    assert not offenders, "вызов suggest_negative_keywords без protected=:\n" + "\n".join(offenders)
