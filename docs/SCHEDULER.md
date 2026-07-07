# Планировщик: отчёты, аномалии, очистка (ТЗ §3, golden rule #3)

Фоновые задачи на APScheduler в общем event loop бота. **READ-ONLY + уведомления.** Планировщик
**никогда не меняет аккаунт**: он не импортирует `ads.mutations` и не зовёт `execute_confirmed`/
`apply_*` (golden rule #3 — это гард в коде, не в промпте). Реализация:
[`scheduler/service.py`](../scheduler/service.py) (запуск), [`scheduler/jobs.py`](../scheduler/jobs.py)
(задачи), [`scheduler/anomaly.py`](../scheduler/anomaly.py) (детектор). Тесты —
[`tests/test_scheduler.py`](../tests/test_scheduler.py).

## Задачи и кадэнс (`scheduler/service.py`)
| Задача | Триггер по умолчанию | env | Что делает |
|---|---|---|---|
| `run_scheduled_report` (глобальная) | cron **09:00** ежедневно | `REPORT_SCHEDULE` | плановый отчёт за последние 7 дн. → рассылка операторам БЕЗ персонального расписания |
| `run_scheduled_report` (персональная, `only_chat`) | cron из `UserSettings.report_schedule` | — (per-user) | §14: свой отчёт оператору с собственным расписанием (`register_user_report_schedules` на старте) |
| `run_anomaly_check` | каждые **6 ч** | `ANOMALY_INTERVAL_HOURS` | week-over-week сравнение, алерт при спайке расхода / падении конверсий |
| `cleanup_stale_proposals` | каждые **60 мин** | `CLEANUP_INTERVAL_MINUTES` | просроченные `pending`-черновики → `rejected` (с аудитом) |
| `cleanup_stale_campaign_drafts` | каждые **60 мин** | `CAMPAIGN_DRAFT_TTL_HOURS` (72) | §19: активные черновики визарда старше TTL → `abandoned` (переживают рестарт, но не вечно) |
| `reconcile_stale_crawls` | каждые **60 мин** | `CRAWL_STALE_MINUTES` (30) | §20.4: зависшие `running` crawl_jobs (процесс умер на рестарте) → `failed` |
| `reconcile_stale_executing` | каждые **60 мин** + прогон сразу на старте | `EXECUTING_STALE_MINUTES` (30) | §12: `executing`-черновики (крэш ПОСЛЕ claim — исход мутации в Ads НЕИЗВЕСТЕН) → терминальный `needs_review` + audit + уведомление владельца; НЕ авто-ретрай |

Кадэнс задаётся из env (не зашит): cron глобального отчёта — `REPORT_SCHEDULE`; интервалы —
`ANOMALY_INTERVAL_HOURS` и `CLEANUP_INTERVAL_MINUTES` (последний — общий кадэнс всех очисток/
реконсиляций). **Персональное расписание:** оператор с непустым `UserSettings.report_schedule` получает
отдельную per-chat cron-джобу (`register_user_report_schedules`), а глобальная рассылка его ПРОПУСКАЕТ
(без дубля).

**Получатели** — `settings.whitelist` (env-whitelist, бутстрап), пустой ⇒ задача пропускается.
⚠️ Планировщик — **не** hot-path и намеренно использует **только env-whitelist**, а не объединение
env ∪ БД: операторы, добавленные в рантайме (`/adduser`, таблица `whitelist`), начинают получать
плановые отчёты/алерты **после рестарта** процесса (плановая рассылка не критична к секунде). Дайджест/
алерты локализуются per-recipient (язык из `/lang`), денежные суммы несут код валюты аккаунта.

## Детектор аномалий (`scheduler/anomaly.py`)
`detect_anomalies(current, previous, thresholds)` — **чистая логика** (без SDK/сети, полностью
тестируема). Сравнивает текущий период с предыдущим равным. Пороги по умолчанию:

| Порог | Дефолт | Смысл |
|---|---|---|
| `spend_spike_pct` | 50.0 | расход вырос на ≥ X% к пред. периоду → алерт |
| `conv_drop_pct` | 50.0 | конверсии упали на ≥ X% → алерт |
| `min_spend` | 1.0 | игнорировать шум при копеечном расходе (в валюте аккаунта) |

Виды сигналов (`Alert.kind`): `spend_spike`, `conv_drop`, `spend_no_conv` (расход есть, конверсий
нет, а раньше были). Если в обоих периодах расход < `min_spend` — молчим (не шумим на нуле).
Деления на ноль нет: при `prev<=0` процент = `None` (сигнал не строится).

### Пороги per-chat
Переопределяются на пользователя через `UserSettings.alert_thresholds` (JSON) — **точка входа:
команда `/alerts`** (пресеты + ручной ввод «✏️»; диапазоны валидирует код). Метрики аккаунта
общие, но алерты считаются для каждого получателя со своими порогами (иначе — дефолтные). Это
**только чтение** настроек Google Ads — аккаунт не трогается.

## Очистка (три задачи, общий кадэнс `CLEANUP_INTERVAL_MINUTES`)
- **`cleanup_stale_proposals`** — `pending`-черновики мутаций старше `PROPOSAL_TTL_HOURS`
  (env, дефолт 24 ч — 2.6: раньше значение было зашито в коде) →
  `rejected` (с audit). Безопасно: они **не подтверждались** → SDK не звался → деньги не тратились.
- **`cleanup_stale_campaign_drafts`** (§19) — активные черновики визарда старше
  `CAMPAIGN_DRAFT_TTL_HOURS=72` → `abandoned`. Черновик визарда переживает рестарт и Sheets
  round-trip, но не живёт вечно. Это НЕ proposal (Google Ads не трогается).
- **`reconcile_stale_crawls`** (§20.4) — `running` crawl_jobs старше `CRAWL_STALE_MINUTES=30` →
  `failed`: краул — in-process asyncio-задача, умирает с процессом; на рестарте «висящие» задачи
  честно помечаются провалом (иначе остались бы `running` навсегда).

Возраст сравнивается в Python (наивный `created_at` из SQLite трактуется как UTC) — корректно и на
SQLite, и на Postgres (tz-aware). Жизненный цикл proposal — [DATABASE.md](DATABASE.md).

## Устойчивость
Один недоступный чат **не роняет** рассылку (исключение на отправку логируется редактированно и
идёт дальше). Сбой сети/SDK при сборке отчёта/метрик логируется и не валит планировщик.
`misfire_grace_time` задан на каждую задачу (отчёт 3600с, аномалии 1800с, очистка 600с).

Формулировка для пользователя в алертах прямая: «Это только сигнал — сам я ничего не меняю. Реши и
дай команду». Гарантии безопасности целиком — [SECURITY.md](SECURITY.md).

## Новое (аудит 2026-07-06, Фазы 1–2)
- **`advise_digest` → «утренний экран действий»** — топ-N рекомендаций ПО ВСЕМ аккаунтам разом
  (доля расхода под риском, БЕЗ FX) отдельными карточками с кнопками 👍/👎/🙈/применить; персист
  только показанных. Кнопка «применить» лишь СТАРТУЕТ confirm-гейт (proposal из scheduler не
  создаётся). `ADVISE_DIGEST_TOP_N=5`, `ADVISE_DIGEST_SEND_PAUSE=0.7`.
- **`business_digest`** — недельный БИЗНЕС-дайджест менеджерам (WoW-сводка + CPA + топ-3 совета +
  аномалии), opt-in `/bizdigest`; cron `BUSINESS_DIGEST_SCHEDULE=0 9 * * 1`.
- **`mcc_rediscovery`** — суточный пере-обход детей MCC (новый дочерний виден без рестарта);
  `MCC_REDISCOVERY_HOURS=24` (0 = выкл). Кэши клиентов не сбрасывает (в отличие от `/refresh`).
- **`threshold_tuning`** — авто-подстройка порогов аномалий: READ-ONLY расчёт волатильности
  (12 недель, `scheduler/threshold_tuner.py`) и ПРЕДЛОЖЕНИЕ per-account порогов с кнопкой
  «✅ Принять» (запись — только тап человека). Opt-in `THRESHOLD_TUNE_ENABLED=true`,
  cron `THRESHOLD_TUNE_SCHEDULE=0 10 * * 2`; анти-спам 14/28 дней.
- **Окна отчёта/аномалий** — `REPORT_WINDOW_DAYS=7`, `ANOMALY_WINDOW_DAYS=7` (2.6: раньше в коде).
- **`/myschedule`** — сеттер персонального расписания отчёта (пресеты/cron/off); применяется
  без рестарта; персональные расписания видят и рантайм-whitelisted операторов (env ∪ БД).
