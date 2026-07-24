"""§20.4: вычитание шаблона сайта (меню/подвал/сквозные блоки) из текстов страниц.

`core.ingest._html_to_text(drop_chrome=True)` сносит семантическую обвязку (nav/footer/aside), но
половина сайтов верстает меню и подвал div'ами без ролей. Второй рубеж — статистический и не зависит
от вёрстки: строка, встречающаяся на большинстве страниц, — это шаблон, а не содержимое.

Замер на живом сайте (darial.co.jp): 468 813 символов сырого текста → 234 085 уникальных. Ровно
половина промпта была одним и тем же меню; на восьми страницах текста не было вообще — только оно.

Fail-safe: если после вычитания от страницы осталось меньше `MIN_KEEP_CHARS`, возвращаем ОРИГИНАЛ.
Лучше отдать в LLM страницу с меню, чем пустую: на сайте-одностраничнике «шаблон» и «содержимое» —
одно и то же, и агрессивная чистка стёрла бы всё.
"""

from __future__ import annotations

from collections import Counter

MIN_PAGES = 5  # на меньшем корпусе частота ничего не значит
BOILERPLATE_SHARE = 0.5  # строка на ≥50% страниц ⇒ шаблон
MIN_KEEP_CHARS = 200  # осталось меньше — откатываемся к оригиналу (fail-safe)
MIN_LINE_CHARS = 3  # совсем короткие строки не считаем (шум разметки)


def boilerplate_lines(texts: list[str], *, share: float = BOILERPLATE_SHARE) -> set[str]:
    """Строки, встречающиеся не менее чем на `share` доле страниц (по НАЛИЧИЮ, не по числу вхождений)."""
    if len(texts) < MIN_PAGES:
        return set()
    counts: Counter[str] = Counter()
    for t in texts:
        seen = {ln.strip() for ln in (t or "").splitlines() if len(ln.strip()) >= MIN_LINE_CHARS}
        counts.update(seen)
    threshold = max(2, int(len(texts) * share))
    return {line for line, n in counts.items() if n >= threshold}


def subtract(texts: list[str], *, share: float = BOILERPLATE_SHARE) -> list[str]:
    """Вернуть тексты без шаблонных строк. Страница, схлопнувшаяся почти в ноль, остаётся как была."""
    common = boilerplate_lines(texts, share=share)
    if not common:
        return list(texts)
    out: list[str] = []
    for t in texts:
        kept = [ln for ln in (t or "").splitlines() if ln.strip() not in common]
        cleaned = "\n".join(kept).strip()
        out.append(cleaned if len(cleaned) >= MIN_KEEP_CHARS else (t or ""))
    return out
