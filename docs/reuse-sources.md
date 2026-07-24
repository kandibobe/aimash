# Что переиспользуем из готового (знание, не бэкенд)

Принцип: берём **знание/инструкции** (после ревью на инъекции/секреты), а **код записи + confirm-гейт + audit пишем сами**. Готовые write-MCP небезопасны как бэкенд (см. план-файл, «Оценка готовых скилов/MCP»).

> **Область этого файла — провенанс порогов `/audit` (ниже) и источники-паттерны.** Сводный аудит
> открытых источников/библиотек/**dev-MCP** живёт в `AUDIT-open-source.md`, а
> карта «что даёт фреймворк / что переиспользуем / что строим» — в `REUSE-MAP.md` там же. При расхождении
> по dev-MCP и стеку прав `AUDIT-open-source.md` (см. пометку в разделе «MCP-серверы для разработки»).

## AgriciDaniel/claude-ads (MIT, 6.5k★, активный)
Audit/analysis-скил для Claude Code. **Без записи, без GAQL** (делегирует MCP). Security low-risk (статический markdown, без секретов).
**Брать (как контент для system-prompt / reference):**
- **Каталог аудит-проверок (209 ID)** — для health-score аккаунта и логики аномалий (фаза 3 алерты).
- **Scoring** — взвешенные severity-множители для приоритизации находок.
- **Правила аномалий** — «3x Kill Rule» (CPA > 3× target), пороги достаточности бюджета, защита learning-phase → прямо в Scheduler/anomaly (фаза 3).
- **Копирайт** — brand-DNA / copy-brief шаблоны → в генерацию текстов (фаза 2).
- **Дисциплина ключей/минус-слов** — предупреждения broad-match, привязка к Smart Bidding.
**Не брать:** Claude-специфичный оркестратор-слой, MCP-интеграцию, creative-pipeline с внешними зависимостями (Playwright/reportlab/image API) — проверять отдельно.

### Портировано в детерминированное ядро `/audit` (2026-07-09, экспертное расширение)
Решение владельца: приватное использование → пороги/эвристики перенесены в код `audit/engine.py` +
`reports/queries.py` (числа считает КОД, проходят fact-guard, всё read-only, one-tap НЕ расширен).
Источники порогов: **claude-ads (MIT, G##-проверки)** + **TheMattBerman/google-ads-copilot (MIT)** +
чек-лист North Country + **Hassanelsisi/google-ads-audit-script (без лицензии → только идея/GAQL-факты)**.

| Новая проверка / фича | Порог (источник) |
|---|---|
| `is_lost_revenue` (IS→упущенная выручка, score_intensity, at_risk=0) | budget-lost >10% / rank >20% (Hassanelsisi/cohnen) |
| `adgroup_bloat` / `rsa_thin` / `no_negative_list` (структура) | >20 kw/группа (G03), <2 RSA/группа (copilot), 0 минус-листов (G14) |
| `qs_low` + `qs_ctr_below`/`qs_relevance_below`/`qs_landing_below` (QS 3 компонента) | QS ≤4 (G20), доля «ниже среднего» >35% (G22-24) |
| `manual_bid_high_vol` (ручная стратегия при объёме) | >30 конв/период (G40) |
| `geo_no_conv` (денежная) + `schedule_waste` (heatmap час×день) | регион $≥порог/0 конв (North Country), Hassanelsisi heatmap |
| аудит за произвольную дату (`/audit июнь 2025`, ISO-диапазон) | — (переиспользован `reports.period`) |

**GAQL-фетчеры новых срезов** (`geographic_view`+location_type / `keyword_view` quality_info / `shared_set`
/ структура групп / `segments.day_of_week×hour`) обёрнуты в `_safe` — кривое поле v24 деградирует в
data_gap, не крашит; точные поля сверить на TEST MCC (live-смоук). ads-mcp (amekala) НЕ взят: write-MCP
без confirm-гейта (архитектурная причина, не лицензионная).

### Ф0 (2026-07-13): «accuracy notes» claude-ads — анти-ложноположительные правила
В `46e48b5` взяли ПОРОГИ из таблиц каталога, но **не приписки под ними** — а они как раз про то, чего
НЕ флажить. Три чека врали. Эпоха скор-модели бампнута 2 → 3 (семантику тел проверок хэш не видит).

| Чек | Было (ложный позитив) | Стало |
|---|---|---|
| `broad_unmanaged` (G17) | Флажили BROAD + Manual CPC | Legacy BMM (текст с «+») не флажим; **добавлена** ветка «BROAD под Smart Bidding без единого минус-слова» |
| `no_negative_list` (G14→**G15**) | Смотрели только `shared_set` → аккаунт с минусами на кампании получал ложное «гигиены нет» | Минусы прямо на кампании (`campaign_criterion.negative`) считаются наравне |
| `adgroup_bloat` (G03) | Считали ВСЕ ключи подряд | Только ключи **с показами** за период (`keyword_view`) + **дедуп по тексту** (BROAD+PHRASE = 1) |

⚠️ **Приписку G17 взяли НЕ целиком — её посылка неверна.** claude-ads пишет: «Google срезал „+“ у BMM
при миграции 2021, BMM неотличим от BROAD ⇒ не флажить весь BROAD+Manual CPC». На деле **«+» в тексте
ключа сохранился** — BMM отличим точно. Слепо следуя приписке, мы бы молча погасили реальный слив;
вместо этого отсекаем BMM по тексту (`audit.engine._is_legacy_bmm`), а настоящий BROAD без Smart
Bidding продолжаем флажить. Мораль: приписки каталога — тоже источник, а не истина; сверять факт.

Порог `kw_min_spend` (G16 велит «>$10») **намеренно оставлен 3.0**: наш порог — в ВАЛЮТЕ АККАУНТА
(у заказчика UGX), фиксированные $10 туда не переносятся; поднять = молча погасить слив там, где клик
дешёвый. Это настройка (переопределяема per-chat), а не ложное срабатывание.

### Ф1–Ф8 (2026-07-13/14): волна «аудит → инструмент оптимизации» — новые пороги и что РЕЖЕТСЯ

Пороги — в `audit/thresholds.py` (валюта аккаунта, переопределяемы per-chat). Источник каждого — ниже;
где каталог claude-ads расходится с фактом v24, прав **факт** (проверено запуском python по прото SDK).

| Фаза | Проверки (check_id) | Порог / источник |
|---|---|---|
| Ф1 ставки | `bid_below_first_page`, `bid_below_top_of_page`, `top_is_rank_lost`, `sim_bid_upside`, `sim_budget_upside` | `bid_gap_min` 0.10, `kw_rank_lost_top_min` 0.30, `sim_min_conv_gain` 0.5 — свои (шум оценки Google); данные: `ad_group_criterion.position_estimates`, `*_simulation` |
| Ф2 Google | `google_recommendations_pending` | без порога; `recommendation.impact.potential−base` (докстринг «impact недоступен в v24» был НЕВЕРЕН) |
| Ф3 тексты | `rsa_keyword_coverage_low`, `rsa_headlines_thin`, `rsa_descriptions_thin`, `rsa_ad_strength_poor`, `rsa_overpinned`, `rsa_stale` | покрытие = `adcopy.validate.MIN_KEYWORD_COVERAGE` (ЕДИНОЕ с генерацией), <8 заголовков (G27), <3 описаний (G28), 90 дней (G-AD1) |
| Ф4 ключи | `keyword_harvest`, `ngram_waste`, `keyword_cannibalization`, `zero_impression_keywords` | `harvest_min_conv` 1.0, `ngram_min_cost` 10 + `ngram_min_terms` 2 (система, а не случай), `zero_impr_share` 0.5 при ≥20 ключах (G-KW1) |
| Ф5 конкуренты | `competitive_pressure` + `/competitors` (CSV) | `comp_min_impressions` 500 / `comp_is_max` 0.60 / `comp_rank_min` 0.20. **Auction Insights в API за закрытым вайтлистом Google** (набор закрыт) — только импорт CSV |
| Ф6 деньги | `target_cpa_too_low` (G37), `brand_nonbrand_mixed` (G05), `assets_*` (G50-52), `display_on_search_campaign` (G12), `geo_interest_waste` (G11) | `tcpa_gap_factor` 2.0, `brand_mix_min_nonbrand` 3, `assets_min_sitelinks/callouts` 4, `content_on_search_min_spend` 5, `geo_interest_min_spend` 20 |
| Ф7 PMax | `pmax_search_term_waste`, `pmax_asset_group_no_conv`, `pmax_brand_cannibalization`, `pmax_ad_strength_poor`, `pmax_no_video`, `pmax_no_signals`, `pmax_no_negatives`, `pmax_insufficient_conversions` | `pmax_min_spend` 20, `pmax_brand_conv_share` 0.30 (G-PM3), `pmax_learn_min_conv` 30 / `pmax_min_days` 14 (G-PM7) |

**Срезано по фактам v24** (каталог обещал, API не даёт):
- **G34 `pmax_url_expansion`** — поля `campaign.url_expansion_opt_out` в v24 **нет** (url-поля Campaign:
  только `tracking_url_template`, `url_custom_parameters`, `final_url_suffix`). Чек невозможен.
- **G31 `pmax_asset_density`** (≥20 картинок / ≥5 лого / ≥5 видео) — это **слотовые максимумы Google**, а не
  порог качества. Вместо самодельной «плотности» читаем вердикт самого Google: `asset_group.ad_strength` +
  `asset_coverage.ad_strength_action_items` («добавьте 1 видео») — он же и рецепт.
- **G07** (brand exclusions при живой брендовой Search-кампании) — через asset-set недоказуем (`AssetSetTypeEnum`
  не содержит `BRAND_LIST`); свёрнут в факт `brand_excluded` внутри `pmax_brand_cannibalization` (читаем
  `SharedSet(BRANDS)` → `campaign_shared_set`).

**Модель скора (Ф8).** Уровень `critical` (×1.5) — ровно три чека: `no_conversion_tracking`,
`zero_conversions` (без измерения все прочие числа — гадание), `kill_rule` (втрое дороже своей цели).
`FAMILY_WEIGHT` (Σ=100): ожили `assets` 3 и `pmax` 3, доноры waste 30→28, conversion_tracking 20→18,
budget 10→9, geo 8→7. `competition`/`recommendations` осознанно вне вектора (вес 0) — диагноз и мнение
Google, а не дефект аккаунта. `SCORE_MODEL_EPOCH` **не бампнут**: всё, что изменила Ф8, хэш версии видит сам.

## itallstartedwithaidea/agent-skills (MIT)
73 скила-инструкции (SKILL.md) — keyword research, копирайт, бид, PMax/Shopping, аудитории. **Брать как текст** в `agent/system_prompt.py` после ревью (инъекции/хардкод-ключи). Без гард-рейлов — безопасность своя.

## cohnen/mcp-google-ads (read-only)
**Референс GAQL-запросов** (list_accounts, get_campaign_performance и т.п.) для `ads/read.py`. Не бэкенд записи.

## Куда подключать (по фазам)
- Фаза 2 (тексты): copy-шаблоны из claude-ads + agent-skills.
- Фаза 3 (scheduler/алерты): правила аномалий + аудит-проверки из claude-ads.
- Фаза 0/1 (чтение): GAQL-паттерны из cohnen.

---

## MCP-серверы для разработки → перенесено в `AUDIT-open-source.md §6`

**Актуальный набор dev-MCP и его обоснование — `AUDIT-open-source.md §6`**
(и готовый `../.mcp.json` в корне репозитория): context7 + `crystaldba/postgres-mcp` (restricted) +
filesystem + git + fetch + sequential-thinking.

> ⚠️ Прежний вердикт этого файла «filesystem/fetch **НЕ ставим** (встроены в Claude Code)» **снят**: он
> относился к разработке старого слоя внутри Claude Code, где эти тулы дублируются встроенными. Для
> проекта-пивота источник истины по dev-MCP — `AUDIT §6`, там filesystem/git/fetch включены.

Что из прежних заметок остаётся верным (и уже отражено в `AUDIT §6/§2`): официальный
`@modelcontextprotocol/server-postgres` не берём (архивирован, SQL-инъекция в обход read-only → заменён
на `crystaldba/postgres-mcp`); `google-marketing-solutions/google_ads_mcp` не берём как write-бэкенд
(архивирован, write без confirm-гейта/audit → прямо против золотых правил).

## Дополнительные источники (паттерны, не бэкенд)
- **google-ads-python `generate_user_credentials.py`** — паттерн refresh-токена (сверить с `scripts/get_refresh_token.py`).
- **aiogram**: `CallbackData` factory + `InlineKeyboardBuilder` — типизировать confirm-callback, чтобы `confirmation_id` сверялся с `audit_log` (не доверять кнопке). `AsyncIOScheduler`-в-loop, но scheduler НЕ может звать `mutations` (golden rule №3). `aiogram_dialog` — только если confirm станет многошаговым.
- **OpenRouter tool-loop**: `finish_reason=="tool_calls"` → локальное исполнение → `role:"tool"` → ресабмит (для `agent/loop.py`). Strict-схемы варьируются по моделям → всегда ревалидировать Pydantic-ом.
- **`FGRibreau/mcp-google-ads`** (REFERENCE, не бэкенд): хороший чек-лист гард-рейлов (dry-run, preview-before-execute, PAUSED-by-default, budget/bid caps, double-confirm) → перенести в `confirm/` и скил `new-mutation`.
- **AVOID на денежном пути:** gomarble SaaS (третья сторона держит доступ к деньгам), любые «автономные» агенты. confirm-гейт/`confirmation_id`/allow-list/audit — 100% наш код.
- **pydantic `SecretStr`** (внедрено в `core/config.py`) + Fernet (`core/secrets.py`); **gitleaks** pre-commit + CI.
