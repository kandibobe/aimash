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
   `_require_confirmation(...)` (атомарный one-shot claim, `ads/mutations.py:70-81`).

### Замок аккаунта и confirm-гейт (золотые правила)

- **Мутации только на Draft `7753643025`.** Каждая `apply_*` первым делом зовёт
  `ensure_allowed(customer_id)` (импорт `ads/mutations.py:30`). `execute_confirmed`
  всегда передаёт `customer_id = DRAFT_ACCOUNT_ID`.
- **`confirmation_id` обязателен в каждой `apply_*`.** Без валидного одноразового
  подтверждения `_require_confirmation` бросает `PermissionError`
  (`ads/mutations.py:76-80`).
- **Деньги — только по прямой команде пользователя** (`user_initiated`). Проверяется
  внутри денежных `apply_*` (см. столбец «деньги?» ниже). Провенанс
  «прямая команда» агент себе НЕ проставляет — `user_initiated` в `agent/loop.py`
  не выставляется (`agent/loop.py:164-166`), его ставит только доверенный вход бота.

## Таблица покрытия

Все 29 операций ниже присутствуют одновременно в `SUPPORTED_OPERATIONS`
(`ads/service.py:21-53`) и имеют ветку в `execute_confirmed`. Столбец «в
MUTATION_TOOLS?» отмечает членство в наборе, разрешённом LLM-агенту
(`agent/tools/schemas.py:41-69`); часть операций, будучи в наборе, всё же минтуется
только ботом-визардом (нет tool-схемы для модели) — это отмечено в ячейке.

| operation | `apply_*` (ads/mutations.py) | Что делает | Деньги? (нужен `user_initiated`) | В `MUTATION_TOOLS`? |
|---|---|---|---|---|
| `update_budget` | `apply_update_budget` (:85) | Меняет дневной бюджет кампании (CampaignBudget) | **Да** | Да |
| `update_bid` | `apply_update_bid` (:225) | Меняет CPC-ставку на всех группах кампании; только при MANUAL_CPC | **Да** | Да |
| `add_keywords` | `apply_add_keywords` (:258) | Добавляет позитивные ключи во все группы кампании | Нет | Да |
| `remove_keywords` | `apply_remove_keywords` (:1155) | Удаляет ключи по тексту+типу из групп кампании | Нет | Да |
| `add_negative_keywords` | `apply_add_negative_keywords` (:282) | Добавляет минус-слова на уровне кампании | Нет | Да |
| `remove_negative_keywords` | `apply_remove_negative_keywords` (:304) | Снимает минус-слова кампании по тексту+типу | Нет | Да |
| `pause_campaign` | `apply_pause_campaign` (:121) | Ставит кампанию на паузу (status=PAUSED) | Нет | Да |
| `resume_campaign` | `apply_resume_campaign` (:140) | Включает кампанию (status=ENABLED) | Нет | Да |
| `update_campaign` | `apply_update_campaign` (:161) | Переименовывает кампанию (§3 «изменение»); имя ≤255, уникально в аккаунте | Нет | Да |
| `pause_ad_group` | `apply_pause_ad_group` (:188) | Пауза отдельной группы объявлений | Нет | Да |
| `resume_ad_group` | `apply_resume_ad_group` (:206) | Возобновление отдельной группы объявлений | Нет | Да |
| `set_geo_proximity` | `apply_set_geo_proximity` (:340) | Радиус-таргетинг вокруг адреса (remove-before-create) | Нет | Да |
| `set_geo_location` | `apply_set_geo_location` (:379) | Гео по стране/городу через geoTargetConstant (remove-before-create) | Нет | Да |
| `set_bidding_strategy` | `apply_set_bidding_strategy` (:850) | Смена стратегии ставок кампании | **Да** | Да |
| `attach_audience` | `apply_attach_audience` (:420) | Прикрепляет user_list/audience к кампании | Нет | В наборе, но без tool-схемы → минтует бот |
| `detach_audience` | `apply_detach_audience` (:473) | Снимает ранее прикреплённые аудитории с кампании | Нет | В наборе, но без tool-схемы → минтует бот |
| `create_rsa` | `apply_create_rsa` (:934) | Создаёт RSA-объявление в группе (PAUSED) | Нет | Да (минтуется ботом после курации) |
| `create_gdn_campaign` | `apply_create_gdn_campaign` (:2131) | Создаёт Display-кампанию из фото (всё PAUSED) | **Да** | Да (обычно через бот-визард) |
| `create_search_campaign` | `apply_create_search_campaign` (:1571) | Создаёт поисковую кампанию (всё PAUSED) | **Да** | Да (обычно через бот-визард) |
| `create_demand_gen_campaign` | `apply_create_demand_gen_campaign` (:2402) | Создаёт Demand Gen из YouTube-видео (всё PAUSED) | **Да** | Да (обычно через бот-визард) |
| `create_video_campaign` | `apply_create_video_campaign` (:2630) | Создаёт Video-кампанию (YouTube, всё PAUSED) | **Да** | Да (обычно через бот-визард) |
| `add_sitelinks` | `apply_add_sitelinks` (:583) | Добавляет sitelinks (campaign_asset SITELINK) | Нет | Да |
| `add_callouts` | `apply_add_callouts` (:605) | Добавляет callouts (уточнения) | Нет | Да |
| `add_structured_snippets` | `apply_add_structured_snippets` (:625) | Добавляет структурное описание (header+values) | Нет | Да |
| `attach_image_asset` | `apply_attach_image_asset` (:651) | Прикрепляет изображение-ассет к кампании | Нет | Нет (нужно фото → бот-визард) |
| `add_call_asset` | `apply_add_call_asset` (:730) | Телефон-расширение (CallAsset) | Нет | Да |
| `add_promotion` | `apply_add_promotion` (:756) | Промо-расширение (PromotionAsset) | Нет | Да |
| `add_price_asset` | `apply_add_price_asset` (:791) | Прайс-расширение (PriceAsset, 3–8 оферов) | Нет | Да |
| `remove_asset_link` | `apply_remove_asset_link` (:822) | Открепляет связь campaign_asset (не сам ассет) | Нет | Да |

**Примечание по «деньги?».** Денежными помечены операции, где внутри `apply_*`
стоит явная проверка `if not proposal.user_initiated: raise PermissionError`: это
`update_budget`, `update_bid`, `set_bidding_strategy` и все четыре `create_*_campaign`
(они несут бюджет). Остальные (статусы, переименование, ключи, минус-слова, гео,
аудитории, ассеты-расширения, `create_rsa`) деньгами не управляют → `user_initiated`
не требуют. Инвариант
`test_money_apply_functions_match_registry_and_guard_user_initiated`
(`tests/test_invariants_core.py`) держит этот список в синхроне с кодом.

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

### (a) Изменение кампании — реализовано ПЕРЕИМЕНОВАНИЕ, прочие настройки — нет

`apply_update_campaign` (`ads/mutations.py:161`, SDK-исполнитель
`_update_campaign_name_via_sdk` `:1014`) меняет **только имя** уже созданной кампании
(§3 «изменение»); имя уникально в аккаунте — `DUPLICATE_CAMPAIGN_NAME` перехватывается
понятной ошибкой. Прочие настройки существующей кампании (сети, расписание, даты,
URL-опции) **править нельзя** — они задаются только при создании
(`apply_create_search_campaign` `:1571`). Итого апдейты уровня campaign, которые код
умеет: имя (`update_campaign`), статус (`pause`/`resume_campaign`) и стратегия ставок
(`set_bidding_strategy`).

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
