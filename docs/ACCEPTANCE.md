# §18 — Чек-лист приёмки Aimash

Источник истины по критериям — [`ТЗ.md` §18](../ТЗ.md) («Результат и критерии приёмки»).
Ниже каждый критерий сопоставлен с **реальной** реализацией и **реальным** тестом, который её
доказывает. Дополнения §19/§20 — в конце. Точечные `file:line` со временем дрейфуют при правках;
где важна устойчивость, ссылаемся по **имени функции** (grep надёжнее номера строки).

Легенда статусов:

- ✅ — реализовано и покрыто офлайн-тестами (SDK/сеть подменяются в тестах);
- ⚠️ — реализовано, но с оговоркой (обычно: SDK-цепочка не сверена на живом тест-аккаунте).

Важно про офлайн-природу тестов: почти все тесты подменяют `_*_via_sdk`-исполнители и живой
Google Ads SDK моками (см. заголовки тест-файлов, напр. `tests/test_write_layer.py:1`,
`tests/test_reports.py:1`). Это проверяет **гейты, валидацию и сборку операций**, но НЕ живой
ответ Google Ads. Где это критично для приёмки — отмечено ⚠️.

---

## 1. Рабочий Telegram-бот, привязанный к MCC; корректное чтение и управление

**Статус: ✅** (управление — на замке единственного аккаунта Draft; см. критерии 2–3).

- Бот на aiogram, регистрация команд/хендлеров: `bot/main.py` (`@dp.message(CommandStart())`
  — `bot/main.py:617`; `/status`, `/campaigns`, `/report` и др. — `bot/main.py:644`,
  `bot/main.py:661`, `bot/main.py:948`).
- Привязка к MCC (login_customer_id) и построение SDK-клиента: `ads/client.py:142` (`_env_cfg`
  подставляет `login_customer_id`), `ads/client.py:157` (`build_client`).
- Замок обхода MCC (чтение дочерних от имени менеджера) — `ads/client.py:274`
  (`ensure_manager_allowed`, fail-closed при пустом наборе MCC).
- Чтение аккаунтов/кампаний (GAQL): `ads/read.py:47` (`list_child_accounts`), `ads/read.py:73`
  (`account_stats`), `ads/read.py:171` (`list_campaigns`).
- Управление кампаниями (§3 «создание/пауза/возобновление/изменение»): создание —
  `apply_create_search_campaign`; пауза/возобновление — `apply_pause_campaign`/
  `apply_resume_campaign`; **изменение (переименование)** — `apply_update_campaign`
  (`ads/mutations.py:161`). Полная карта из 29 операций (вкл. симметричные add/remove
  для ключей, минус-слов и аудиторий) и честных пробелов — в [MUTATIONS.md](MUTATIONS.md).
- **Тесты:** `tests/test_ads_read.py`, `tests/test_bot_integration.py`, `tests/test_bot_slash.py`,
  `tests/test_mcc_discovery.py`, `tests/test_mcc_summary.py`, `tests/test_write_layer.py`.

---

## 2. (Обязательно) Агент не меняет сам — только по командам, показ «было→станет» и «да»

**Статус: ✅**

Confirm-гейт разделяет предложение (proposal, только черновик) и выполнение (после «да»):

- Черновик изменения (`Proposal`, не выполняется до подтверждения): `confirm/gate.py:13`.
- Показ «было → станет» с реальным снимком «было» из Google Ads: `bot/main.py:544`
  (`_present_proposal`) → `ads.service.read_before` кладёт снимок в `params['_before']`
  (`bot/main.py:552`), diff рисует `texts.fmt_mutation_summary` (`bot/main.py:572`).
- Кнопки ✅/❌ и подтверждение «да»: обработчик `on_confirm` — `bot/main.py:5321`, исполнение
  идёт только из `_do_confirm` (`bot/main.py:5217`) после атомарного `STORE.confirm(...)`
  (`bot/main.py:5220`) и вызова `execute_confirmed` (`bot/main.py:5241`).
- Каждая мутация в коде требует валидного одноразового `confirmation_id` — `ads/mutations.py:70`
  (`_require_confirmation`) через атомарный `claim` (`confirm/store.py:119`).
- **Списки (ключи/минус-слова) — XLSX-ссылкой, не текстом:** `bot/main.py:590` → вложение через
  `ux.send_proposal_keywords_xlsx` (`bot/main.py:591`); длинные RSA — отдельным вложением
  (`bot/main.py:607`).
- Модель/агент физически не может исполнить операцию: `agent/loop.py` создаёт только proposal,
  исполнимые операции ограничены `SUPPORTED_OPERATIONS` (`ads/service.py:21`), а
  `execute_confirmed` отвергает операцию вне списка (`ads/service.py:163`).
- **Тесты:** `tests/test_safety_core.py`, `tests/test_write_layer.py`,
  `tests/test_confirm_ownership.py`, `tests/test_before_diff.py`, `tests/test_invariants_core.py`.

---

## 3. (Обязательно) Бюджет — не по инициативе; по команде (%/$/грн) после показа и «да»

**Статус: ✅**

- Бюджет меняется только прямой командой пользователя: `ads/mutations.py:108` — после `claim`
  проверяется `proposal.user_initiated`, иначе `PermissionError` («изменение бюджета должно быть
  прямой командой пользователя», `ads/mutations.py:109`).
- Флаг `user_initiated` fail-closed: дефолт `False` в `Proposal` (`confirm/gate.py:24`); `True`
  ставит только доверенный слой при показе кнопок человеку (`bot/main.py:580` в
  `_present_proposal`), а не агент про себя.
- Тот же денежный гард продублирован на ставке CPC (`ads/mutations.py:222`) и смене стратегии
  ставок (`ads/mutations.py:759`) — defense-in-depth сверх золотого правила о бюджете.
- Показ реального «было → станет» с суммой и % до подтверждения — `bot/main.py:552`
  (`read_before`); валюта команды ≠ валюте аккаунта → отказ без FX (`bot/main.py:564`,
  `currency_mismatch`).
- **Тесты:** `tests/test_safety_core.py`, `tests/test_write_layer.py`,
  `tests/test_invariants_core.py` (мета-гард: новая денежная мутация без `user_initiated` валит
  тест), `tests/test_before_diff.py`, `tests/test_period_currency.py`.

---

## 4. Keyword research + добавление с типом/интентом + предложение минус-слов

**Статус: ✅**

- Подбор идей (KeywordPlanIdeaService, замок аккаунта до запроса): `ads/keyword_plan.py:105`
  (`generate_keyword_ideas`).
- Кластеризация с интентом по таксономии ТЗ §7 (транзакционный/коммерческий/информационный/
  навигационный): `keywords/cluster.py:49` (`normalize_intent`), `keywords/cluster.py:111`
  (`cluster_keywords`), приоритезация — `keywords/cluster.py:150` (`rank_clusters`).
- Предложение минус-слов (advisory, ничего не пишет в аккаунт): `keywords/cluster.py:197`
  (`suggest_negative_keywords`).
- Добавление ключей с **типом соответствия** через confirm-гейт: `ads/mutations.py:231`
  (`apply_add_keywords`), флоу «добавить в кампанию» — `bot/main.py:1836` (выбор match-type),
  собирает только черновик `add_keywords`.
- Добавление минус-слов через confirm-гейт: `ads/mutations.py:255`
  (`apply_add_negative_keywords`).
- **Тесты:** `tests/test_keyword_plan.py`, `tests/test_keywords_cluster.py`,
  `tests/test_kw_add.py`, `tests/test_keywords_pipeline.py`, `tests/test_keywords_export_csv.py`,
  `tests/test_keyword_sheets.py`.

---

## 5. Создание кампаний из медиа с черновиком PAUSED и запуском по команде

**Статус: ⚠️** (реализованы Search / GDN / Video / Demand Gen; см. оговорку про live-сверку).

**UAC (Universal App Campaigns) ПОЛНОСТЬЮ ИСКЛЮЧЁН из объёма** — у клиента нет приложения
(`CLAUDE.md`, раздел «Фазы»). В §18 ТЗ упомянут «GDN/UAC», но по согласованному объёму строятся
GDN / Video / Demand Gen, а UAC не реализуется намеренно.

Все создаваемые сущности принудительно `PAUSED` в КОДЕ (0 расхода), запуск — отдельной командой:

- Search-кампания из текстов (всё PAUSED): `ads/mutations.py:1378`
  (`apply_create_search_campaign`), статус зашит в КОДЕ (`ads/mutations.py:1634`,
  `ads/mutations.py:1677`, `ads/mutations.py:1689`); требует `user_initiated`
  (`ads/mutations.py:1453`).
Ссылки ниже — по **именам функций** (устойчиво к сдвигу строк; ранее были номера строк, дрейфовавшие
при правках):

- **GDN-кампания из фото** (DISPLAY, PAUSED): `apply_create_gdn_campaign` (`ads/mutations.py`),
  статус PAUSED и `user_initiated` зашиты в КОДЕ. ⚠️ **GDN live-сверялась вручную РАНЕЕ, но
  ВОСПРОИЗВОДИМОГО harness нет** (`scripts/live_smoke_video_dg.py` покрывает только Demand Gen/Video).
  Добавить GDN-smoke до сдачи — nice-to-have (см. план-файл).
- **Demand Gen из YouTube-видео** (DEMAND_GEN, PAUSED): `apply_create_demand_gen_campaign`
  (`ads/mutations.py`), PAUSED и `user_initiated` — в КОДЕ. **Сверен LIVE ✅** (см. §18#5).
- **Video-кампания (YouTube)** (PAUSED): `apply_create_video_campaign` (`ads/mutations.py`),
  `user_initiated` — в КОДЕ. ⚠️ Создание Video Google разрешает только по allowlist аккаунта
  (иначе `MUTATE_NOT_ALLOWED`) → в визарде кнопка Video **СКРЫТА по умолчанию**
  (`GOOGLE_ADS_VIDEO_ENABLED`, B4); рабочий путь из видео — Demand Gen. Docstring
  `apply_create_video_campaign` предупреждает о необходимости live-сверки при получении allowlist.
- Запуск по команде (PAUSED → ENABLED отдельным confirm-гейтом): кнопка «🚀 Запустить» минтит
  **`launch_campaign`**-черновик (`cc_launch` в `bot/handlers/campaign_wizard.py`); операция
  `apply_launch_campaign` включает кампанию ПОЛНОСТЬЮ — **кампания + ВСЕ группы + ВСЕ объявления**
  (A1-фикс: `resume_campaign` включал только кампанию ⇒ PAUSED группа/RSA давали 0 показов). Запуск
  НЕ происходит автоматически при создании.
- Медиа (фото/логотип) хранятся вне `proposal.params`/логов, во временном хранилище по `media_id`:
  `ads/service.py:386` (`load_pending_media`/`clear_pending_media`).
- **Тесты:** `tests/test_search_campaign.py`, `tests/test_gdn_campaign.py`,
  `tests/test_video_campaigns.py` (заголовок теста сам отмечает ⚠️: «SDK-цепочки требуют
  live-сверки», `tests/test_video_campaigns.py:4`), `tests/test_clone_campaign.py`,
  `tests/test_campaign_wizard_state.py`, `tests/test_cc_composite_create.py`.

---

## 6. Статистика по каждому аккаунту + глубокие отчёты с экспортом (Sheets/.xlsx)

**Статус: ✅**

- Статистика по аккаунту (GAQL): `ads/read.py:73` (`account_stats`); сводка по дочерним MCC —
  `reports/mcc.py` (`build_mcc_summary_async`), команда `/mcc` — `bot/main.py:1105`.
- Глубокий отчёт по аккаунту (метрики + разбивки, производные CTR/CPC/CPA/ROAS):
  `reports/service.py:45` (`build_account_report_async`), текстовая сводка —
  `reports/service.py:115` (`summary_text`).
- Экспорт в `.xlsx`: `reports/xlsx.py:104` (`build_workbook`), `reports/xlsx.py:115`
  (`write_report_xlsx`); MCC-книга — `reports/xlsx.py:122` (`build_mcc_workbook`). Команда
  `/export` — `bot/main.py:1004`.
- Экспорт в Google Sheets (scope `drive.file`): `reports/sheets.py:120`
  (`publish_report_to_sheets`), ключи — `reports/sheets.py:197` (`publish_keywords_to_sheets`).
  Команда `/sheets` — `bot/main.py:1035`.
- **Тесты:** `tests/test_reports.py`, `tests/test_sheets.py`, `tests/test_keyword_sheets.py`,
  `tests/test_account_medians.py`, `tests/test_mcc_summary.py`, `tests/test_period_currency.py`.

---

## 7. Генерация текстов (RSA) с подтверждением каждого элемента

**Статус: ✅**

- Генерация RSA (заголовки/описания): `adcopy/generate.py:119` (`generate_rsa`).
- Длину каждого элемента считает КОД, не модель (кириллица=1, 30/90/15): `adcopy/validate.py`
  (`validate`), применяется в `ads/mutations.py:777` (`_validate_rsa_inputs`).
- **Поэлементное** одобрение/отклонение/доработка ПЕРЕД созданием объявления: сессия курации
  `adcopy/session.py:94` (`SessionStore`); статус элемента pending|approved|rejected
  (`adcopy/session.py:187` `set_state`, `adcopy/session.py:206` `approve_all_valid`,
  `adcopy/session.py:221` `replace_element`). Доработка одного элемента LLM — `adcopy/refine.py:51`
  (`refine_element`). Готовность (минимум одобренных) — `adcopy/session.py:62` (`can_finalize`).
- Реальная мутация создаётся отдельным черновиком за confirm-гейтом уже из одобренных элементов:
  `ads/mutations.py:815` (`apply_create_rsa`, всё PAUSED — `ads/mutations.py:831`).
- **Тесты:** `tests/test_rsa_session.py`, `tests/test_rsa_create.py`, `tests/test_rsa_refine.py`,
  `tests/test_rsa_length_properties.py`, `tests/test_adcopy_generate.py`,
  `tests/test_cc_rsa_handoff.py`.
- **Отклонение UX (принято заказчиком 2026-07):** в **визарде §19** курация поэлементная —
  карточки ✅ Применить / ✏️ Доработать / ❌ Отклонить на каждый заголовок/описание (полное
  соответствие §10). В **отдельной команде `/rsa`** по умолчанию — быстрый **список-UX** (кнопка
  «✅ Применить набор») с поэлементной доработкой; заказчик подтвердил это как приемлемую замену
  поэлементных карточек для быстрого сценария. Мутация в обоих случаях создаёт объявление ТОЛЬКО из
  одобренных элементов за confirm-гейтом.

---

## 8. Audit-лог всех действий

**Статус: ✅**

- Каждое решение и результат пишутся в `audit_log`: подтверждение — `confirm/store.py:156`
  (`confirm` пишет строку `confirmed` с actor «кто»), отклонение — `confirm/store.py:205`
  (`reject`), успех — `confirm/store.py:233` (`finalize` → `applied`), сбой —
  `confirm/store.py:261` (`record_failure` → `failed`). Сборка строки — `confirm/store.py:284`
  (`_audit`).
- Секреты в журнал не попадают: ошибка редактируется на границе БД `redact_text(str(error))`
  (`confirm/store.py:280`), размер результата ограничен (`confirm/store.py:301`, `_cap_result`).
- Просмотр журнала (что/когда/кто/результат): `confirm/store.py:318` (`list_recent_audit`),
  команда `/journal` — `bot/main.py:655` → `_send_journal` (`bot/main.py:496`).
- Защита от повторного исполнения (double-spend): атомарный `claim` (`confirm/store.py:119`).
- **Тесты:** `tests/test_journal.py`, `tests/test_write_layer.py`, `tests/test_history.py`,
  `tests/test_confirm_ownership.py`, `tests/test_logging_redaction.py`.

---

## 9. Документация (установка, OAuth, команды)

**Статус: ✅**

- Обзор/оглавление документации: `docs/README.md:1`.
- Получение доступов и OAuth: `docs/OAUTH_SETUP.md:1`.
- Установка и деплой: `docs/DEPLOYMENT.md:1`, корневой `README.md:1`, `Dockerfile`,
  `docker-compose.yml`.
- Команды/поведение бота и профильные разделы: `docs/HANDOVER.md`, `docs/REPORTS.md`,
  `docs/KEYWORD_RESEARCH.md`, `docs/GDN_CAMPAIGNS.md`, `docs/SCHEDULER.md`, `docs/SECURITY.md`,
  `docs/DATABASE.md`, `docs/TESTING.md`, `docs/BACKUP.md`.
- **Тесты:** документация проверяется людьми/ревью (тестами не покрывается — это ожидаемо).

---

## §19 — Флоу создания Search-кампании (`/newcampaign`)

**Статус: ✅** (все обязательные пункты; известные отклонения — в шапке [`ТЗ.md`](../ТЗ.md)).
Ссылки — по **именам функций** (устойчиво к сдвигу строк): смотри `bot/main.py` (хендлеры `cc_*`),
`bot/campaign_wizard/store.py`, `agent/campaign_settings.py`, `ads/mutations.py`.

- **19.2 Этап 0 (выбор аккаунта):** `_cc_present_stage0` → `cc_accounts_kb` (постраничный пикер,
  B7); выбор фиксируется в `campaign_drafts` (`set_preview`). Черновик переживает рестарт.
- **19.3 Этап 1 (описание → настройки):** `extract_campaign_settings` (LLM) + fallback «по аналогии»
  из медиан аккаунта (`ads/read.py::search_campaign_medians`, деньги кратны биллинг-единице — B2);
  правки командой (`_cc_apply_settings_patch`); кнопки ✅/✏️/❌.
- **19.4 Этап 2 (ключи):** свои — текст/файл XLSX·CSV/ссылка Sheets/`_cc_keywords_from_document`;
  генерация — `cc_kw_generate` → `KeywordPlanIdeaService` → фильтр релевантности → Google Sheets →
  round-trip `cc_kw_verify` (сверка `sheet_id`; ❌-строки и мусорные ключи отфильтровываются — B5);
  тип соответствия — подтверждённый на Этапе 1 (B6). Явный гейт `cc_kw_confirm`.
- **19.5 Этап 3 (RSA):** Final URL → авто display path (`adcopy/display_path.py`, кириллица=1;
  charset-валидация — B12) → 15 заголовков / 4 описания, поэлементная курация ✅/✏️/❌ (§10).
- **19.6 Этап 4 (изображения):** `_cc_present_stage4` (гейта доступности нет — известное упрощение).
- **19.7 Этап 5 (ассеты):** «текущие» (`cc_use_assets`) / «новые» из профиля §20 (`cc_asset_type`).
- **19.8 Этап 6–7 (URL-опции, финал):** `cc_url_text` (tracking/suffix) → финальная сводка →
  правки командой (`agent/campaign_edit.py`) → **один composite proposal** `create_search_campaign`
  (всё PAUSED; при сбое шага — полный откат бюджет+кампания+группа, B1); запуск — отдельной
  командой `cc_launch` → `launch_campaign` (включает кампанию+группы+объявления, тот же confirm-гейт).
  Черновик гасится только при
  успешном подтверждении (B9).
- **Тесты:** `tests/test_cc_stage_flow.py`, `tests/test_campaign_wizard_state.py`,
  `tests/test_cc_composite_create.py`, `tests/test_search_campaign.py`, `tests/test_cc_display_path.py`,
  `tests/test_predelivery_fixes.py` (откат/micros/charset/пагинация/match_type).

---

## §20 — Информация про клиентов (`/clients`, `/client <id>`)

**Статус: ✅** (все 7 критериев §20.9). Ссылки — `bot/main.py` (`cli_*`), `clients/*`.

- **20.2 Меню/список аккаунтов** с ✅ у заполненных: `_cli_present_accounts` → `clients_accounts_kb`
  (постраничный, B7); контекст customer_id в FSM (один аккаунт — один профиль, UNIQUE в БД).
- **20.3 Приём текста:** накопление нескольких сообщений (`cli_accumulate_text`) до «💾 Сохранить»
  **или авто-сохранения по таймауту** (`client_text_idle_s`, B13); LLM-разбор `extract_profile` →
  «было→станет» + confirm-гейт (memory-домен, `clients/execute.py`, вне `ads.mutations`).
- **20.4 Краулер:** `clients/crawler.py` (BFS от главной + sitemap, robots.txt, лимиты `CRAWL_*`,
  извлечение услуг/цен/контактов/мета) → `structure_crawl` (LLM) → сводка; фон, дедуп по
  customer_id (B15).
- **20.5 Обновление/перекраул:** мердж непустых полей (`clients/store.py::apply_upsert`),
  инкрементальный перекраул по `content_hash` (`diff_against`), история версий (`client_profile_history`).
- **20.6 Профиль в генерации:** `profile_context_text` → RSA/ассеты/seed-ключи (PII не кладём).
- **20.7 Хранение:** таблицы `client_profiles`/`client_contacts`/`client_services`/`client_site_pages`/
  `client_profile_history`/`crawl_jobs` (миграции `0013`/`0014`); изменения — в audit log.
- **Отклонение (принято, 2026-07):** краул СВЕЖЕГО/пустого профиля (когда `before is None`) сохраняет
  результат без отдельного «было→станет»-подтверждения — перезаписывать нечего, а краул уже запущен
  явной командой менеджера; пишется audit-строка `applied`. Краул/обновление СУЩЕСТВУЮЩЕГО профиля
  по-прежнему проходит confirm-гейт (`_present_memory_proposal`). Google Ads это не затрагивает
  (профиль — локальная память, золотые правила 1–3 про `ads.mutations`).
- **Тесты:** `tests/test_client_store.py`, `tests/test_client_extract.py`, `tests/test_client_crawler.py`,
  `tests/test_client_crawl_orchestration.py`, `tests/test_client_confirm.py`, `tests/test_client_wizard.py`,
  `tests/test_client_profile_wiring.py`.

---

## Золотые правила безопасности (проверены по коду)

| Правило | Реализация | Тест |
|---|---|---|
| Мутации только на Draft `7753643025` | `ads/client.py:212` (`ensure_allowed`); потолок в коде `ALLOWED_CEILING` — `ads/client.py:25`; fail-closed при пустом allow-list — `ads/client.py:223` | `tests/test_safety_core.py`, `tests/test_invariants_core.py` |
| Confirm-гейт с `confirmation_id` в каждом `apply_*` | `ads/mutations.py:70` (`_require_confirmation`) → атомарный `claim` `confirm/store.py:119` | `tests/test_write_layer.py`, `tests/test_invariants_core.py` |
| Бюджет/деньги только при `user_initiated` | `ads/mutations.py:108` (бюджет), `ads/mutations.py:222` (ставка), `ads/mutations.py:759` (стратегия); дефолт `False` — `confirm/gate.py:24` | `tests/test_safety_core.py`, `tests/test_invariants_core.py` |
| Секреты не утекают (логи/audit/чат) | `redact_text` в audit — `confirm/store.py:280`; whitelist fail-closed (env ∪ БД, рантайм `/adduser`) — `bot.main.WhitelistMiddleware` → `core.access.is_whitelisted` | `tests/test_logging_redaction.py`, `tests/test_whitelist.py`, `tests/test_runtime_whitelist.py` |

---

### Сводка по статусам

- ✅ критерии §18: 1, 2, 3, 4, 6, 7, 8, 9; дополнения **§19** (визард) и **§20** (клиенты).
- ✅/⚠️ критерий 5 — **live-сверка выполнена 2026-07-03** (`scripts/live_smoke_video_dg.py`):
  - **Search/GDN** — сверены live ранее; **Demand Gen — СВЕРЕН LIVE** ✅: кампания создана
    PAUSED через полный confirm-гейт, перечитана из API (status=PAUSED, channel=DEMAND_GEN),
    удалена. Попутно закрыты live-требования API: обязательный `ad.name`, обязательный
    `logo_images` (≥1), минимальный дневной бюджет DG (AUD) = 8 единиц, откат
    budget+campaign+ad_group при сбое ad-шага.
  - **Video** — код полный и корректный, но Google API отклоняет создание VIDEO-кампаний на
    стандартном доступе (`MUTATE_NOT_ALLOWED`, trigger «VIDEO»): видеокампании через API —
    только по allowlist Google. **Это ограничение платформы, не дефект** — фиксируется как
    известное; рабочий путь «кампания из видео» — Demand Gen (рекомендован в /newvideo).
    **B4 (2026-07): кнопка Video в визарде СКРЫТА по умолчанию** (`GOOGLE_ADS_VIDEO_ENABLED=false`) —
    не ведём менеджера в гарантированный тупик; при выборе Video без флага бот продолжает на
    Demand Gen. Владелец включает флаг, когда его аккаунт добавлен в allowlist Google.
  - UAC исключён из объёма намеренно.

### Дельты раунда 2 (видимость аккаунтов, роутинг визардов, RU-гео, мультиаккаунт-мутации)

- **F §8/§12** — пикеры показывали только Draft+гранты (пер-юзер грант `ensure_account_allowed_for_user`
  при `ACCOUNT_ACCESS_MODE=auto` после первого `/grant`): **админ (`ADMIN_CHAT_IDS`) видит все
  read-allowed аккаунты без грантов**. Тест `test_access.py::test_admin_bypasses_per_user_read_grant`.
- **N4** — `/templates`/`/savetemplate`/`/recent` отвечали ошибкой формата `/newsearch` (brief-state
  съедал позже-зарегистрированные команды): middleware `SlashCommandExitsWizardMiddleware` мягко
  сворачивает визард на любой `/команде`. Тесты `test_slash_command_guard.py`.
- **N5/N3b** — URL/текст на экранах-без-хендлера (пикер /rsa, параметры /keywords) утекал в агента:
  гард `on_text` (активный state → подсказка), хендлер `KwWizard.params` (текст = сиды), не чистим
  state на сбое запуска, state на RSA-пикерах. Тест `test_bot_integration.py::test_on_text_with_active_wizard_state_*`.
- **K §7** — «The input has an invalid value» на гео Россия: **RU/BY не обслуживаются Keyword Planner**
  → `ads.geo.drop_non_serviceable_geo` выкидывает их перед SDK-запросом (подбор без гео) + пометка
  `kw_geo_dropped`. Тесты `test_keyword_plan.py::test_ru_geo_dropped_*`.
- **G §3/§18 (мультиаккаунт-мутации, управляемый список)** — `ads.client.allowed_ceiling()` = Draft ∪
  видимые аккаунты; включение мутаций = добавить видимый аккаунт в `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS`
  (+OAuth при чужом MCC, чек-лист `docs/DEPLOYMENT.md §2.1`). NL-команды изменений идут на АКТИВНЫЙ
  аккаунт (`_present_proposal` штампует его, карточка с баннером; не-включённый → отказ «только
  чтение»). Confirm-гейт/гард бюджета/per-account OAuth сохранены; чтение дочернего НЕ открывает
  мутации (`test_mutation_lock_unchanged_by_read_allowlist`). **Дефолт = Draft-only, пока владелец не
  включит аккаунт.** Визард/RSA/меню пока читают Draft (мультиаккаунт-создание — отдельный шаг).
  Тесты `test_safety_core.py::test_mutation_enabled_for_visible_account_in_allowlist` и др.,
  `test_bot_integration.py::test_agentloop_mutation_*`.

### Дельты пост-тестового прохода 2026-07-03 (баги живого теста + UX/контекст)

Закрыты дефекты и недоработки, найденные владельцем при живом тесте (скриншоты/журнал):

- **A1 §7** — экран параметров подбора ключей был мёртв (`AttributeError: kw_params_kb`): забыт
  импорт `kw_params_kb`/`kw_geo_kb` в `bot/main.py`. Гард класса — `tests/test_late_binding.py`
  (AST-скан: каждое `bm.<name>` в хендлерах существует на `bot.main`).
- **A2 §15** — `provider.sort: Invalid input` (400 OpenRouter на каждом парсинге): валидация
  `openrouter_parsing_provider_sort` в `core/config.py` (только price|throughput|latency|пусто).
  Тесты `test_config_failfast.py::test_provider_sort_*`.
- **A3 §14/§15** — журнал завален `scheduler:anomaly … PERMISSION_DENIED`: `discover_read_children`
  пропускает не-ENABLED дочерние (`ads/client.py`); scheduler логирует ожидаемый отказ как info, не
  как error (`core/ads_errors.is_account_access_error`). Тесты `test_mcc_discovery.py::test_discover_skips_inactive_children`,
  `test_ads_errors_classify.py`.
- **A4 §9/§15** — Sheets/xlsx по деактивированному аккаунту показывал подсказку про Sheets-scope
  (сбивало): теперь честная причина «аккаунт отключён/деактивирован» (`err_account_inactive`).
- **B1 §10** — RSA-описания стабильно переполняли 90 символов → флоу сдавался: добавлен ремонт-проход
  (LLM «сократи до ≤90») + детерминированный трим-фолбэк, гарантирующий минимум (`adcopy/generate.py`).
  Тесты `test_adcopy_generate.py::test_overlong_descriptions_recovered_*`.
- **B2 §10/§11** — «The operation is not allowed for the given context» при создании RSA: пикер
  показывает только Search-кампании, резолв групп отсекает не-`SEARCH_STANDARD` (`accepts_rsa`),
  а `_create_rsa_via_sdk` переводит context-ошибку в понятный текст. Тесты `test_ads_resolve.py::test_accepts_rsa_*`,
  `test_rsa_create.py::test_create_rsa_translates_context_error_*`, `test_ads_read.py::test_list_campaigns_channel_filter_in_query`.
- **C1–C3 §4** — «измени гео **этой** кампании» давало черновик с буквальным «этой кампании» и терял
  контекст между ходами: пер-чат контекст диалога (`_CHAT_CTX`) + детерминированная подстановка
  местоимения + короткое окно истории для модели (гибрид). Тесты `test_agent_loop.py::test_pronoun_campaign_*`.
- **C4 §20.3** — «➕ Добавить информацию» терял следующий текст (уходил в агент-задачу): кнопка
  несёт `customer_id` (restart-safe), приём текста всегда выставляет FSM-состояние. Тесты
  `test_client_wizard.py::test_add_button_carries_customer_id_restart_safe`.
- **D §6** — меню перегруппировано по смыслу (одним тапом); пикеры аккаунтов/кампаний отсортированы
  «активные → по имени» (Draft первым). Тесты `test_campaign_scope_and_picker.py::test_read_account_rows_sorted_*`,
  `test_ads_read.py::test_list_campaigns_sorted_active_first_then_name`.

### Дельты предсдаточного аудита 2026-07-03 (волны 1–4, «доводка до идеала»)

Закрыты пробелы ТЗ, найденные полным аудитом (детали — HANDOVER §7a, MUTATIONS «Закрыто…»):

- **§3 «чтение … ГЕО»** — `ads.read.read_campaign_targeting` + показ текущего гео в меню
  кампании (раньше гео только писалось). Тесты `test_ads_read.py::test_read_campaign_targeting_*`.
- **§7 параметры research** — ГЕО/язык/сеть/период доступны пользователю в `/keywords`
  (экран параметров; раньше все четыре были зашиты). Тесты `test_keyword_plan_params.py`.
- **§12 полнота журнала** — зависшие `executing` (крэш посреди мутации) → `needs_review` в
  audit + уведомление (`reconcile_stale_executing`). Тесты `test_reconcile_executing.py`.
- **§14 пороги per-chat** — команда `/alerts` (раньше заявлены, но точки входа не было).
- **§4 get_stats** — резолв аккаунта из аргумента (id/имя) вместо молчаливого первого
  разрешённого. Тесты `test_agent_loop.py::test_get_stats_*`.
- **Мультиаккаунт-подготовка** (мутации НЕ включены): исполнение по `proposal.customer_id`
  (`test_execute_account_binding.py`), грант-aware доступ (`ACCOUNT_ACCESS_MODE`,
  `/grant /revoke /accounts /whoami`), `/mcc` по всем MCC, пикеры из discovered-meta.
- **UX**: кнопки меню работают во время визардов (`test_menu_guard.py`), «‹ Назад»/крошки в
  визарде, пагинация пикеров, хаб «➕ Ещё», человекочитаемые итоги операций
  (`test_result_humanizer.py`), warnings частичного успеха composite-create.
- **Надёжность SDK**: partial_failure батчей ключей (`test_partial_failure.py`), честный учёт
  квоты по операциям, превью==созданное (micros кратны биллинг-единице), drop офлайн-бэклога.

## Доводка по живому тесту (round-2, 2026-07-04)

Правки по скринам владельца с боевого MCC (Draft ведётся в **AUD**). Все — офлайн-покрыты.

- **Валюта: любой ISO** — `update_budget`/`update_bid`/промо/прайс принимают ЛЮБОЙ 3-буквенный
  ISO-код (AUD/CZK/PLN…), а не Literal из 4 значений (раньше `literal_error` ронял бюджет на
  AUD-Draft ДО валютной сверки). Голая цифра без валютного слова → валюта аккаунта (детектор
  `ads.resolve.detect_currency_token`), петля «переформулируй в AUD» устранена. Тесты
  `test_currency_reconcile.py` (ISO/алиасы/детектор/мисматч).
- **Удаление (§3, необратимо, Draft-only, ДВОЙНОЙ confirm)** — `remove_campaign`/`remove_ad_group`
  (новые) + существующие `remove_keywords`/`remove_asset_link`; кнопка «🗑 Удалить кампанию» в
  меню + NL. Оба гейта (`ensure_allowed` Draft-only + claim/replay-one-shot) + `_DESTRUCTIVE_OPS`
  → двухшаговое подтверждение (`confirm_destructive_kb`→`confirm_final_kb`). Тесты
  `test_write_layer.py` (happy/replay + негатив-матрица чужой-аккаунт/без-confirm для ВСЕХ ops).
- **§19 визард**: ГЕО→авто-язык (Украина→uk детерминированно, промпт не «угадывает»);
  относительные даты «от сегодня до завтра» резолвит КОД (`parse_relative_dates`) + patch-ветки
  дат/сетей/расписания в `_cc_apply_settings_patch`; сгенерированные ключи сохраняются СРАЗУ
  (раньше терялись без round-trip таблицы) + кнопка «✅ Использовать эти ключи». Тесты
  `test_campaign_settings_extract.py`.
- **§7 ключи**: метрики с ЖИВОГО аккаунта (`_keyword_metrics_account`; Draft/тест → пустые);
  плоский топ best→worst в сводке; больше слов; NL seed-cap 10→25; «свои ключи/описание словами».
- **§8 экспорт всех аккаунтов**: `/mcc` шлёт xlsx по всем дочерним (`write_mcc_xlsx`); NL
  «экспорт статистики всех аккаунтов за N дней» → детерминированный роутинг (`is_export_all_accounts`).
  Тесты `test_export_all_intent.py`. Диагностический лог обхода MCC (всего/ENABLED/скрыто) — объясняет
  «почему в пикере N аккаунтов».
- **§11 DG/Video бюджет**: апфронт-предупреждение при бюджете ниже валюто-зависимого минимума
  (`core.limits.dg_video_min_daily_units`, AUD=8) + humanize per_day_minimum-ошибки.
- **§20 клиенты**: «🔎 Подтянуть из аккаунта» — facts-only patch из GAQL (валюта/таймзона/гео/языки/
  домен, БЕЗ LLM/выдумок) через тот же confirm-гейт памяти (`clients/account_facts.py`).
- **UX/тексты**: пикеры отчёта/экспорта и «‹ Назад» визарда редактируют сообщение (без дублей);
  стартовый промпт визарда/keywords/clients — копируемые `<code>`-примеры и вольная форма без спецсимволов.

## Доводка по независимому предсдаточному аудиту (2026-07-04)

Аудит 3 ТЗ независимой сверкой кода (17 агентов) нашёл реальные дефекты сверх самоотчёта. Закрыто:

- **A1 §11 (P0, был тихий дефект):** «🚀 Запустить» включала ТОЛЬКО кампанию — группа и RSA
  оставались PAUSED ⇒ 0 показов, менеджер думал, что кампания идёт. Введена операция
  `launch_campaign` (`apply_launch_campaign` + `_launch_campaign_via_sdk`), включающая кампанию +
  ВСЕ группы + ВСЕ объявления в ENABLED (фильтр REMOVED, идемпотентно). `cc_launch` минтит её вместо
  `resume_campaign`. Тесты `test_search_campaign.py::test_launch_campaign_via_sdk_enables_whole_tree`,
  `test_cc_stage_flow.py::test_launch_button_mints_launch_proposal`, негатив-матрица `test_write_layer`.
- **A2 §5 (целостность confirm-гейта):** `remove_negative_keywords` не было в `_KEYWORD_OPS` →
  удаление >20 минус-слов не прикладывало обязательный .xlsx, хотя сводка обещала вложение. Добавлено.
  Тест `test_bot_integration.py::test_big_remove_negative_keywords_proposal_attaches_xlsx`.
- **B4 §11 Video:** создание Video Google разрешает только по allowlist (`MUTATE_NOT_ALLOWED`). Кнопка
  Video в визарде СКРЫТА по умолчанию (`GOOGLE_ADS_VIDEO_ENABLED`); выбор Video без флага → продолжаем
  на Demand Gen. Тест `test_video_campaigns.py::test_video_type_kb_hides_video_button_by_default`.
- **B3 доки-замка:** шапка `ads/client.py` и CLAUDE.md «Что НЕ делать» приведены к реальному контракту
  `allowed_ceiling()` (код-минимум {Draft} нельзя понизить через env; эффективный потолок = минимум ∪
  видимые; включение мутаций — управляемым конфигом среди видимых). Раньше доки утверждали абсолютный
  Draft-only «env не расширит» — расходилось с кодом.
- **D9 §14:** плановая рассылка/аномалии/advise теперь идут env ∪ БД-операторам
  (`core.access.whitelisted_ids`, `scheduler.jobs._recipients` стал async) — рантайм-добавленный
  `/adduser` оператор получает отчёты без рестарта. Тесты `test_runtime_whitelist.py`.
- **B5 доки-оверклеймы:** §11-ссылки ACCEPTANCE переведены на имена функций (номера строк дрейфовали);
  честно отмечено, что воспроизводимого GDN live-smoke пока нет (покрыт DG/Video).
