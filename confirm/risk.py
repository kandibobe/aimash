"""Тир риска черновика — классификатор, который решает ФОРМУ вопроса, а не нужен ли вопрос.

Контракт, ради которого модуль вообще отделён (и который держит AST-гард
`tests/test_risk_tier_presentation_only.py`): **ни один модуль `ads/**` не импортирует
`confirm.risk`**. Тир не попадает в Ads-слой — ни в `ensure_allowed`, ни в провенанс, ни в одну
из девяти базовых проверок §2.2 — и никогда не может убрать человеческое подтверждение. Он влияет
ровно на четыре вещи, причём последние две только ужесточают существующий confirm-гейт:

1. полноту карточки (блок «последствия» — `confirm/consequences.py`),
2. наличие вложения с графиком проекции расхода,
3. срок жизни согласия (L3 — короче, `confirm/store.py::_l3_fresh` + `effective_ttl_hours`),
4. при включённом `FOUR_EYES_REQUIRED` — необходимость независимого голоса для выбранных тиров
   (по умолчанию L3) внутри того же CAS `ConfirmStore.claim`.

Почему именно так, а не «L1 = применять без человека» из исходной формулировки: классификатор
может ошибиться, и цена ошибки обязана быть ограничена. При этом контракте ошибка стоит лишнего
клика; при «L1 = авто» — чужих денег. Автоприменение поставляется отдельно, выключенным и на том
же гейте (Волна 6b), а не через эту функцию.

Монотонность: L1 ⊂ L2 ⊂ L3. Всё, что показано на L1, показано и на L3; всё, что требуется на L2,
требуется и на L3. Поэтому ошибка «недооценил» стоит меньше показанного, а не меньше проверок, а
ошибка «переоценил» — лишнего подтверждения. Из-за этого же неизвестность (нет снимка «было»)
классифицируется как L3, а не как L1: отсутствие сведений — не доказательство безопасности.
"""

from __future__ import annotations

TIER_L1 = "L1"
TIER_L2 = "L2"
TIER_L3 = "L3"
TIERS: tuple[str, str, str] = (TIER_L1, TIER_L2, TIER_L3)

# P1-6: необратимые удаления. ЕДИНСТВЕННЫЙ источник истины — раньше литерал жил в `bot/main.py`
# (кнопочный слой, архивируемый), и второй потребитель списка неизбежно завёл бы свою копию.
DESTRUCTIVE_OPS: frozenset[str] = frozenset(
    {"remove_campaign", "remove_ad_group", "remove_ad", "remove_keywords", "remove_asset_link"}
)

# Денежные операции со снимком «было → станет» в `_before` (ads/service.py::read_state).
# Зеркало `ads.resolve.MONEY_OPS`; импортом не берём намеренно — `confirm/**` не должен тянуть
# `ads/**` ради трёх строк, а дрейф ловит тест (`test_money_ops_mirror_ads_resolve`).
MONEY_OPS: frozenset[str] = frozenset({"update_budget", "update_bid", "update_keyword_bid"})

# Создание тратящей деньги сущности: обязательства есть, снимка «было» нет — сравнивать не с чем,
# поэтому не L3 (двух актов за каждую новую кампанию никто не просил), но и не L1.
_CREATE_OPS: frozenset[str] = frozenset(
    {
        "create_search_campaign",
        "create_gdn_campaign",
        "create_demand_gen_campaign",
        "create_video_campaign",
        "create_app_campaign",
    }
)

# Пороги в ПРОЦЕНТАХ относительного сдвига. Абсолютного порога здесь нет намеренно: суммы приходят
# в микро-единицах валюты аккаунта, а «100 000 000 микро» — это 100 USD и 100 UAH одновременно.
# Абсолютный порог без знания валюты — это не строгость, а совпадение.
L3_DELTA_PCT = 50.0
L2_DELTA_PCT = 5.0


def _pairs(before, after) -> list[tuple[int, int]]:
    """(было, станет) в микро — и для скаляра (бюджет), и для списка (ставки по группам/ключам).

    Списки разной длины срезаются по короткому: позиционная сверка — тот же порядок, что у
    `ads/service.py::_verify_*`, и «пар нет» честнее выдуманной пары."""
    if isinstance(before, list) and isinstance(after, list):
        out = []
        for b, a in zip(before, after):
            try:
                out.append((int(b), int(a)))
            except (TypeError, ValueError):
                continue
        return out
    try:
        return [(int(before), int(after))]
    except (TypeError, ValueError):
        return []


def max_delta_pct(before, after) -> float | None:
    """Наибольший по модулю относительный сдвиг, %. `None` — сравнивать нечем.

    Берётся МАКСИМУМ, а не среднее: «ставки в пяти группах +3%, в шестой +200%» — это риск шестой
    группы, и усреднение его прячет. `before == 0` при ненулевом `after` — рост из ничего, у него
    нет конечного процента: возвращаем `inf`, и вызывающий трактует это как L3."""
    pairs = _pairs(before, after)
    if not pairs:
        return None
    worst = 0.0
    for b, a in pairs:
        if b == a:
            continue
        if b <= 0:
            return float("inf")
        worst = max(worst, abs(a - b) * 100.0 / b)
    return worst


def risk_tier(operation: str, params: dict | None = None) -> str:
    """Тир риска по (операция, params). Чистая функция: ни БД, ни сети, ни времени.

    Чистота — не стиль, а требование двух вызывающих: тир считает тул-слой в момент создания
    черновика и ПЕРЕСЧИТЫВАЕТ курьер вложений через минуту из персистентной строки. Разойтись им
    не на чем ровно потому, что второго места, где решение принимается, нет."""
    if operation in DESTRUCTIVE_OPS:
        return TIER_L3  # необратимо: повторный вызов ошибку не отменяет
    if operation in MONEY_OPS:
        b = (params or {}).get("_before")
        if not isinstance(b, dict) or b.get("before_micros") is None:
            # Снимка нет (чтение не удалось) — размер изменения неизвестен. Fail-closed в сторону
            # более полного показа: неизвестность не есть малый риск.
            return TIER_L3
        if b.get("shared_campaigns"):
            # Общий бюджет: изменение задевает ЧУЖИЕ кампании, которых нет в тексте команды.
            return TIER_L3
        pct = max_delta_pct(b.get("before_micros"), b.get("after_micros"))
        if pct is None:
            return TIER_L3
        if pct >= L3_DELTA_PCT:
            return TIER_L3
        if pct >= L2_DELTA_PCT:
            return TIER_L2
        return TIER_L1
    if operation in _CREATE_OPS:
        return TIER_L2
    return TIER_L1
