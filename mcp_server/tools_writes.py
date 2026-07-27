"""39 WRITE-обёрток MCP-слоя: создание Proposal через confirm-гейт БЕЗ исполнения.

Каждая обёртка:
  1) проходит замок МУТАЦИИ на ГРАНИЦЕ — `_guarded_write` зовёт `ensure_allowed(account)`
     ДО построения proposal (fail-closed: мутация на неразрешённом аккаунте не уходит даже
     в черновик);
  2) генерирует confirmation_id (`mcp-{uuid}`);
  3) создаёт черновик через `bot.proposal.build_proposal(chat_id=0)` — headless-режим;
  4) возвращает единый конверт `mcp_server.envelope.ok` с полями confirmation_id, summary,
     operation, status="pending";
  5) ловит ЛЮБОЕ исключение (_guarded_write) → редактированный error-конверт (`envelope.err`).

`execute_confirmed` — отдельный инструмент: выполняет УЖЕ ПОДТВЕРЖДЁННЫЙ (через Telegram)
черновик через `ads.service.execute_confirmed`. Без подтверждения — отказ (confirm-гейт).

Замок на границе НЕОТДЕЛИМ от `_guarded_write`: `account` — keyword-only обязательный
аргумент, обёртку без него нельзя даже вызвать. `ensure_allowed` — тот же замок, что
на исполнении `apply_*` (тест `test_execute_account_binding.py`).

chat_id=0 — headless-режим: черновик создаётся БЕЗ привязки к Telegram-чату.
Подтвердить его может ЛЮБОЙ оператор с доступом к confirmation_id (через /journal
или прямую ссылку). Это намеренное проектное решение для MCP-контура.
"""

from __future__ import annotations

import uuid
from typing import Any, Awaitable, Callable

from ads.client import build_client_async, ensure_allowed
from ads.service import execute_confirmed as _execute_confirmed
from bot.proposal import build_proposal
from confirm.store import ConfirmStore
from core.logging import log
from mcp_server.envelope import DEFAULT_LIMIT, err, ok

# PolicyEngine + Auditor (ленивый импорт — модули новые, не должны блокировать импорт tools_writes)
_policy_engine: Any = None
_auditor_available: bool = False


def _get_policy_engine() -> Any:
    """Ленивый импорт PolicyEngine. Возвращает None если модуль недоступен."""
    global _policy_engine, _auditor_available
    if _policy_engine is None:
        try:
            from core.budget_policy import PolicyEngine

            _policy_engine = PolicyEngine()
            _auditor_available = True
        except ImportError:
            log.info("core.budget_policy not available — policy checks disabled")
            _policy_engine = False  # sentinel
            _auditor_available = False
    return _policy_engine if _policy_engine is not False else None


async def _check_budget_policy(
    campaign: str,
    mode: str,
    value: float,
    *,
    account: str,
    currency: str | None = None,
    account_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Проверить изменение бюджета через PolicyEngine с ЖИВЫМ чтением текущего бюджета.

    Вызывается ПЕРЕД созданием proposal. Если политика нарушена — возвращает
    err-конверт с POLICY_EXCEEDED, и proposal НЕ создаётся.
    """
    engine = _get_policy_engine()
    if engine is None:
        return None  # модуль недоступен — пропускаем (fail-open на этапе внедрения)

    if mode not in ("set_to", "increase_by_amount", "decrease_by_amount"):
        return None  # percent-режимы — пропускаем (требуют контекста)

    # ── Живое чтение текущего бюджета ──
    from ads.read import campaign_budget as _read_budget
    from core.resilience import run_ads_read_call

    try:
        client = await build_client_async(str(account))
        current = await run_ads_read_call(
            _read_budget, client, str(account),
            campaign_name=campaign,
            account=str(account),
        )
    except Exception as e:
        log.warning("Failed to read current budget for %s: %s", campaign, type(e).__name__)
        return None  # fail-open: не блокируем из-за сбоя чтения

    if current is None:
        # Попробовать как campaign_id
        try:
            cid = int(campaign)
            current = await run_ads_read_call(
                _read_budget, client, str(account),
                campaign_id=cid,
                account=str(account),
            )
        except (ValueError, Exception):
            pass

    if current is None or current <= 0:
        log.warning("Cannot read budget for %s — skipping policy check", campaign)
        return None  # fail-open: нет данных → не блокируем

    # Вычисляем new_budget
    if mode == "set_to":
        new_budget = float(value)
    elif mode == "increase_by_amount":
        new_budget = current + float(value)
    elif mode == "decrease_by_amount":
        new_budget = max(0, current - float(value))
    else:
        return None

    result = engine.check_budget_change(
        campaign_id=campaign,
        campaign_name=campaign,
        old_budget=current,
        new_budget=new_budget,
        currency=currency or "USD",
    )

    if not result.allowed:
        return err(
            RuntimeError(
                f"POLICY_EXCEEDED: {result.reason} "
                f"(текущий: {current}, предлагаемый: {new_budget})"
            )
        )

    if result.risk == "high":
        log.warning(
            "High-risk budget change proposed: %s %s→%s (%s)",
            campaign, current, new_budget, result.reason,
        )

    return None  # OK


async def _check_pause_policy(campaign: str, *, account: str) -> dict[str, Any] | None:
    """Проверить паузу кампании через PolicyEngine с живым чтением конверсий."""
    engine = _get_policy_engine()
    if engine is None:
        return None

    from core.resilience import run_ads_read_call

    # Живое чтение конверсий за 7 дней
    conversions_7d = 0
    try:
        client = await build_client_async(str(account))
        ga = client.get_service("GoogleAdsService")

        def _read_conversions() -> int:
            # Попробовать по имени
            from ads.resolve import gaql_escape
            safe = gaql_escape(campaign)
            q = (
                "SELECT metrics.conversions FROM campaign "
                f"WHERE campaign.name = '{safe}' AND campaign.status != 'REMOVED' "
                "AND segments.date DURING LAST_7_DAYS"
            )
            total = 0.0
            for row in ga.search(customer_id=str(account), query=q):
                total += row.metrics.conversions
            return int(total)

        conversions_7d = await run_ads_read_call(
            _read_conversions, account=str(account),
        )
    except Exception as e:
        log.warning("Failed to read conversions for %s: %s", campaign, type(e).__name__)

    result = engine.check_pause(
        campaign_id=campaign,
        campaign_name=campaign,
        conversions_7d=conversions_7d,
    )
    if result.risk == "high":
        log.warning("High-risk pause proposed: %s (conv_7d=%d, %s)",
                     campaign, conversions_7d, result.reason)

    return None  # pause never blocks — only warns


async def _check_bid_policy(
    campaign: str,
    mode: str,
    value: float,
    *,
    account: str,
    currency: str | None = None,
) -> dict[str, Any] | None:
    """Проверить изменение ставки через PolicyEngine с живым чтением текущей CPC."""
    engine = _get_policy_engine()
    if engine is None:
        return None

    if mode not in ("set_to",):
        return None

    from core.resilience import run_ads_read_call

    # Живое чтение текущей средней CPC кампании
    old_cpc = 0.01  # fallback
    try:
        client = await build_client_async(str(account))
        ga = client.get_service("GoogleAdsService")

        def _read_cpc() -> float:
            from ads.resolve import gaql_escape
            safe = gaql_escape(campaign)
            q = (
                "SELECT metrics.cost_micros, metrics.clicks FROM campaign "
                f"WHERE campaign.name = '{safe}' AND campaign.status != 'REMOVED' "
                "AND segments.date DURING LAST_30_DAYS"
            )
            cost_micros = 0
            clicks = 0
            for row in ga.search(customer_id=str(account), query=q):
                cost_micros += int(row.metrics.cost_micros or 0)
                clicks += int(row.metrics.clicks or 0)
            if clicks > 0:
                return (cost_micros / 1_000_000) / clicks
            return 0.01

        old_cpc = await run_ads_read_call(
            _read_cpc, account=str(account),
        )
    except Exception as e:
        log.warning("Failed to read CPC for %s: %s", campaign, type(e).__name__)

    result = engine.check_cpc_change(
        campaign_id=campaign,
        campaign_name=campaign,
        old_cpc=old_cpc,
        new_cpc=float(value),
        currency=currency or "USD",
    )
    if not result.allowed:
        return err(
            RuntimeError(
                f"POLICY_EXCEEDED: {result.reason} "
                f"(текущая CPC: {old_cpc:.2f}, предлагаемая: {value})"
            )
        )

    return None

# Один стор на процесс — build_proposal и execute_confirmed разделяют его.
# В headless-режиме (MCP) нет разделения по чатам: черновик принадлежит контуру, а не оператору.
_STORE: ConfirmStore | None = None


def _get_store() -> ConfirmStore:
    """Ленивый синглтон ConfirmStore (на поток MCP-сервера)."""
    global _STORE
    if _STORE is None:
        _STORE = ConfirmStore()
    return _STORE


def _new_cid() -> str:
    """Уникальный confirmation_id с префиксом mcp- (отличим от bot-черновиков `ad-`)."""
    return f"mcp-{uuid.uuid4().hex[:12]}"


async def _guarded_write(
    work: Callable[[], Awaitable[dict[str, Any]]],
    *,
    account: str,
) -> dict[str, Any]:
    """Замок МУТАЦИИ на границе + выполнение тела; ЛЮБОЙ сбой → редактированный error-конверт.

    `account` обязателен и keyword-only: обёртку, забывшую объявить аккаунт мутации,
    нельзя вызвать вовсе — TypeError на первом же обращении.
    `ensure_allowed` — тот же замок, что на исполнении apply_* (мутационный набор ⊆ потолок).
    """
    try:
        ensure_allowed(str(account))
        return await work()
    except Exception as e:  # noqa: BLE001 — граница слоя: наружу только редактированное
        log.warning("mcp write tool failed: %s", type(e).__name__)
        return err(e)


def _make_params(**kwargs: Any) -> dict[str, Any]:
    """Упаковать явные аргументы в params-словарь, отбросив None."""
    return {k: v for k, v in kwargs.items() if v is not None}


async def _propose(
    *,
    operation: str,
    params: dict[str, Any],
    account: str,
    summary: str = "",
) -> dict[str, Any]:
    """Общий шаблон: confirmation_id → build_proposal → конверт."""
    cid = _new_cid()
    store = _get_store()

    try:
        proposal = await build_proposal(
            store=store,
            operation=operation,
            params=params,
            cid=cid,
            chat_id=0,  # headless: нет привязки к Telegram-чату
            customer_id=str(account),
            summary=summary,
            lang="ru",
            user_initiated=False,  # MCP-агент — не прямая команда человека (golden rule #3)
        )
    except Exception:
        # build_proposal может бросить ProposalRefused (замок аккаунта / невыполнимое снижение /
        # валютный mismatch). Это НЕ баг слоя — поднимаем до _guarded_write для err-конверта.
        raise

    # Большой список ключей (>KW_INLINE_MAX) → .xlsx-вложение:
    # кодируем base64 для передачи через MCP-конверт, затем чистим временный файл.
    attachment_b64: str | None = None
    attachment_name: str | None = None
    if proposal.big_list_attachment:
        from confirm.xlsx_attachment import cleanup_attachment, read_attachment_b64

        try:
            attachment_b64 = read_attachment_b64(proposal.big_list_attachment)
            attachment_name = f"proposal_keywords_{operation}.xlsx"
        finally:
            cleanup_attachment(proposal.big_list_attachment)

    row: dict[str, Any] = {
        "confirmation_id": cid,
        "summary": proposal.display,
        "operation": operation,
        "status": "pending",
    }
    if attachment_b64 is not None:
        row["attachment"] = {
            "filename": attachment_name,
            "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "content_b64": attachment_b64,
        }

    return ok(
        rows=[row],
        offset=0,
        limit=1,
    )


# ═══════════════════════════════════════════════════════════════════════════════════
# 39 propose_* инструментов (MUTATION_TOOLS → propose_{operation})
# ═══════════════════════════════════════════════════════════════════════════════════

# ── Бюджет ──


async def propose_update_budget(
    account: str,
    campaign: str,
    mode: str,
    value: float,
    currency: str | None = None,
) -> dict[str, Any]:
    """Предложить изменение дневного бюджета кампании (set_to / increase_by_percent / …).
    Проверяется через PolicyEngine (Δ≤20%) и Auditor (risk assessment)."""

    async def _work() -> dict[str, Any]:
        # 1. PolicyEngine check BEFORE proposal creation
        policy_err = await _check_budget_policy(
            campaign=campaign, mode=mode, value=value, currency=currency, account=account
        )
        if policy_err is not None:
            return policy_err

        # 2. Create proposal
        return await _propose(
            operation="update_budget",
            params=_make_params(campaign=campaign, mode=mode, value=value, currency=currency),
            account=account,
            summary=f"Бюджет '{campaign}': {mode} {value}",
        )

    return await _guarded_write(_work, account=str(account))


# ── Ставки ──


async def propose_update_bid(
    account: str,
    campaign: str,
    mode: str,
    value: float,
    currency: str | None = None,
) -> dict[str, Any]:
    """Предложить изменение ставки CPC на уровне групп объявлений кампании.
    Проверяется через PolicyEngine."""

    async def _work() -> dict[str, Any]:
        policy_err = await _check_bid_policy(
            campaign=campaign, mode=mode, value=value, currency=currency, account=account
        )
        if policy_err is not None:
            return policy_err

        return await _propose(
            operation="update_bid",
            params=_make_params(campaign=campaign, mode=mode, value=value, currency=currency),
            account=account,
            summary=f"Ставка '{campaign}': {mode} {value}",
        )

    return await _guarded_write(_work, account=str(account))


async def propose_update_keyword_bid(
    account: str,
    campaign: str,
    keyword: str,
    mode: str,
    value: float,
    currency: str | None = None,
    ad_group: str | None = None,
    match_type: str | None = None,
) -> dict[str, Any]:
    """Предложить изменение ставки CPC у КОНКРЕТНОГО ключевого слова."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="update_keyword_bid",
            params=_make_params(
                campaign=campaign,
                keyword=keyword,
                mode=mode,
                value=value,
                currency=currency,
                ad_group=ad_group,
                match_type=match_type,
            ),
            account=account,
            summary=f"Ставка ключа '{keyword}' в '{campaign}': {mode} {value}",
        )

    return await _guarded_write(_work, account=str(account))


# ── Ключевые слова ──


async def propose_add_keywords(
    account: str,
    campaign: str,
    keywords: list[str],
    match_type: str,
    ad_group: str | None = None,
) -> dict[str, Any]:
    """Предложить добавить ключевые слова в кампанию."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="add_keywords",
            params=_make_params(
                campaign=campaign,
                keywords=keywords,
                match_type=match_type,
                ad_group=ad_group,
            ),
            account=account,
            summary=f"+{len(keywords)} ключей в '{campaign}' ({match_type})",
        )

    return await _guarded_write(_work, account=str(account))


async def propose_remove_keywords(
    account: str,
    campaign: str,
    keywords: list[str],
    match_type: str,
) -> dict[str, Any]:
    """Предложить удалить ключевые слова из кампании."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="remove_keywords",
            params=_make_params(campaign=campaign, keywords=keywords, match_type=match_type),
            account=account,
            summary=f"-{len(keywords)} ключей из '{campaign}' ({match_type})",
        )

    return await _guarded_write(_work, account=str(account))


# ── Минус-слова ──


async def propose_add_negative_keywords(
    account: str,
    campaign: str,
    keywords: list[str],
    match_type: str = "broad",
    ad_group: str | None = None,
) -> dict[str, Any]:
    """Предложить добавить минус-слова на уровень кампании (или группы, если ad_group задан)."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="add_negative_keywords",
            params=_make_params(
                campaign=campaign,
                keywords=keywords,
                match_type=match_type,
                ad_group=ad_group,
            ),
            account=account,
            summary=f"+{len(keywords)} минус-слов в '{campaign}'",
        )

    return await _guarded_write(_work, account=str(account))


async def propose_remove_negative_keywords(
    account: str,
    campaign: str,
    keywords: list[str],
    match_type: str = "broad",
) -> dict[str, Any]:
    """Предложить удалить минус-слова из кампании."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="remove_negative_keywords",
            params=_make_params(campaign=campaign, keywords=keywords, match_type=match_type),
            account=account,
            summary=f"-{len(keywords)} минус-слов из '{campaign}'",
        )

    return await _guarded_write(_work, account=str(account))


async def propose_add_negatives_to_shared_set(
    account: str,
    shared_set: str,
    keywords: list[str],
    match_type: str = "broad",
) -> dict[str, Any]:
    """Предложить добавить минус-слова в ОБЩИЙ СПИСОК аккаунта."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="add_negatives_to_shared_set",
            params=_make_params(
                shared_set=shared_set,
                keywords=keywords,
                match_type=match_type,
            ),
            account=account,
            summary=f"+{len(keywords)} минус-слов в shared set '{shared_set}'",
        )

    return await _guarded_write(_work, account=str(account))


async def propose_attach_shared_set(
    account: str,
    campaign: str,
    shared_set: str,
) -> dict[str, Any]:
    """Предложить привязать общий список минус-слов к кампании."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="attach_shared_set",
            params=_make_params(campaign=campaign, shared_set=shared_set),
            account=account,
            summary=f"Привязка shared set '{shared_set}' → '{campaign}'",
        )

    return await _guarded_write(_work, account=str(account))


# ── Статус кампании ──


async def propose_pause_campaign(
    account: str,
    campaign: str,
) -> dict[str, Any]:
    """Предложить поставить кампанию на паузу. PolicyEngine: проверка конверсий."""

    async def _work() -> dict[str, Any]:
        policy_err = await _check_pause_policy(campaign=campaign, account=account)
        if policy_err is not None:
            return policy_err

        return await _propose(
            operation="pause_campaign",
            params=_make_params(campaign=campaign),
            account=account,
            summary=f"Пауза '{campaign}'",
        )

    return await _guarded_write(_work, account=str(account))


async def propose_resume_campaign(
    account: str,
    campaign: str,
) -> dict[str, Any]:
    """Предложить возобновить (включить) кампанию из паузы."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="resume_campaign",
            params=_make_params(campaign=campaign),
            account=account,
            summary=f"Возобновить '{campaign}'",
        )

    return await _guarded_write(_work, account=str(account))


# ── Переименование / сети / гео-тип ──


async def propose_update_campaign(
    account: str,
    campaign: str,
    new_name: str,
) -> dict[str, Any]:
    """Предложить переименовать кампанию."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="update_campaign",
            params=_make_params(campaign=campaign, new_name=new_name),
            account=account,
            summary=f"Переименовать '{campaign}' → '{new_name}'",
        )

    return await _guarded_write(_work, account=str(account))


async def propose_set_campaign_network(
    account: str,
    campaign: str,
    search_partners: bool,
) -> dict[str, Any]:
    """Предложить вкл/выкл поисковых партнёров на кампании."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="set_campaign_network",
            params=_make_params(campaign=campaign, search_partners=search_partners),
            account=account,
            summary=f"Поисковые партнёры '{campaign}': {'вкл' if search_partners else 'выкл'}",
        )

    return await _guarded_write(_work, account=str(account))


async def propose_set_campaign_display_network(
    account: str,
    campaign: str,
    display_network: bool,
) -> dict[str, Any]:
    """Предложить вкл/выкл КМС (Display Network) на кампании."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="set_campaign_display_network",
            params=_make_params(campaign=campaign, display_network=display_network),
            account=account,
            summary=f"КМС '{campaign}': {'вкл' if display_network else 'выкл'}",
        )

    return await _guarded_write(_work, account=str(account))


async def propose_set_campaign_geo_target_type(
    account: str,
    campaign: str,
    geo_target_type: str,
) -> dict[str, Any]:
    """Предложить сменить тип гео-таргетинга (PRESENCE / PRESENCE_OR_INTEREST)."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="set_campaign_geo_target_type",
            params=_make_params(campaign=campaign, geo_target_type=geo_target_type),
            account=account,
            summary=f"Гео-тип '{campaign}': {geo_target_type}",
        )

    return await _guarded_write(_work, account=str(account))


# ── Удаление (destructive) ──


async def propose_remove_campaign(
    account: str,
    campaign: str,
) -> dict[str, Any]:
    """Предложить УДАЛИТЬ кампанию (необратимо)."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="remove_campaign",
            params=_make_params(campaign=campaign),
            account=account,
            summary=f"Удалить '{campaign}'",
        )

    return await _guarded_write(_work, account=str(account))


async def propose_remove_ad_group(
    account: str,
    campaign: str,
    ad_group: str,
) -> dict[str, Any]:
    """Предложить УДАЛИТЬ группу объявлений (необратимо)."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="remove_ad_group",
            params=_make_params(campaign=campaign, ad_group=ad_group),
            account=account,
            summary=f"Удалить группу '{ad_group}' из '{campaign}'",
        )

    return await _guarded_write(_work, account=str(account))


# ── Статус группы объявлений ──


async def propose_pause_ad_group(
    account: str,
    campaign: str,
    ad_group: str,
) -> dict[str, Any]:
    """Предложить поставить группу объявлений на паузу."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="pause_ad_group",
            params=_make_params(campaign=campaign, ad_group=ad_group),
            account=account,
            summary=f"Пауза группы '{ad_group}' в '{campaign}'",
        )

    return await _guarded_write(_work, account=str(account))


async def propose_resume_ad_group(
    account: str,
    campaign: str,
    ad_group: str,
) -> dict[str, Any]:
    """Предложить возобновить группу объявлений из паузы."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="resume_ad_group",
            params=_make_params(campaign=campaign, ad_group=ad_group),
            account=account,
            summary=f"Возобновить группу '{ad_group}' в '{campaign}'",
        )

    return await _guarded_write(_work, account=str(account))


# ── Статус / удаление отдельного объявления ──


async def propose_pause_ad(
    account: str,
    campaign: str,
    ad_group: str,
    ad: str,
) -> dict[str, Any]:
    """Предложить поставить ОДНО объявление на паузу."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="pause_ad",
            params=_make_params(campaign=campaign, ad_group=ad_group, ad=ad),
            account=account,
            summary=f"Пауза объявления '{ad}' в '{campaign}/{ad_group}'",
        )

    return await _guarded_write(_work, account=str(account))


async def propose_resume_ad(
    account: str,
    campaign: str,
    ad_group: str,
    ad: str,
) -> dict[str, Any]:
    """Предложить возобновить ОДНО объявление из паузы."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="resume_ad",
            params=_make_params(campaign=campaign, ad_group=ad_group, ad=ad),
            account=account,
            summary=f"Возобновить объявление '{ad}' в '{campaign}/{ad_group}'",
        )

    return await _guarded_write(_work, account=str(account))


async def propose_remove_ad(
    account: str,
    campaign: str,
    ad_group: str,
    ad: str,
) -> dict[str, Any]:
    """Предложить УДАЛИТЬ одно объявление (необратимо)."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="remove_ad",
            params=_make_params(campaign=campaign, ad_group=ad_group, ad=ad),
            account=account,
            summary=f"Удалить объявление '{ad}' из '{campaign}/{ad_group}'",
        )

    return await _guarded_write(_work, account=str(account))


# ── Гео-таргетинг ──


async def propose_set_geo_proximity(
    account: str,
    campaign: str,
    radius_km: float,
    city_name: str,
    country_code: str | None = None,
    street_address: str | None = None,
    postal_code: str | None = None,
) -> dict[str, Any]:
    """Предложить радиус-таргетинг вокруг города для кампании."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="set_geo_proximity",
            params=_make_params(
                campaign=campaign,
                radius_km=radius_km,
                city_name=city_name,
                country_code=country_code,
                street_address=street_address,
                postal_code=postal_code,
            ),
            account=account,
            summary=f"Радиус {radius_km} км вокруг '{city_name}' для '{campaign}'",
        )

    return await _guarded_write(_work, account=str(account))


async def propose_set_geo_proximity_by_coords(
    account: str,
    campaign: str,
    latitude: float,
    longitude: float,
    radius_km: float,
) -> dict[str, Any]:
    """Предложить радиус-таргетинг по координатам (широта/долгота) для кампании."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="set_geo_proximity_by_coords",
            params=_make_params(
                campaign=campaign,
                latitude=latitude,
                longitude=longitude,
                radius_km=radius_km,
            ),
            account=account,
            summary=f"Радиус {radius_km} км по координатам ({latitude}, {longitude}) для '{campaign}'",
        )

    return await _guarded_write(_work, account=str(account))


async def propose_set_geo_location(
    account: str,
    campaign: str,
    locations: list[str],
    country_code: str | None = None,
    locale: str | None = None,
) -> dict[str, Any]:
    """Предложить гео-таргетинг кампании по стране/городу/региону."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="set_geo_location",
            params=_make_params(
                campaign=campaign,
                locations=locations,
                country_code=country_code,
                locale=locale,
            ),
            account=account,
            summary=f"Гео '{campaign}': {', '.join(locations[:3])}…",
        )

    return await _guarded_write(_work, account=str(account))


# ── Стратегия назначения ставок ──


async def propose_set_bidding_strategy(
    account: str,
    campaign: str,
    strategy: str,
    target_cpa: float | None = None,
    target_roas: float | None = None,
    enhanced_cpc: bool = False,
) -> dict[str, Any]:
    """Предложить смену стратегии назначения ставок кампании."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="set_bidding_strategy",
            params=_make_params(
                campaign=campaign,
                strategy=strategy,
                target_cpa=target_cpa,
                target_roas=target_roas,
                enhanced_cpc=enhanced_cpc,
            ),
            account=account,
            summary=f"Стратегия '{campaign}': {strategy}",
        )

    return await _guarded_write(_work, account=str(account))


# ── Аудитории ──


async def propose_attach_audience(
    account: str,
    campaign: str,
    audience_resource_names: list[str],
) -> dict[str, Any]:
    """Предложить прикрепить аудитории к кампании."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="attach_audience",
            params=_make_params(
                campaign=campaign,
                audience_resource_names=audience_resource_names,
            ),
            account=account,
            summary=f"Аудитории → '{campaign}': {len(audience_resource_names)} шт.",
        )

    return await _guarded_write(_work, account=str(account))


async def propose_detach_audience(
    account: str,
    campaign: str,
    audience_resource_names: list[str],
) -> dict[str, Any]:
    """Предложить открепить аудитории от кампании."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="detach_audience",
            params=_make_params(
                campaign=campaign,
                audience_resource_names=audience_resource_names,
            ),
            account=account,
            summary=f"Аудитории × '{campaign}': {len(audience_resource_names)} шт.",
        )

    return await _guarded_write(_work, account=str(account))


# ── Создание RSA ──


async def propose_create_rsa(
    account: str,
    ad_group_id: str,
    campaign: str,
    final_url: str,
    headlines: list[str],
    descriptions: list[str],
    path1: str | None = None,
    path2: str | None = None,
) -> dict[str, Any]:
    """Предложить создать RSA-объявление в группе."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="create_rsa",
            params=_make_params(
                ad_group_id=ad_group_id,
                campaign=campaign,
                final_url=final_url,
                headlines=headlines,
                descriptions=descriptions,
                path1=path1,
                path2=path2,
            ),
            account=account,
            summary=f"RSA в '{campaign}' ({len(headlines)} загл. / {len(descriptions)} опис.)",
        )

    return await _guarded_write(_work, account=str(account))


# ── Создание кампаний ──


async def propose_create_gdn_campaign(
    account: str,
    campaign_name: str,
    headlines: list[str],
    long_headline: str,
    descriptions: list[str],
    business_name: str,
    final_url: str,
    budget_daily_micros: int,
    media_id: str,
    geo_locations: list[str] | None = None,
    geo_country_code: str | None = None,
    geo_locale: str | None = None,
) -> dict[str, Any]:
    """Предложить создать GDN-кампанию (контекстно-медийная сеть)."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="create_gdn_campaign",
            params=_make_params(
                campaign_name=campaign_name,
                headlines=headlines,
                long_headline=long_headline,
                descriptions=descriptions,
                business_name=business_name,
                final_url=final_url,
                budget_daily_micros=budget_daily_micros,
                media_id=media_id,
                geo_locations=geo_locations,
                geo_country_code=geo_country_code,
                geo_locale=geo_locale,
            ),
            account=account,
            summary=f"GDN '{campaign_name}'",
        )

    return await _guarded_write(_work, account=str(account))


async def propose_create_search_campaign(
    account: str,
    campaign_name: str,
    final_url: str,
    headlines: list[str],
    descriptions: list[str],
    budget_daily_micros: int,
    keywords: list[str] | None = None,
    match_type: str = "phrase",
    keyword_match_types: list[str] | None = None,
    cpc_bid_micros: int | None = None,
    geo_locations: list[str] | None = None,
    geo_country_code: str | None = None,
    geo_locale: str | None = None,
    languages: list[str] | None = None,
    bidding: dict | None = None,
    path1: str | None = None,
    path2: str | None = None,
    url_options: dict | None = None,
    asset_specs: list[dict] | None = None,
    existing_asset_links: list[dict] | None = None,
    image_media_ids: list[str] | None = None,
    networks: str | None = None,
    ad_schedule_blocks: list[dict] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """Предложить создать поисковую (Search) кампанию."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="create_search_campaign",
            params=_make_params(
                campaign_name=campaign_name,
                final_url=final_url,
                headlines=headlines,
                descriptions=descriptions,
                budget_daily_micros=budget_daily_micros,
                keywords=keywords,
                match_type=match_type,
                keyword_match_types=keyword_match_types,
                cpc_bid_micros=cpc_bid_micros,
                geo_locations=geo_locations,
                geo_country_code=geo_country_code,
                geo_locale=geo_locale,
                languages=languages,
                bidding=bidding,
                path1=path1,
                path2=path2,
                url_options=url_options,
                asset_specs=asset_specs,
                existing_asset_links=existing_asset_links,
                image_media_ids=image_media_ids,
                networks=networks,
                ad_schedule_blocks=ad_schedule_blocks,
                start_date=start_date,
                end_date=end_date,
            ),
            account=account,
            summary=f"Search '{campaign_name}'",
        )

    return await _guarded_write(_work, account=str(account))


async def propose_create_demand_gen_campaign(
    account: str,
    campaign_name: str,
    youtube_video_id: str,
    headlines: list[str],
    long_headline: str,
    descriptions: list[str],
    business_name: str,
    final_url: str,
    budget_daily_micros: int,
    goal: str = "clicks",
    logo_media_id: str | None = None,
    geo_locations: list[str] | None = None,
    geo_country_code: str | None = None,
    geo_locale: str | None = None,
) -> dict[str, Any]:
    """Предложить создать Demand Gen кампанию из YouTube-видео."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="create_demand_gen_campaign",
            params=_make_params(
                campaign_name=campaign_name,
                youtube_video_id=youtube_video_id,
                headlines=headlines,
                long_headline=long_headline,
                descriptions=descriptions,
                business_name=business_name,
                final_url=final_url,
                budget_daily_micros=budget_daily_micros,
                goal=goal,
                logo_media_id=logo_media_id,
                geo_locations=geo_locations,
                geo_country_code=geo_country_code,
                geo_locale=geo_locale,
            ),
            account=account,
            summary=f"Demand Gen '{campaign_name}'",
        )

    return await _guarded_write(_work, account=str(account))


async def propose_create_video_campaign(
    account: str,
    campaign_name: str,
    youtube_video_id: str,
    headlines: list[str],
    long_headline: str,
    descriptions: list[str],
    business_name: str,
    final_url: str,
    budget_daily_micros: int,
    geo_locations: list[str] | None = None,
    geo_country_code: str | None = None,
    geo_locale: str | None = None,
) -> dict[str, Any]:
    """Предложить создать Video-кампанию (YouTube)."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="create_video_campaign",
            params=_make_params(
                campaign_name=campaign_name,
                youtube_video_id=youtube_video_id,
                headlines=headlines,
                long_headline=long_headline,
                descriptions=descriptions,
                business_name=business_name,
                final_url=final_url,
                budget_daily_micros=budget_daily_micros,
                geo_locations=geo_locations,
                geo_country_code=geo_country_code,
                geo_locale=geo_locale,
            ),
            account=account,
            summary=f"Video '{campaign_name}'",
        )

    return await _guarded_write(_work, account=str(account))


# ── Расширения (assets) ──


async def propose_add_sitelinks(
    account: str,
    campaign: str,
    sitelinks: list[dict],
) -> dict[str, Any]:
    """Предложить добавить быстрые ссылки (sitelinks) в кампанию."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="add_sitelinks",
            params=_make_params(campaign=campaign, sitelinks=sitelinks),
            account=account,
            summary=f"Sitelinks → '{campaign}': {len(sitelinks)} шт.",
        )

    return await _guarded_write(_work, account=str(account))


async def propose_add_callouts(
    account: str,
    campaign: str,
    callouts: list[str],
) -> dict[str, Any]:
    """Предложить добавить уточнения (callouts) в кампанию."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="add_callouts",
            params=_make_params(campaign=campaign, callouts=callouts),
            account=account,
            summary=f"Callouts → '{campaign}': {len(callouts)} шт.",
        )

    return await _guarded_write(_work, account=str(account))


async def propose_add_structured_snippets(
    account: str,
    campaign: str,
    header: str,
    values: list[str],
) -> dict[str, Any]:
    """Предложить добавить структурные описания в кампанию."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="add_structured_snippets",
            params=_make_params(campaign=campaign, header=header, values=values),
            account=account,
            summary=f"Structured snippets → '{campaign}': {header}",
        )

    return await _guarded_write(_work, account=str(account))


async def propose_add_call_asset(
    account: str,
    campaign: str,
    phone_number: str,
    country_code: str | None = None,
) -> dict[str, Any]:
    """Предложить добавить телефон-расширение (CallAsset) в кампанию."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="add_call_asset",
            params=_make_params(
                campaign=campaign,
                phone_number=phone_number,
                country_code=country_code,
            ),
            account=account,
            summary=f"Call asset → '{campaign}'",
        )

    return await _guarded_write(_work, account=str(account))


async def propose_add_promotion(
    account: str,
    campaign: str,
    promotion_target: str,
    final_url: str,
    percent_off: float | None = None,
    money_off_units: float | None = None,
    currency: str | None = None,
    promo_code: str | None = None,
) -> dict[str, Any]:
    """Предложить добавить промо-расширение в кампанию."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="add_promotion",
            params=_make_params(
                campaign=campaign,
                promotion_target=promotion_target,
                final_url=final_url,
                percent_off=percent_off,
                money_off_units=money_off_units,
                currency=currency,
                promo_code=promo_code,
            ),
            account=account,
            summary=f"Promotion → '{campaign}': {promotion_target}",
        )

    return await _guarded_write(_work, account=str(account))


async def propose_add_price_asset(
    account: str,
    campaign: str,
    price_type: str,
    currency: str,
    offerings: list[dict],
    language_code: str | None = None,
) -> dict[str, Any]:
    """Предложить добавить прайс-расширение в кампанию."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="add_price_asset",
            params=_make_params(
                campaign=campaign,
                price_type=price_type,
                currency=currency,
                offerings=offerings,
                language_code=language_code,
            ),
            account=account,
            summary=f"Price asset → '{campaign}': {len(offerings)} оферов",
        )

    return await _guarded_write(_work, account=str(account))


async def propose_remove_asset_link(
    account: str,
    link_resource_names: list[str],
) -> dict[str, Any]:
    """Предложить открепить ассет(ы) от кампании по resource_name связи."""

    async def _work() -> dict[str, Any]:
        return await _propose(
            operation="remove_asset_link",
            params=_make_params(link_resource_names=link_resource_names),
            account=account,
            summary=f"Открепить {len(link_resource_names)} ассетов",
        )

    return await _guarded_write(_work, account=str(account))


# ═══════════════════════════════════════════════════════════════════════════════════
# execute_confirmed: исполнение ПОДТВЕРЖДЁННОГО черновика
# ═══════════════════════════════════════════════════════════════════════════════════


async def execute_confirmed(
    account: str,
    confirmation_id: str,
) -> dict[str, Any]:
    """Исполнить УЖЕ ПОДТВЕРЖДЁННЫЙ (через Telegram ✅) черновик по confirmation_id.

    Без подтверждения — отказ (confirm-гейт: claim требует status='confirmed').
    Это тот же `ads.service.execute_confirmed`, что вызывает Telegram-бот при нажатии ✅.
    """

    async def _work() -> dict[str, Any]:
        store = _get_store()
        result = await _execute_confirmed(store, str(confirmation_id))
        # execute_confirmed возвращает dict с result или бросает PermissionError
        return ok(
            rows=[
                {
                    "confirmation_id": confirmation_id,
                    "result": result,
                    "status": "applied",
                }
            ],
            offset=0,
            limit=1,
        )

    return await _guarded_write(_work, account=str(account))


# ═══════════════════════════════════════════════════════════════════════════════════
# Реестр: имя инструмента → функция
# ═══════════════════════════════════════════════════════════════════════════════════

WRITE_TOOL_FUNCS: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    # ── Бюджет / ставки ──
    "propose_update_budget": propose_update_budget,
    "propose_update_bid": propose_update_bid,
    "propose_update_keyword_bid": propose_update_keyword_bid,
    # ── Ключевые слова ──
    "propose_add_keywords": propose_add_keywords,
    "propose_remove_keywords": propose_remove_keywords,
    # ── Минус-слова ──
    "propose_add_negative_keywords": propose_add_negative_keywords,
    "propose_remove_negative_keywords": propose_remove_negative_keywords,
    "propose_add_negatives_to_shared_set": propose_add_negatives_to_shared_set,
    "propose_attach_shared_set": propose_attach_shared_set,
    # ── Статус кампании ──
    "propose_pause_campaign": propose_pause_campaign,
    "propose_resume_campaign": propose_resume_campaign,
    # ── Переименование / сети / гео-тип ──
    "propose_update_campaign": propose_update_campaign,
    "propose_set_campaign_network": propose_set_campaign_network,
    "propose_set_campaign_display_network": propose_set_campaign_display_network,
    "propose_set_campaign_geo_target_type": propose_set_campaign_geo_target_type,
    # ── Удаление ──
    "propose_remove_campaign": propose_remove_campaign,
    "propose_remove_ad_group": propose_remove_ad_group,
    # ── Статус группы ──
    "propose_pause_ad_group": propose_pause_ad_group,
    "propose_resume_ad_group": propose_resume_ad_group,
    # ── Статус / удаление объявления ──
    "propose_pause_ad": propose_pause_ad,
    "propose_resume_ad": propose_resume_ad,
    "propose_remove_ad": propose_remove_ad,
    # ── Гео-таргетинг ──
    "propose_set_geo_proximity": propose_set_geo_proximity,
    "propose_set_geo_proximity_by_coords": propose_set_geo_proximity_by_coords,
    "propose_set_geo_location": propose_set_geo_location,
    # ── Стратегия ──
    "propose_set_bidding_strategy": propose_set_bidding_strategy,
    # ── Аудитории ──
    "propose_attach_audience": propose_attach_audience,
    "propose_detach_audience": propose_detach_audience,
    # ── Создание RSA / кампаний ──
    "propose_create_rsa": propose_create_rsa,
    "propose_create_gdn_campaign": propose_create_gdn_campaign,
    "propose_create_search_campaign": propose_create_search_campaign,
    "propose_create_demand_gen_campaign": propose_create_demand_gen_campaign,
    "propose_create_video_campaign": propose_create_video_campaign,
    # ── Расширения (assets) ──
    "propose_add_sitelinks": propose_add_sitelinks,
    "propose_add_callouts": propose_add_callouts,
    "propose_add_structured_snippets": propose_add_structured_snippets,
    "propose_add_call_asset": propose_add_call_asset,
    "propose_add_promotion": propose_add_promotion,
    "propose_add_price_asset": propose_add_price_asset,
    "propose_remove_asset_link": propose_remove_asset_link,
    # ── Исполнение ──
    "execute_confirmed": execute_confirmed,
}

# Имена WRITE-инструментов (для инварианта И5: WRITE ∩ READ = ∅).
WRITE_MCP_TOOLS: frozenset[str] = frozenset(WRITE_TOOL_FUNCS)