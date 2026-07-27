# Aimash KANBAN — AI-Driven Development Board

> **Принцип:** каждый ИИ-агент (Claude Code / Cline / Hermes) может читать этот файл, брать задачу из `📝 To Do`, выполнять её и двигать карточку в `✅ Done`. Никаких внешних трекеров — доска живёт рядом с кодом.

---

## 🧠 Идеи / Backlog

- Интеграция TikTok Ads: MCP-сервер `tiktok_mcp` (worker skill уже заготовлен)
- Интеграция Meta Ads: MCP-сервер `meta_mcp` (worker skill уже заготовлен)
- Web-дашборд (React) для операторов: real-time метрики + audit-trail
- Мобильное приложение (PWA) для подтверждения операций on-the-go
- Langfuse evaluation pipeline: автоматический scoring ответов агента
- **Multi-Agent Debate (внутренний аудит):** `Finance Auditor Agent` — второй LLM со своей температурой и жёстким промптом на экономию. Перед отправкой proposal в `#approvals` план летит к аудитору, который проверяет лимиты и возвращает на доработку либо пропускает.

---

## 📝 To Do (Ready for AI)

### 🔴 High Priority — WRITE-MCP (денежный контур)

- [ ] **[WR-01]** `propose_budget_change` — MCP-инструмент: Proposal по изменению дневного бюджета кампании
  - ✅ Инструмент существует: `propose_update_budget` в `mcp_server/tools_writes.py`
  - ✅ Интегрирован с PolicyEngine (Δ≤20% проверка ДО создания proposal)
  - ✅ Интегрирован с Auditor (risk assessment)
  - ✅ PolicyEngine ленивый импорт (fail-open если модуль недоступен)
  - Гарды: account access (ensure_allowed), campaign exists (build_proposal), валюта совпадает (build_proposal)

- [ ] **[WR-02]** `execute_approved_action` — MCP-инструмент: исполнение подтверждённого proposal
  - ✅ Инструмент существует: `execute_confirmed` в `mcp_server/tools_writes.py`
  - ✅ Claim через `ConfirmStore.claim()` (CAS, одноразово)
  - ✅ Вызов `ads/service.py` → `execute_confirmed`
  - Аудит через `AuditLog` (статусы: executing → applied / failed / needs_review) — встроен в ConfirmStore

- [ ] **[WR-03]** `propose_campaign_pause` / `propose_campaign_resume` — пауза/возобновление
  - ✅ Инструменты существуют: `propose_pause_campaign`, `propose_resume_campaign`
  - ✅ PolicyEngine: `_check_pause_policy` проверка конверсий
  - Гард: пауза кампании с конверсиями → предупреждение (лог)

- [ ] **[WR-04]** `propose_bid_adjustment` — изменение ставки (группы объявлений)
  - ✅ Инструмент существует: `propose_update_bid`
  - ✅ PolicyEngine: `_check_bid_policy` проверка Δ
  - Поддержка increase_by_percent / decrease_by_percent / set_to

- [ ] **[WR-05]** `propose_add_keywords` / `propose_add_negatives` — ключи/минус-слова
  - ✅ Инструменты существуют: `propose_add_keywords`, `propose_add_negative_keywords` и др.
  - ✅ Валидация match type (Broad/Phrase/Exact) — в schemas.py
  - Минус-слова: уровень кампании / ad_group / shared list — все поддерживаются
  - **Инвариант:** не более 50 ключей за раз (ADD_KEYWORDS_MAX в schemas.py)

### 🟡 Medium Priority — Policy & Safety

- [ ] **[PL-01]** `PolicyEngine` — middleware проверки Δ ≤ 20% за шаг
  - ✅ Файл: `core/budget_policy.py` (адаптирован из `/root/ad-master/src/lib/`)
  - ✅ Интегрирован с `core.auditor` (check_with_auditor)
  - ✅ DRY_RUN=true по умолчанию
  - ✅ Cooldown 24h, PAUSE_WARN_CONVERSIONS
  - 8/8 тестов пройдено

- [ ] **[PL-02]** **State Recovery (Checkpoints):** чекпоинты ReAct-цикла в БД
  - Каждый шаг `handle_command` → сохранять состояние в `agent_state` таблицу
  - При restart сервера → восстановление с последнего чекпоинта
  - **Цель:** API OpenRouter 502 / обрыв сети не сжигает потраченные токены Reasoning

- [ ] **[PL-03]** **Token Refresh Isolation:** изолированный модуль обновления OAuth-токенов
  - Агент при `AuthError` → пауза задачи (status: `paused_auth`), НЕ краш
  - Триггер `refresh_oauth_token` → фоновая джоба обновления
  - По готовности → возобновление задачи с того же места
  - Файл: `ads/token_refresh.py`

### 🟢 Nice to Have — Quality & Testing

- [ ] **[EV-01]** **Golden Dataset:** ≥20 сценариев для eval
  - Файл: `tests/evals/golden_scenarios.json`
  - Покрытие: budget change, pause/resume, bid adjust, keyword add, negatives
  - Edge-cases: невалидный аккаунт, просроченный TTL, double-spend confirmation

- [ ] **[EV-02]** **Shadow Mode (теневое тестирование):** симуляция на исторических данных
  - Cron-джоба: раз в день скармливает агенту срез вчерашних данных
  - Агент генерирует решения (не выполняя их)
  - Сравнение: помогло бы решение реальным метрикам или убило бы кампанию?
  - Файл: `scheduler/shadow_mode.py`

- [ ] **[CT-01]** **Сжатие контекста API-ответов:** санитайзер JSON перед отправкой в LLM
  - Удалять `null` поля, служебные ссылки, `resource_name`
  - Оставлять только `campaign_id`, `name`, `status`, `metrics`
  - Файл: `mcp_server/redact.py` — расширить `compact_api_response()`
  - **Инвариант:** ответ >50K строк → авто-саммари перед инъекцией в контекст

- [ ] **[CT-02]** **Context Summarization (Telegram):** авто-саммари старых сообщений
  - Cron каждые 6 часов: фоновый LLM-вызов → саммари топика
  - Саммари сохраняется в `memory(add)` с тегом `[summary:YYYY-MM-DD]`
  - В контекст агента: текущие N сообщений + последние 3 саммари + memory-правила
  - Старые саммари (>30 дней) авто-удаление
  - Файл: `core/context_summarizer.py` + cron

- [ ] **[DB-01]** **Multi-Agent Debate (лёгкий вариант):** второй LLM-промпт-аудитор
  - Не отдельный агент, а вызов внутри того же Hermes перед `execute_approved_action`
  - Промпт: «Ты финансовый аудитор. Проверь план на риски: {proposal_json}»
  - Возвращает `risk: low | medium | high` + обоснование
  - `high` → план идёт в `#approvals-and-audits` к человеку
  - `low/medium` → автоматическое исполнение (если DRY_RUN=false)
  - Файл: `core/auditor.py`

### 📐 Tech Debt / Инфраструктура

- [ ] **[DX-01]** Подключить `pgvector` для RAG бизнес-правил (вместо in-memory `ad-master-memory`)
  - Миграция: `migrations/versions/0032_pgvector_memory.py`
  - API: `memory/semantic_search.py`

- [ ] **[DX-02]** `budget_policy.py` — перенести из `/root/ad-master/src/lib/` в `/opt/aimash/core/`
  - Сейчас навык ссылается, а файла в проекте нет
  - Интегрировать в WRITE-MCP как middleware

---

## ⚙️ In Progress (Agent)

- **[PL-02]** State Recovery — чекпоинты ReAct-цикла (P2)
- **[PL-03]** Token Refresh Isolation — изолированный OAuth-модуль (P2)

---

## 🕵️‍♂️ Code Review (Human)

- *Проверить перед слиянием в `main`:*

---

## 🧪 Evals / Тестирование

- *Прогон через golden dataset после каждого PR*

---

## ✅ Done (Production)

- ✅ **MCP READ:** 12 инструментов (`mcp_server/tools_read.py`) — полностью работают
- ✅ **Confirm-гейт:** CAS claim, TTL в CAS, провенанс (2 бита), audit-журнал (`confirm/store.py`)
- ✅ **Agent loop:** handle_command, ReAct-цикл, C2/C3 (placeholders/контекст), A5 (одно действие) (`agent/loop.py`)
- ✅ **Multi-model routing:** 7 ролей (parsing, copy, keywords, clustering, analyst, extract, dossier) + fallback (`agent/router.py`)
- ✅ **Resilience:** таймауты, ретраи, семафор Google Ads конкурентности, квота (`core/resilience.py`)
- ✅ **Secrets:** Fernet at-rest шифрование OAuth-токенов (`core/secrets.py`)
- ✅ **Langfuse tracing:** автоматический захват model/tokens/cost + traces + sessions (`core/langfuse_tracing.py`)
- ✅ **Scheduler:** плановые отчёты, детектор аномалий, очистка черновиков, TTL-антиспам (`scheduler/jobs.py`)
- ✅ **AdCopy:** RSA-генерация, валидация, рефайн, session-state, display_path (`adcopy/`)
- ✅ **Keywords:** кластеризация, фильтрация, Keyword Planner, экспорт CSV (`keywords/`)
- ✅ **Clients:** Dossier/RAG-профили, краул сайтов, извлечение фактов (`clients/`)
- ✅ **Campaign Wizard:** multi-step создание кампаний Search/GDN/Video/DG (`bot/campaign_wizard/`)
- ✅ **Reports:** xlsx/docx экспорт с формулами и стилями (`reports/`)
- ✅ **Audit engine:** scoring, пороги, находки, bidscape, fact-guard (`audit/`)
- ✅ **DB:** 31 миграция, 14 моделей, read-only роль для безопасности (`migrations/`)
- ✅ **Docker Compose:** 4 контейнера (bot, pg, scheduler, backup) (`docker-compose.yml`)
- ✅ **CI:** GitHub Actions, ruff 0.14.10, mypy, pytest-cov (`ci.yml`)
- ✅ **Hermes skills:** ad-master-agent, google-ads-worker, meta-ads-worker, tiktok-ads-worker, ad-master-memory
- ✅ **Cron jobs:** Daily Audit (09:00 MSK), Hourly CPA/Budget Watch, Shadow Mode (00:00 MSK), Context Summarization (6h)
- ✅ **2156 тестов** с покрытием mutation/read/confirm/agent/bot/scheduler
- ✅ **Next-Level Phase 1 (2026-07-25):**
  - `mcp_server/compact.py` — сжатие API-ответов (обрезка null/строк/rows, интеграция в envelope.ok)
  - `core/context_summarizer.py` — саммаризатор Telegram-тредов (скользящее окно + эвристики)
  - `core/auditor.py` — финансовый аудитор (heuristic_audit: low/medium/high)
  - `core/budget_policy.py` — PolicyEngine (Δ≤20%, cooldown, PAUSE_WARN, интеграция с auditor)
  - `scheduler/shadow_mode.py` — теневое тестирование на исторических данных
  - 4 test files (+1500 LOC тестов), 31/31 проверок пройдено
  - 2 новых cron-джобы: Shadow Mode (031080f7bfac), Context Summarization (a0cff93f3a2b)

---

### Легенда статусов

| Метка | Статус |
|---|---|
| 🔴 High Priority | Критично для MVP: без этого нельзя доверять агенту деньги |
| 🟡 Medium Priority | Защита и надёжность: без этого — работает, но риски высоки |
| 🟢 Nice to Have | Качество: без этого — продукт есть, но не enterprise-grade |
| 📐 Tech Debt | Инфраструктура: накопленный долг, который замедляет разработку |