# §20 «Информация про клиентов» — база знаний по клиентам + краулинг сайта

Документация для разработчика/ревьюера. Источник истины — `ТЗ.md` §20. Код: пакет `clients/`,
хендлеры `bot/main.py` (`cli_*`, `_cli_*`, `_run_client_crawl`, `_spawn_crawl`), модели `db/models.py`,
миграции `migrations/versions/0013_client_kb.py` и `0014_site_page_hash.py`.

## 1. Обзор

Персональная **база знаний по `customer_id`** (один рекламный аккаунт — один профиль,
`client_profiles.customer_id` UNIQUE). Профиль хранит бренд, описание бизнеса, гео, язык аудитории,
сайт, соцсети, заметки + детали: контакты, услуги/товары, карту страниц сайта.

Ключевые инварианты:

- **База знаний НЕ меняет рекламный аккаунт.** Это локальная БД. Профиль подаётся только как
  **контекст в генераторы** §10/§19 (seed-ключи, релевантность, RSA, ассеты). SDK Google Ads и замок
  аккаунта (`ads.client.ensure_allowed`) к профилям неприменимы.
- **Любое изменение памяти — через confirm-гейт**, но это отдельный **memory-домен**
  (`clients.execute.execute_confirmed_memory`, `MEMORY_OPERATIONS = {profile_save, profile_update,
  profile_clear}`), а НЕ `ads.mutations`. `bot.main._do_confirm` маршрутизирует ✅: если
  `proposal.operation ∈ MEMORY_OPERATIONS` — в `clients.execute`, иначе в `ads.service.execute_confirmed`.
- **Cross-domain fail-closed** (см. §8): memory-op, случайно дошедший до ads-исполнителя, отвергается там
  (op вне `SUPPORTED_OPERATIONS`); ads-op здесь — вне `MEMORY_OPERATIONS`. Оба отказывают.

Доступ к аккаунту (`_cli_check_access`) — тот же fail-closed, что и для чтения: `ensure_read_allowed`
(глобальный read-замок) + `ensure_account_allowed_for_user` (пер-пользователь). Draft доступен всем
whitelisted. `CLIENTS = ClientProfileStore()` — синглтон стора в `bot.main`.

## 2. Меню и кнопки (§20.2)

Вход: команда `/clients` или reply-кнопка (`BTN_CLIENTS_ALL`) → `_cli_present_accounts`.

- **Список аккаунтов** — дочерние аккаунты MCC (`_cli_read_accounts`, деградация на Draft при сбое),
  кэш в `_CLI_ACCT_CACHE[chat_id]`. У заполненных профилей — ✅ (`CLIENTS.accounts_with_profile`),
  у пустых — ▫️. Клавиатура `clients_accounts_kb`.
- **Пагинация пикера** — `_ACCT_PAGE = 8` аккаунтов на страницу; кнопки ‹ / индикатор `page/pages` / ›
  (`ClientCB(action="page")` → `cli_account_page_cb`, перерисовка разметки, `_CLI_WITH_PROFILE` кэширует
  отметки между страницами). `idx` в callback — ГЛОБАЛЬНАЯ позиция в кэше (customer_id не кладём).
- **Карточка клиента** (`_cli_show_card` → `texts.fmt_client_card`, клавиатура `client_card_kb`):
  - нет профиля → **➕ Добавить информацию** (`add`);
  - есть профиль → **✏️ Обновить инфу** (`update`), **🗑 Очистить профиль** (`clear`), и если есть сайт —
    **🔄 Перекраулить полностью** (`recrawl`) + **🆕 Перекраулить только новое** (`recrawl` sub=`incr`);
  - **‹ Назад** (`back`) → снова список.
- `/client <customer_id>` — карточка по id напрямую (`client_cmd`, `normalize_customer_id`; пустой/битый
  id → подсказка).
- Выбор аккаунта из списка (`cli_account_cb`) фиксирует `cli_customer_id` в FSM и **сбрасывает буфер
  текста** `_CLI_TEXT_BUF` + гасит idle-таймер — чтобы набранное для прежнего клиента не уехало в чужой профиль.

## 3. Приём текста (§20.3)

По `add`/`update` (`cli_add_update_cb`) бот входит в состояние `ClientInfoWizard.awaiting_text` и обнуляет
`_CLI_TEXT_BUF[chat_id]`. Клавиатура ввода `client_input_kb` — **💾 Сохранить** / **✖ Отмена**.

- **Накопление** (`cli_accumulate_text`): менеджер может слать инфу несколькими сообщениями подряд — они
  копятся в буфер; на каждом сообщении отбивается «накоплено N сообщений, chars». Пустой текст отклоняется.
- **Два пути сохранения** ведут в общий `_cli_extract_and_propose`:
  1. Ручное **💾 Сохранить** (`cli_save_cb`) — гасит idle-таймер, выходит из режима накопления.
  2. **Авто-сохранение по таймауту** `client_text_idle_s` (по умолчанию 60 c; `≤ 0` — отключено). Idle-таймер
     (`_cli_arm_idle` → `_cli_idle_autosave`) **сбрасывается и заново взводится при каждом сообщении**
     (`_CLI_IDLE_TASK[chat_id]`). По тишине — извлекает буфер и показывает «было→станет» **точно так же,
     как ручное сохранение** (тот же confirm-гейт: без ✅ ничего не пишется). Фон обёрнут в try/except —
     не роняет event loop.
- **LLM-разбор** (`clients.profile_extract.extract_profile`, роль `parsing`, `temperature=0.2`): свободный
  текст → строгий JSON → `ClientProfileExtract` (все поля опциональны; незамапленное кладётся в `notes`).
  Терпимость к кривому ответу модели (`field_validator` salvage; пустой ввод/сбой → пустой объект без падения).
  `to_patch()` не кладёт пустые/None — чтобы мердж не затирал.
- **«было→станет»** (`texts.fmt_client_diff`): `before = CLIENTS.get_by_account`; `operation` =
  `profile_update`, если профиль есть, иначе `profile_save`. Если в патче есть `website` — вместо простого
  ✅ показывается `client_save_kb`: **✅ Сохранить как есть** / **🕷 Сохранить и краулить** / ❌ Отмена
  (см. §4).
- **Сброс буфера при смене аккаунта** — см. §2 (`cli_account_cb`).

## 4. Краулер (§20.4)

Чистая логика в `clients/crawler.py` (без БД и бота); оркестрация — `bot.main._run_client_crawl`
(фоновая `asyncio`-задача через `_spawn_crawl`).

- **Обход** `crawl_site` — BFS от главной + сиды из `sitemap.xml` (`fetch_sitemap`, глубина сидов = 1).
  Уважает `robots.txt` (`load_robots` → `can_fetch`; нет/битый robots → permissive). Внешние ссылки не
  обходятся — фиксируются как соцсети.
- **Лимиты** из `core.config`: `crawl_max_pages` (50), `crawl_max_depth` (3), `crawl_time_budget_s` (90 c,
  общий `asyncio.wait_for`), `crawl_delay_s` (0.5 c вежливая пауза), `crawl_max_text_chars` (5000 на страницу).
- **Извлечение** (`_extract`, bs4 в `to_thread`): title, текст, ссылки того же домена, соцсети (по хост-мапу
  `_SOCIAL_HOSTS`), телефоны/e-mail (regex). В начало текста вшиваются **мета-данные** — `<meta description>`
  и заголовки H1–H3 (сигнал тематики до усечения). Тип страницы — эвристика по пути/заголовку (`_page_type`:
  home/about/contacts/price/services/catalog/blog/other). `CrawlResult.combined_text()` — единый текст для LLM;
  `site_pages_payload()` — карта страниц для сохранения; `diff_against()` — см. §5.
- **SSRF-защита**: реальная сеть `fetch_url_html` переиспользует гард `core.ingest._is_public_host` на
  исходном URL и КАЖДОМ редиректе (event hook), таймаут `FETCH_TIMEOUT_S`, потолок `MAX_FETCH_BYTES`,
  только http/https. Внутренние/приватные адреса блокируются.
- **Фон и дедуп**: `_spawn_crawl` держит ссылку на задачу в `_CRAWL_INFLIGHT[customer_id]` (иначе GC соберёт
  незавершённую) и **дедуплицирует по customer_id** — второй параллельный краул того же аккаунта не плодится
  (двойной клик → False, «обход уже идёт»).
- **Журнал** `crawl_jobs` (`clients/crawl_jobs.py`): `create_running` → `mark_done(pages_crawled)` |
  `mark_failed(error)`; `error` редактируется `redact_text` и усекается (без секретов/PII). Зависшие
  `running` (in-process задача умерла на рестарте процесса) реконсилятся в `failed` scheduler-джобой
  `reconcile_stale_crawls` (порог `crawl_stale_minutes`, 30 мин; возраст считается в Python как UTC).
- **Куда пишется результат** (`_run_client_crawl`): краулер собирает страницы → `structure_crawl` (LLM)
  сводит в профиль → `_crawl_patch_from_result` доливает код-извлечённые контакты/соцсети → сводка «что
  нашли» (`_crawl_findings` + `texts.fmt_crawl_summary`). Затем **развилка по наличию профиля**:
  - **свежий профиль** (`before is None`) → **авто-сохранение** (`apply_upsert` op=`crawl_save`,
    `source="crawl"`) + запись audit-строки (`crawl_save`, applied). Без гейта осознанно: краул запущен
    явным действием пользователя и не перезаписывает прежних данных.
  - **профиль существует** → `profile_update`-**черновик** («было→станет») с confirm-гейтом (`confirm_kb`)
    — молча не перезаписываем.
- **«🕷 Сохранить и краулить»** (`cli_save_crawl_cb`): сперва подтверждает текстовый save-proposal тем же
  `_do_confirm`, ЗАТЕМ запускает краул. Так текст не теряется, а краул мёржит поверх уже сохранённого
  профиля (существующий → `profile_update`-черновик), без гонки с auto-save.

## 5. Обновление и перекраул (§20.5)

- **Мердж непустых полей** (`store.apply_upsert`): скалярное непустое поле патча перекрывает; непустой список
  категории (контакты/услуги) **заменяет её целиком**; `socials` мёржатся dict-обновлением; пустое/None —
  оставляет как было. Карта страниц (`site_pages`, только краул) заменяется целиком (лимит 200).
- **Инкрементальный перекраул** (`recrawl` sub=`incr`, mode=`incremental`): сравнение обойденных страниц с
  прошлым краулом по `content_hash` (короткий sha1 по title+усечённый текст). `CrawlResult.diff_against`
  (`prev_hashes` из `CLIENTS.site_page_hashes`) → `(new_urls, changed_urls)`; неизменённые не попадают ни туда,
  ни туда. Если `prev_hashes` есть и нет new/changed → **профиль не трогаем** («сайт не изменился»). Иначе —
  обычный `profile_update`-черновик со сводкой diff (сколько новых/изменённых). Страницы без хэша (старый
  краул) в diff не участвуют.
- **История версий** `client_profile_history`: перед каждым upsert/clear пишется snapshot «до» (JSON) +
  operation + confirmation_id. Ключ — `customer_id` (НЕ FK), поэтому история **переживает `apply_clear`** —
  для отката и аудита.
- **Очистка** (`clear` → `cli_clear_cb` → `apply_clear`): удаляет профиль и все детали
  (contacts/services/site_pages), но пишет history-snapshot «до». Это изменяющая операция → confirm-гейт
  обязателен (`profile_clear`).

## 6. Профиль в генерации (§20.6)

`store.profile_context_text(customer_id, max_chars=1500)` — компактный текст профиля как КОНТЕКСТ для
генераторов. Порядок от важного к второстепенному (бренд/бизнес/гео/язык/услуги+цены/заметки), чтобы
усечение срезало наименее важное. **PII не кладём: телефоны и e-mail сюда НЕ попадают** (генерации не нужны;
контакты идут отдельными ассетами `call`).

Проброс в §19/§10 (`bot.main._cc_profile_ctx_account`/`_cc_profile_ctx` → `CopyBrief.profile`,
`adcopy.generate`): контекст питает RSA/описания, sitelinks/snippets/callouts, seed-ключи и релевантность
(любой сбой чтения не роняет генерацию — деградация на пустой контекст). Ассеты с ФАКТАМИ
(`clients/profile_assets.py`, §19.7.2) строятся строго из структурированного профиля — **call** (телефон),
**price** (≥3 услуг с числовой ценой единой валюты), **promotion** (явная скидка + число %); нет данных →
`ValueError` и семейство пропускается (ничего не выдумываем). Сайт профиля также подаётся как seed-URL
(`_cc_profile_site`, только http/https).

## 7. Хранение (§20.7)

Таблицы (`db/models.py`), созданы миграцией **0013_client_kb**; `content_hash` добавлен **0014_site_page_hash**.
На SQLite (dev) — `create_all`; на Postgres (prod) — Alembic.

- **`client_profiles`**: `id`, `customer_id` (UNIQUE), `brand`, `business_desc`, `geo`, `language`,
  `website`, `socials` (JSON), `notes`, `last_crawled_at`, `created_at`, `updated_at`.
- **`client_contacts`**: `id`, `profile_id`, `kind` (phone|email|address|social|msgr), `value`, `created_at`.
- **`client_services`**: `id`, `profile_id`, `name`, `description`, `price` (текст, не micros), `category`,
  `created_at`.
- **`client_site_pages`**: `id`, `profile_id`, `url`, `title`, `page_type`, `key_links` (JSON),
  `content_hash` (sha1/16, §20.5), `crawled_at`.
- **`crawl_jobs`**: `id`, `job_id` (UNIQUE), `customer_id`, `chat_id`, `domain`, `mode` (full|incremental),
  `status` (running|done|failed), `pages_crawled`, `error` (редактировано), `created_at`, `finished_at`.
- **`client_profile_history`**: `id`, `customer_id`, `snapshot` (JSON), `operation`, `confirmation_id`,
  `created_at`.

Детали слабо связаны по `profile_id` без FK-констрейнта (идиома проекта — связь по id, проще миграции/heal).
Все изменяющие операции (включая `crawl_save`) отражаются в **audit_log** (кто/когда/что/результат),
дополнительно к `client_profile_history` и `crawl_jobs`.

## 8. Безопасность / PII

- **Краул хранит только нужное для генерации**: карта страниц + усечённый текст (сигнатура), контакты/соцсети.
  Полный HTML не сохраняется.
- **PII в БД есть**: контакты (телефоны/e-mail) хранятся в `client_contacts` — бэкапы БД содержат PII;
  учитывать при доступе к дампам. В **логи PII сырьём не пишем** (golden rule #5); `profile_context_text` PII
  не отдаёт.
- **`crawl_jobs.error` редактируется** через `redact_text` (без секретов/PII), сообщения об ошибке
  краула пользователю — только `type(e).__name__` под `redact_text` + `texts.esc`.
- **Egress в LLM (аудит 2026-07)**: телефоны/e-mail извлекаются краулером детерминированно
  (regex) и **НЕ включаются** в `combined_text`, уходящий во внешний LLM (OpenRouter); соцсети
  (публичные хэндлы) включаются. Residual: контакт в самом тексте страницы может попасть в
  payload — осознанное ограничение.
- **Cross-domain инвариант**: memory-операция не пройдёт через ads-исполнитель, и наоборот — оба
  fail-closed по спискам операций (`MEMORY_OPERATIONS` vs `SUPPORTED_OPERATIONS`). `claim` атомарен
  (одноразовый confirmed→executing) — защита от replay/гонок. Доступ к аккаунту перепроверяется
  и НА ИСПОЛНЕНИИ memory-операции (TOCTOU: read-замок + пер-пользовательский грант).

## 9. Тесты

`tests/`:

- `test_client_store.py` — стор: upsert/мердж, clear, history, `profile_context_text` (в т.ч. усечение и
  отсутствие PII), `site_page_hashes`.
- `test_client_extract.py` — LLM-разбор текста в `ClientProfileExtract`, salvage кривого JSON, `to_patch`.
- `test_client_crawler.py` — BFS, sitemap, robots, извлечение, `combined_text`/`diff_against`, SSRF-гард.
- `test_client_crawl_job.py` — журнал `crawl_jobs` (running→done/failed, редакция error).
- `test_client_crawl_orchestration.py` — `_run_client_crawl`: свежий → auto-save, существующий → черновик,
  инкрементальный diff по content_hash, дедуп.
- `test_client_confirm.py` — memory-домен за confirm-гейтом (`execute_confirmed_memory`), cross-domain отказ.
- `test_client_wizard.py` — хендлеры `cli_*` (пикер, карточка, накопление текста, save/clear, save&crawl).
- `test_client_profile_wiring.py` — профиль → `CopyBrief.profile`/ассеты (проброс в генерацию §19/§10).
