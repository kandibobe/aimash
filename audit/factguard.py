"""Fact-guard для агентного нарратива аудита — ОБРАТНАЯ проверка чисел (крит.S3/C4).

`advisor/formulate._facts_preserved` ловит ПРОПУСК фактов (base ⊆ llm). Здесь — ловим ДОБАВЛЕНИЕ:
каждое ЗНАЧИМОЕ число нарратива обязано принадлежать множеству CODE-чисел (факты аудита ∪ выводы
drill-инструментов), с допуском округления. Модель вписала выдуманную цифру (галлюцинация денег/
метрик) → нарратив ОТВЕРГАЕТСЯ целиком, bot-слой откатывается на детерминированную карточку.

Дисциплина golden rule #4: числа считает КОД, нейросеть лишь ПЕРЕСКАЗЫВАЕТ готовые факты. Сочинить
цифру она структурно не сможет — на выходе фильтр. Не-факты (порядковые «1.», «топ-3», знаменатель
score «/100», годы/даты периода, одиночные малые счётчики) — не обязаны быть в данных (allowlist).

Порог значимости: токен с ≥2 цифрами обязан совпасть с CODE-числом (деньги/CPA/клики — всегда ≥2
цифр). Одноцифровые (0..9: порядковые/малые счётчики) — allowlist (низкий риск, часто и так в данных).
Чистая функция, без сети/SDK — полностью тестируемо.
"""

from __future__ import annotations

import re

# Токен-число в прозе: серия цифр с внутренними пробелами/запятыми/точками-разделителями.
# Границы \b не годятся (пробел-разделитель тысяч), поэтому едим цифру-...-цифру или одиночную цифру.
_NUM_TOKEN = re.compile(r"\d[\d  .,]*\d|\d")
# Разрешённые не-факты: знаменатель score и типичные порядковые «топ-3». Годы — отдельной проверкой.
_ALLOW_CONSTANTS = frozenset({0.0, 1.0, 2.0, 3.0, 100.0})


def _num_candidates(token: str) -> set[float]:
    """Возможные числовые значения одного токена прозы. Пробуем обе трактовки запятой (разделитель
    тысяч vs десятичная) — membership-проверка от этого лишь чуть мягче (в пользу валидного нарратива;
    строгий случай ловит fallback). Токены-«телефоны»/id тоже парсятся как целые (это ОК — id в данных)."""
    t = token.strip().replace(" ", "").replace(" ", "")
    if not t:
        return set()
    cands: set[float] = set()

    def _add(s: str) -> None:
        try:
            cands.add(round(float(s), 2))
        except ValueError:
            pass

    _add(t.replace(",", ""))  # запятая = разделитель тысяч
    if "," in t and "." not in t:  # запятая = десятичная (европейский формат)
        _add(t.replace(",", "."))
    _add(t)
    return cands


def _digit_count(token: str) -> int:
    return sum(c.isdigit() for c in token)


def collect_numbers(obj) -> set[float]:
    """Все числовые значения из произвольной JSON-структуры (факты аудита / выводы инструментов) —
    множество CODE-чисел, которым дозволено появляться в нарративе. Строки тоже парсятся (имена
    кампаний с числами → их числа citeable). bool не считаем числом."""
    out: set[float] = set()

    def _walk(v) -> None:
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            out.add(round(float(v), 2))
        elif isinstance(v, dict):
            for x in v.values():
                _walk(x)
        elif isinstance(v, (list, tuple, set)):
            for x in v:
                _walk(x)
        elif isinstance(v, str):
            for m in _NUM_TOKEN.finditer(v):
                out.update(_num_candidates(m.group(0)))

    _walk(obj)
    return out


def _matches(val: float, code: set[float], *, tol_rel: float = 0.02, tol_abs: float = 0.5) -> bool:
    """val принадлежит множеству CODE-чисел с допуском округления (относит. 2% или абс. 0.5)."""
    for c in code:
        if abs(val - c) <= max(tol_abs, tol_rel * abs(c)):
            return True
    return False


def _is_year(token: str, val: float) -> bool:
    """4-значный «год» (2000..2099) — часть даты/периода, не денежный факт."""
    return _digit_count(token) == 4 and 1990.0 <= val <= 2099.0 and val == int(val)


def narrative_facts_preserved(narrative: str, code_numbers: set[float]) -> bool:
    """True, если КАЖДЫЙ значимый (≥2 цифр) числовой токен нарратива совпадает с CODE-числом
    (± округление), либо это дозволенный не-факт (год/знаменатель/малый счётчик). Иначе — False
    (нарратив отвергается → детерминированный fallback). Пустой нарратив → False (нечего показывать)."""
    text = (narrative or "").strip()
    if not text:
        return False
    for m in _NUM_TOKEN.finditer(text):
        token = m.group(0)
        if _digit_count(token) < 2:
            continue  # одиночная цифра — порядковый/малый счётчик, не проверяем
        cands = _num_candidates(token)
        if not cands:
            continue
        ok = any(
            (v in _ALLOW_CONSTANTS) or _is_year(token, v) or _matches(v, code_numbers)
            for v in cands
        )
        if not ok:
            return False
    return True
