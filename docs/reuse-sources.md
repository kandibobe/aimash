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

## itallstartedwithaidea/agent-skills (MIT)
73 скила-инструкции (SKILL.md) — keyword research, копирайт, бид, PMax/Shopping, аудитории. **Брать как текст** в `agent/system_prompt.py` после ревью (инъекции/хардкод-ключи). Без гард-рейлов — безопасность своя.

## cohnen/mcp-google-ads (read-only)
**Референс GAQL-запросов** (list_accounts, get_campaign_performance и т.п.) для `ads/read.py`. Не бэкенд записи.

## Куда подключать (по фазам)
- Фаза 2 (тексты): copy-шаблоны из claude-ads + agent-skills.
- Фаза 3 (scheduler/алерты): правила аномалий + аудит-проверки из claude-ads.
- Фаза 0/1 (чтение): GAQL-паттерны из cohnen.
