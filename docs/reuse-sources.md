# Что переиспользуем из готового (знание, не бэкенд)

Принцип: берём **знание/инструкции** (после ревью на инъекции/секреты), а **код записи + confirm-гейт + audit пишем сами**. Готовые write-MCP небезопасны как бэкенд (см. план-файл, «Оценка готовых скилов/MCP»).

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

## itallstartedwithaidea/agent-skills (MIT)
73 скила-инструкции (SKILL.md) — keyword research, копирайт, бид, PMax/Shopping, аудитории. **Брать как текст** в `agent/system_prompt.py` после ревью (инъекции/хардкод-ключи). Без гард-рейлов — безопасность своя.

## cohnen/mcp-google-ads (read-only)
**Референс GAQL-запросов** (list_accounts, get_campaign_performance и т.п.) для `ads/read.py`. Не бэкенд записи.

## Куда подключать (по фазам)
- Фаза 2 (тексты): copy-шаблоны из claude-ads + agent-skills.
- Фаза 3 (scheduler/алерты): правила аномалий + аудит-проверки из claude-ads.
- Фаза 0/1 (чтение): GAQL-паттерны из cohnen.

---

## MCP-серверы для разработки (ресёрч 2026-06-25)

**Ставим (`.mcp.json`):**
- **Postgres** — `crystaldba/postgres-mcp --access-mode=restricted` (read-only dev-БД). + отдельная роль `aimash_ro` (SELECT-only) как defense-in-depth.
- **Google Ads (read-only)** — `cohnen/mcp-google-ads` (выбор) либо официальный `googleads/google-ads-mcp` (pipx, без клона). Только TEST-аккаунт `7753643025`. **Build-time помощник, НЕ бэкенд** — запись всегда через свой `ads/mutations.py`.
- **Context7** — `@upstash/context7-mcp` (свежие доки google-ads/aiogram).
- **GitHub** — официальный remote `https://api.githubcopilot.com/mcp`.

**НЕ ставим (важно):**
- `@modelcontextprotocol/server-postgres` — **архивирован + известная SQL-инъекция** в обход read-only. Заменён на `crystaldba/postgres-mcp`.
- `google-marketing-solutions/google_ads_mcp` — **архивирован (2026-06-25), write без confirm-гейта/audit** → прямо против golden rules. Никогда как write-бэкенд.
- filesystem MCP (нативные файл-тулы), Telegram MCP (сайд-эффекты вне confirm-гейта), fetch/websearch (встроены в Claude Code).

## Дополнительные источники (паттерны, не бэкенд)
- **google-ads-python `generate_user_credentials.py`** — паттерн refresh-токена (сверить с `scripts/get_refresh_token.py`).
- **aiogram**: `CallbackData` factory + `InlineKeyboardBuilder` — типизировать confirm-callback, чтобы `confirmation_id` сверялся с `audit_log` (не доверять кнопке). `AsyncIOScheduler`-в-loop, но scheduler НЕ может звать `mutations` (golden rule №3). `aiogram_dialog` — только если confirm станет многошаговым.
- **OpenRouter tool-loop**: `finish_reason=="tool_calls"` → локальное исполнение → `role:"tool"` → ресабмит (для `agent/loop.py`). Strict-схемы варьируются по моделям → всегда ревалидировать Pydantic-ом.
- **`FGRibreau/mcp-google-ads`** (REFERENCE, не бэкенд): хороший чек-лист гард-рейлов (dry-run, preview-before-execute, PAUSED-by-default, budget/bid caps, double-confirm) → перенести в `confirm/` и скил `new-mutation`.
- **AVOID на денежном пути:** gomarble SaaS (третья сторона держит доступ к деньгам), любые «автономные» агенты. confirm-гейт/`confirmation_id`/allow-list/audit — 100% наш код.
- **pydantic `SecretStr`** (внедрено в `core/config.py`) + Fernet (`core/secrets.py`); **gitleaks** pre-commit + CI.
