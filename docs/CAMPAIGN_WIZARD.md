# §19 — Визард «Создание Search-кампании» (`/newcampaign`)

Документ для разработчика/ревьюера. Описывает 8-этапный флоу создания поисковой кампании
Google Ads, реализованный в `bot/main.py` (хендлеры `cc_*` / `_cc_*`), с персистом черновика в
таблице `campaign_drafts` и финальной атомарной мутацией `create_search_campaign`.

## 1. Обзор

Менеджер описывает кампанию на естественном языке; бот раскладывает её на настройки, ключи,
объявление, ассеты и в конце выпускает **ОДИН** composite-proposal. Ключевые принципы:

- **PAUSED-черновик.** Всё, что создаётся в Google Ads, — со статусом `PAUSED` (0 расхода).
  Статусы зашиты в КОДЕ (`_create_search_campaign_via_sdk`), не в промпте.
- **Запуск — отдельной командой.** Кнопка «🚀 Запустить» (`cc_launch`) НЕ включает кампанию сама:
  она минтует отдельный proposal `resume_campaign` (та же ветка, что `/campaigns` → «Возобновить»),
  который тоже проходит confirm-гейт.
- **Всё через confirm-гейт.** Единственная реальная мутация визарда — финальное
  «✅ Создать черновик» (`cc_create` → `_present_proposal`), исполняется только после явного «да»
  с `confirmation_id` + audit-строка. Этапы 0–6 не трогают SDK (advisory-разбор, накопление в БД).
- **Мутации только на Draft.** Аккаунт мутации всегда `DRAFT_ACCOUNT_ID = 7753643025`
  (`ads.client.ensure_allowed`). Дочерний аккаунт, выбранный на Этапе 0 (`preview_customer_id`), —
  ТОЛЬКО для чтения (медианы «по аналогии», ассеты для переиспользования).

Вход: `/newcampaign` (`newcampaign_cmd`) или кнопка меню (`btn_newcampaign`) → `_cc_entry`.
Если есть незавершённый активный черновик — предлагается «▶️ Продолжить» (`cc_resume`) /
«🆕 Начать заново» (`cc_new`); иначе `_cc_begin` создаёт свежий черновик и показывает Этап 0.

## 2. Модель данных

Таблица `campaign_drafts` (`db/models.py::CampaignDraft`, миграция
`migrations/versions/0012_campaign_drafts.py`; на SQLite/dev создаёт `create_all`):

| Колонка | Тип | Назначение |
| --- | --- | --- |
| `session_id` | `String(64)` unique | hex uuid черновика (ключ FSM `cc_session`) |
| `chat_id` | `BigInteger` | владелец; гард `expected_chat_id` во всех мутациях store |
| `customer_id` | `String(20)` | аккаунт МУТАЦИИ — всегда Draft `7753643025` |
| `preview_customer_id` | `String(20)?` | дочерний MCC Этапа 0 (read-only) |
| `current_step` | `Integer` | курсор этапа `0..7` |
| `wizard_state` | `JSON` | единый источник накопленных данных всех этапов |
| `status` | `String(16)` | `active` / `done` / `abandoned` |
| `created_at` / `updated_at` | `DateTime(tz)` | `updated_at` — база TTL |

`wizard_state` — скелет из `bot/campaign_wizard/store.py::empty_wizard_state()`; хендлеры
рассчитывают на наличие всех ключей:

- `settings` — `campaign_name/product/geo_locations/languages/budget_daily_micros/cpc_bid_micros/`
  `bidding_strategy/match_type/target_language/geo_country_code/networks/ad_schedule_blocks/`
  `start_date/end_date/by_analogy` (поля из медиан помечены тегом `by_analogy`).
- `keywords` — `list / match_type / match_types (per-keyword, None ⇒ однородный) / source`
  (`sheet|file|text|generated`) `/ sheet_id / verified`.
- `ad` — `final_url / path1 / path2 / rsa_session_id / headlines / descriptions`.
- `images` — `media_ids / skipped / eligible` (бинарь во временном медиа-хранилище по `media_id`,
  НЕ в JSON — golden rule #5).
- `assets` — `reuse_links / new [{family, params}]`.
- `url_options` — `tracking_url_template / final_url_suffix / custom_parameters`.

**Статусы и «один активный на чат».** `CampaignDraftStore.create` при новом черновике гасит
прежние `active` этого чата в `abandoned` (start-over). Правки идут ТОЛЬКО на `active`
(`patch/set_step/set_preview` с `active_only=True`): поздний колбэк после abandon/done → `None`
(контракт), иначе scheduler-abandon «воскресил» бы черновик. `_load` мутирует через
deepcopy → `flag_modified` (JSON-колонка не отслеживает вложенные изменения; паттерн из
`adcopy.session.SessionStore`).

**TTL.** `core.config.campaign_draft_ttl_hours = 72`. Черновик переживает рестарт бота (в БД, не в
`MemoryStorage`) и Этап-2 round-trip с Google Sheets (менеджер уходит редактировать таблицу и
возвращается позже, возможно на другой день). Просроченные `active` гасит
`scheduler/jobs.py::cleanup_stale_campaign_drafts` (`status='abandoned'` по возрасту `updated_at`,
считается в Python как UTC; заодно чистит осиротевшие `media_ids` и логотипы `business_logo`).

## 3. Восемь этапов

Отрисовка при возобновлении — `_cc_render_stage` (по `current_step`). Каждый `_cc_present_stageN`
двигает курсор (`set_step`) и ставит FSM-состояние `CreateCampaignWizard.*`.

| # | Этап | Хендлер / present | Ввод менеджера | Что показывает бот | Кнопка / переход |
| --- | --- | --- | --- | --- | --- |
| 0 | Аккаунт | `_cc_present_stage0`, `cc_account_cb`, `cc_account_page_cb` | выбор дочернего аккаунта (read-only превью) | постраничный пикер дочерних MCC | фиксирует `preview_customer_id` → Этап 1 |
| 1 | Настройки | `cc_settings_desc`, `cc_settings_edit`, `cc_edit_hint`, `cc_accept` | свободное описание, затем текстовые правки | сводка настроек (`fmt_cc_settings_summary`, теги «(по аналогии)») | ✏️ Изменить / ✅ Принять (`accept`) → Этап 2 |
| 2 | Ключи | `_cc_present_stage2`, `cc_keywords_text`, `cc_kw_generate`, `cc_kw_verify`, `cc_kw_confirm` | свои ключи ИЛИ генерация (см. §4) | обзор списка + тип соответствия | ✅ Подтвердить ключевые слова (`kw_confirm`) → Этап 3 |
| 3 | RSA-объявление | `_cc_present_stage3`, `cc_ad_url`, `_cc_finalize_ad` | Final URL | display path (КОД) + генерация 15/4 → сессия курации `adcopy.session` | курация RSA (list-UX) → Этап 4 |
| 4 | Изображения | `_cc_present_stage4`, `_cc_image_from_photo`, `cc_skip` | фото (нарезка 1.91:1 + 1:1) или пропуск | приглашение прикрепить/пропустить | Готово/⏭ Пропустить (`skip`) → Этап 5 |
| 5 | Ассеты | `_cc_present_stage5`, `cc_use_assets`, `cc_add_assets`, `cc_asset_type`, `_cc_asset_logo_from_photo` | переиспользовать ассеты Draft / добавить новые / пропустить | список доступных ассетов | Готово → Этап 6 |
| 6 | URL-опции | `_cc_present_stage6`, `cc_url_text`, `cc_skip` | `tracking \| suffix \| custom` | приглашение или пропуск | ⏭ Пропустить → Этап 7 |
| 7 | Финал | `_cc_present_stage7`, `cc_final_edit`, `cc_create`, `cc_launch` | правка командой «X на Y» / правка настроек | итоговая сводка (`fmt_cc_final_summary`) | ✅ Создать черновик (`create`) |

Настройки Этапа 1 извлекает `agent.campaign_settings.extract_campaign_settings` (роль parsing,
LLM только переносит поля; деньги/имя считает КОД), затем `assemble_settings` доливает недостающие
поля медианами `ads.read.search_campaign_medians` («по аналогии»: median бюджет / avg CPC / частый
match_type за 90 дней активных Search-кампаний превью-аккаунта). Ассеты Этапа 5 читаются строго с
`DRAFT_ACCOUNT_ID` (CampaignAsset не может ссылаться на ассет другого аккаунта).

## 4. Этап 2 подробно — ключевые слова

Развилка на кнопках `cc_kw_kb()`:

**А. Свои ключи** — 4 способа, все ведут в `_cc_save_keywords`:
1. **Текст** (`cc_keywords_text`) — `keywords.ingest.parse_keywords_text`; маркеры типа
   соответствия per-keyword (смешанный список сохраняется в `keywords.match_types`, дедуп ПАРАМИ
   `(текст, тип)`).
2. **Файл** XLSX/CSV/TXT — `on_document` → `_cc_keywords_from_document` (колонка ключей).
3. **Ссылка Google Sheets** — распознаётся `parse_spreadsheet_id`, читается `read_keyword_column`
   (scope `spreadsheets.readonly` → любая доступная таблица менеджера).
4. **Инструкция + список** — текст с указанием типа соответствия для всего списка.

**Б. Генерация** (`cc_kw_generate`):
seed (`keywords.seeds.generate_seed_keywords`, тема = `product`/имя, язык и ГЕО целевой страны,
сайт из §20-профиля) → `ads.keyword_plan.generate_keyword_ideas` (KeywordPlanIdeaService,
`keyword_and_url_seed`) → `keywords.filter.filter_relevance` (LLM-фильтр релевантности) →
`reports.sheets.publish_keywords_to_sheets` (колонка «Релевантность», возвращает `(url, sid)`) →
менеджер правит таблицу → присылает ссылку (`cc_kw_verify`).

**Round-trip верификация.** `cc_kw_verify` сверяет `parse_spreadsheet_id` присланной ссылки с
сохранённым `keywords.sheet_id`: чужая ссылка (перепутанная таблица) отвергается
(`cc_kw_wrong_sheet`). Затем `read_keyword_column` читает колонку A, **отбрасывая** строки с явной
пометкой `❌ Нерелевантно` (не переопределённые менеджером). Если Sheets недоступен (нет scope) —
fallback: релевантные идеи берутся напрямую, без round-trip.

**Тип соответствия** для способов sheet/file/generated — `_cc_default_match_type(draft)`:
подтверждённый на Этапе 1 (`settings.match_type`, «по аналогии» из аккаунта либо `phrase`-дефолт),
**НЕ хардкод** `phrase` (баг B6). **Санитайз** — `_cc_sanitize_keywords` прогоняет сырые ключи
через `keywords.ingest.assert_keyword_ok` (длина / число слов / символы), отброшенные считаются и
сообщаются (`cc_kw_dropped`), иначе финал падал бы безликим `ValidationError` на 11-словном
заголовке-ключе из колонки A.

Явный гейт «✅ Подтвердить ключевые слова» (`cc_kw_confirm`) стоит ПЕРЕД Этапом 3; замена =
прислать новый список (остаёмся на Этапе 2). Пропуск Этапа 2 допустим (кампания без ключей).

## 5. Финал — composite proposal и атомарный откат

`cc_create` собирает params через `_cc_build_create_params(draft)`, валидирует
`SCHEMAS["create_search_campaign"]`, показывает `fmt_search_proposal_summary` и выпускает **один**
proposal `create_search_campaign` (`_present_proposal`). Порог RSA (`≥` min headlines/descriptions)
и `final_url` проверяются до минта. Список ключей режется до `MAX_CAMPAIGN_KEYWORDS` — обрезка НЕ
молчится (лог + сообщение `cc_keywords_truncated`).

На «да» → `ads.mutations.apply_create_search_campaign` (двойной гейт: `ensure_allowed` +
`confirmation_id`) → `_create_search_campaign_via_sdk` — синхронная цепочка v24:

1. бюджет (`explicitly_shared=False`) → 2. кампания (`SEARCH`, `PAUSED`, стратегия + URL-опции +
сети/даты) → 3. группа объявлений (`SEARCH_STANDARD`, `PAUSED`) → 4. RSA (`PAUSED` + display path) →
5. ключи → 6. гео → 7. язык → 8. расписание. **Все сущности `PAUSED` (0 расхода).**

**Атомарный откат.** Сбой шага 2 (кампания) удаляет осиротевший бюджет. Шаги 3–4 (группа + RSA)
обёрнуты в единый `try`: любой сбой вызывает `_rollback_partial` — удаляет группу (если создана),
кампанию и бюджет. Иначе на аккаунте осталась бы мусорная PAUSED-кампания ($0), а имя было бы
занято → повтор визарда падал бы на `DUPLICATE_CAMPAIGN_NAME`. Каждое удаление изолировано, чтобы
сбой отката не маскировал исходную ошибку. Шаги 5–8 (ключи/гео/язык/расписание) — best-effort:
их сбой уже созданную кампанию НЕ роняет (`kw_created=0` сигналит о недобавленных ключах).

**Кратность денег.** Все micros округляются до минимальной биллинг-единицы `_MICROS_UNIT = 10_000`
(0.01 валюты) через `_round_micros` — иначе API reject.

**Гашение черновика.** Черновик гасится (`finish` → `done`) ТОЛЬКО при УСПЕШНОМ подтверждении
(`_do_confirm`, баг B9), не в `cc_create`. При ❌ (reject) он остаётся `active` → менеджер
возобновляет «▶️ Продолжить» и не теряет RSA/ключи/настройки из-за одного нажатия «Отмена».
После успеха предлагается «🚀 Запустить» (`cc_launch` → отдельный `resume_campaign` proposal;
`cc_bidding_downgraded`, если стратегия была понижена из-за отсутствия отслеживания конверсий).

## 6. Google Ads API методы по этапам

| Этап | Чтение/мутация | Метод / сервис |
| --- | --- | --- |
| 0 | read | `CustomerService` / `list_child_accounts` (GAQL по MCC) |
| 1 | read | `search_campaign_medians` (GAQL по бюджетам/CPC/match_type) |
| 2 (генерация) | read | `KeywordPlanIdeaService.GenerateKeywordIdeas` |
| 5 | read | `list_account_assets` (GAQL по ассетам Draft) |
| 7 (create) | mutate | `CampaignBudgetService` → `CampaignService` → `AdGroupService` → `AdGroupAdService` (RSA) → `AdGroupCriterionService` (ключи) → критерии гео/языка → `CampaignCriterionService` (расписание) |
| launch | mutate | `CampaignService` (`resume_campaign`: PAUSED → ENABLED) |

Sheets (Этап 2): `spreadsheets.create` + `values.batchUpdate` (publish), `values.get` (read),
scope `drive.file` / `spreadsheets.readonly`.

## 7. Известные отклонения (из шапки `ТЗ.md`)

- **Кириллица = 1 символ** (golden rule #4, `adcopy.validate.rsa_len`), CJK = 2. Утверждение
  docx §19.5.1 «кириллица = 2» — **ошибка документа**, игнорируется (`adcopy/display_path.py`).
- **Image-гейт не проверяется** (§19.6): этап изображений показывается всегда; при неприменимости
  ассет пропускается на SDK-шаге (кампания остаётся PAUSED/$0).
- ~~Custom parameters вне UI~~ — **сделано** (2026-07): третье поле pipe-ввода `tracking |
  suffix | custom` принимается в UI (`cc_url_text`).
- ~~Расписание / даты не выводятся в сводку Этапа 1~~ — **сделано** (2026-07): выводятся в
  `fmt_cc_settings_summary`; без данных в описании применяются дефолты (24/7, старт сегодня).
- **Минус-слова — вне визарда**: при генерации показываются как ADVISORY-подсказка
  (`suggest_negative_keywords`), но НЕ добавляются; добавление — отдельной командой агента за гейтом.
- **Гео-радиус вне UI**: `extract_campaign_settings` поддерживает города/страны; радиус по
  координатам (proximity) в визарде не собирается.
- **§19.7.1 Location / Affiliate / Lead form** — config-gated заглушки (нужны Business Profile /
  реестр / privacy URL), пропускаются чисто. App-ассет вне объёма (UAC исключён).

## 8. Тесты

- `tests/test_cc_stage_flow.py` — прохождение этапов 0→7, переходы, стейл-черновики.
- `tests/test_cc_composite_create.py` — сборка params + composite create + откат.
- `tests/test_cc_display_path.py` — `build_display_path` (лимит 15, кириллица=1, CJK=2).
- `tests/test_cc_rsa_handoff.py` — handoff курации RSA (`_cc_finalize_ad`) в черновик.
- `tests/test_cc_assets_and_edit.py` — ассеты Этапа 5 + правки «X на Y» финала.
- `tests/test_campaign_wizard_state.py` — `empty_wizard_state` / `CampaignDraftStore` (active-only,
  один активный на чат, гард владения).
- `tests/test_search_campaign.py` — `apply_create_search_campaign` / SDK-цепочка / гейты.
- `tests/test_predelivery_fixes.py` — предсдаточные баги (B3/B5/B6/B9/B10/B11: санитайз, тип
  соответствия, гашение черновика, обрезка ключей, дедуп парами).
