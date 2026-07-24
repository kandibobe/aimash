# БД и миграции

ORM — SQLAlchemy 2.0 ([`db/models.py`](../db/models.py)); схему в проде ведёт **Alembic**
([`migrations/`](../migrations/)). Dev/тесты могут работать на SQLite; прод — Postgres 16.

## Все таблицы (15)

Полный перечень моделей из [`db/models.py`](../db/models.py). Каждая таблица подтверждена
Alembic-миграцией (см. колонку «Миграция»). Колонка «Статус» отражает **реальное** использование
в рантайме (что читается/пишется кодом), а не намерение.

| Таблица | Назначение | Ключевые поля | Миграция | Статус |
|---|---|---|---|---|
| `whitelist` | рантайм-allow-list доступа к БОТУ (§12); объединяется с env `TELEGRAM_WHITELIST_CHAT_IDS` | `chat_id` (unique), `added_by`, `note` | `0001` (drop `0016` → возврат `0017`) | **ACTIVE** — читается гейтом (см. ниже) |
| `user_settings` | язык, пороги алертов (`/alerts`), override модели, активный аккаунт, per-user расписание отчётов, ui_prefs | `chat_id` (unique), `report_schedule` (crontab-строка; **читается** планировщиком — `scheduler.service.register_user_report_schedules` заводит per-chat cron-джобу; **UI-сеттер есть** — `bot/handlers/commands.py:1109` (пресеты/свой cron), сохранение `_save_report_schedule`), `alert_thresholds` (JSON, пишет `/alerts`), `model_override`, `language`, `selected_customer_id`, `ui_prefs` (JSON) | `0001` (+`0005`,`0007`,`0015`) | ACTIVE |
| `proposals` | очередь черновиков изменений Google Ads (diff «было→станет») | `confirmation_id` (unique), `operation`, `customer_id`, `summary`, `params` (JSON), `user_initiated`, `status`, **провенанс хода**: `origin_human_turn`, `author_user_id`, `run_id`, `tg_message_id` | `0001` (+индекс `0003`, +`0030` провенанс) | ACTIVE |
| `audit_log` | журнал всех операций (кто/когда/что/результат), по `confirmation_id` | `confirmation_id`, `operation`, `customer_id`, `chat_id`, `actor_user_id`, `status`, `result` (JSON) | `0001` (+`0004` актор) | ACTIVE |
| `oauth_tokens` | per-account refresh-токены (§8), **зашифрованы at-rest** | `account` (unique), `refresh_token_enc`, `login_customer_id` | `0001` (+`0008`) | **ACTIVE** — загружается на старте (см. ниже) |
| `error_events` | перехваченные исключения для триажа (§15), текст **редактирован** | `request_id`, `chat_id`, `customer_id`, `where`, `exc_type`, `message`, `traceback` | `0006` (+`0011`) | ACTIVE |
| `account_access` | пер-пользовательский доступ к аккаунтам (§12), fail-closed | `chat_id`, `customer_id` (unique пара) | `0009` | ACTIVE |
| `campaign_templates` | именованные пресеты параметров кампании (§2B) | `chat_id`, `name` (unique пара), `params` (JSON), `source_campaign` | `0010` | ACTIVE |
| `campaign_drafts` | накопленный черновик 8-этапного визарда «Создание кампании» (§19); переживает рестарт | `session_id` (unique), `chat_id`, `customer_id`, `preview_customer_id`, `current_step`, `wizard_state` (JSON), `status` | `0012` | ACTIVE |
| `client_profiles` | база знаний о клиенте на `customer_id` (§20): бренд/ниша/гео/сайт | `customer_id` (unique), `brand`, `business_desc`, `geo`, `language`, `website`, `socials` (JSON), `notes`, `last_crawled_at` | `0013` | ACTIVE |
| `client_contacts` | контакты клиента (§20.7): телефон/e-mail/адрес/соцсеть/мессенджер | `profile_id`, `kind`, `value` | `0013` | ACTIVE |
| `client_services` | услуги/товары клиента (§20.7): для сниппетов/callouts/релевантности | `profile_id`, `name`, `description`, `price`, `category` | `0013` | ACTIVE |
| `client_site_pages` | карта страниц сайта после краулинга (§20.7) → sitelinks + сырьё для досье | `profile_id`, `url`, `title`, `page_type`, `key_links` (JSON), `content_hash`, `text` (очищенный от шаблона; ретеншн `site_page_text_retain_days`) | `0013` (+`0014` hash, +`0028` text) | ACTIVE |
| `crawl_jobs` | журнал задач краулинга сайта (§20.4): статус/страницы/ошибка | `job_id` (unique), `customer_id`, `chat_id`, `domain`, `mode`, `status`, `pages_crawled`, `error` | `0013` | ACTIVE |
| `client_profile_history` | версии профиля «до» для отката/аудита (§20.5); переживают clear | `customer_id`, `snapshot` (JSON), `operation`, `confirmation_id` | `0013` | ACTIVE |
| `client_dossiers` | досье по сайту клиента (§20, map-reduce): `.md`-файл владельцу + PII-free контекст генераторам. Своя таблица, а НЕ поля профиля: снапшоты `client_profile_history` переживают «🗑 Очистить профиль», и имена сотрудников клиента остались бы в БД после удаления — здесь каскад по `profile_id` их уносит | `customer_id`, `profile_id`, `version` (монотонная), `status` (draft\|current), `markdown` (с контактами), `llm_context` (без PII), `data` (JSON) | `0029` | ACTIVE |
| `account_health_snapshot` | агрегаты health-score `/audit` на дату в TZ аккаунта (субстрат трендов, N1.1); без PII/имён кампаний | `customer_id` + `snapshot_date` + `period_days` (unique тройка), `score`, `grade`, `at_risk`, `family_penalty` (JSON), `score_model_version` | `0022` | ACTIVE |
| `sheet_exports` | реестр созданных ботом Google-таблиц (`/sheets`, ключи визарда §19.4.2) → выдача в `/mysheets`; секретов нет (url уже уходил в чат) | `chat_id` (+ индекс `chat_id, id`), `customer_id`, `kind` (keywords\|report), `spreadsheet_id`, `url`, `title`, `share` (роль\|off\|failed) | `0025` | ACTIVE |
| `ads_quota_ops` | распределённый счётчик дневной квоты Google Ads (§3, `core.quota`): общий стор вместо per-process deque — bot + scheduler + per-session MCP видят один потолок; PII/секретов нет (`account` = customer_id) | `ts` (индекс + `account,ts`), `account` (customer_id\|NULL), `kind` (read\|mutate), `op_count`; окно 24ч в SQL `SUM(op_count) WHERE ts>cutoff` (`db_dt`), ретеншн `ads_quota_ops_retain_days` | `0031` | ACTIVE |

> Все таблицы объявлены в `db/models.py` и создаются миграциями `0001`–`0031`
> (`op.create_table(...)`). Инициалка `0001` создаёт базовые таблицы
> (`whitelist`, `user_settings`, `proposals`, `audit_log`, `oauth_tokens`;
> [`migrations/versions/0001_initial.py:22-92`](../migrations/versions/0001_initial.py)), остальные —
> последующими ревизиями. Таблица `whitelist` была удалена в `0016` (тогда — мёртвая) и **возвращена
> уже рантайм-активной** в `0017` (см. ниже).

## `whitelist` — рантайм-allow-list (drop `0016` → возврат `0017`)

Гейтинг доступа к БОТУ делает `WhitelistMiddleware`, и его источник — **объединение**
env `TELEGRAM_WHITELIST_CHAT_IDS` **∪ таблица `whitelist`** (`core.access.is_whitelisted`, кэш с TTL).
Fail-closed: пустое объединение блокирует всех; в prod пустой env-набор роняет старт (env — бутстрап
первого админа). Сбой чтения БД трактуется как пустой БД-набор (fail-closed: env-бутстрап проходит,
неизвестные — отказ).

История таблицы: создана в `0001`, но долго **не читалась** рантаймом (иллюзия БД-allow-list) →
удалена как мёртвая в `0016_drop_whitelist` (аудит 2026-07) → **возвращена и подключена к гейту** в
`0017_whitelist_runtime` (2026-07-04) с колонками `added_by`/`note`. Пополнение — админом
(`ADMIN_CHAT_IDS`): `/adduser <chat_id> [note]` (+ inline-пикер read-scope), `/removeuser <chat_id>`,
`/users` (список env ∪ БД). Грант чтения whitelist **не** открывает мутаций (отдельный замок).

Пер-пользовательский доступ к АККАУНТАМ (не к боту) — таблица `account_access` (`/grant`, `/revoke`;
режим — env `ACCOUNT_ACCESS_MODE`).

## `oauth_tokens` — ACTIVE (загрузка на старте)

Per-account OAuth-токены (§8/мультиаккаунт) **загружаются при старте бота**:
`load_oauth_cache()` вызывается в стартовой последовательности после `init_db()`
([`bot/main.py:5533-5535`](../bot/main.py)). Функция читает все строки `oauth_tokens`, расшифровывает
`refresh_token_enc` через `core.secrets.decrypt` и кладёт `(refresh_token, login_customer_id)` в
рантайм-кэш `_OAUTH_RUNTIME` ([`ads/client.py:112-139`](../ads/client.py)). Далее `build_client(child)`
для дочерних под другими MCC берёт per-account креды из этого кэша.

Отказоустойчивость: сбой расшифровки ОДНОГО аккаунта логируется и пропускается (не роняет старт;
[`ads/client.py:130-134`](../ads/client.py)); общий сбой `load_oauth_cache` тоже не критичен — Draft и
тест-MCC покрыты единым `.env`-токеном ([`bot/main.py:5536-5537`](../bot/main.py),
`ads/client._env_cfg`). Пусто ⇒ работает Draft на едином `.env`-токене.

Статус at-rest-шифрования как задела под §8 (таблица + `core.secrets`) описан в
[`core/secrets.py`](../core/secrets.py); замок мутаций (только Draft `7753643025`) от этого не зависит.

### Где секреты
Refresh-токены хранятся **только** зашифрованными (`oauth_tokens.refresh_token_enc`,
`core.secrets.encrypt`). В `proposals.params` и `audit_log.result` секретов **нет** by design — туда
идут структурированные (Pydantic-валидированные) параметры и результат операции. PII клиента (§20:
телефоны/e-mail в `client_contacts`) — не секрет проекта, но в логи сырьём не пишется; текст ошибок в
`error_events.message`/`crawl_jobs.error` **редактируется** (`core.logging.redact_text`, golden rule #5).

## Жизненный цикл proposal
`status`: `pending → confirmed → executing → applied` (или `failed` / `rejected`)
([`db/models.py:85-87`](../db/models.py)).
- `user_initiated` дефолтит в **`False`** (fail-closed; [`db/models.py:84`](../db/models.py)): только
  доверенный вход — Telegram-команда человека — ставит `True`. Автосоздатель (scheduler/anomaly),
  забывший флаг, получит `False`, и бюджет/ставка будут заблокированы гейтом (golden rule #3). Дефолт
  `True` был бы fail-open.
- Мутация в Google Ads возможна ТОЛЬКО на Draft `7753643025` (`ads.client.ensure_allowed`) и ТОЛЬКО
  после подтверждения по `confirmation_id` (`confirm.store`) — таблица `proposals` это очередь
  черновиков, а не исполнение.
- Подтверждение «тратится» атомарно один раз (`ConfirmStore.claim`) — см. [SECURITY.md](SECURITY.md).
- Истёкшие `pending`-черновики подчищает плановая задача (см. [SCHEDULER.md](SCHEDULER.md)); композитный
  индекс `ix_proposals_status_created_at` обслуживает этот скан ([`db/models.py:68`](../db/models.py),
  миграция `0003`).

## Миграции (Alembic)

```bash
docker compose up -d postgres            # dev-Postgres (хост-порт 5433)
alembic upgrade head                     # применить все миграции
alembic downgrade -1                     # откатить последнюю
alembic current                          # текущая ревизия
alembic history                          # список ревизий
```

Текущая цепочка ревизий: `0001` (init: whitelist/user_settings/proposals/audit_log/oauth_tokens) →
`0003` (индекс очистки proposals) → `0004` (актор в audit) → `0005` (язык) → `0006` (error_events) →
`0007` (выбранный аккаунт) → `0008` (login_customer_id в oauth_tokens) → `0009` (account_access) →
`0010` (campaign_templates) → `0011` (customer_id в error_events) → `0012` (campaign_drafts) →
`0013` (§20 client KB: 6 таблиц) → `0014` (content_hash в client_site_pages) →
`0015` (ui_prefs в user_settings) → `0016` (drop мёртвой whitelist) →
`0017` (возврат whitelist рантайм-активной: `added_by`/`note`) → `0018` (recommendations) →
`0019` (bug_reports) → `0020` (индекс chat_id в audit_log) → `0021` (admins рантайм) →
`0022` (account_health_snapshot: снапшоты health-score `/audit`, волна N1.1) →
`0023` (recommendation.topic VARCHAR(16)→(32): семьи чеков `/audit` длиннее тем `/advise`) →
`0024` (recommendation.kind: снять префикс `audit_` — слияние бакетов обучения 👍/👎 после того, как
источником рекомендаций стал один движок аудита) →
`0025` (sheet_exports: реестр созданных ботом Google-таблиц — `/sheets` и таблицы ключей визарда;
ссылка переживает закрытие визарда и рестарт, выдаётся в `/mysheets`) →
`0026` (auction_insight_row: импортированные срезы «Статистики аукционов», `/competitors`) →
`0027` (индекс `ix_proposals_chat_status` на `proposals(chat_id, status)`: пикер/списки черновиков
чата шли seq-scan по всей таблице — она растёт с каждой мутацией) →
`0028` (`client_site_pages.text`: текст страницы после вычитания шаблона — досье §20 пересобирается
без повторного обхода чужого сайта; ретеншн `site_page_text_retain_days`, 90 дней) →
`0029` (`client_dossiers`: досье по сайту клиента — `markdown` владельцу, PII-free `llm_context`
генераторам; `draft` → `current` только внутри атомарного `claim` confirm-гейта) →
`0030` (провенанс хода в `proposals`: `origin_human_turn`, `author_user_id`, `run_id`, `tg_message_id`
— второй, неподделываемый бит денежного гейта, Волна 1.4; `server_default=false` объявляет **все**
существующие строки машинными: это осознанный отказ по правилу 10, а не «пропустить старое») →
`0031` (`ads_quota_ops`: распределённый счётчик дневной квоты Google Ads — общий стор вместо
per-process deque; с появлением scheduler-процесса и per-session MCP счётчики стали слепы друг к другу,
и потолок Basic (15 000 операций/сутки) пробивался молча. Одна строка = один `quota.record()` с
`op_count`; окно 24ч считается в SQL `SUM(op_count) WHERE ts > cutoff` через `db.session.db_dt`;
ретеншн — `scheduler.jobs.purge_stale_rows`/`ads_quota_ops_retain_days`) — **head**.

### Добавить миграцию
```bash
alembic revision --autogenerate -m "описание изменения"
```
1. `--autogenerate` сравнивает модели с БД и набрасывает diff.
2. **Обязательно вычитай** сгенерированный файл в `migrations/versions/` — autogenerate не ловит
   всё (переименования, изменения типов, данные); поправь руками `upgrade()`/`downgrade()`.
3. Проверь применение **и откат** на dev-Postgres (`upgrade head` → `downgrade -1` → `upgrade head`).
4. Файлы версий лежат в [`migrations/versions/`](../migrations/versions/); конфиг — `alembic.ini`,
   `migrations/env.py` (берёт `database_url` из `core.config`).

### dev/SQLite vs prod/Postgres
- `db.session.init_db()` (`create_all`) — **только** для dev/SQLite и тестов; в проде схему ведёт
  **исключительно** Alembic. Не полагайся на `create_all` на Postgres.
- **`func.now()` / timezone.** `server_default=func.now()` транслируется по-разному: SQLite отдаёт
  UTC без tz-метки, Postgres — `CURRENT_TIMESTAMP` с tz. Учитывай это при переезде dev→prod и в
  tz-чувствительных выборках (сейчас даты используются только для аудита/очистки, поэтому риск
  низкий, но при добавлении tz-логики — проверь на Postgres).

## Резервные копии
Прод-чеклист требует настроенные бэкапы БД (см. [DEPLOYMENT.md §Prod-чеклист](DEPLOYMENT.md)).
`audit_log` — источник истины по выполненным операциям; не очищать без бэкапа.
