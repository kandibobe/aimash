"""Блок «последствия» для карточки L3: во что изменение обходится в сутки и в месяц.

**Каждое число здесь выведено из снимка, который карточка уже показала** (`params['_before']`,
аттестованный `ads/freshness.py`). Ни одного нового чтения Google Ads, ни одной цифры от модели.
Это не экономия, а единственный способ не соврать: сводка «40.00 → 48.00» и блок последствий
обязаны считаться из одних и тех же двух чисел, иначе на одной карточке окажутся два разных
«станет», и правило 15 будет репортить «выполнено» по одному из них.

Из этого же правила следуют два отказа, которые важнее того, что модуль считает:

* **Ставки (`update_bid`/`update_keyword_bid`) последствий не получают.** Ставка — цена клика, а не
  скорость расхода: чтобы перевести «+20% к ставке» в «+N в сутки», нужна модель аукциона, которой
  у нас нет. Любое число здесь было бы выдумано, а выдуманное число на денежной карточке хуже
  отсутствующего.
* **Доли от бюджета аккаунта и исторического CPA здесь нет.** Оба требуют ДОПОЛНИТЕЛЬНОГО чтения
  Ads в момент создания черновика; тогда часть чисел блока опиралась бы на снимок, а часть — на
  отдельное чтение, и инвариант «всё из одного снимка» перестал бы существовать как проверяемое
  свойство. Понадобятся — придут вместе со своим снимком в `_before`, а не мимо него.

Проекция — линейная экстраполяция и называется так в тексте пользователю. Слово «симуляция»
здесь запрещено намеренно: оно обещает модель поведения, которой нет.
"""

from __future__ import annotations

from dataclasses import dataclass

# Google считает месячный лимит списаний как «средний дневной бюджет × 30.4» — это ЕГО правило, а не
# наша округлённая «длина месяца». Держим ту же константу, иначе «за сколько дней выберется месячный
# бюджет» разошлось бы с тем, что клиент увидит в биллинге.
MONTH_DAYS = 30.4

# Горизонт проекции на графике и в строке «за 30 дней»: месяц — единица, в которой клиент планирует
# бюджет, и та же, в которой Google выставляет счёт.
PROJECTION_DAYS = 30


@dataclass(frozen=True, slots=True)
class Consequences:
    """Чистые числа (микро-единицы валюты аккаунта). Форматирует их `confirm.render`."""

    before_micros: int  # дневной бюджет «было»
    after_micros: int  # дневной бюджет «станет»
    delta_micros: int  # знак несёт направление: < 0 — снижение
    delta_pct: float | None  # None — от нуля процент не считается
    delta_horizon_micros: int  # Δ за PROJECTION_DAYS дней
    month_before_micros: int  # месячный потолок «было» (× MONTH_DAYS, правило Google)
    month_after_micros: int
    days_to_month: float | None  # за сколько дней новая скорость выберет ПРЕЖНИЙ месячный потолок
    currency: str
    shared_campaigns: tuple[str, ...]  # общий бюджет: кого ещё задевает изменение


def consequences(operation: str, params: dict | None = None) -> Consequences | None:
    """Последствия изменения дневного бюджета. `None` — считать не из чего (и печатать нечего).

    `None` возвращается молча и часто: снимок не прочитан, операция не бюджетная, значения
    нечисловые. Это нормальный путь, а не ошибка — блок последствий необязателен, а карточка без
    него остаётся полной по §5."""
    if operation != "update_budget" or not isinstance(params, dict):
        return None
    b = params.get("_before")
    if not isinstance(b, dict) or b.get("kind") != "budget":
        return None
    try:
        before = int(b["before_micros"])
        after = int(b["after_micros"])
    except (KeyError, TypeError, ValueError):
        return None
    if before < 0 or after < 0:
        return None
    delta = after - before
    pct = (delta * 100.0 / before) if before > 0 else None
    month_before = int(before * MONTH_DAYS)
    # «Прежний месячный потолок при новой скорости кончится за N дней» — считаем только при РОСТЕ:
    # при снижении фраза формально верна (дней больше 30), но отвечает на вопрос, которого никто не
    # задавал, и на карточке снижения читается как предупреждение о трате.
    days = (MONTH_DAYS * before / after) if after > before > 0 else None
    shared = tuple(str(x) for x in (b.get("shared_campaigns") or []))
    return Consequences(
        before_micros=before,
        after_micros=after,
        delta_micros=delta,
        delta_pct=pct,
        delta_horizon_micros=delta * PROJECTION_DAYS,
        month_before_micros=month_before,
        month_after_micros=int(after * MONTH_DAYS),
        days_to_month=days,
        currency=str(b.get("currency") or ""),
        shared_campaigns=shared,
    )


def projection_rows(cons: Consequences, days: int = PROJECTION_DAYS) -> list[tuple[int, int, int]]:
    """Накопленный расход по дням: (день, при прежнем бюджете, при новом) в микро.

    Прямые линии, и это честно названо в подписи листа: модели поведения кампании у нас нет, есть
    дневной потолок и арифметика. Расхождение двух линий на 30-м дне — ровно `delta_horizon_micros`
    из блока последствий, то есть график и текст не могут разойтись."""
    n = max(1, int(days))
    return [(d, cons.before_micros * d, cons.after_micros * d) for d in range(1, n + 1)]
