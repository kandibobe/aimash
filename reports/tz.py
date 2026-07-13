"""§8: окно отчёта — в ТАЙМЗОНЕ АККАУНТА, а не хоста.

Google Ads режет `segments.date` по таймзоне аккаунта. Пока бот строил окно по дате хоста,
относительные периоды («последние 7 дн.», «с начала месяца») уезжали на день для аккаунтов,
чья TZ расходится с хостовой:

* аккаунт западнее хоста (Европа-хост → аккаунт в Америке): «вчера» хоста для аккаунта ещё
  СЕГОДНЯ → в окно попадал неполный день → «просадка» на ровном месте;
* аккаунт восточнее (Уганда/Кения при вечернем прогоне из Европы): последний полный день
  аккаунта в окно НЕ попадал.

Логика уже была написана трижды (scheduler._account_period, bot._mcc_period_factory,
bot._account_local_date) и не применялась к /report, /export, /sheets, /audit, /bids,
/searchterms. Здесь она одна, а вызывающие делегируют.

Правило: пере-якоряем ТОЛЬКО относительные окна (kind last_n | mtd). Произвольные даты
(kind custom: «за 3 августа», ISO-диапазон) менеджер назвал ЯВНО — их не трогаем.
READ-ONLY, без секретов. Чтение TZ — best-effort: сбой/неизвестная зона → дата хоста (как раньше).
"""

from __future__ import annotations

from datetime import date, datetime

from core.resilience import run_ads_read_call
from reports.period import Period, last_n_days, month_to_date


def reanchor(period: Period, today: date) -> Period:
    """Пересобрать ОТНОСИТЕЛЬНОЕ окно от указанного «сегодня». custom → как есть (идемпотентно:
    reanchor(reanchor(p, t), t) == reanchor(p, t) — вызывающие могут якорить повторно)."""
    if period.kind == "last_n" and period.n > 0:
        return last_n_days(period.n, today=today)
    if period.kind == "mtd":
        return month_to_date(today=today)
    return period


async def account_today(client, customer_id: str, *, label: str = "acct_tz") -> date:
    """Сегодня в таймзоне аккаунта. Best-effort: TZ не прочитана/неизвестна → дата хоста.
    Чтение кэшируется в ads.read._TIMEZONE_CACHE (один GAQL на аккаунт за процесс)."""
    try:
        from zoneinfo import ZoneInfo

        from ads.read import account_timezone

        tz = await run_ads_read_call(account_timezone, client, str(customer_id), label=label)
        if tz:
            return datetime.now(ZoneInfo(tz)).date()
    except Exception:  # noqa: BLE001 — TZ необязательна: окно по host-дате, отчёт не роняем
        pass
    return datetime.now().date()


async def account_period(client, customer_id: str, period, *, label: str = "acct_tz"):
    """Единая точка §8: окно, пере-якоренное на «сегодня» аккаунта. Абсолютный период (custom) и
    None (вызывающий без окна) возвращаются сразу — без чтения TZ (незачем тратить запрос)."""
    if period is None or getattr(period, "kind", "custom") == "custom":
        return period
    return reanchor(period, await account_today(client, customer_id, label=label))
