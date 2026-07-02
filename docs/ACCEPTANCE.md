# §18 — Чек-лист приёмки Aimash

Источник истины по критериям — [`ТЗ.md` §18](../ТЗ.md) («Результат и критерии приёмки»).
Ниже каждый критерий сопоставлен с **реальной** реализацией (`file:line`) и **реальным** тестом,
который её доказывает. Все ссылки проверены по коду на дату документа.

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
- **GDN-кампания из фото** (DISPLAY, PAUSED): `ads/mutations.py:1938`
  (`apply_create_gdn_campaign`), PAUSED — `ads/mutations.py:2051`; `user_initiated` —
  `ads/mutations.py:1974`. Цепочка помечена «сверено live» в коде (`ads/mutations.py:2016`).
- **Demand Gen из YouTube-видео** (DEMAND_GEN, PAUSED): `ads/mutations.py:2209`
  (`apply_create_demand_gen_campaign`), PAUSED — `ads/mutations.py:2335`; `user_initiated` —
  `ads/mutations.py:2256`.
- **Video-кампания (YouTube)** (PAUSED): `ads/mutations.py:2437`
  (`apply_create_video_campaign`), `user_initiated` — `ads/mutations.py:2474`.
  ⚠️ Docstring прямо предупреждает: «SDK-цепочка требует live-сверки на тест-аккаунте перед
  сдачей» (`ads/mutations.py:2458`); аналогично для Demand Gen (`ads/mutations.py:2299`).
- Запуск по команде (PAUSED → ENABLED отдельным confirm-гейтом): кнопка «🚀 Запустить» минтит
  `resume_campaign`-черновик — `bot/main.py:3827` (`cc_launch`), а сама операция —
  `ads/mutations.py:140` (`apply_resume_campaign`). Запуск НЕ происходит автоматически при
  создании (`bot/main.py:5272`).
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

## Золотые правила безопасности (проверены по коду)

| Правило | Реализация | Тест |
|---|---|---|
| Мутации только на Draft `7753643025` | `ads/client.py:212` (`ensure_allowed`); потолок в коде `ALLOWED_CEILING` — `ads/client.py:25`; fail-closed при пустом allow-list — `ads/client.py:223` | `tests/test_safety_core.py`, `tests/test_invariants_core.py` |
| Confirm-гейт с `confirmation_id` в каждом `apply_*` | `ads/mutations.py:70` (`_require_confirmation`) → атомарный `claim` `confirm/store.py:119` | `tests/test_write_layer.py`, `tests/test_invariants_core.py` |
| Бюджет/деньги только при `user_initiated` | `ads/mutations.py:108` (бюджет), `ads/mutations.py:222` (ставка), `ads/mutations.py:759` (стратегия); дефолт `False` — `confirm/gate.py:24` | `tests/test_safety_core.py`, `tests/test_invariants_core.py` |
| Секреты не утекают (логи/audit/чат) | `redact_text` в audit — `confirm/store.py:280`; whitelist fail-closed — `bot.main.WhitelistMiddleware` | `tests/test_logging_redaction.py`, `tests/test_whitelist.py` |

---

### Сводка по статусам

- ✅ критерии 1, 2, 3, 4, 6, 7, 8, 9.
- ⚠️ критерий 5 — Search/GDN реализованы (GDN помечен «сверено live» в коде,
  `ads/mutations.py:2016`); Video и Demand Gen реализованы полностью в коде, но их SDK-цепочки по
  собственным комментариям требуют **живой сверки на тест-аккаунте перед сдачей**
  (`ads/mutations.py:2458`, `ads/mutations.py:2299`). UAC исключён из объёма намеренно.
