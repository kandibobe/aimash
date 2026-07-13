"""Сводный отчёт по дочерним аккаунтам MCC (ТЗ §8). READ-ONLY.

Обходит дочерние (`list_child_accounts` → замок обхода `ensure_manager_allowed`), берёт totals на
каждый РАЗРЕШЁННЫЙ на чтение лист-аккаунт (`ensure_read_allowed`) и агрегирует ПО ВАЛЮТАМ.

⚠️ FX-курсы НЕ выдумываем (деньги считает КОД, не модель): аккаунты с разной валютой НЕ
суммируются «в одну сумму» — даём честные подытоги на КАЖДУЮ валюту. Единый grand-total через
сконфигурированный источник курсов — отдельная опция позже, чтобы не врать про деньги.

Замок: мутации не затрагиваются (свой узкий `ensure_allowed`). Дочерние вне read-list
(`GOOGLE_ADS_READ_CUSTOMER_IDS`) и сами менеджерские аккаунты — пропускаются (fail-closed), и
пропуски ВИДНЫ в `MccSummary.skipped`/`.managers` (без тихого замалчивания).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ads.client import ensure_read_allowed
from ads.read import ChildAccount, list_child_accounts
from core.logging import redact_text
from core.resilience import run_ads_read_call
from reports.period import Period
from reports.queries import Metrics, campaign_status_counts, fetch_totals


@dataclass
class ChildReport:
    account: ChildAccount
    totals: Metrics
    active_campaigns: int | None = (
        None  # §8 (P1-G): активных (ENABLED) кампаний; None = не прочитано
    )
    # §8: РАЗБИВКА кампаний по статусу ({ENABLED: n, PAUSED: m, …}); None = не прочитано. active_campaigns
    # — производное (ENABLED-счётчик) для обратной совместимости рендера/xlsx.
    campaign_status: dict[str, int] | None = None


@dataclass
class CurrencySubtotal:
    currency: str
    accounts: int  # сколько аккаунтов с этой валютой
    totals: Metrics  # агрегат по валюте (производные пересчитываются из суммы сырых счётчиков)


@dataclass
class MccSummary:
    manager_id: str
    period: Period
    children: list[ChildReport] = field(default_factory=list)  # включённые (read-allowed) листы
    subtotals: list[CurrencySubtotal] = field(default_factory=list)  # агрегат по валюте
    skipped: list[str] = field(default_factory=list)  # id листов вне read-list (fail-closed)
    managers: list[str] = field(default_factory=list)  # id менеджерских (нет собственных метрик)
    # Неактивные листы (status != ENABLED: CANCELED/SUSPENDED/CLOSED/…): их НЕ запрашиваем на метрики
    # (запрос упал бы или вернул нули) → они больше НЕ попадают в errors. Показываем именами отдельной
    # секцией (это и есть большинство прежних «ошибок чтения»). Храним ChildAccount ради имени/статуса.
    inactive: list[ChildAccount] = field(default_factory=list)
    # Частичные сбои чтения дочернего (сеть/SDK/таймаут): (id, редактированная причина). Сбой одного
    # аккаунта НЕ топит всю сводку (build_mcc_summary_async, return_exceptions) — но и не молчит.
    errors: list[tuple[str, str]] = field(default_factory=list)


def _is_active(ch: ChildAccount) -> bool:
    """Аккаунт активен (есть смысл запрашивать метрики). Только ENABLED. Прочие статусы
    (CANCELED/SUSPENDED/CLOSED/UNKNOWN/UNSPECIFIED) → неактивен: метрику НЕ тянем (иначе строка в
    errors или нулевой шум), показываем отдельной секцией «неактивные»."""
    return (ch.status or "").upper() == "ENABLED"


def aggregate_by_currency(children: list[ChildReport]) -> list[CurrencySubtotal]:
    """Подытоги по валюте (БЕЗ FX): на каждую валюту — свой агрегат `Metrics`. Порядок — по
    убыванию расхода. Производные (CTR/CPA/ROAS) корректны, т.к. считаются из суммы сырых счётчиков."""
    by_cur: dict[str, CurrencySubtotal] = {}
    for cr in children:
        cur = cr.account.currency or "?"
        sub = by_cur.get(cur)
        if sub is None:
            sub = CurrencySubtotal(currency=cur, accounts=0, totals=Metrics())
            by_cur[cur] = sub
        sub.totals.add(cr.totals)
        sub.accounts += 1
    return sorted(by_cur.values(), key=lambda s: s.totals.cost_micros, reverse=True)


@dataclass
class MccDeep:
    """2.2 (аудит 2026-07-06): ГЛУБОКИЙ отчёт по всем дочерним MCC — ReportData (§9-разбивки)
    на каждый read-allowed ENABLED лист. items: (ChildAccount, ReportData). Бухгалтерия пропусков —
    как в MccSummary (без тихого замалчивания)."""

    manager_id: str
    period: Period
    items: list = field(default_factory=list)  # (ChildAccount, reports.service.ReportData)
    skipped: list[str] = field(default_factory=list)
    managers: list[str] = field(default_factory=list)
    inactive: list[ChildAccount] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)


async def build_mcc_deep_async(
    client,
    manager_id: str,
    period: Period,
    *,
    list_children=list_child_accounts,
    build_report=None,
    tz_of=None,
    period_for=None,
) -> MccDeep:
    """2.2: DEEP-отчёт по всем дочерним MCC (лист на аккаунт в xlsx) — поверх обхода
    build_mcc_summary_async, но на каждый ребёнок собирается ПОЛНЫЙ ReportData
    (build_account_report_async: totals+prev+7 разбивок §9, все GAQL параллелятся под общим
    семафором). READ-ONLY; замки те же: ensure_manager_allowed (list_children) +
    ensure_read_allowed per-child (не в read-list ⇒ skipped, fail-closed). Сбой одного ребёнка →
    errors, не провал книги (gather return_exceptions). ~7 акк. × ~10 GAQL ≈ 70+ запросов —
    вызывающий показывает прогресс и держит общий таймаут (asyncio.wait_for)."""
    if build_report is None:
        from reports.service import build_account_report_async as build_report  # noqa: N813

    deep = MccDeep(manager_id=str(manager_id), period=period)
    children = await run_ads_read_call(list_children, client, manager_id, label="mccdeep_children")
    eligible: list[ChildAccount] = []
    for ch in children:
        if ch.manager:
            deep.managers.append(ch.id)
            continue
        try:
            ensure_read_allowed(ch.id)
        except PermissionError:
            deep.skipped.append(ch.id)
            continue
        if not _is_active(ch):
            deep.inactive.append(ch)
            continue
        eligible.append(ch)

    async def _deep_child(ch: ChildAccount):
        per = period
        if tz_of is not None and period_for is not None:
            try:
                tz = await run_ads_read_call(tz_of, client, ch.id, label=f"mccdeep_tz_{ch.id}")
                if tz:
                    per = period_for(tz) or period
            except Exception:  # noqa: BLE001 — TZ best-effort, общий period
                per = period
        return await build_report(
            client,
            ch.id,
            per,
            currency=ch.currency or "",
            account_name=getattr(ch, "name", "") or "",
        )

    results = await asyncio.gather(*[_deep_child(ch) for ch in eligible], return_exceptions=True)
    for ch, res in zip(eligible, results):
        if isinstance(res, Exception):
            deep.errors.append((ch.id, f"{type(res).__name__}: {redact_text(str(res))}"))
            continue
        deep.items.append((ch, res))
    return deep


async def build_mcc_summary_async(
    client,
    manager_id: str,
    period: Period,
    *,
    list_children=list_child_accounts,
    fetch=fetch_totals,
    tz_of=None,
    period_for=None,
) -> MccSummary:
    """Сводка по дочерним MCC (ЕДИНСТВЕННАЯ; синхронный близнец удалён — он не вызывался ниоткуда,
    а инварианты пропусков тестировались ТОЛЬКО на нём). Дочерние читаются ПАРАЛЛЕЛЬНО (а не N
    round-trip подряд): для ~10 аккаунтов — ceil(N/ADS_MAX_CONCURRENCY) волн вместо N
    последовательных. READ-ONLY; `list_children`/`fetch` инъектируются для тестов.

    §8-нормализация таймзон (опц.): если переданы `tz_of(client, cid) -> tz` и
    `period_for(tz) -> Period`, окно каждого дочернего строится в ЕГО таймзоне (чтобы не смешивать
    неполные календарные дни разных TZ). Оба None (дефолт/тесты) ⇒ у всех единый `period` (поведение
    не меняется). Сбой чтения TZ одного аккаунта → откат на общий `period` (не роняет его строку).

    Отличие от build_account_report_async: gather с return_exceptions=True — сбой ОДНОГО дочернего
    (сеть/SDK/таймаут) НЕ роняет всю сводку, а попадает в `summary.errors` (редактированно, без
    секретов; §5). Замок обхода (ensure_manager_allowed) держит list_children; per-account замок
    чтения — ensure_read_allowed (не в read-list ⇒ skipped, fail-closed). Менеджерские строки
    пропускаем (нет собственных метрик)."""
    summary = MccSummary(manager_id=str(manager_id), period=period)
    children = await run_ads_read_call(list_children, client, manager_id, label="mcc_children")
    eligible: list[ChildAccount] = []
    for ch in children:
        if ch.manager:
            summary.managers.append(ch.id)
            continue
        try:
            ensure_read_allowed(ch.id)
        except PermissionError:
            summary.skipped.append(ch.id)  # вне read-list — честно отмечаем, не читаем
            continue
        if not _is_active(ch):
            summary.inactive.append(ch)  # CANCELED/SUSPENDED/… — не метрику, не «ошибка»
            continue
        eligible.append(ch)

    async def _fetch_child(ch: ChildAccount):
        """Прочитать totals одного дочернего в ЕГО таймзоне (§8), если заданы tz_of/period_for."""
        per = period
        if tz_of is not None and period_for is not None:
            try:
                tz = await run_ads_read_call(tz_of, client, ch.id, label=f"mcc_tz_{ch.id}")
                if tz:
                    per = period_for(tz) or period
            except Exception:  # noqa: BLE001 — TZ не прочитан → общий period (не роняем строку)
                per = period
        totals = await run_ads_read_call(fetch, client, ch.id, per, label=f"mcc_{ch.id}")
        # §8: разбивка кампаний по СТАТУСУ — best-effort доп. лёгкий запрос (сбой → None, не роняем
        # строку и не топит сводку). Параллелится фан-аутом по аккаунтам (не сериализует весь /mcc).
        # active_campaigns (ENABLED-счётчик) — производное для обратной совместимости рендера/xlsx.
        status_counts: dict[str, int] | None = None
        try:
            status_counts = await run_ads_read_call(
                campaign_status_counts, client, ch.id, label=f"mcc_camps_{ch.id}"
            )
        except Exception:  # noqa: BLE001 — разбивка по статусу необязательна
            status_counts = None
        return totals, status_counts

    # Параллельный фан-аут по разрешённым листам под общим семафором Google Ads (как
    # build_account_report_async). return_exceptions: один упавший аккаунт → строка в errors, а не
    # провал всей сводки (частичный результат для портфеля из ~10 — приемлем, тишина — нет).
    results = await asyncio.gather(
        *[_fetch_child(ch) for ch in eligible],
        return_exceptions=True,
    )
    for ch, res in zip(eligible, results):
        if isinstance(res, Exception):
            summary.errors.append((ch.id, f"{type(res).__name__}: {redact_text(str(res))}"))
            continue
        totals, status_counts = res  # (Metrics, dict|None) — см. _fetch_child (§8)
        active = status_counts.get("ENABLED") if isinstance(status_counts, dict) else None
        summary.children.append(
            ChildReport(
                account=ch,
                totals=totals,
                active_campaigns=active,
                campaign_status=status_counts,
            )
        )
    summary.subtotals = aggregate_by_currency(summary.children)
    return summary
