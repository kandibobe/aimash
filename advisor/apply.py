"""One-tap «применить совет»: какая операция допустима в один тап и с какими params.

Чистые функции от рекомендации — ни Telegram, ни БД, ни Google Ads. Живут в `advisor/`, а не в
`bot/main.py`, потому что зовут их ДВА процесса: интерактивный /advise (bot) и плановый дайджест
(scheduler), а планировщик обязан подниматься без кнопочного слоя (SPEC.md §5.3, мина C4). Пока
функции лежали в `bot/main.py`, `scheduler/jobs.py` тянул их поздним импортом — цикл
`bot.main` ↔ `scheduler.jobs`, из-за которого планировщик нельзя было запустить отдельным процессом.

Allow-list берём из `audit.engine.ONE_TAP_OPS` — единственного bot-free источника этого множества.
Раньше боевым множеством было `bot/keyboards.ADVISE_APPLY_OPS = frozenset(_ADVISE_APPLY_LABELS)`,
то есть денежный гард выводился из СЛОВАРЯ ПОДПИСЕЙ КНОПОК: правка UI-строки меняла список
операций, применимых в один тап, а при архивации `bot/` множество исчезло бы вместе с подписями.
Теперь направление обратное — подписи обязаны покрывать операции (гард в
`tests/test_onetap_and_harvest.py`), а не задавать их.
"""

from __future__ import annotations

from audit.engine import ONE_TAP_OPS

__all__ = ["ONE_TAP_OPS", "one_tap_op", "one_tap_params"]


def one_tap_params(rec) -> dict | None:
    """Собрать params мутации из рекомендации для one-tap «применить». None — нельзя собрать.
    pause_campaign → {campaign}; add_negative_keywords → {campaign, keywords:[ключ], match_type};
    set_campaign_display_network → {campaign, display_network: False};
    set_campaign_geo_target_type → {campaign, geo_target_type: 'PRESENCE'}.

    ⚠️ Направление у последних двух — КОНСТАНТА, а не поле находки: одним тапом бот только ЧИНИТ
    (выключает КМС, сужает гео). Включить КМС или расширить гео до PRESENCE_OR_INTEREST можно лишь
    прямой командой человека — иначе кнопка «применить совет» стала бы способом РАСШИРИТЬ показы."""
    campaign = rec.target_campaign
    if not campaign:
        return None
    if rec.suggested_operation == "pause_campaign":
        return {"campaign": campaign}
    if rec.suggested_operation == "set_campaign_display_network":
        return {"campaign": campaign, "display_network": False}
    if rec.suggested_operation == "set_campaign_geo_target_type":
        return {"campaign": campaign, "geo_target_type": "PRESENCE"}
    if rec.suggested_operation == "add_negative_keywords":
        kw = (rec.evidence or {}).get("keyword")
        if not kw:
            return None
        # match_type из evidence: audit search-terms → 'exact' (режет только этот запрос, не шире);
        # дефолт 'broad' (advisor wasteful_keyword — как раньше). Схема add_negative_keywords ревалидирует.
        mt = (rec.evidence or {}).get("match_type") or "broad"
        return {"campaign": campaign, "keywords": [kw], "match_type": mt}
    if rec.suggested_operation == "remove_negative_keywords":
        # 3.2в: negative_keyword_conflicts — снять КАМПАНИЙНЫЙ минус, блокирующий свой ключ.
        # Текст и тип берём из находки как есть (снимаем ровно тот минус, что конфликтует).
        neg = (rec.evidence or {}).get("negative")
        mt = (rec.evidence or {}).get("match_type")
        if not neg or not mt:
            return None
        return {"campaign": campaign, "keywords": [neg], "match_type": mt}
    return None


def one_tap_op(rec) -> str | None:
    """Операция для кнопки «применить» под советом/находкой, либо None (кнопки нет).

    Два гейта: (1) allow-list ONE_TAP_OPS — деньги/ставки не в один тап (golden rule #3);
    (2) ИСПОЛНИМОСТЬ — params реально собираются из evidence. Второй нужен потому, что
    advisor.from_findings сливает advice_operation (метку для outcome-линковки) в
    rec.suggested_operation: у ngram_waste метка add_negative_keywords, но исполнимого ключа в
    evidence нет — гейт только по allow-list рисовал бы кнопку, вечно отвечающую «совет устарел»."""
    op = getattr(rec, "suggested_operation", None)
    if op not in ONE_TAP_OPS:
        return None
    return op if one_tap_params(rec) is not None else None
