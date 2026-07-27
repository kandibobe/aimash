"""Контур автооткатa: наблюдение за применённой мутацией и вердикт «не стало ли хуже».

Волна 4 (§1 плана). Здесь — **только детектор и режим `shadow`**. Мутаций этот модуль не делает
ни одной, наружу в `shadow` не отправляет ничего; он копит вердикты, по которым потом оценивается
точность. Исполнение компенсации — Волна 6a, и оно придёт со своими шестью проверками поверх
девяти существующих, а не отсюда.

Четыре вещи, которые определяют устройство и которые нельзя обойти «настройкой»:

**1. За 3–6 часов измерим РАСХОД, а не CPA.** Конверсия атрибутируется ко времени КЛИКА и
досчитывается сутками: столбец `metrics.conversions` за последние 4 часа систематически занижен и
будет расти ещё двое суток. Поэтому вердикт выносится по расходу и кликам, а формулировка
«деградация CPA» в тексте человеку употребляться не может — мы её не мерили. Это ограничение
данных, а не недоделка: обойти его нечем, кроме как ждать сутки.

**2. База сравнения — тот же час того же дня недели.** Реклама сезонна внутри суток и внутри
недели: сравнивать вечер вторника со средним по неделе значит объявлять деградацией нормальный
вечерний пик. Берём медиану и MAD (робастное отклонение) по тому же часу того же дня недели за
4 предыдущие недели, по той же кампании.

**3. База масштабируется на ОЖИДАЕМЫЙ эффект изменения.** Без этого детектор ловит собственную
причину: человек поднял бюджет на 20% — расход вырос на 20% — «деградация», откат, и так каждый
раз. Изменение бюджета/ставки на то и делается, чтобы расход изменился; сигналом является не сам
рост, а рост СВЕРХ заказанного. Поэтому `expected_ratio` (из снимка «было»→«станет») пишется в
строку наблюдения в момент применения и умножает базу при вердикте.

**4. `insufficient` НИКОГДА не повышается до `degraded`.** Мало данных — не доказательство вреда.
Кампания, стоявшая на паузе месяц, имеет базу «ноль», и любой запуск формально «превышает» её в
бесконечное число раз; такой вердикт — артефакт, а не сигнал. Fail-closed здесь направлен в
сторону БЕЗДЕЙСТВИЯ: сомневаешься — молчи.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from ads.client import build_client_async
from ads.read import HourlyPoint, read_campaign_hourly
from core.ads_errors import is_account_access_error
from core.config import settings
from core.context import reset_context, set_context
from core.errors import capture_exception
from core.resilience import run_ads_read_call

log = logging.getLogger(__name__)

# Сколько предыдущих недель берём в базу сравнения и сколько из них обязаны быть непустыми.
BASELINE_WEEKS = 4
MIN_BASELINE_SAMPLES = 3
# Операции, за которыми наблюдаем. Это ПОДМНОЖЕСТВО `confirm.reverse.ROLLBACKABLE_OPS`, и сужение
# намеренное: расход — единственная метрика, измеримая за часы (пункт 1), а осмысленно сравнивать
# расход можно только там, где известен ожидаемый эффект изменения. У `pause_campaign` расход
# уходит в ноль по замыслу, у `update_campaign` (переименование) не меняется вовсе — вердикт по
# расходу для них не «строгий» и не «мягкий», он бессмысленный. Наблюдать за тем, что нечем
# измерить, значит копить строки, которые потом придётся молча игнорировать.
WATCHABLE_OPS = frozenset({"update_budget", "update_bid", "update_keyword_bid"})
# Ниже этого суммарного расхода за окно вердикт не выносим вовсе: на копейках любая относительная
# метрика скачет, а откатывать бюджет из-за шума на $0.20 — вред, а не польза.
MIN_OBSERVED_MICROS = 1_000_000  # 1.00 в валюте аккаунта


@dataclass(frozen=True)
class Verdict:
    """Результат наблюдения. `metric` называет то, что мерили НА САМОМ ДЕЛЕ (правило 15)."""

    state: str  # ok | degraded | insufficient
    metric: str  # всегда "cost_micros" — см. пункт 1 в шапке модуля
    observed: int  # суммарный расход за окно наблюдения, micros
    baseline: int  # сумма медиан тех же часов за предыдущие недели, micros
    threshold: int  # база + k×MAD, суммарно по часам
    deviation_k: float | None  # во сколько MAD ушли от базы; None — MAD нулевой/базы нет
    hours_observed: int
    hours_with_baseline: int
    reason: str  # почему именно такой вердикт — человекочитаемо, без секретов


def _median(vals: list[int]) -> int:
    s = sorted(vals)
    n = len(s)
    if not n:
        return 0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) // 2


def _mad(vals: list[int]) -> int:
    """Median absolute deviation — робастная замена сигме. Один выброс её не сдвигает, а среднее
    квадратичное сдвигает: именно выбросами реклама и живёт."""
    if not vals:
        return 0
    m = _median(vals)
    return _median([abs(v - m) for v in vals])


def expected_ratio(operation: str, params: dict, before) -> float | None:
    """Во сколько раз изменение ДОЛЖНО было изменить расход. `None` — не вычислимо.

    Считается из той же пары «было → станет», которую видел человек на карточке: «было» — из
    аттестованного снимка `_before`, «станет» — тем же `resolve.compute_new_micros`, которым
    считалось применяемое значение. Не из намерения модели и не из текста.

    `update_bid`/`update_keyword_bid`: ставка — не расход, но при прочих равных расход движется
    вместе с ней, и это лучшее приближение из доступного за часы. Разнородные ставки по группам
    дают `None` — ровно как в `reverse_spec`: одно число их не описывает.

    Возврат `None` означает «наблюдать не за чем»: без ожидаемого эффекта база несопоставима, и
    вердикт был бы про сам факт изменения, а не про его последствия."""
    from ads.resolve import compute_new_micros

    if (
        operation not in WATCHABLE_OPS
        or not isinstance(before, dict)
        or not isinstance(params, dict)
    ):
        return None
    b = before.get("before_micros")
    if isinstance(b, list):  # ставки по группам: сопоставимы только одинаковые
        uniq = {int(x) for x in b if x}
        if len(uniq) != 1:
            return None
        b = next(iter(uniq))
    mode, value = params.get("mode"), params.get("value")
    try:
        b = int(b)
        after = compute_new_micros(b, str(mode), float(value), currency=None)
    except Exception:  # noqa: BLE001 — кривой снимок/режим/DecreaseBelowZero: эффект неизвестен
        return None
    if b <= 0 or after <= 0:
        return None
    return after / b


def assess(
    observed: list[HourlyPoint],
    history: list[HourlyPoint],
    *,
    k: float | None = None,
    ratio: float = 1.0,
) -> Verdict:
    """Чистая функция: ни БД, ни сети, ни Google Ads. `observed` — часы окна наблюдения,
    `history` — часы за предыдущие недели (из них берутся совпадающие по (день недели, час)),
    `ratio` — во сколько раз изменение ДОЛЖНО было изменить расход (см. `expected_ratio`).

    Вердикт агрегатный, а не почасовой: считаем сумму расхода за окно и сумму порогов тех же
    часов. Один шумный час так не переворачивает решение, а устойчивый перерасход — переворачивает.
    Считать «сколько часов превысило» было бы чувствительнее к дроблению окна и давало бы разный
    ответ на одних и тех же данных при разной длине окна."""
    kk = float(settings.rollback_watch_mad_k if k is None else k)
    rr = max(0.01, float(ratio or 1.0))
    obs_total = sum(p.cost_micros for p in observed)

    if not observed:
        return Verdict(
            "insufficient", "cost_micros", 0, 0, 0, None, 0, 0, "нет данных за окно наблюдения"
        )
    if obs_total < MIN_OBSERVED_MICROS:
        return Verdict(
            "insufficient",
            "cost_micros",
            obs_total,
            0,
            0,
            None,
            len(observed),
            0,
            "расход за окно ниже порога значимости — на шуме вердикт не выносим",
        )

    # (день недели, час) → расходы тех же слотов за предыдущие недели.
    buckets: dict[tuple[int, int], list[int]] = {}
    for p in history:
        try:
            wd = date.fromisoformat(p.day).weekday()
        except (ValueError, TypeError):  # чужой формат даты — слот просто не участвует в базе
            continue
        buckets.setdefault((wd, p.hour), []).append(p.cost_micros)

    baseline_total = threshold_total = 0
    with_base = 0
    spread_total = 0
    for p in observed:
        try:
            wd = date.fromisoformat(p.day).weekday()
        except (ValueError, TypeError):
            continue
        samples = buckets.get((wd, p.hour)) or []
        if len(samples) < MIN_BASELINE_SAMPLES:
            continue
        # База масштабируется на ОЖИДАЕМЫЙ эффект: изменение бюджета на +20% и делалось ради роста
        # расхода на ~20%, и объявлять этот рост деградацией значит ловить собственную причину.
        med = int(_median(samples) * rr)
        spread = int(_mad(samples) * rr)
        # MAD == 0 бывает у стабильных слотов (одинаковый расход неделя к неделе). Порог «медиана
        # ровно» объявил бы деградацией лишний цент; берём floor в 25% медианы — это не подгонка, а
        # признание, что нулевой разброс в рекламе означает «мало разрешения», а не «нет шума»:
        # дневная кривая трафика гуляет заметно сильнее, чем на проценты.
        spread = max(spread, med // 4)
        baseline_total += med
        spread_total += spread
        threshold_total += med + int(kk * spread)
        with_base += 1

    if with_base < MIN_BASELINE_SAMPLES:
        return Verdict(
            "insufficient",
            "cost_micros",
            obs_total,
            baseline_total,
            threshold_total,
            None,
            len(observed),
            with_base,
            f"базы сравнения нет: часов с историей {with_base} < {MIN_BASELINE_SAMPLES}",
        )
    if baseline_total <= 0:
        # Кампания в этих слотах раньше не тратила ничего. Формально «рост бесконечный», по сути —
        # сравнивать не с чем. Повышать до degraded нельзя (пункт 3 в шапке).
        return Verdict(
            "insufficient",
            "cost_micros",
            obs_total,
            0,
            0,
            None,
            len(observed),
            with_base,
            "в те же часы прошлых недель расхода не было — база нулевая, сравнение невозможно",
        )

    dev = (obs_total - baseline_total) / spread_total if spread_total > 0 else None
    if obs_total > threshold_total:
        return Verdict(
            "degraded",
            "cost_micros",
            obs_total,
            baseline_total,
            threshold_total,
            dev,
            len(observed),
            with_base,
            f"расход за окно {obs_total / 1e6:.2f} против нормы {baseline_total / 1e6:.2f} "
            f"(порог {threshold_total / 1e6:.2f} = медиана + {kk:g}×MAD тех же часов"
            + (f", база масштабирована ×{rr:.2f} на ожидаемый эффект" if rr != 1.0 else "")
            + ")",
        )
    return Verdict(
        "ok",
        "cost_micros",
        obs_total,
        baseline_total,
        threshold_total,
        dev,
        len(observed),
        with_base,
        "расход в пределах нормы для этих часов",
    )


async def record_watch(
    *,
    confirmation_id: str,
    customer_id: str,
    campaign_id: str,
    operation: str,
    applied_at: datetime | None = None,
    expected_ratio: float | None = None,
) -> bool:
    """Завести наблюдение за применённой мутацией. `True` — строка создана.

    Best-effort по построению: наблюдение — это АУДИТ, а не часть денежного пути, и уронить
    исполнение подтверждённой человеком мутации из-за недоступности этой таблицы было бы обменом
    важного на второстепенное. Отказ ⇒ наблюдения нет, мутация состоялась, запись в лог.

    Вызывающий обязан сам проверить, что операция обратима (`confirm.reverse.reverse_spec` вернул
    не-None): наблюдать за тем, чего не откатить, значит копить вердикты без применения.

    `expected_ratio` берётся из снимка «было → станет» ЗДЕСЬ и сохраняется: к моменту вердикта
    (через несколько часов) прежнего значения в API уже нет, и восстановить ожидаемый эффект будет
    неоткуда."""
    from db.models import RollbackWatch
    from db.session import Session, db_dt

    hours = max(2, int(settings.rollback_watch_window_hours))
    now = datetime.now(timezone.utc)
    started = applied_at or now
    try:
        async with Session() as s:
            exists = (
                await s.execute(
                    select(RollbackWatch.id).where(
                        RollbackWatch.confirmation_id == str(confirmation_id)
                    )
                )
            ).first()
            if exists:  # UNIQUE держит БД, но гонку дешевле не создавать
                return False
            s.add(
                RollbackWatch(
                    confirmation_id=str(confirmation_id),
                    customer_id=str(customer_id),
                    campaign_id=str(campaign_id),
                    operation=str(operation),
                    applied_at=db_dt(started),
                    window_until=db_dt(started + timedelta(hours=hours)),
                    expected_ratio=(float(expected_ratio) if expected_ratio is not None else None),
                    mode=str(settings.rollback_watch_mode or "shadow"),
                    state="watching",
                    created_at=db_dt(now),
                )
            )
            await s.commit()
        return True
    except Exception as e:  # noqa: BLE001 — аудит не смеет ронять применённую мутацию
        log.warning("rollback: наблюдение не заведено (%s)", type(e).__name__)
        return False


def _window_dates(applied_at: datetime, window_until: datetime) -> tuple[str, str]:
    """Диапазон дат окна наблюдения. Берём с запасом в сутки с обеих сторон: `segments.date` —
    дата в таймзоне АККАУНТА, а `applied_at` лежит в UTC, и совпадения календарных суток тут нет
    (§8). Лишние часы отфильтруются при сборке окна, недостающих не будет."""
    a = (applied_at - timedelta(days=1)).date()
    b = (window_until + timedelta(days=1)).date()
    return a.isoformat(), b.isoformat()


async def _observe_one(watch, client) -> Verdict:
    """Прочитать метрики и вынести вердикт по одному наблюдению. READ-ONLY."""
    applied = watch.applied_at
    until = watch.window_until
    if applied.tzinfo is None:  # SQLite отдаёт наивный UTC (db_dt) — доводим до aware
        applied = applied.replace(tzinfo=timezone.utc)
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)

    obs_from, obs_to = _window_dates(applied, until)
    hist_from, hist_to = _window_dates(applied - timedelta(weeks=BASELINE_WEEKS), until)

    rows = await run_ads_read_call(
        read_campaign_hourly,
        client,
        watch.customer_id,
        watch.campaign_id,
        date_from=hist_from,
        date_to=hist_to,
    )
    # Час относим к окну по его началу: час применения неполный (мутация случилась в середине), и
    # его расход смешивает «до» и «после». Отсчёт ведём со СЛЕДУЮЩЕГО полного часа.
    first_full = (applied + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    observed: list[HourlyPoint] = []
    history: list[HourlyPoint] = []
    for p in rows:
        try:
            slot = datetime.fromisoformat(p.day).replace(hour=p.hour, tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if first_full <= slot < until:
            observed.append(p)
        elif slot < applied - timedelta(days=1):
            history.append(p)
    if obs_from > obs_to:  # защита от перевёрнутого окна (кривые данные в строке)
        return Verdict("insufficient", "cost_micros", 0, 0, 0, None, 0, 0, "окно наблюдения пусто")
    return assess(observed, history, ratio=getattr(watch, "expected_ratio", None) or 1.0)


async def run_rollback_watch(bot=None) -> dict:
    """Джоба: вынести вердикты по наблюдениям, чьё окно закрылось.

    В режиме `shadow` (дефолт) — только пишет вердикт. `alert` шлёт человеку текст и НИЧЕГО не
    исполняет: обратный черновик рождается в ЕГО ходе обычным путём, правок гейта ноль. `auto` не
    реализован и здесь отвергается явно, а не молча трактуется как `alert` — молчаливая деградация
    режима исполнения ровно тот класс ошибки, из-за которого настройка «выглядит рабочей»."""
    from db.models import RollbackWatch
    from db.session import Session, db_dt

    mode = str(settings.rollback_watch_mode or "shadow").strip().lower()
    if mode not in ("shadow", "alert", "auto"):
        log.warning("rollback: ROLLBACK_WATCH_MODE=%r неизвестен — работаем в shadow", mode)
        mode = "shadow"
    if mode == "auto":
        # Волна 6a. Пока пути исполнения нет, «auto» обязан вести себя как самый слабый из
        # доступных режимов, а не как самый сильный.
        log.warning("rollback: режим auto ещё не реализован (Волна 6a) — работаем в shadow")
        mode = "shadow"

    result = {"checked": 0, "degraded": 0, "insufficient": 0, "notified": 0}
    now = datetime.now(timezone.utc)
    async with Session() as s:
        due = list(
            (
                await s.execute(
                    select(RollbackWatch)
                    .where(
                        RollbackWatch.state == "watching",
                        RollbackWatch.window_until <= db_dt(now),
                    )
                    .order_by(RollbackWatch.id)
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
    if not due:
        return result

    for watch in due:
        tok = set_context(customer_id=watch.customer_id)
        try:
            client = await build_client_async(watch.customer_id)
            verdict = await _observe_one(watch, client)
            result["checked"] += 1
            if verdict.state == "degraded":
                result["degraded"] += 1
            elif verdict.state == "insufficient":
                result["insufficient"] += 1
            async with Session() as s:
                row = (
                    await s.execute(select(RollbackWatch).where(RollbackWatch.id == watch.id))
                ).scalar_one_or_none()
                if row is None or row.state != "watching":
                    continue  # кто-то уже обработал (второй воркер/ручной разбор)
                row.state = {
                    "ok": "verdict_ok",
                    "degraded": "verdict_degraded",
                    "insufficient": "expired",
                }[verdict.state]
                row.verdict_json = json.dumps(asdict(verdict), ensure_ascii=False)
                row.checked_at = db_dt(datetime.now(timezone.utc))
                await s.commit()
            if mode == "alert" and verdict.state == "degraded" and bot is not None:
                if await _notify(bot, watch, verdict):
                    result["notified"] += 1
        except Exception as e:  # noqa: BLE001 — одна строка не отменяет разбор остальных
            if is_account_access_error(e):
                log.info("rollback: аккаунт %s недоступен (ожидаемо, пропуск)", watch.customer_id)
            else:
                await capture_exception(e, where=f"scheduler:rollback:{watch.customer_id}")
        finally:
            reset_context(tok)
    log.info(
        "rollback: разобрано %d наблюдений (деградация %d, недостаточно данных %d), режим %s",
        result["checked"],
        result["degraded"],
        result["insufficient"],
        mode,
    )
    return result


async def _notify(bot, watch, verdict: Verdict) -> bool:
    """Режим `alert`: сообщить человеку и остановиться. Ни черновика, ни мутации отсюда не рождается.

    Формулировка называет метрику честно — «расход», а не «CPA»: за окно в несколько часов
    конверсии недосчитаны, и написать «упал CPA» значило бы выдать неизмеренное за измеренное
    (правило 15). Числа форматирует КОД.

    Адресат берётся из РОДИТЕЛЬСКОГО черновика: наблюдение принадлежит конкретному подтверждению,
    и сигнал должен прийти туда же, где человек это изменение заказывал, — а не в общий канал.
    Черновика нет (убран ретеншном) ⇒ адресата нет ⇒ молчим."""
    from confirm.store import ConfirmStore

    snap = await ConfirmStore().get_confirmed(watch.confirmation_id)
    if snap is None or not snap.chat_id:
        log.info("rollback: адресат для %s не найден — алерт не шлём", watch.confirmation_id)
        return False

    text = (
        f"⚠️ Наблюдение после изменения «{watch.operation}» (кампания {watch.campaign_id}, "
        f"аккаунт {watch.customer_id}).\n"
        f"Расход за {settings.rollback_watch_window_hours} ч — {verdict.observed / 1e6:.2f}, "
        f"норма для этих часов — {verdict.baseline / 1e6:.2f}.\n"
        f"{verdict.reason}\n"
        "Это сигнал, а не откат: если решите вернуть прежнее значение — напишите, и я подготовлю "
        "обычный черновик с подтверждением."
    )
    try:
        await bot.send_message(snap.chat_id, text)
        return True
    except Exception as e:  # noqa: BLE001 — доставка не должна ронять разбор
        log.warning("rollback: алерт не доставлен (%s)", type(e).__name__)
        return False
