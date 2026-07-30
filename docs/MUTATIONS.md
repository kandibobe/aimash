# MUTATIONS — карта покрытия изменяющих операций

Документ фиксирует **каждую** мутацию, которую код реально способен выполнить над
Google Ads, где она объявлена и какими гейтами защищена. Источник истины — код;
все утверждения ниже сверены по файлам с указанием строк.

## Три уровня, которые операция обязана пройти

1. **Схема агента** — `agent/tools/schemas.py`. Множество разрешённых к предложению
   инструментов — `MUTATION_TOOLS` (`agent/tools/schemas.py:41-69`). Модель лишь
   заполняет Pydantic-схему, диапазоны валидирует КОД.
2. **Диспетчер исполнения** — `ads/service.py`. Единый источник истины о том, что
   реально исполняется за confirm-гейтом — `SUPPORTED_OPERATIONS`
   (`ads/service.py:21-53`). `execute_confirmed` резолвит имя→id и вызывает
   соответствующую `apply_*`.
3. **Слой мутаций** — `ads/mutations.py`. «Единственное место, где код реально
   меняет аккаунт» (`ads/mutations.py:1`). На каждой `apply_*` — **два независимых
   гейта**: замок аккаунта `ensure_allowed(customer_id)` и confirm-гейт
   `_require_confirmation(...)`, шаги которого фиксированы: (0) аварийный рубильник
   BZ-1 → (1) freshness → (1b) blast-radius кап бюджета B1-4 → (2) money-event
   fail-closed → (3) атомарный one-shot claim. Отказы шагов 0–2 НЕ сжигают
   одноразовое подтверждение (claim ещё не случился). Для `BUDGET_INCREASE_OPS`
   шаги 1b→3 сериализованы модульным `asyncio.Lock` — конкурентные ✅ кап не обходят.
   Отказ по ВНЕШНЕЙ временной причине (рубильник, кап, недоступная история) — это
   `GateRefusal(PermissionError)` из `core/killswitch.py`: живой путь `_do_confirm`
   возвращает черновик `confirmed → pending` (`ConfirmStore.reopen`, audit
   `reopened`) и восстанавливает карточку — после снятия причины то же «да»
   проходит весь CAS-путь заново. Обычный `PermissionError` (дефект черновика) —
   черновик жжётся в `failed`, как и раньше.

### Замок аккаунта и confirm-гейт (золотые правила)

- **Мутируем только аккаунт, ВИДИМЫЙ боту.** Каждая `apply_*` первым делом зовёт
  `ensure_allowed(customer_id)` — единственный чокпойнт замка (проверка №0 внутри —
  аварийный рубильник `core/killswitch.py`, BZ-1). Draft-only режим **снят**
  (решение владельца 2026-07): явный сентинел `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS=all`
  = мутации на всех видимых аккаунтах, явный список id **сужает** набор, пустой
  список ⇒ мутаций нет вовсе В ЛЮБОМ окружении (fail-closed; тихая коэрция «пусто
  в prod → all» снята 2026-07-30). `execute_confirmed` берёт аккаунт **из самого черновика**
  (`proposal.customer_id`, `ads/service.py:322`), а не из константы, и заново проходит
  `ensure_allowed`. Точный контракт потолка — [`SECURITY.md`](SECURITY.md).
- **`confirmation_id` обязателен в каждой `apply_*`.** Без валидного одноразового
  подтверждения `_require_confirmation` бросает `PermissionError`
  (`ads/mutations.py:76-80`).
- **Подтверждение не живёт вечно.** Возраст черновика (`PROPOSAL_TTL_HOURS`, дефолт
  24 ч) стоит условием в `WHERE` у `ConfirmStore.confirm` **и** `claim` — то есть
  внутри самого CAS, а не проверкой после выборки (TOCTOU) и не в фоновой джобе
  (Волна 1.2). Просроченный черновик не подтверждается (`False`) и не столбится
  (`None` → `PermissionError`), даже если `cleanup_stale_proposals` не отработал.
  Это **не** freshness-recheck: TTL ограничивает возраст подтверждения, freshness —
  расхождение снимка с живым Google Ads. Инварианты — `tests/test_confirm_ttl_cas.py`.
- **Деньги — только по прямой команде пользователя, ДВА независимых бита** (Волна 1.4).
  Единая проверка `_require_user_command` внутри денежных `apply_*` (см. столбец
  «деньги?» ниже) требует **обоих**:
  - `user_initiated` — **аргумент** `save_proposal`. Сегодня верен по построению (черновик
    рождается внутри aiogram-хендлера за whitelist), но в headless-контуре его пишет
    вызывающий: MCP-инструмент, cron-джоба, self-improvement-форк. Бит, который можно
    передать аргументом, охраняет только аккуратных.
  - `origin_human_turn` — **аргументом не задаётся вовсе**: `ConfirmStore.save_proposal`
    берёт его из `core.provenance` (ContextVar). Поднять может только `human_turn(...)`, и
    единственный прод-call-site — `WhitelistMiddleware` (`bot/main.py`); `request_scope`
    (все scheduler-джобы, краул, dev-скрипты) наоборот **опускает** бит в `machine_turn`.
    Allow-list call-site'ов стережёт AST-тест.
  Бит штампуется в момент **СОЗДАНИЯ** черновика и **не повышается подтверждением** — иначе
  cron предлагает поднять бюджет, человек жмёт ✅, и проверка становится тождественно
  истинной. Выпускной гейт: `tests/test_provenance_gate.py::test_machine_draft_confirmed_by_human_still_refused`.
  Оба поля читаются напрямую, без `getattr(..., default)`: сторонний стор, забывший поле,
  обязан упасть на денежном пути, а не получить трактовку по умолчанию.

## Таблица покрытия

Все 39 операций ниже присутствуют одновременно в `SUPPORTED_OPERATIONS`
(`ads/service.py`) и имеют ветку в `execute_confirmed`. Столбец «в
MUTATION_TOOLS?» отмечает членство в наборе, разрешённом LLM-агенту
(`agent/tools/schemas.py`); часть операций, будучи в наборе, всё же минтуется
только ботом-визардом (нет tool-схемы для модели) — это отмечено в ячейке.
Полноту таблицы стережёт тест `test_docs_mutations_table_matches_supported_operations`
(`tests/test_write_layer.py`): новая операция без строки здесь роняет сборку — раньше
док молча отставал (в таблице было 29 из 39, среди пропавших — **денежная**
`update_keyword_bid`).

| operation | `apply_*` (ads/mutations.py) | Что делает | Деньги? (нужен `user_initiated`) | В `MUTATION_TOOLS`? |
|---|---|---|---|---|
| `update_budget` | `apply_update_budget` (:107) | Меняет дневной бюджет кампании (CampaignBudget) | **Да** | Да |
| `update_bid` | `apply_update_bid` (:455) | Меняет CPC-ставку на всех группах кампании; только при MANUAL_CPC | **Да** | Да |
| `update_keyword_bid` | `apply_update_keyword_bid` (:495) | Ф1: ставка на уровне КЛЮЧА (ad_group_criterion.cpc_bid_micros); только MANUAL_CPC | **Да** | Да |
| `add_keywords` | `apply_add_keywords` (:590) | Добавляет позитивные ключи в группы кампании; опц. `ad_group` СУЖАЕТ до одной группы (Ф4 «сбор урожая» — иначе веер по всем группам = каннибализация) | Нет | Да |
| `remove_keywords` | `apply_remove_keywords` (:1824) | Удаляет ключи по тексту+типу из групп кампании | Нет | Да |
| `add_negative_keywords` | `apply_add_negative_keywords` (:620) | Добавляет минус-слова на уровне кампании | Нет | Да |
| `remove_negative_keywords` | `apply_remove_negative_keywords` (:648) | Снимает минус-слова кампании по тексту+типу | Нет | Да |
| `add_negatives_to_shared_set` | `apply_add_negatives_to_shared_set` (:703) | 3.2б: минус-слова в ОБЩИЙ СПИСОК (NEGATIVE_KEYWORDS shared set); списка нет — создаёт (после claim) | Нет | Да |
| `attach_shared_set` | `apply_attach_shared_set` (:739) | 3.2б: привязывает СУЩЕСТВУЮЩИЙ общий список минус-слов к кампании (CampaignSharedSet); нет списка — отказ | Нет | Да |
| `pause_campaign` | `apply_pause_campaign` (:143) | Ставит кампанию на паузу (status=PAUSED) | Нет | Да |
| `resume_campaign` | `apply_resume_campaign` (:162) | Включает кампанию (status=ENABLED) | Нет | Да |
| `launch_campaign` | `apply_launch_campaign` (:189) | Запуск созданной визардом кампании (PAUSED → ENABLED) | Нет | В наборе нет: минтует бот (§19 визард) |
| `remove_campaign` | `apply_remove_campaign` (:424) | Необратимое удаление кампании (status→REMOVED) | Нет | Да (в UI — двойное подтверждение) |
| `update_campaign` | `apply_update_campaign` (:207) | Переименовывает кампанию (§3 «изменение»); имя ≤255, уникально в аккаунте | Нет | Да |
| `set_campaign_network` | `apply_set_campaign_network` (:237) | Поисковые партнёры Google (`target_search_network`) вкл/выкл | Нет | Да |
| `set_campaign_display_network` | `apply_set_campaign_display_network` (:264) | Ф6.2b (G12): КМС (`target_content_network`) на поисковой кампании вкл/выкл | Нет | Да |
| `set_campaign_geo_target_type` | `apply_set_campaign_geo_target_type` (:294) | Ф6.2b (G11): тип гео-таргетинга PRESENCE / PRESENCE_OR_INTEREST | Нет | Да |
| `pause_ad_group` | `apply_pause_ad_group` (:324) | Пауза отдельной группы объявлений | Нет | Да |
| `resume_ad_group` | `apply_resume_ad_group` (:342) | Возобновление отдельной группы объявлений | Нет | Да |
| `remove_ad_group` | `apply_remove_ad_group` (:439) | Необратимое удаление группы (status→REMOVED) | Нет | Да (в UI — двойное подтверждение) |
| `pause_ad` | `apply_pause_ad` (:364) | Пауза отдельного объявления | Нет | Да |
| `resume_ad` | `apply_resume_ad` (:383) | Возобновление отдельного объявления | Нет | Да |
| `remove_ad` | `apply_remove_ad` (:402) | Необратимое удаление объявления (status→REMOVED) | Нет | Да (в UI — двойное подтверждение) |
| `set_geo_proximity` | `apply_set_geo_proximity` (:690) | Радиус-таргетинг вокруг адреса (remove-before-create) | Нет | Да |
| `set_geo_location` | `apply_set_geo_location` (:729) | Гео по стране/городу через geoTargetConstant (remove-before-create) | Нет | Да |
| `set_bidding_strategy` | `apply_set_bidding_strategy` (:1232) | Смена стратегии ставок кампании | **Да** | Да |
| `attach_audience` | `apply_attach_audience` (:770) | Прикрепляет user_list/audience к кампании | Нет | В наборе, но без tool-схемы → минтует бот |
| `detach_audience` | `apply_detach_audience` (:823) | Снимает ранее прикреплённые аудитории с кампании | Нет | В наборе, но без tool-схемы → минтует бот |
| `create_rsa` | `apply_create_rsa` (:1329) | Создаёт RSA-объявление в группе (PAUSED) | Нет | Да (минтуется ботом после курации) |
| `create_gdn_campaign` | `apply_create_gdn_campaign` (:3013) | Создаёт Display-кампанию из фото (всё PAUSED) | **Да** | Да (обычно через бот-визард) |
| `create_search_campaign` | `apply_create_search_campaign` (:2292) | Создаёт поисковую кампанию (всё PAUSED) | **Да** | Да (обычно через бот-визард) |
| `create_demand_gen_campaign` | `apply_create_demand_gen_campaign` (:3295) | Создаёт Demand Gen из YouTube-видео (всё PAUSED) | **Да** | Да (обычно через бот-визард) |
| `create_video_campaign` | `apply_create_video_campaign` (:3561) | Создаёт Video-кампанию (YouTube, всё PAUSED) | **Да** | Да (обычно через бот-визард) |
| `add_sitelinks` | `apply_add_sitelinks` (:933) | Добавляет sitelinks (campaign_asset SITELINK) | Нет | Да |
| `add_callouts` | `apply_add_callouts` (:962) | Добавляет callouts (уточнения) | Нет | Да |
| `add_structured_snippets` | `apply_add_structured_snippets` (:989) | Добавляет структурное описание (header+values) | Нет | Да |
| `attach_image_asset` | `apply_attach_image_asset` (:1018) | Прикрепляет изображение-ассет к кампании | Нет | Нет (нужно фото → бот-визард) |
| `add_call_asset` | `apply_add_call_asset` (:1100) | Телефон-расширение (CallAsset) | Нет | Да |
| `add_promotion` | `apply_add_promotion` (:1129) | Промо-расширение (PromotionAsset) | Нет | Да |
| `add_price_asset` | `apply_add_price_asset` (:1167) | Прайс-расширение (PriceAsset, 3–8 оферов) | Нет | Да |
| `remove_asset_link` | `apply_remove_asset_link` (:1201) | Открепляет связь campaign_asset (не сам ассет) | Нет | Да |

**Примечание по «деньги?».** Денежными помечены операции, где внутри `apply_*`
стоит вызов `_require_user_command(proposal, ...)` (оба бита провенанса, см. выше): это
`update_budget`, `update_bid`, `update_keyword_bid`, `set_bidding_strategy` и все
четыре `create_*_campaign` (они несут бюджет). Остальные (статусы, переименование,
ключи, минус-слова, гео, сети, аудитории, ассеты-расширения, `create_rsa`) деньгами
не управляют → провенанса не требуют. Инварианты
`test_money_apply_functions_match_registry_and_guard_user_initiated` (реестр денежных
операций ⇔ call-site'ы гейта) и `test_money_gate_requires_both_provenance_bits` (тело
гейта ссылается на **оба** бита — иначе гард выхолащивается до одного, а call-site'ы
остаются на месте и первый тест этого не заметит) — `tests/test_invariants_core.py`.

**Примечание по общему бюджету (П1).** `update_budget` меняет `CampaignBudget`, а он
может быть ОБЩИМ (`explicitly_shared` / `reference_count > 1`) — тогда изменение затронет
ВСЕ привязанные кампании, не только названную. `read_before` раскрывает радиус
(`resolve.campaigns_sharing_budget` → `_before.shared_campaigns`), карточка печатает
«⚠️ Общий бюджет — затронет также: …», а `apply_update_budget` без раскрытого согласия
(`disclosed_shared_scope`, который `execute_confirmed` ставит из `_before.shared`) —
**fail-closed `raise PermissionError`**: молча тронуть чужие кампании нельзя. Радиус
`apply_*` перечитывает по ЖИВОМУ аккаунту (не по snapshot карточки) → TOCTOU-safe в обе
стороны (бюджет стал общим ПОСЛЕ показа карточки — откажем; перестал — не соврём).

**Примечание по аудиториям и `attach_image_asset`.** `attach_audience`/`detach_audience`
входят в `MUTATION_TOOLS`, но НЕ имеют tool-схемы в `TOOLS` — модель их не эмитит,
их минтует бот из пикера аудиторий (`resource_name` из `ads.read.list_audiences`).
`attach_image_asset` не входит и в `MUTATION_TOOLS` — тоже бот-визард (бинарь по
`media_id`).

## Allow-list: агент вызывает только пересечение списков

Агент может инициировать операцию, только если она — в `MUTATION_TOOLS`, а исполнить
её код согласится, только если она в `SUPPORTED_OPERATIONS`. **Неизвестную операцию
код отклоняет ДО показа кнопок подтверждения**: в `agent/loop.py` для ветки
`name in MUTATION_TOOLS` (`agent/loop.py:142`) стоит capability-guard —
`from ads.service import SUPPORTED_OPERATIONS; if name not in SUPPORTED_OPERATIONS:
return {"type": "text", ...}` (`agent/loop.py:147-150`). Это не даёт пользователю
подтвердить то, что код не выполнит.

Тот же барьер продублирован как defense-in-depth на исполнении: `execute_confirmed`
повторно проверяет `if op not in SUPPORTED_OPERATIONS: raise PermissionError`, а на
«объявлено, но нет ветки» — fail-closed `raise` в конце.

## Симметрия add/remove — полная

Для всех трёх управляемых сущностей есть и добавление, и снятие (пробелы (b)/(c)
прежнего аудита закрыты):

- **Ключи:** `add_keywords` (:258) / `remove_keywords` (:1155).
- **Минус-слова:** `add_negative_keywords` (:282) / `remove_negative_keywords` (:304)
  → SDK-резолв text→resource_name + remove (`_remove_negative_keywords_via_sdk`
  `:1248`), зеркало add (`_add_negative_keywords_via_sdk` `:1220`).
- **Аудитории:** `attach_audience` (:420) / `detach_audience` (:473) → SDK-резолв
  `resource_name` аудитории → `campaign_criterion` + remove
  (`_detach_audience_via_sdk` `:498`), зеркало attach (`_attach_audience_via_sdk`
  `:445`).

## Пробелы покрытия и ограничения (честно)

### (a) Изменение кампании — имя, статус, сети, гео-тип, стратегия; расписание/даты — нет

`apply_update_campaign` (`ads/mutations.py:207`) меняет **только имя** уже созданной
кампании (§3 «изменение»); имя уникально в аккаунте — `DUPLICATE_CAMPAIGN_NAME`
перехватывается понятной ошибкой. Кроме имени код умеет править у СУЩЕСТВУЮЩЕЙ
кампании: статус (`pause`/`resume`/`launch`/`remove_campaign`), стратегию ставок
(`set_bidding_strategy`), поисковых партнёров (`set_campaign_network`), КМС
(`set_campaign_display_network`, Ф6.2b) и тип гео-таргетинга
(`set_campaign_geo_target_type`, Ф6.2b), а также гео-локации/радиус
(`set_geo_location`/`set_geo_proximity`). **Не правятся** расписание показов, даты
старта/окончания и URL-опции — они задаются только при создании
(`apply_create_search_campaign` `:2292`).

### (b) Смена типа соответствия у ключа — через remove+add

Тип соответствия (match type) у ключевого слова в Google Ads **иммутабелен** —
единственный путь сменить его на существующем criterion — **удалить старый ключ и
создать новый**. Отдельной операции «сменить match type» нет, но паттерн remove+add
поддержан: удаление позитивных ключей реализовано — `apply_remove_keywords`
(`ads/mutations.py:1155`) → `_remove_keywords_via_sdk` (`:1177`) резолвит текст+тип в
`resource_name` через GAQL и шлёт remove. Смена типа = `remove_keywords` со старым
типом + `add_keywords` с новым (аналогично для минус-слов).

### Прочие ограничения

- Все `create_*_campaign` создают сущности в статусе **PAUSED** (нулевой расход) —
  зафиксировано в докстрингах и проверяется тестами (`tests/test_*_campaign*.py`).
- `remove_asset_link` (`ads/mutations.py:822`) удаляет **связь** `campaign_asset`, а
  не сам ассет.
- `update_bid` работает только при ручной стратегии MANUAL_CPC; при автостратегии код
  сам откажет до мутации (`_apply_bid_via_sdk`, `ads/mutations.py:1058`).

### Закрыто предсдаточным аудитом 2026-07 (волны 1–3)

- **Чтение текущего ГЕО** (§3 «чтение … ГЕО») — реализовано: `ads.read.read_campaign_targeting`
  (LOCATION/PROXIMITY/LANGUAGE + резолв имён), показывается в меню кампании «📍 Гео» перед
  изменением. Клон (`read_campaign_config`) гео по-прежнему не переносит (честно сообщает).
- **partial_failure для батчей ключей** — `_add_keywords_via_sdk` и
  `_add_negative_keywords_via_sdk` шлют `partial_failure=True`: одна плохая позиция не валит
  батч; отклонённые возвращаются в `result['rejected']` с причиной (UX: «добавлено M, отклонено
  K»); все отклонены ⇒ честный `failed`. Скоуп осознанно узкий: `remove_*`/ассеты — whole-batch.
- **Warnings частичного успеха composite-create** — best-effort шаги 5–8
  (`keywords/geo/languages/ad_schedule`) кладут `result['warnings']`, бот показывает
  «⚠️ гео НЕ применено (0 из 2)» (раньше — молчаливый 0).
- **Исполнение привязано к `proposal.customer_id`** — `execute_confirmed` читает аккаунт из
  черновика и ЗАНОВО проходит `ensure_allowed` (fail-closed; раньше хардкод Draft — латентная
  ловушка включения мутаций на дочерних).
- **Реконсиляция зависших `executing`** — крэш процесса посреди мутации ⇒ терминальный
  `needs_review` + audit-строка + уведомление владельца (НЕ авто-ретрай: мутации не идемпотентны).
