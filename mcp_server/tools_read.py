"""12 READ-обёрток MCP-слоя над существующими ридерами (Контур A, инкремент «MCP READ»).

Каждая обёртка:
  1) проходит замок ЧТЕНИЯ на ГРАНИЦЕ слоя — `_guarded` требует `account=` и зовёт
     `ensure_read_allowed`/`ensure_manager_allowed` ДО тела инструмента (см. ниже);
  2) строит клиент `build_client_async(account)` и период из `date_from/date_to|period_days`;
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

§20 READ-инструменты профиля (get_client_profile, get_profile_context, get_client_dossier):
читают НАШУ БД (clients.store), а не Google Ads. Замок `_guarded` — ЕДИНСТВЕННАЯ защита
(ридеры БД своего замка не имеют). Работают через тот же _guarded c account=, хотя и не требуют
build_client_async (не Google Ads).
"""

from __future__ import annotations

from datetime import date
from typing import Any, Awaitable, Callable

from ads.client import build_client_async, ensure_manager_allowed, ensure_read_allowed
from ads.keyword_plan import generate_keyword_ideas
from ads.read import list_child_accounts
from audit.collect import gather_audit
from core.logging import log
from core.resilience import run_ads_read_call
from db.history import list_recent_applied_by_customer
from mcp_server.envelope import DEFAULT_LIMIT, err, ok
from mcp_server.serialize import (
    audit_payload,
    breakdown_extra,
    breakdown_rows,
    budget_dict,
    child_account_dict,
    dossier_dict,
    impression_share_dict,
    keyword_idea_dict,
    negatives_payload,
    profile_dict,
    recent_action_dict,
    search_term_dict,
)
from reports import period as pperiod
from reports.queries import (
    fetch_budgets,
    fetch_by_ad,
    fetch_by_ad_group,
    fetch_by_campaign,
    fetch_by_keyword,
    fetch_impression_share,
    fetch_negative_keywords,
    fetch_search_terms,
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
    """
    try:
        if manager:
            ensure_manager_allowed(str(account))
        else:
            ensure_read_allowed(str(account))
        return await work()
    except Exception as e:  # noqa: BLE001 — граница слоя: наружу только редактированное
        log.warning("mcp read tool failed: %s", type(e).__name__)
        return err(e)


def _period(date_from: str | None, date_to: str | None, period_days: int | None):
    """ISO-даты `date_from`+`date_to` (обе) → custom-период; иначе — последние N дней (N=period_days).
    Арифметику дат считает КОД (reports.period), не модель."""
    if date_from and date_to:
        return pperiod.custom(date.fromisoformat(date_from), date.fromisoformat(date_to))
    return pperiod.last_n_days(max(1, int(period_days or 30)))


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
        client = await build_client_async(account)
        bd = await run_ads_read_call(
            fetch_by_campaign,
            client,
            str(account),
            _period(date_from, date_to, period_days),
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
        client = await build_client_async(account)
        bd = await run_ads_read_call(
            fetch_by_ad_group,
            client,
            str(account),
            _period(date_from, date_to, period_days),
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
        client = await build_client_async(account)
        bd = await run_ads_read_call(
            fetch_by_keyword,
            client,
            str(account),
            _period(date_from, date_to, period_days),
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
        client = await build_client_async(account)
        bd = await run_ads_read_call(
            fetch_by_ad,
            client,
            str(account),
            _period(date_from, date_to, period_days),
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
        client = await build_client_async(account)
        rows = await run_ads_read_call(
            fetch_search_terms,
            client,
            str(account),
            _period(date_from, date_to, period_days),
            campaign_id,
            int(reader_limit),
            account=str(account),
            label="mcp.get_search_terms",
        )
        return ok([search_term_dict(r) for r in rows], offset=offset, limit=limit)

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
        return ok(rows, offset=offset, limit=limit, extra=extra)

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
        return ok([budget_dict(b) for b in rows], offset=offset, limit=limit)

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
        client = await build_client_async(account)
        rows = await run_ads_read_call(
            fetch_impression_share,
            client,
            str(account),
            _period(date_from, date_to, period_days),
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
        client = await build_client_async(account)
        result = await gather_audit(
            client, str(account), _period(date_from, date_to, period_days), target_cpa=target_cpa
        )
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
        return ok([recent_action_dict(a) for a in actions], offset=offset, limit=limit)

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
        return ok([keyword_idea_dict(k) for k in ideas], offset=offset, limit=limit)

    return await _guarded(_work, account=str(account))


# ── §20: READ-инструменты профиля клиента (НАША БД, не Google Ads) ────────────────


async def get_client_profile(
    account: str,
) -> dict[str, Any]:
    """§20: полный профиль клиента (бренд/описание/гео/язык/сайт/соцсети/услуги/контакты).
    account — customer_id Google Ads аккаунта. Замок: ensure_read_allowed (ридер БД своего замка
    не имеет). Нет профиля → rows=[], error=null."""

    async def _work() -> dict[str, Any]:
        from clients.store import ClientProfileStore

        p = await ClientProfileStore().get_by_account(str(account))
        if p is None:
            return ok([], extra={"customer_id": str(account)})
        row = profile_dict(p)
        return ok([row], extra={"customer_id": str(account)})

    return await _guarded(_work, account=str(account))


async def get_profile_context(
    account: str,
    max_chars: int | None = None,
) -> dict[str, Any]:
    """§20: компактный PII-free контекст профиля для генераторов RSA/ключей.
    account — customer_id. max_chars — опц. обрезка (дефолт из config).
    Нет профиля или досье → rows=[{text: ''}]."""

    async def _work() -> dict[str, Any]:
        from clients.store import ClientProfileStore

        text = await ClientProfileStore().profile_context_text(
            str(account), max_chars=max_chars
        )
        return ok([{"text": text}], extra={"customer_id": str(account)})

    return await _guarded(_work, account=str(account))


async def get_client_dossier(
    account: str,
) -> dict[str, Any]:
    """§20: подтверждённое досье клиента (сводка краулинга, PII-free).
    account — customer_id. Возвращает current-досье: версия, статус, наличие markdown/контекста.
    Нет досье → rows=[], error=null."""

    async def _work() -> dict[str, Any]:
        from clients.dossier_store import ClientDossierStore

        d = await ClientDossierStore().get_current(str(account))
        if d is None:
            return ok([], extra={"customer_id": str(account)})
        dd = dossier_dict(d)
        return ok([dd], extra={"customer_id": str(account)})

    return await _guarded(_work, account=str(account))


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
    # §20: READ-инструменты профиля клиента
    "get_client_profile": get_client_profile,
    "get_profile_context": get_profile_context,
    "get_client_dossier": get_client_dossier,
}

READ_MCP_TOOLS: frozenset[str] = frozenset(READ_TOOL_FUNCS)
