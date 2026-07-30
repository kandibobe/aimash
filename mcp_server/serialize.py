"""Сериализация dataclass-результатов ридеров → JSON-совместимые dict для MCP-конверта.

Форму берём из bot-рендеров (`_make_audit_drill` и т.п.), НЕ импортируя их (там aiogram). Числа
метрик (cost/CTR/CPC/CPA/ROAS) считает КОД — через свойства `reports.queries.Metrics`, не модель
(§8.1). Раунды — как в METRIC_FORMATS: деньги/ценность 2 знака, CTR — доля (4 знака), счётчики целые.

Всё здесь — чистые функции без побочек и без обращения к SDK/БД (вызываются уже над готовыми
объектами). Пустые списки/словари сериализуются как есть («прочитали, ничего нет» — валидный факт).
"""

from __future__ import annotations

from typing import Any

from core.texts import mask_email  # общая с алертом джобы: одна маскировка на оба выхода наружу


def metrics_dict(m) -> dict[str, Any]:
    """reports.queries.Metrics → dict. Производные (ctr/avg_cpc/cpa/roas) считает КОД (свойства m)."""
    return {
        "impressions": int(m.impressions),
        "clicks": int(m.clicks),
        "ctr": round(m.ctr, 4),  # доля 0..1 (как METRIC_FORMATS "0.00%")
        "avg_cpc": round(m.avg_cpc, 2),
        "cost": round(m.cost, 2),
        "conversions": round(m.conversions, 2),
        "conv_value": round(m.conv_value, 2),
        "cpa": round(m.cpa, 2),
        "roas": round(m.roas, 2),
    }


def breakdown_rows(bd) -> list[dict[str, Any]]:
    """reports.queries.Breakdown → список строк. Каждая: измерения (по dim_headers) + метрики.
    dim_headers — RU-заголовки (система RU); значения-измерения зипуются с ними по позиции."""
    out: list[dict[str, Any]] = []
    for dims, m in bd.rows:
        out.append(
            {
                "dimensions": dict(zip(bd.dim_headers, dims)),
                "metrics": metrics_dict(m),
            }
        )
    return out


def breakdown_extra(bd) -> dict[str, Any]:
    """Верхнеуровневые поля конверта для Breakdown: заголовок + пометка усечения (без тихих обрезаний)."""
    extra: dict[str, Any] = {"title": bd.title, "dimensions_schema": list(bd.dim_headers)}
    if bd.note:
        extra["note"] = bd.note
    return extra


def search_term_dict(r) -> dict[str, Any]:
    return {
        "search_term": r.search_term,
        "campaign": r.campaign,
        "ad_group": r.ad_group,
        "keyword": r.keyword,
        "match_type": r.match_type,
        "metrics": metrics_dict(r.metrics),
    }


def impression_share_dict(r) -> dict[str, Any]:
    """ImpressionShareRow: доли 0..1 (search_is/потери), НЕ micros — отдаём как есть (округл. до 4)."""
    return {
        "campaign_id": r.campaign_id,
        "campaign_name": r.campaign_name,
        "channel_type": r.channel_type,
        "search_is": round(float(r.search_is), 4),
        "budget_lost_is": round(float(r.budget_lost_is), 4),
        "rank_lost_is": round(float(r.rank_lost_is), 4),
    }


def negative_keyword_dict(r) -> dict[str, Any]:
    return {
        "scope": r.scope,
        "campaign": r.campaign,
        "ad_group": r.ad_group,
        "text": r.text,
        "match_type": r.match_type,
        "list_name": r.list_name,
    }


def negatives_payload(info) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """NegativeKeywordsInfo → (rows, extra). shared_attachments: frozenset → list (JSON-сериализуемо)."""
    rows = [negative_keyword_dict(r) for r in info.rows]
    extra = {
        "shared_attachments": {k: sorted(v) for k, v in info.shared_attachments.items()},
    }
    return rows, extra


def child_account_dict(c) -> dict[str, Any]:
    return {
        "id": c.id,
        "name": c.name,
        "currency": c.currency,
        "manager": bool(c.manager),
        "level": int(c.level),
        "status": c.status,
    }


def keyword_idea_dict(k) -> dict[str, Any]:
    return {
        "text": k.text,
        "avg_monthly_searches": int(k.avg_monthly_searches),
        "competition": k.competition,
        "competition_index": int(k.competition_index),
        "low_bid": round(float(k.low_bid), 2),
        "high_bid": round(float(k.high_bid), 2),
        "avg_cpc": round(float(k.avg_cpc), 2),
        "peak_month": k.peak_month,
    }


def budget_dict(b) -> dict[str, Any]:
    return {
        "campaign_id": b.campaign_id,
        "campaign": b.campaign,
        "channel_type": b.channel_type,
        "status": b.status,
        "budget": round(float(b.budget), 2),
    }


def recent_action_dict(a) -> dict[str, Any]:
    """db.history.RecentAction → dict. decided_at → ISO-строка (JSON) или None."""
    return {
        "confirmation_id": a.confirmation_id,
        "operation": a.operation,
        "params": a.params,
        "summary": a.summary,
        "decided_at": a.decided_at.isoformat() if a.decided_at else None,
    }


def change_event_dict(r) -> dict[str, Any]:
    """reports.queries.ChangeEventRow → dict. `user_email` МАСКИРУЕТСЯ (`core.texts.mask_email` —
    одна маскировка на ОБА выхода наружу: конверт инструмента и алерт джобы в Telegram; пока их было
    две, дайджест рассылал рабочие почты сотрудников клиента целиком).
    `via_api` считает КОД (свойство строки), чтобы модель не разбирала enum-строку сама."""
    return {
        "changed_at": r.changed_at,  # в таймзоне аккаунта — так отдаёт Google
        "resource_type": r.resource_type,
        "operation": r.operation,
        "client_type": r.client_type,
        "via_api": bool(r.via_api),
        "user_email": mask_email(r.user_email),
        "resource_name": r.resource_name,
        "changed_fields": list(r.changed_fields),
    }


def finding_dict(f) -> dict[str, Any]:
    """audit.engine.Finding → компактный dict. Числа агенту несёт facts (→ code_numbers).
    `evidence` СОЗНАТЕЛЬНО НЕ отдаём: это внутренний канал advisor (ранжирование rules._magnitude,
    сборка one-tap-параметров apply.one_tap_params, baseline outcome), а не факты для модели —
    что агент обязан видеть, движок кладёт в `facts`. one_tap считает КОД (f.one_tap), не модель."""
    return {
        "check_id": f.check_id,
        "family": f.family,
        "severity": f.severity,
        "at_risk": round(float(f.at_risk), 2),
        "target_campaign": f.target_campaign,
        "suggested_operation": f.suggested_operation if f.one_tap else None,
        "one_tap": bool(f.one_tap),
        "facts": f.facts,
    }


def period_dict(p) -> dict[str, Any]:
    """reports.period.Period → dict. `days` — производное (свойство Period), считает КОД."""
    return {
        "date_from": p.date_from.isoformat(),
        "date_to": p.date_to.isoformat(),
        "label": p.label,
        "days": int(p.days),
    }


def _mcc_extra(x) -> dict[str, Any]:
    """Общая для MccSummary/MccDeep бухгалтерия обхода MCC.

    `skipped`/`managers`/`inactive`/`errors` отдаются ВСЕГДА, даже пустыми: они и есть ответ на
    вопрос «почему аккаунта нет в строках». Молчаливое отсутствие модель читает как «аккаунт
    отсутствует у клиента», а не как «мы его не читали» — а это разные выводы про чужие деньги
    (`reports/mcc.py` собирает их ровно затем: «без тихого замалчивания»)."""
    return {
        "manager_id": x.manager_id,
        "period": period_dict(x.period),
        "skipped": list(x.skipped),
        "managers": list(x.managers),
        "inactive": [child_account_dict(c) for c in x.inactive],
        "errors": [{"account_id": aid, "reason": reason} for aid, reason in x.errors],
    }


def mcc_summary_payload(s) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """reports.mcc.MccSummary → (строки-дочерние, extra). Строка = один read-allowed ENABLED лист.

    Форма (rows, extra) — та же, что у `audit_payload`, и она не косметическая: `envelope.ok`
    ПАГИНИРУЕТ первый аргумент. Отдать сюда цельный dict нельзя — `paginate` сделает ему срез
    (`rows[offset:offset+limit]`) и упадёт на TypeError, а `_guarded` превратит падение в
    error-конверт, то есть инструмент молча не работал бы никогда. Дочерние аккаунты — это
    естественный список, `subtotals` и бухгалтерия обхода — верхний уровень.

    Валютные подытоги БЕЗ FX (`aggregate_by_currency`): суммировать разные валюты нельзя, и
    отдельный подытог на валюту — единственная честная агрегация. Производные внутри подытога
    (CTR/CPA/ROAS) корректны, потому что считаются из суммы сырых счётчиков, а не усреднением."""
    rows: list[dict[str, Any]] = []
    for cr in s.children:
        row: dict[str, Any] = {
            "account": child_account_dict(cr.account),
            "totals": metrics_dict(cr.totals),
        }
        if cr.active_campaigns is not None:
            row["active_campaigns"] = int(cr.active_campaigns)
        if cr.campaign_status:
            row["campaign_status"] = dict(cr.campaign_status)
        if cr.health_score is not None:
            row["health"] = {
                "score": int(cr.health_score),
                "grade": cr.health_grade,
                # `is not None`, а не truthy: at_risk == 0.0 — это «под риском ничего», РЕЗУЛЬТАТ
                # аудита. Свернув его в None, инструмент сообщил бы «аудит не прогонялся».
                "at_risk": (
                    round(float(cr.health_at_risk), 2) if cr.health_at_risk is not None else None
                ),
                "date": cr.health_date,
                "stale": bool(cr.health_stale),
            }
        rows.append(row)

    extra = _mcc_extra(s)
    extra["subtotals"] = [
        {"currency": st.currency, "accounts": int(st.accounts), "totals": metrics_dict(st.totals)}
        for st in s.subtotals
    ]
    return rows, extra


def mcc_deep_payload(
    d, *, breakdown_keys: tuple[str, ...] = ("campaign",)
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """reports.mcc.MccDeep → (строки-дочерние, extra). Строка = лист + его ПОЛНЫЙ ReportData.

    Отдаём: `totals`, `prev_totals` (предыдущий равный период — именно за ним и ходят в deep) и
    разбивки из `breakdown_keys`. `build_mcc_deep_async` собирает все семь разбивок §9 на каждый
    аккаунт независимо от того, сколько мы сериализуем, — но выложить все семь × N аккаунтов в
    контекст модели значит утопить в них ответ. Поэтому по умолчанию только `campaign`: она
    отвечает на вопрос, ради которого зовут deep («в каком аккаунте и какой кампании сдвиг»),
    дальше агент идёт точечно `get_campaign_stats`/`get_adgroup_stats` по одному аккаунту.

    Невыложенные разбивки перечислены в `breakdowns_omitted` — молча их скрыть нельзя: модель
    прочитала бы отсутствие как «данных нет», а они прочитаны и оплачены запросом."""
    rows: list[dict[str, Any]] = []
    for ch, report in d.items:
        wanted = [bd for bd in report.breakdowns if bd.key in breakdown_keys]
        row: dict[str, Any] = {
            "account": child_account_dict(ch),
            "currency": report.currency,
            "totals": metrics_dict(report.totals),
            "prev_totals": metrics_dict(report.prev_totals) if report.prev_totals else None,
            "breakdowns": [
                {"key": bd.key, **breakdown_extra(bd), "rows": breakdown_rows(bd)} for bd in wanted
            ],
            "breakdowns_omitted": [
                bd.key for bd in report.breakdowns if bd.key not in breakdown_keys
            ],
        }
        rows.append(row)
    return rows, _mcc_extra(d)


def audit_payload(a) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """audit.engine.AuditResult → (findings, extra). extra несёт score/grade/итоги; findings — rows.
    families отдаём как есть (family→счётчики/at_risk/penalty), google_recommendations — агрегат
    «тип Google-рекомендации → число активных». Тяжёлые срезы (report/audit_tables) НЕ включаем —
    это бумага для выгрузки, не для инструмента."""
    rows = [finding_dict(f) for f in a.findings]
    extra = {
        "customer_id": a.customer_id,
        "currency": a.currency,
        "score": a.score,
        "grade": a.grade,
        "total_spend": round(float(a.total_spend), 2),
        "at_risk": round(float(a.at_risk), 2),
        "has_activity": bool(a.has_activity),
        "optimization_score": a.optimization_score,
        "account_status": a.account_status,
        "measurement_gap": bool(a.measurement_gap),
        "lost_opportunity": round(float(a.lost_opportunity), 2),
        "score_model_version": a.score_model_version,
        "families": a.families,
        # Тип Google-рекомендации → число активных (AuditResult.google_recommendations). Дублирует
        # facts находки google_recommendations_pending НАМЕРЕННО: та находка severity=info и
        # at_risk=0 ⇒ сортируется в самый хвост (engine.build_audit findings.sort) и на аккаунте с
        # >50 находками в первую страницу конверта (DEFAULT_LIMIT=50) не попадает вовсе.
        "google_recommendations": a.google_recommendations,
    }
    return rows, extra


# ── §6.1: структура и настройки кампаний («было» для правок, не метрики) ───────────


def campaign_dict(c: dict[str, Any]) -> dict[str, Any]:
    """ads.read.list_campaigns отдаёт уже dict — копируем ПОЛЯМИ, а не ссылкой.

    Ридер возвращает свой изменяемый список; отдать его наружу как есть значит пустить структуру
    ридера в конверт целиком и молча начать отдавать любое поле, которое там однажды появится.
    Явное перечисление — контракт инструмента: он не меняется от правки ридера, а ломается тестом."""
    return {"id": str(c["id"]), "name": c.get("name", ""), "status": c.get("status", "")}


def targeting_payload(t) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """ads.read.CampaignTargeting → (строки-критерии, extra).

    Каждый критерий — отдельная строка (`kind` + `value`), а не четыре списка: `envelope.ok`
    ПАГИНИРУЕТ первый аргумент (см. `mcc_summary_payload`), и цельный dict туда отдать нельзя.

    `all_locations`/`all_languages` считает КОД, и это не украшение. У Google пустой набор
    LOCATION/PROXIMITY значит «показ во ВСЕХ регионах», а не «таргетинг не прочитан», — пустые
    rows модель прочитала бы как «данных нет» и на этом основании предложила бы гео-правку с
    обратным смыслом. Флаг делает разницу явной.

    Исключения (`negative_location`) в счёт `all_locations` НЕ идут: «все регионы, кроме N» — это
    по-прежнему «все регионы» как база показа."""
    rows: list[dict[str, Any]] = [
        *({"kind": "location", "value": v} for v in t.locations),
        *({"kind": "negative_location", "value": v} for v in t.negative_locations),
        *({"kind": "proximity", "value": v} for v in t.proximity),
        *({"kind": "language", "value": v} for v in t.languages),
    ]
    extra = {
        "counts": {
            "locations": len(t.locations),
            "negative_locations": len(t.negative_locations),
            "proximity": len(t.proximity),
            "languages": len(t.languages),
        },
        "all_locations": not t.locations and not t.proximity,
        "all_languages": not t.languages,
    }
    return rows, extra


def campaign_config_payload(cfg) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """ads.read.CampaignConfig | None → (строки-группы, extra-кампания).

    `found` отдаётся ВСЕГДА: «кампания с таким именем не найдена» и «найдена, но групп нет» — разные
    факты, и различает их флаг, а не пустота rows (та же логика, что у `_mcc_extra`).

    Деньги отдаём ПАРОЙ (`*_micros` + округлённое): micros — то, из чего собирается «было» черновика
    (правило 4: округление по биллинг-единице делает код мутации, а не читатель), округлённое — то,
    что агент вправе процитировать человеку."""
    if cfg is None:
        return [], {"found": False}
    rows = [
        {
            "id": g.id,
            "name": g.name,
            "cpc_bid_micros": int(g.cpc_bid_micros),
            "cpc_bid": round(int(g.cpc_bid_micros) / 1_000_000, 2),
            "keywords": [{"text": k.text, "match_type": k.match_type} for k in g.keywords],
            "headlines": list(g.headlines),
            "descriptions": list(g.descriptions),
            "final_url": g.final_url,
            "path1": g.path1,
            "path2": g.path2,
        }
        for g in cfg.ad_groups
    ]
    extra = {
        "found": True,
        "campaign_id": cfg.id,
        "campaign": cfg.name,
        "status": cfg.status,
        "channel_type": cfg.channel_type,
        "budget_micros": int(cfg.budget_micros),
        "budget": round(int(cfg.budget_micros) / 1_000_000, 2),
        # Что в конфиг НЕ входит — говорим прямо: ридер собирает клонируемую часть, и гео/минус-слова/
        # стратегия/аудитории читаются другими инструментами. Без этой строки «нет в ответе» читается
        # как «нет в кампании» — на чужих деньгах это ошибка, а не неточность.
        "not_included": ["targeting", "negatives", "bidding_strategy", "audiences"],
    }
    return rows, extra


def bidding_dict(b) -> dict[str, Any]:
    """reports.queries.CampaignBidding → dict. `target_cpa` ридер уже отдаёт в валюте аккаунта.

    `manual_bidding` считает КОД: при автостратегии SDK отвергает мутацию ставки
    (BID_TYPE_AND_BIDDING_STRATEGY_MISMATCH, см. `ads/mutations.py:2103`). Флаг отвечает на «имеет ли
    смысл предлагать правку CPC» ДО того, как черновик создан и человека спросили.

    Множество ручных стратегий берём из `audit.bidscape`, а не литералом здесь: два независимых
    списка дали бы инструменту и аудиту разные ответы на «можно ли тут менять ставку»."""
    from audit.bidscape import MANUAL_BID_STRATEGIES

    return {
        "campaign_id": b.campaign_id,
        "campaign": b.name,
        "strategy_type": b.strategy_type,
        "target_cpa": round(float(b.target_cpa), 2) if b.target_cpa is not None else None,
        "manual_bidding": (b.strategy_type or "").upper() in MANUAL_BID_STRATEGIES,
    }


def audience_dict(a) -> dict[str, Any]:
    """ads.read.Audience → dict. `size` — size_for_display (0 = закрытый список/размер неизвестен)."""
    return {"resource_name": a.resource_name, "name": a.name, "size": int(a.size)}


def quota_payload(
    snap: dict[str, Any], *, account: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """core.quota.snapshot() → (строка запрошенного аккаунта, extra-глобальные счётчики).

    `by_account` целиком наружу НЕ отдаём: квота одна на токен разработчика, но её разрез по
    аккаунтам — это перечисление ЧУЖИХ аккаунтов и их активности, то есть кросс-клиентская утечка
    мимо И6 через служебную телеметрию. Инструмент отдаёт расход запрошенного (и уже прошедшего
    замок чтения) аккаунта плюс общие лимит/расход — их и так видит любой владелец токена.

    `remaining` считает КОД (лимит − расход, не ниже нуля): модель не должна выводить остаток
    вычитанием сама."""
    lim = int(snap.get("limit", 0) or 0)
    used = int(snap.get("used", 0) or 0)
    per = snap.get("by_account") or {}
    rows = [{"account": str(account), "ops": int(per.get(str(account), 0) or 0)}]
    extra = {
        "limit": lim,
        "used": used,
        "remaining": max(0, lim - used),
        "pct": round(float(snap.get("pct", 0.0) or 0.0), 4),
        "window_hours": int(snap.get("window_hours", 0) or 0),
    }
    return rows, extra
