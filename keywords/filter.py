"""§19.4.2 (iii): AI-слой релевантности — помечает идеи ключей релевантно/нерелевантно бизнесу.

Только текст: модель видит ТОЛЬКО тексты идей и тему/профиль (метрики к ней НЕ уходят — golden
rule #4, их считает КОД). Это подсказка: финальное решение за менеджером (он редактирует таблицу).
Fail-open: при сбое/неизвестном — считаем релевантным (не теряем ключи молча, менеджер уберёт сам).

КОНТРАКТ ИНВЕРТИРОВАН (2026-07): модель возвращает ТОЛЬКО НЕРЕЛЕВАНТНЫЕ ключи, а не вердикт по
каждому. Раньше просили эхо-JSON `{ключ: true|false}` по всему батчу: на 120 ключах ответ не
влезал в max_tokens (LLM_MAX_TOKENS_KEYWORDS) → обрыв → битый JSON → `{}` → fail-open пометил ВСЕ
ключи релевантными, БЕЗ единой строки лога. То есть на живом объёме фильтр §19.4.2 был no-op — и
молчал об этом. Теперь размер ответа O(#нерелевантных), обрыв структурно почти невозможен, а если
он всё же случился (finish_reason='length') или ответ не разобрался — пишем WARNING, а не тишину.
"""

from __future__ import annotations

import json
import logging

from llm.router import chat, finish_reason
from core.config import settings
from core.llm_budget import LLMBudgetError

log = logging.getLogger("aimash.keywords")

_BATCH = 80  # сколько текстов отдаём модели за раз (запрос длинный, ответ — только нерелевантные)
# Порог недоверия: модель пометила почти ВЕСЬ батч нерелевантным ⇒ она не поняла тему (а не «все
# ключи мусорные»). Верить нельзя: в визарде это дало бы кампанию с нулём ключей (fallback-путь
# берёт только релевантные). Fail-open + WARNING.
_DISTRUST_SHARE = 0.9

_SYSTEM = (
    "Ты — специалист по контекстной рекламе. По теме кампании и информации о клиенте найди среди "
    "присланных ключевых слов НЕРЕЛЕВАНТНЫЕ бизнесу клиента (нецелевой трафик, другая услуга, "
    "другая страна, другой продукт). "
    'Верни СТРОГО JSON-массив строк ["…"] — только нерелевантные ключи, без пояснений и markdown. '
    "Каждый элемент — ключ РОВНО в том написании, в каком он прислан; новых не выдумывай. "
    "Если все ключи релевантны — верни пустой массив []."
)


def _parse_offtopic(content: str, valid: list[str]) -> set[str] | None:
    """JSON-массив нерелевантных → множество ключей (в ИСХОДНОМ написании). None — ответ не
    разобран (обрыв/не JSON/не массив): вызывающий обязан отличить это от «нерелевантных нет»."""
    s = content or ""
    start, end = s.find("["), s.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None  # в т.ч. обрыв по max_tokens: закрывающей скобки нет
    try:
        data = json.loads(s[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    by_lower = {k.casefold(): k for k in valid}
    out: set[str] = set()
    for x in data:
        if not isinstance(x, (str, int, float)):
            continue
        orig = by_lower.get(str(x).strip().casefold())
        if orig is not None:  # только реально присланные ключи (модель не может выдумать новых)
            out.add(orig)
    return out


async def filter_relevance(
    *,
    texts: list[str],
    topic: str,
    profile: str = "",
    language: str = "ru",
    target_geo: str = "",
) -> dict[str, bool]:
    """Вернуть {текст: релевантно?} по списку идей. Fail-open: отсутствующие/сбой → True (advisory).
    Дедупит вход по casefold, сохраняет исходное написание ключа."""
    uniq = list(dict.fromkeys(t.strip() for t in texts if t and t.strip()))
    if not uniq:
        return {}
    result: dict[str, bool] = {}
    for i in range(0, len(uniq), _BATCH):
        batch = uniq[i : i + _BATCH]
        ctx = (
            f"Язык: {language}.\nТема кампании: {topic}.\n"
            + (
                f"Целевое гео: {target_geo}. Ключи про другие страны нерелевантны.\n"
                if target_geo
                else ""
            )
            + (
                f"О клиенте:\n{profile[: settings.profile_ctx_chars]}\n"
                if (profile or "").strip()
                else ""
            )
            + "Ключевые слова:\n"
            + "\n".join(batch)
        )
        off_topic: set[str] = set()
        try:
            msg = await chat(
                [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": ctx},
                ],
                role="keywords",
                temperature=0.2,
            )
            parsed = _parse_offtopic(getattr(msg, "content", "") or "", batch)
            if parsed is None:
                # Раньше это была ТИХАЯ деградация в «всё релевантно». Теперь видно в логах, а
                # 'length' прямо говорит: потолок генерации мал для этого батча.
                log.warning(
                    "релевантность: ответ модели не разобран (finish_reason=%r, батч %d ключей) "
                    "— считаем все релевантными (fail-open)",
                    finish_reason(msg) or "?",
                    len(batch),
                )
            elif len(parsed) >= max(10, int(len(batch) * _DISTRUST_SHARE)):
                log.warning(
                    "релевантность: модель пометила %d из %d ключей нерелевантными — не доверяем "
                    "(вероятно, не поняла тему), считаем все релевантными",
                    len(parsed),
                    len(batch),
                )
            else:
                off_topic = parsed
        except LLMBudgetError:
            # BZ-4: бюджет-стоп — НЕ «сбой модели». Fail-open здесь означал бы «все ключи
            # релевантны» на все часы до сброса потолка, то есть нерелевантные ключи уехали бы
            # в кампанию с пометкой «проверено ИИ». Отказываем явно (вызыватель покажет причину).
            raise
        except Exception as e:  # noqa: BLE001 — advisory; fail-open, но НЕ молча
            log.warning(
                "релевантность: сбой модели (%s) — считаем все релевантными", type(e).__name__
            )
            off_topic = set()
        for t in batch:
            result[t] = t not in off_topic  # не назван нерелевантным → релевантен (fail-open)
    return result
