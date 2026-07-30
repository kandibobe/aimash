"""23 READ-обёртки MCP-слоя над существующими ридерами (Контур A, инкремент «MCP READ»).

Каждая обёртка:
  1) проходит замок ЧТЕНИЯ на ГРАНИЦЕ слоя — `_guarded` требует `account=` и зовёт
     `ensure_read_allowed`/`ensure_manager_allowed` ДО тела инструмента (см. ниже);
  2) разбирает период из `date_from/date_to|period_days` — ДО клиента, потому что разбор аргументов
     не имеет права зависеть от сети, — и только потом строит `build_client_async(account)`;
  3) зовёт ридер через `core.resilience.run_ads_read_call` (таймаут/ретрай/квота/to_thread) — как
     это уже делают `_do_read`/`gather_audit`;
  4) сериализует результат (`mcp_server.serialize`) в единый конверт (`mcp_server.envelope.ok`);
  5) ловит ЛЮБОЕ исключение (`_guarded`) → редактированный error-конверт (`envelope.err`), наружу
     сырой `str(e)` не пускает (правило 5; FastMCP иначе кладёт его в ToolError).

Замок на границе, а не только внутри ридера. Ридеры Google Ads держат `ensure_read_allowed` первой
строкой, и обёртка над таким ридером наследовала бы его сама. Но ридеры НАШЕЙ БД (`db/history.py`,
`clients/store.py`, `audit/*`) не имеют замка ни одного — обёртка над ними без явной проверки читает
чужой аккаунт. Раньше это лечилось вручную (`get_change_history`), то есть по памяти автора. Теперь
замок неотделим от `_guarded`: `account` — обязательный keyword-аргумент, обёртку без него нельзя
даже вызвать. Дублирование с ридером сознательное (defense in depth): проверка in-memory, стоит
наносекунды, а снимает целый класс «новая обёртка забыла замок».

Кросс-аккаунтность: у бот-схем `ANALYSIS_TOOLS` намеренно НЕТ `account` (бот залочен на один
аккаунт); MCP кросс-аккаунтный → `account` обязателен у всех, кроме `list_accounts` (там `manager_id`).
Мутаций тут нет по построению — только READ (инвариант И4 проверяет `server.py`).
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any, Awaitable, Callable

from ads.client import build_client_async, ensure_manager_allowed, ensure_read_allowed
from ads.keyword_plan import generate_keyword_ideas

# Имена инструментов реестра §6.1 совпадают с именами ридеров (`list_campaigns`,
# `read_campaign_targeting`, …). Ридеры импортируем под алиасами `_read_*`: имя инструмента —
# контракт с агентом, и переименовывать его, чтобы разойтись с ридером, нельзя.
from ads.read import (
    account_timezone,
    list_attached_audiences as _read_attached_audiences,
    list_audiences as _read_audiences,
    list_campaigns as _read_campaigns,
    list_child_accounts,
    read_campaign_config as _read_config,
    read_campaign_targeting as _read_targeting,
)
from audit.collect import gather_audit
from clients.store import ClientProfileStore
from core import observe  # Волна 3: вызов инструмента — прогон в журнале событий
from core.logging import log
from core.quota import snapshot as quota_snapshot
from core.resilience import run_ads_read_call
from db.history import list_recent_applied_by_customer
from mcp_server.envelope import DEFAULT_LIMIT, client_context, err, ok
from mcp_server.serialize import (
    audit_payload,
    audience_dict,
    bidding_dict,
    breakdown_extra,
    breakdown_rows,
    budget_dict,
    campaign_config_payload,
    campaign_dict,
    child_account_dict,
    impression_share_dict,
    keyword_idea_dict,
    mcc_deep_payload,
    mcc_summary_payload,
    negatives_payload,
    quota_payload,
    recent_action_dict,
    search_term_dict,
    targeting_payload,
)
from reports import period as pperiod
from reports.tz import account_period, reanchor
from reports.queries import (
    fetch_budgets,
    fetch_by_ad,
    fetch_by_ad_group,
    fetch_by_campaign,
    fetch_by_day,
    fetch_by_device,
    fetch_by_keyword,
    fetch_by_network,
    fetch_impression_share,
    fetch_negative_keywords,
    fetch_search_terms,
    read_campaign_bidding,
)


async def _guarded(
    work: Callable[[], Awaitable[dict[str, Any]]],
    *,
    account: str,
    manager: bool = False,
) -> dict[str, Any]:
    """Замок ЧТЕНИЯ на границе + выполнение тела; ЛЮБОЙ сбой → редактированный error-конверт
    (правило 5, fail-closed). В лог — только `type(e).__name__` (не str(e): исключение
    google-ads/OpenRouter может нести токен).

    `account` обязателен и keyword-only НАМЕРЕННО: обёртку, забывшую объявить читаемый аккаунт,
    нельзя вызвать вовсе — TypeError на первом же обращении, а не тихое чтение без замка.
    `manager=True` — адресация менеджерским id (обход MCC): другой чокпойнт, шире по смыслу
    (`ensure_manager_allowed`), пустой id там тоже отказ.

    Отказ замка приезжает как `error_code == "forbidden_account"` (`envelope.classify_error`) —
    инвариант `tests/test_hermes_isolation.py` ассертит именно код, а не текст.

    Волна 3 (event sourcing): тело выполняется внутри `observe.run_scope` — вызов инструмента обязан
    быть прогоном в журнале, иначе `ads_read`-события, которые `core.resilience` уже эмитит, в
    MCP-процессе падают в никуда (вне scope `record_event` — no-op). Отсюда берётся ответ на
    «что агент реально читал перед тем, как это предложить». Прогон закрывается статусом 'error',
    если тело бросило, — и до конверта `err` это доезжает неизменным.
    """
    try:
        if manager:
            ensure_manager_allowed(str(account))
        else:
            ensure_read_allowed(str(account))
        async with observe.run_scope("mcp_read"):
            return await work()
    except Exception as e:  # noqa: BLE001 — граница слоя: наружу только редактированное
        log.warning("mcp read tool failed: %s", type(e).__name__)
        return err(e)


def _period(date_from: str | None, date_to: str | None, period_days: int | None):
    """ISO-даты `date_from`+`date_to` (обе) → custom-период; ни одной → последние N дней (period_days).
    Арифметику дат считает КОД (reports.period), не модель.

    Одна дата без второй — ОТКАЗ, а не молчаливый откат на `last_n_days`. Прежнее поведение было
    fail-open в цифрах: модель просила «с 1 июля», получала период «последние 30 дней» и цитировала
    его как запрошенный — расхождение окна нигде не видно, потому что конверт отдаёт метрики без
    жалоб. Про чужие деньги такая тишина дороже отказа: `invalid_argument` заставляет назвать окно
    целиком (`envelope.classify_error`: ValueError → invalid_argument)."""
    if bool(date_from) != bool(date_to):
        raise ValueError(
            "период задаётся ЛИБО обеими датами (date_from и date_to), ЛИБО ни одной "
            "(тогда берётся period_days) — одна дата без второй трактовке не подлежит"
        )
    if date_from and date_to:
        return pperiod.custom(date.fromisoformat(date_from), date.fromisoformat(date_to))
    return pperiod.last_n_days(max(1, int(period_days or 30)))


async def _account_period(client, account: str, window):
    """Окно, пере-якоренное на «сегодня» АККАУНТА (§8, `reports.tz.account_period`).

    Без этого относительное окно строилось от даты ХОСТА: для аккаунта в чужой TZ (UG при вечернем
    прогоне из Европы — `DEFAULT_GEO_COUNTRY_CODE=UG`) последний полный день выпадал из окна, и цифры
    MCP расходились и с интерфейсом Google Ads, и с bot-путём на тех же данных. custom-диапазон (обе
    ISO-даты названы менеджером явно) `account_period` НЕ трогает; относительный (last_n) — пере-
    якоряет. Best-effort: TZ не прочитана → дата хоста (деградирует как раньше, отчёт не роняем).

    Готовое `window` аргументом, а НЕ сырые даты: разбор аргументов не имеет права зависеть от сети.
    Пока `_period` вызывался отсюда, порядок в теле был «клиент → период», и на неполном периоде
    наружу уезжал код от того, что сломалось РАНЬШЕ, — при недоступном OAuth `internal` вместо
    `invalid_argument`. Модель по такому конверту чинит не свой вызов, а несуществующую аварию.
    Порядок держит гард `tests/test_mcp_read_smoke.py::test_args_are_validated_before_the_network`."""
    return await account_period(client, str(account), window)


def _default_manager() -> str:
    """MCC по умолчанию для list_accounts — первый из настроенных (login_customer_id ∪ доп.). Пусто ⇒
    "" (ensure_manager_allowed отвергнет — fail-closed, не гадаем чужой MCC)."""
    from core.config import settings

    return next(iter(sorted(settings.login_customer_id_set)), "")


# ── Инструменты ───────────────────────────────────────────────────────────────────


async def list_accounts(
    manager_id: str | None = None, offset: int = 0, limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    """Дочерние аккаунты MCC (id/имя/валюта/статус/уровень). manager_id пуст ⇒ настроенный MCC.
    Замок: ensure_manager_allowed (перечисление чужого менеджера запрещено, GR#9)."""
    # mid считаем ДО `_guarded`: замок обхода MCC применяется к тому же id, что уйдёт в ридер.
    mid = str(manager_id) if manager_id else _default_manager()

    async def _work() -> dict[str, Any]:
        client = await build_client_async(mid)
        rows = await run_ads_read_call(
            list_child_accounts, client, mid, account=mid, label="mcp.list_accounts"
        )
        return ok([child_account_dict(c) for c in rows], offset=offset, limit=limit)

    return await _guarded(_work, account=mid, manager=True)


async def get_campaign_stats(
    account: str,
    date_from: str | None = None,
    date_to: str | None = None,
    period_days: int = 30,
    campaign_id: str | None = None,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Метрики по кампаниям аккаунта за период (показы/клики/CTR/CPC/расход/конверсии/CPA/ROAS —
    считает КОД). campaign_id сужает до одной кампании. Замок: ensure_read_allowed."""

    async def _work() -> dict[str, Any]:
        window = _period(date_from, date_to, period_days)
        client = await build_client_async(account)
        period = await _account_period(client, account, window)
        bd = await run_ads_read_call(
            fetch_by_campaign,
            client,
            str(account),
            period,
            campaign_id,
            account=str(account),
            label="mcp.get_campaign_stats",
        )
        return ok(breakdown_rows(bd), offset=offset, limit=limit, extra=breakdown_extra(bd))

    return await _guarded(_work, account=str(account))


async def get_adgroup_stats(
    account: str,
    date_from: str | None = None,
    date_to: str | None = None,
    period_days: int = 30,
    campaign_id: str | None = None,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Метрики по группам объявлений за период. campaign_id сужает до одной кампании. Замок:
    ensure_read_allowed."""

    async def _work() -> dict[str, Any]:
        window = _period(date_from, date_to, period_days)
        client = await build_client_async(account)
        period = await _account_period(client, account, window)
        bd = await run_ads_read_call(
            fetch_by_ad_group,
            client,
            str(account),
            period,
            campaign_id,
            account=str(account),
            label="mcp.get_adgroup_stats",
        )
        return ok(breakdown_rows(bd), offset=offset, limit=limit, extra=breakdown_extra(bd))

    return await _guarded(_work, account=str(account))


async def get_keywords(
    account: str,
    date_from: str | None = None,
    date_to: str | None = None,
    period_days: int = 30,
    campaign_id: str | None = None,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Метрики по ключевым словам за период (топ по расходу; ридер сам помечает усечение в note).
    Замок: ensure_read_allowed."""

    async def _work() -> dict[str, Any]:
        window = _period(date_from, date_to, period_days)
        client = await build_client_async(account)
        period = await _account_period(client, account, window)
        bd = await run_ads_read_call(
            fetch_by_keyword,
            client,
            str(account),
            period,
            campaign_id,
            account=str(account),
            label="mcp.get_keywords",
        )
        return ok(breakdown_rows(bd), offset=offset, limit=limit, extra=breakdown_extra(bd))

    return await _guarded(_work, account=str(account))


async def get_ads(
    account: str,
    date_from: str | None = None,
    date_to: str | None = None,
    period_days: int = 30,
    campaign_id: str | None = None,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Метрики по объявлениям за период (топ по расходу; усечение — в note ридера). Замок:
    ensure_read_allowed."""

    async def _work() -> dict[str, Any]:
        window = _period(date_from, date_to, period_days)
        client = await build_client_async(account)
        period = await _account_period(client, account, window)
        bd = await run_ads_read_call(
            fetch_by_ad,
            client,
            str(account),
            period,
            campaign_id,
            account=str(account),
            label="mcp.get_ads",
        )
        return ok(breakdown_rows(bd), offset=offset, limit=limit, extra=breakdown_extra(bd))

    return await _guarded(_work, account=str(account))


async def get_search_terms(
    account: str,
    date_from: str | None = None,
    date_to: str | None = None,
    period_days: int = 30,
    campaign_id: str | None = None,
    reader_limit: int = 200,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Поисковые запросы (search_term_view) за период — майнинг минус-слов/новых ключей.
    reader_limit — сколько строк тянуть из GAQL (топ по расходу). Замок: ensure_read_allowed."""

    async def _work() -> dict[str, Any]:
        window = _period(date_from, date_to, period_days)
        client = await build_client_async(account)
        period = await _account_period(client, account, window)
        rows = await run_ads_read_call(
            fetch_search_terms,
            client,
            str(account),
            period,
            campaign_id,
            int(reader_limit),
            account=str(account),
            label="mcp.get_search_terms",
        )
        return ok(
            [search_term_dict(r) for r in rows],
            offset=offset,
            limit=limit,
            reader_limit=int(reader_limit),  # LIMIT 200 в GAQL: усечение видно как reader_capped
        )

    return await _guarded(_work, account=str(account))


async def get_negatives(
    account: str,
    reader_limit: int = 5000,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Минус-слова трёх уровней (кампания/группа/shared) + карта привязки shared-списков к кампаниям.
    Без периода. Замок: ensure_read_allowed."""

    async def _work() -> dict[str, Any]:
        client = await build_client_async(account)
        info = await run_ads_read_call(
            fetch_negative_keywords,
            client,
            str(account),
            int(reader_limit),
            account=str(account),
            label="mcp.get_negatives",
        )
        rows, extra = negatives_payload(info)
        return ok(rows, offset=offset, limit=limit, extra=extra, reader_limit=int(reader_limit))

    return await _guarded(_work, account=str(account))


async def get_budgets(
    account: str, reader_limit: int = 500, offset: int = 0, limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    """Дневные бюджеты кампаний аккаунта (валюта аккаунта; делит КОД). Без периода. Замок:
    ensure_read_allowed."""

    async def _work() -> dict[str, Any]:
        client = await build_client_async(account)
        rows = await run_ads_read_call(
            fetch_budgets,
            client,
            str(account),
            int(reader_limit),
            account=str(account),
            label="mcp.get_budgets",
        )
        return ok(
            [budget_dict(b) for b in rows],
            offset=offset,
            limit=limit,
            reader_limit=int(reader_limit),
        )

    return await _guarded(_work, account=str(account))


async def get_auction_insights(
    account: str,
    date_from: str | None = None,
    date_to: str | None = None,
    period_days: int = 30,
    campaign_id: str | None = None,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Impression share по Search/Shopping-кампаниям (доля показов + потери по бюджету/рангу, доли
    0..1). Имена КОНКУРЕНТОВ через API недоступны (Google) — только собственный IS. Замок:
    ensure_read_allowed."""

    async def _work() -> dict[str, Any]:
        window = _period(date_from, date_to, period_days)
        client = await build_client_async(account)
        period = await _account_period(client, account, window)
        rows = await run_ads_read_call(
            fetch_impression_share,
            client,
            str(account),
            period,
            campaign_id,
            account=str(account),
            label="mcp.get_auction_insights",
        )
        return ok([impression_share_dict(r) for r in rows], offset=offset, limit=limit)

    return await _guarded(_work, account=str(account))


async def get_account_audit(
    account: str,
    date_from: str | None = None,
    date_to: str | None = None,
    period_days: int = 30,
    target_cpa: float | None = None,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Полный аудит аккаунта за период: score/grade/итоги + находки (worst-first). gather_audit
    сам асинхронен и внутри проходит замки ЧТЕНИЯ на под-фетчах. rows = находки, extra = сводка."""

    async def _work() -> dict[str, Any]:
        window = _period(date_from, date_to, period_days)
        client = await build_client_async(account)
        period = await _account_period(client, account, window)
        result = await gather_audit(client, str(account), period, target_cpa=target_cpa)
        rows, extra = audit_payload(result)
        return ok(rows, offset=offset, limit=limit, extra=extra)

    return await _guarded(_work, account=str(account))


async def get_change_history(
    account: str,
    operation: str | None = None,
    reader_limit: int = 20,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """История ПРИМЕНЁННЫХ ботом операций по аккаунту (наш audit-trail из proposals, НЕ Google Ads
    change-history). operation сужает по типу. Замок: ensure_read_allowed на границе `_guarded`
    (И6 — ридер НАШЕЙ БД своего замка не имеет, граница здесь единственная защита)."""

    async def _work() -> dict[str, Any]:
        actions = await list_recent_applied_by_customer(
            str(account), operation=operation, limit=int(reader_limit)
        )
        return ok(
            [recent_action_dict(a) for a in actions],
            offset=offset,
            limit=limit,
            reader_limit=int(reader_limit),
        )

    return await _guarded(_work, account=str(account))


async def keyword_ideas(
    account: str,
    seeds: list[str] | None = None,
    url: str | None = None,
    language: str | None = None,
    geo_ids: list[int] | None = None,
    reader_limit: int | None = None,
    network: str | None = None,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Идеи ключевых слов (Keyword Planner) по сид-ключам и/или URL. Синхронный ридер → to_thread.
    Незаданные параметры не передаём — берётся дефолт ридера (язык/гео/сеть). Замок:
    ensure_read_allowed."""

    async def _work() -> dict[str, Any]:
        client = await build_client_async(account)
        kw: dict[str, Any] = {}
        if seeds is not None:
            kw["seeds"] = list(seeds)
        if url:
            kw["url"] = url
        if language:
            kw["language"] = language
        if geo_ids:
            kw["geo_ids"] = tuple(int(g) for g in geo_ids)
        if reader_limit:
            kw["limit"] = int(reader_limit)
        if network:
            kw["network"] = network
        ideas = await run_ads_read_call(
            generate_keyword_ideas,
            client,
            str(account),
            account=str(account),
            label="mcp.keyword_ideas",
            **kw,
        )
        # reader_limit объявляем только если вызывающий его задал — иначе кэп у ридера свой (дефолт),
        # и мы его не знаем, а гадать нельзя (ложный reader_capped хуже отсутствия сигнала).
        return ok(
            [keyword_idea_dict(k) for k in ideas],
            offset=offset,
            limit=limit,
            reader_limit=int(reader_limit) if reader_limit else None,
        )

    return await _guarded(_work, account=str(account))


# ── §6.1: структура и настройки кампаний («было» для правок, не метрики) ───────────


async def list_campaigns(
    account: str,
    channel_type: str | None = None,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Кампании аккаунта (id/имя/статус), активные первыми; REMOVED не показываются. `channel_type`
    (напр. 'SEARCH') сужает до одного канала. Без периода и без метрик. Замок: ensure_read_allowed.

    Зачем отдельно от `get_campaign_stats`: тот ходит с `segments.date` и возвращает ТОЛЬКО кампании,
    у которых в окне были показы. Приостановленная, только что созданная или не тратившая кампания в
    нём отсутствует — и модель читает это как «такой кампании нет». Здесь список полный. Он же даёт
    ИМЯ для `read_campaign_config` (тот ищет по имени, а не по id)."""

    async def _work() -> dict[str, Any]:
        client = await build_client_async(account)
        rows = await run_ads_read_call(
            _read_campaigns,
            client,
            str(account),
            account=str(account),
            label="mcp.list_campaigns",
            channel_type=channel_type or None,
        )
        return ok([campaign_dict(c) for c in rows], offset=offset, limit=limit)

    return await _guarded(_work, account=str(account))


async def read_campaign_targeting(
    account: str,
    campaign_id: str,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Текущее ГЕО (локации, исключения, радиусы) и языки кампании — «было» для гео-правок.
    Строка = один критерий (`kind`: location | negative_location | proximity | language, + `value`
    человекочитаемым именем). Замок: ensure_read_allowed.

    Пустой список критериев НЕ значит «не прочитали»: у Google это «показ во всех регионах / на всех
    языках». Разницу отдаёт КОД флагами `all_locations`/`all_languages` — читать её из пустоты rows
    нельзя."""

    async def _work() -> dict[str, Any]:
        client = await build_client_async(account)
        t = await run_ads_read_call(
            _read_targeting,
            client,
            str(account),
            str(campaign_id),
            account=str(account),
            label="mcp.read_campaign_targeting",
        )
        rows, extra = targeting_payload(t)
        return ok(rows, offset=offset, limit=limit, extra=extra)

    return await _guarded(_work, account=str(account))


async def read_campaign_config(
    account: str,
    campaign_name: str,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Настройки кампании ПО ИМЕНИ + группы объявлений с их CPC, ключами и RSA-текстами. Строка =
    группа объявлений; кампания/бюджет/канал — верхний уровень конверта. Замок: ensure_read_allowed.

    Имя, а не id, — контракт ридера (`ads/read.py:409`); список имён даёт `list_campaigns`.
    Кампания не найдена ⇒ `found=false` и пустые rows (это НЕ ошибка вызова). Что в ответ не входит,
    перечислено в `not_included`: гео читает `read_campaign_targeting`, минус-слова `get_negatives`,
    стратегию `get_bidding_strategy` — «нет в ответе» и «нет в кампании» это разные факты."""

    async def _work() -> dict[str, Any]:
        client = await build_client_async(account)
        cfg = await run_ads_read_call(
            _read_config,
            client,
            str(account),
            str(campaign_name),
            account=str(account),
            label="mcp.read_campaign_config",
        )
        rows, extra = campaign_config_payload(cfg)
        return ok(rows, offset=offset, limit=limit, extra=extra)

    return await _guarded(_work, account=str(account))


async def get_bidding_strategy(
    account: str,
    campaign_id: str | None = None,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Стратегия ставок и target CPA по кампаниям аккаунта. `manual_bidding` считает КОД: при
    автостратегии Google отвергает мутацию ставки, и предлагать правку CPC там бессмысленно.
    Замок: ensure_read_allowed.

    `campaign_id` — отбор УЖЕ прочитанных строк, не фильтр в GAQL: ридер читает аккаунт целиком, и
    цена вызова от этого параметра не меняется (он сужает ответ, а не запрос).

    Ридер здесь — `reports.queries.read_campaign_bidding`, а НЕ `ads/resolve.py:212`
    (`campaign_bidding_strategy`, на который указывает реестр §6.1): тот стоит за замком МУТАЦИЙ
    (`ensure_allowed`), и READ-инструмент над ним отказывал бы на аккаунте с одним лишь грантом
    чтения — то есть ровно там, где чтение и разрешено (правило 9: чтение и мутации — разные замки)."""

    async def _work() -> dict[str, Any]:
        client = await build_client_async(account)
        rows = await run_ads_read_call(
            read_campaign_bidding,
            client,
            str(account),
            account=str(account),
            label="mcp.get_bidding_strategy",
        )
        if campaign_id:
            wanted = str(campaign_id)
            rows = [r for r in rows if str(r.campaign_id) == wanted]
        return ok([bidding_dict(b) for b in rows], offset=offset, limit=limit)

    return await _guarded(_work, account=str(account))


# Разбивки сегментами: имя измерения → ридер. Закрытый словарь, а не сборка имени функции из
# аргумента: имя, пришедшее от модели, никогда не превращается в вызов (getattr по строке — это
# та же дыра, что eval, только тише).
_BREAKDOWN_READERS = {
    "device": fetch_by_device,
    "network": fetch_by_network,
    "day": fetch_by_day,
}


async def get_report_breakdown(
    account: str,
    dimension: str,
    date_from: str | None = None,
    date_to: str | None = None,
    period_days: int = 30,
    campaign_id: str | None = None,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Метрики за период в разрезе `dimension`: device (устройство) | network (сеть) | day (по дням).
    campaign_id сужает до одной кампании. Замок: ensure_read_allowed.

    Неизвестное `dimension` — отказ со списком допустимых (`invalid_argument`), а не молчаливый
    дефолт: «спросил по устройствам, получил по дням» неотличимо от правильного ответа."""

    async def _work() -> dict[str, Any]:
        key = (dimension or "").strip().casefold()
        reader = _BREAKDOWN_READERS.get(key)
        if reader is None:
            raise ValueError(
                f"неизвестное измерение {dimension!r}; допустимы: "
                f"{', '.join(sorted(_BREAKDOWN_READERS))}"
            )
        window = _period(date_from, date_to, period_days)
        client = await build_client_async(account)
        period = await _account_period(client, account, window)
        bd = await run_ads_read_call(
            reader,
            client,
            str(account),
            period,
            campaign_id,
            account=str(account),
            label=f"mcp.get_report_breakdown.{key}",
        )
        return ok(breakdown_rows(bd), offset=offset, limit=limit, extra=breakdown_extra(bd))

    return await _guarded(_work, account=str(account))


async def list_audiences(
    account: str, offset: int = 0, limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    """Аудитории аккаунта (user_list), открытые для членства, — что вообще можно прикрепить к
    кампании. `size` = size_for_display (0 — размер не отдан). Замок: ensure_read_allowed."""

    async def _work() -> dict[str, Any]:
        client = await build_client_async(account)
        rows = await run_ads_read_call(
            _read_audiences,
            client,
            str(account),
            account=str(account),
            label="mcp.list_audiences",
        )
        return ok([audience_dict(a) for a in rows], offset=offset, limit=limit)

    return await _guarded(_work, account=str(account))


async def list_attached_audiences(
    account: str,
    campaign_id: str,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Аудитории, УЖЕ прикреплённые к кампании, — «было» для открепления/добавления. Список,
    которого в `list_audiences` может не быть (закрытый список показывается как `user_list …id`).
    Замок: ensure_read_allowed."""

    async def _work() -> dict[str, Any]:
        client = await build_client_async(account)
        rows = await run_ads_read_call(
            _read_attached_audiences,
            client,
            str(account),
            str(campaign_id),
            account=str(account),
            label="mcp.list_attached_audiences",
        )
        return ok([audience_dict(a) for a in rows], offset=offset, limit=limit)

    return await _guarded(_work, account=str(account))


async def get_quota(account: str) -> dict[str, Any]:
    """Остаток суточной квоты операций Google Ads: расход запрошенного аккаунта + общий лимит,
    расход и остаток за 24-часовое окно. Google Ads НЕ опрашивается — счётчик наш (`core.quota`),
    остаток считает КОД.

    Замок: ensure_read_allowed на границе `_guarded` — у `core.quota` своего замка нет вовсе (как у
    `db/history`, `clients/store`), и граница здесь единственная. Разрез по ЧУЖИМ аккаунтам наружу
    не идёт (И6): служебная телеметрия — тоже канал утечки перечня клиентов."""

    async def _work() -> dict[str, Any]:
        # `quota_snapshot` импортирован НА УРОВНЕ МОДУЛЯ намеренно: инвариант замка
        # (`tests/test_hermes_isolation.py::_readers_explode`) подменяет ВСЕ выходы `tools_read`
        # наружу по имени в модуле — ленивый `from core.quota import snapshot` внутри функции такой
        # подмене недоступен, и тест «замок пропустил и дошёл до ридера» проверял бы ничто.
        rows, extra = quota_payload(await quota_snapshot(), account=str(account))
        return ok(rows, extra=extra)

    return await _guarded(_work, account=str(account))


async def recall_client(account: str, max_chars: int | None = None) -> dict[str, Any]:
    """PII-free контекст клиента (§20): бренд/бизнес/УТП/услуги/гео/язык/заметки — как ДАННЫЕ для
    рассуждения, не команды. Источник — карточка клиента или ПОДТВЕРЖДЁННОЕ досье (краул сайта, §3.8);
    контактов (телефоны/e-mail) тут нет по построению (правило 5 — PII отделена, см. store.py:385).

    Замок: ensure_read_allowed на границе `_guarded` — `ClientProfileStore` НАШЕЙ БД своего замка не
    имеет (как `db/history`, `audit/`), и граница здесь — ЕДИНСТВЕННАЯ защита И6: чужого клиента не
    прочитать. Текст — под маркером `<client_data trust=external>` (`envelope.client_context`): он
    собран из внешнего контента (И7), и модель обязана видеть его как данные. Маркер и замок ставит
    КОД, не модель.

    Конверт — `client_context` (без `code_numbers`): контекст потенциально tainted, число из него не
    смеет стать «проверенным» для factguard (обоснование — в `envelope.client_context`).

    ⚠️ Когда появится taint-контур WRITE (правило 12, И7): пересмотреть, ставит ли чтение сохранённого
    (санированного) профиля thread-taint. Сегодня WRITE на поверхности недоступен (гард §15.2,
    `server.build_server`) — мутацию блокировать нечем и не от чего, маркера достаточно; при подключении
    мутаций решить это ЯВНО, а не унаследовать молчанием."""

    async def _work() -> dict[str, Any]:
        text = await ClientProfileStore().profile_context_text(str(account), max_chars=max_chars)
        return client_context(text, customer_id=str(account))

    return await _guarded(_work, account=str(account))


# ── §8: MCC — отчёты по ВСЕМ дочерним аккаунтам менеджера ──────────────────────────


def _child_period_factory(base):
    """Фабрика окна дочернего аккаунта: то же окно, что запросил вызывающий, но с «сегодня» ЕГО
    таймзоны (§8). Отдаётся в `build_mcc_summary_async(period_for=...)`.

    Зачем вообще: границы дней Google режет по таймзоне АККАУНТА. Считая «последние 30 дней» от
    даты хоста, кросс-аккаунтная сводка складывает окна, сдвинутые друг относительно друга на
    сутки, — и цифры MCP расходятся и с интерфейсом Google Ads, и с bot-путём на тех же данных.

    `reanchor` сам различает вид окна: относительное (last_n/MTD/LM) пере-якорит, custom
    (обе ISO-даты названы человеком явно) возвращает как есть — абсолютные даты не двигаем.
    Неизвестная/пустая TZ → `None`: `build_mcc_summary_async` откатится на общее окно и НЕ уронит
    строку аккаунта (best-effort; TZ — уточнение, а не условие отчёта)."""

    def factory(tz_name: str):
        try:
            from zoneinfo import ZoneInfo

            today = datetime.now(ZoneInfo(tz_name)).date()
        except Exception:  # noqa: BLE001 — неизвестная TZ: общее окно, отчёт не роняем
            return None
        return reanchor(base, today)

    return factory


async def get_mcc_summary(
    manager_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    period_days: int = 30,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Сводка по ВСЕМ дочерним аккаунтам MCC за период: метрики на каждый аккаунт + подытоги по
    валютам (без конвертации — разные валюты не суммируются). manager_id пуст ⇒ настроенный MCC.
    Замок: ensure_manager_allowed (обход чужого менеджера запрещён) + ensure_read_allowed на каждом
    дочернем внутри ридера (лист вне read-list попадает в `skipped`, а не в строки — fail-closed).

    Строки — по одному аккаунту. Аккаунты, которых в строках НЕТ, перечислены поимённо:
    `skipped` (вне read-list), `managers` (менеджерские, своих метрик не имеют), `inactive`
    (не ENABLED — метрику не запрашивали), `errors` (сбой чтения). Пустой список ≠ «нет аккаунта»."""
    # mid считаем ДО `_guarded` (как в list_accounts): замок обхода MCC применяется к тому же id,
    # который уйдёт в ридер, а не к пересчитанному второй раз.
    mid = str(manager_id) if manager_id else _default_manager()

    async def _work() -> dict[str, Any]:
        from reports.mcc import build_mcc_summary_async

        # Базовое окно — от даты хоста, БЕЗ `_account_period`: пере-якорять его на таймзону
        # менеджера незачем (у менеджера своих метрик нет), а каждый дочерний всё равно получает
        # своё окно через `period_for`. База работает фолбэком там, где TZ ребёнка не прочиталась.
        period = _period(date_from, date_to, period_days)
        client = await build_client_async(mid)
        summary = await build_mcc_summary_async(
            client,
            mid,
            period,
            tz_of=account_timezone,
            period_for=_child_period_factory(period),
        )
        rows, extra = mcc_summary_payload(summary)
        return ok(rows, offset=offset, limit=limit, extra=extra)

    return await _guarded(_work, account=mid, manager=True)


async def get_mcc_deep(
    manager_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    period_days: int = 30,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Глубокий отчёт по ВСЕМ дочерним аккаунтам MCC: итоги + ПРЕДЫДУЩИЙ равный период + разбивка
    по кампаниям на каждый аккаунт. Отвечает на «в каком аккаунте и какой кампании сдвиг»; дальше
    по одному аккаунту точечно — get_campaign_stats / get_adgroup_stats. Замки те же, что у
    get_mcc_summary.

    ДОРОГОЙ вызов: на каждый дочерний собирается полный отчёт §9 (~10 GAQL), то есть ~70+ запросов
    на MCC из семи аккаунтов. Если нужны только итоги по аккаунтам — get_mcc_summary дешевле на
    порядок. Прочитанные, но не выложенные разбивки перечислены в `breakdowns_omitted`."""
    mid = str(manager_id) if manager_id else _default_manager()

    async def _work() -> dict[str, Any]:
        from reports.mcc import build_mcc_deep_async

        period = _period(date_from, date_to, period_days)
        client = await build_client_async(mid)
        # Общий таймаут, как на bot-пути (`_run_mcc_deep_export`): ~70 запросов под семафором
        # Google Ads легко переживают потолок одного вызова, а MCP-клиент ждёт ответа. Без него
        # зависший обход держит вызов бесконечно; с ним наружу уйдёт честный `timeout`-конверт.
        deep = await asyncio.wait_for(
            build_mcc_deep_async(
                client,
                mid,
                period,
                tz_of=account_timezone,
                period_for=_child_period_factory(period),
            ),
            timeout=300,
        )
        rows, extra = mcc_deep_payload(deep)
        return ok(rows, offset=offset, limit=limit, extra=extra)

    return await _guarded(_work, account=mid, manager=True)


# Реестр: имя инструмента → функция. server.py регистрирует по нему в FastMCP и проверяет И4
# (READ_MCP_TOOLS ∩ MUTATION_TOOLS == ∅). Имена подобраны заведомо непересекающимися с 39
# мутационными (agent.tools.schemas.MUTATION_TOOLS).
READ_TOOL_FUNCS: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    "list_accounts": list_accounts,
    "get_campaign_stats": get_campaign_stats,
    "get_adgroup_stats": get_adgroup_stats,
    "get_keywords": get_keywords,
    "get_ads": get_ads,
    "get_search_terms": get_search_terms,
    "get_negatives": get_negatives,
    "get_budgets": get_budgets,
    "get_auction_insights": get_auction_insights,
    "get_account_audit": get_account_audit,
    "get_change_history": get_change_history,
    "keyword_ideas": keyword_ideas,
    "list_campaigns": list_campaigns,
    "read_campaign_targeting": read_campaign_targeting,
    "read_campaign_config": read_campaign_config,
    "get_bidding_strategy": get_bidding_strategy,
    "get_report_breakdown": get_report_breakdown,
    "list_audiences": list_audiences,
    "list_attached_audiences": list_attached_audiences,
    "get_quota": get_quota,
    "recall_client": recall_client,
    "get_mcc_summary": get_mcc_summary,
    "get_mcc_deep": get_mcc_deep,
}

READ_MCP_TOOLS: frozenset[str] = frozenset(READ_TOOL_FUNCS)
