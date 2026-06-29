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

from dataclasses import dataclass, field

from ads.client import ensure_read_allowed
from ads.read import ChildAccount, list_child_accounts
from reports.period import Period
from reports.queries import Metrics, fetch_totals


@dataclass
class ChildReport:
    account: ChildAccount
    totals: Metrics


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


def build_mcc_summary(
    client,
    manager_id: str,
    period: Period,
    *,
    list_children=list_child_accounts,
    fetch=fetch_totals,
) -> MccSummary:
    """Сводка по дочерним MCC. READ-ONLY. `list_children`/`fetch` инъектируются для тестов
    (по умолчанию — реальные `list_child_accounts`/`fetch_totals`).

    Поток: `list_children` держит замок обхода MCC (`ensure_manager_allowed`). Каждый лист-аккаунт
    проходит `ensure_read_allowed` — НЕ в read-list ⇒ пропуск (fail-closed, в `skipped`), а не падение
    всей сводки. Менеджерские строки (свой MCC/суб-MCC) пропускаем — у них нет собственных метрик.

    Замечание по производительности: дочерние читаются ПОСЛЕДОВАТЕЛЬНО (N round-trip). Для большого
    MCC можно распараллелить через `core.resilience.run_ads_read_call` под семафором — отдельный шаг.
    """
    summary = MccSummary(manager_id=str(manager_id), period=period)
    for ch in list_children(client, manager_id):
        if ch.manager:
            summary.managers.append(ch.id)
            continue
        try:
            ensure_read_allowed(ch.id)
        except PermissionError:
            summary.skipped.append(ch.id)  # вне read-list — честно отмечаем, не читаем
            continue
        summary.children.append(ChildReport(account=ch, totals=fetch(client, ch.id, period)))
    summary.subtotals = aggregate_by_currency(summary.children)
    return summary
