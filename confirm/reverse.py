"""Обратная операция из снимка «было»: детерминированный синтез компенсации.

Волна 4. Код переехал сюда из `bot/main.py` **без изменения поведения** — он нужен трём слоям,
а жил в архивируемом (`SPEC.md` §5.3): кнопка «↩️ Откатить» (`bot/handlers/confirm_flow.py`),
контур автооткатa (`scheduler/rollback.py`) и будущий `execute_compensation`. Оставь он в `bot/`,
и фоновый воркер тянул бы за собой aiogram — ровно та мина C4, которую сняли в Волне 1.

Главное свойство, которое здесь сохранено дословно: **функция честно возвращает `None`, когда
снимка недостаточно.** Разные ставки по группам, ключ без собственной ставки, старый снимок без
флага — во всех этих случаях один `set_to` вернул бы ЧИСЛО, но не СОСТОЯНИЕ. Молча «почти
откатить» чужой аккаунт хуже, чем не откатывать: человек уверен, что вернулся к исходному, а
вернулся к другому. `None` здесь — не пробел в реализации, а отказ врать.

Функция чистая: ни БД, ни сети, ни Google Ads. Её результат — ЗАЯВКА на обратную мутацию, которая
дальше идёт обычным путём через confirm-гейт. Провенанс она не выдаёт и выдать не может.
"""

from __future__ import annotations

# Только детерминированно обратимые операции (снимок `_before` достаточен для ОДНОЙ обратной
# мутации). update_bid откатывается лишь при ОДИНАКОВЫХ ставках групп (иначе один set_to не
# восстановит разнородные ставки — честно НЕ предлагаем). Геостратегия/bidding_strategy — не
# откатываются одной операцией (в backlog). Обратный черновик минтится как ОБЫЧНЫЙ proposal
# (confirm-гейт + user_initiated=True в `_present_proposal` → бюджет-откат легитимен, правило 3).
ROLLBACKABLE_OPS = frozenset(
    {
        "update_budget",
        "update_bid",
        "update_keyword_bid",
        "pause_campaign",
        "resume_campaign",
        "launch_campaign",
        "update_campaign",
        "set_campaign_network",
        "pause_ad_group",
        "resume_ad_group",
        "pause_ad",
        "resume_ad",
    }
)


def reverse_spec(operation: str, params: dict, before) -> tuple[str, dict] | None:
    """Собрать (обратная_операция, params) из снимка _before. None — необратимо/нет снимка/
    неоднозначно. Восстанавливаем прежнее значение/статус ПРОТИВОПОЛОЖНОЙ операцией."""
    if not isinstance(before, dict) or not isinstance(params, dict):
        return None
    camp = params.get("campaign")
    if not camp:
        return None
    kind = before.get("kind")
    if operation == "update_budget" and kind == "budget":
        micros = before.get("before_micros")
        if micros is None:
            return None
        return ("update_budget", {"campaign": camp, "mode": "set_to", "value": int(micros) / 1e6})
    if operation == "update_bid" and kind == "bid":
        uniq = {int(x) for x in (before.get("before_micros") or [])}
        if len(uniq) != 1:  # разные ставки по группам — одним set_to не вернуть (честно skip)
            return None
        return ("update_bid", {"campaign": camp, "mode": "set_to", "value": next(iter(uniq)) / 1e6})
    if operation == "update_keyword_bid" and kind == "keyword_bid":
        kw = params.get("keyword")
        uniq = {int(x) for x in (before.get("before_micros") or [])}
        if not kw or len(uniq) != 1:  # ключ в разных группах со РАЗНЫМИ ставками — одним set_to
            return None  # прежние не вернуть (честно не предлагаем откат)
        # У ключа могло НЕ БЫТЬ своей ставки (наследовал группу) — «было» тогда = ставка группы.
        # set_to завёл бы критерию СОБСТВЕННУЮ ставку: числом то же, состоянием — другое (группа
        # больше не управляет ключом). Это не откат. Нет флага (старый снимок) → тоже не предлагаем.
        own = before.get("own_bid")
        if not isinstance(own, list) or len(own) != len(before.get("before_micros") or []):
            return None
        if not all(bool(x) for x in own):
            return None
        spec = {"campaign": camp, "keyword": kw, "mode": "set_to", "value": next(iter(uniq)) / 1e6}
        for narrow in (
            "ad_group",
            "match_type",
        ):  # сужения черновика сохраняем: откат адресует ТЕ ЖЕ ключи
            if params.get(narrow):
                spec[narrow] = params[narrow]
        return ("update_keyword_bid", spec)
    if operation == "set_campaign_network" and kind == "network":
        return (
            "set_campaign_network",
            {"campaign": camp, "search_partners": bool(before.get("before_search_partners"))},
        )
    if operation == "update_campaign" and kind == "name":
        old, new = before.get("before_name"), params.get("new_name")
        if not old or not new:
            return None
        return (
            "update_campaign",
            {"campaign": new, "new_name": old},
        )  # текущее имя new → назад в old
    if kind == "status":  # восстановить before_status противоположной операцией
        bs = (before.get("before_status") or "").upper()
        if operation in ("pause_campaign", "resume_campaign", "launch_campaign"):
            if bs == "PAUSED":
                return ("pause_campaign", {"campaign": camp})
            if bs == "ENABLED":
                return ("resume_campaign", {"campaign": camp})
        elif operation in ("pause_ad_group", "resume_ad_group"):
            ag = params.get("ad_group")
            if ag and bs == "PAUSED":
                return ("pause_ad_group", {"campaign": camp, "ad_group": ag})
            if ag and bs == "ENABLED":
                return ("resume_ad_group", {"campaign": camp, "ad_group": ag})
        elif operation in ("pause_ad", "resume_ad"):
            ag, ad = params.get("ad_group"), params.get("ad")
            if ag and ad and bs == "PAUSED":
                return ("pause_ad", {"campaign": camp, "ad_group": ag, "ad": ad})
            if ag and ad and bs == "ENABLED":
                return ("resume_ad", {"campaign": camp, "ad_group": ag, "ad": ad})
    return None
