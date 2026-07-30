# AUDIT: открытые источники, документация, библиотеки, MCP

> Аудит на 2026-07-24. Метки уверенности: **[Certain]** (сверено с первоисточником) ·
> **[Likely]** (сильный вывод из 2026-датированных источников) · **[Guessing]** (тонко, проверить).
> Часть рекомендаций «Node-стек» из первичного веб-ресёрча **отменена**: выбрана Python-эволюция +
> фреймворк hermes-agent, поэтому актуальны Python-аналоги. Отклонённые альтернативы оставлены как
> «что и почему не берём».

---

## 1. Hermes: фреймворк vs модель (снять главную путаницу)

**`NousResearch/hermes-agent` — агент-ФРЕЙМВОРК, не модель.** [Certain]
- Пин: release `v0.19.0`, git-тег `v2026.7.20` («The Quicksilver Release»). Тега `v0.19.0` в репо НЕТ — фетч по нему 404; фетчить по `ref` (`deploy/hermes/PIN.json`).
- Платформа **0.x**, ~660 PR/нед, релиз раз в 5–6 дней; неизвестные ключи конфига **игнорируются молча** (гард К10). Issue #54722: агент рапортует «успешно» при провалившихся tool-вызовах. → закладывать пины и `lint_config.py`.
- Запуск: CLI `hermes` + systemd `--user` сервис (`hermes gateway install/start/status`, `loginctl enable-linger`).
- Репозиторий/доки: https://github.com/NousResearch/hermes-agent
- **Хуки фреймворка fail-OPEN** («errors in any hook are caught and logged, never crashing the agent») → на них нельзя вешать HITL-вето; единственный рантайм-запрет инструмента — документированное вето `{"action":"block"}` или «не регистрировать инструмент вовсе».

**Модели Hermes (3/4) на OpenRouter — НЕ дают tool-use.** [Certain]
- Ваш A/B (`docs/ab-results.md`): `hermes-4-70b` и `hermes-4-405b` = **0/11** function-calling («No endpoints found that support tool use»); 405b слабее по русскому.
- Причина: Hermes обучен на формате `<tool_call>{…}</tool_call>`; конвертация в native `tool_calls` зависит от inference-провайдера, запускающего парсер (в vLLM — `--tool-call-parser hermes`). Через OpenRouter провайдеров с этим парсером для Hermes нет.
- **Единственный способ использовать МОДЕЛЬ Hermes с надёжным tool-use** — самохостинг на vLLM с `--tool-call-parser hermes` (требует GPU). По умолчанию не закладывается.
- Справка: https://docs.vllm.ai/en/stable/features/tool_calling/ · https://github.com/NousResearch/Hermes-Function-Calling

**Модель-мозг гейтвея — `openai/gpt-5.6-terra` через OpenRouter.** [Certain]
- `deploy/hermes/config.yaml:22-24`. Обязательно `provider_routing.require_parameters: true` — иначе OpenRouter молча рероутит на провайдера без `tools`, и агент «отвечает текстом» вместо вызова MCP.
- Победитель A/B для парсинга старого бота — `deepseek/deepseek-chat` (≈Claude, ~13× дешевле), fallback `anthropic/claude-sonnet-4.6` (`docs/ab-results.md`).

**OpenRouter API.** [Certain]
- Base URL `https://openrouter.ai/api/v1`, `/chat/completions`, `Authorization: Bearer <key>`; drop-in для OpenAI SDK. Usage Accounting → costs на каждый вызов (для трейсинга).
- Доки: https://openrouter.ai/docs/quickstart · https://openrouter.ai/docs/guides/features/tool-calling

---

## 2. Google Ads API

- **Версия/SDK:** проект на официальном **Python SDK `google-ads` 31.x → API v24** (пин `>=31.1,<32`). Сохраняем. [Certain] Актуальная линия API — v23.x (2026), ежемесячные релизы, сансеты быстрее; SDK-версия ≠ API-версия. Бампить ~раз в месяц (скил `gads-version`, `docs/gads-api-refs.md`).
- **Node/Opteo `google-ads-api` — НЕ берём** (у Google нет офиц. Node SDK): выбрана Python-эволюция, официальный SDK зрелее и уже интегрирован. [Certain]
- **Авторизация:** OAuth2 refresh token + developer token + login_customer_id; per-account токены шифруются Fernet at-rest (`core/secrets.py`, `scripts/get_refresh_token.py`, `scripts/register_account.py`). [Certain]
- **Готовый Google Ads MCP:** официальный `googleads/google-ads-mcp` — **READ-only** (`search` GAQL, `get_resource_metadata`, `list_accessible_customers`), Python, Apache-2.0. Мутаций нет. Ваш собственный READ-MCP (`mcp_server/`) уже реализует 15 READ-инструментов; для WRITE строим свои (нет зрелого готового write-MCP на актуальной версии). [Certain]
  - https://github.com/googleads/google-ads-mcp · community с мутациями (незрелые/отстают по версии): `grantweston/google-ads-mcp-complete` (v21), `cohnen/mcp-google-ads`, `samihalawa/google-ads-mcp-server` (Node).
- Доки: https://developers.google.com/google-ads/api/docs/release-notes · https://developers.google.com/google-ads/api/docs/query/overview

---

## 3. Telegram

- **Интерфейс — платформа фреймворка** `gateway.platforms.telegram` (webhook/поллинг у Hermes), НЕ отдельная библиотека. Три ортогональных гейта `allow_from`/`group_allow_from`/`group_allowed_chats` читаются с уровня блока (не из `extra`); `require_mention: true`, `guest_mode: false`. [Certain] (`deploy/hermes/config.yaml:238-268`, `lint_config.py`)
- **aiogram 3.x** старого бота — архивируется вместе с `bot/` (кнопочные визарды). grammY/Node не берём (Python-эволюция + пивот на фреймворк). [Certain]
- Подтверждение — reply-текстом; inline-кнопки визардов сняты по пивоту (Вопрос 2 «кнопки/слэш-команды» — открыт, нужна подпись; SPEC §2.6).

---

## 4. База данных и векторная память

- **PostgreSQL + pgvector 0.8.0**, образ `pgvector/pgvector:pg17` (замена `ankane/pgvector`). `CREATE EXTENSION vector`; тип `vector(N)`; индекс HNSW + `vector_cosine_ops`. Python: пакет `pgvector` + SQLAlchemy `Vector` (проект уже на SQLAlchemy 2.0). [Certain]
  - https://github.com/pgvector/pgvector · https://www.postgresql.org/about/news/pgvector-080-released-2952/
- **Qdrant — не нужен** на старте: объём RAG бизнес-правил < 1M векторов, нужна JOIN-фильтрация по tenant/customer_id, транзакционность с остальными данными → pgvector в том же Postgres. [Certain]
- **ORM для векторов:** остаёмся на **SQLAlchemy + `pgvector`-python** (не Drizzle/Prisma — они для Node/TS, отменены). [Certain]
- **Эмбеддинги:** `text-embedding-3-small` (1536 dims, $0.02/1M) через OpenRouter (единый биллинг) — дефолт; `text-embedding-3-large` (3072, $0.13/1M) — максимум качества; локальные `bge/e5/nomic` при цене/приватности. Размерность зафиксировать ДО создания `vector(N)` (смена = реэмбеддинг + пересборка индекса). [Certain]
  - https://openrouter.ai/collections/embedding-models
- **Внимание:** фреймворк hermes-agent имеет собственные `memory`/`context_engine` — прежде чем строить свой pgvector-RAG, проверить, покрывает ли встроенная память RAG бизнес-правил (иначе дублирование). [Likely]

---

## 5. Observability

| | Langfuse | Helicone |
|---|---|---|
| Подключение к OpenRouter | SDK-обёртка OpenAI / `@observe()` | Прокси (смена base URL) |
| Free tier | ~50k units/мес, 30 дн | ~10k req/мес, 7 дн |
| Self-host | ядро **MIT** (ClickHouse+Postgres) | OSS AI Gateway |
| Что видно | токены/итерацию, латентность, стоимость, вложенные трейсы шагов | то же |

- **Рекомендация:** Langfuse (детальные трейсы, self-host MIT) как основной; Helicone-прокси — быстрый старт. Включить OpenRouter **Usage Accounting** для costs. [Likely]
- **Нюанс архитектуры:** LLM-вызовы делает gateway напрямую (минуя наш код) → часть трейса живёт на стороне OpenRouter/Hermes-лога, а не только в нашем `core/usage.py`.
- https://langfuse.com/integrations/gateways/openrouter · https://docs.helicone.ai/getting-started/integration-method/openrouter

---

## 6. MCP-серверы для разработки

| MCP | Назначение | Установка |
|---|---|---|
| **context7** | Свежие docs библиотек (hermes-agent, OpenRouter SDK, pgvector, google-ads) | `claude mcp add context7 -- npx -y @upstash/context7-mcp@latest` |
| **postgres (Pro)** | Работа с БД при разработке: health, index-tuning, планы. Restricted-режим | `crystaldba/postgres-mcp` (см. README) |
| **filesystem** | Файловые операции с контролем доступа | `@modelcontextprotocol/server-filesystem <dir>` |
| **git** | Чтение/поиск/операции в репозитории | `@modelcontextprotocol/server-git` |
| **fetch** | Веб-контент → markdown | `@modelcontextprotocol/server-fetch` |
| **sequential-thinking** | Пошаговая декомпозиция | `@modelcontextprotocol/server-sequential-thinking` |

- **НЕ использовать** официальный `@modelcontextprotocol/server-postgres` — **архивирован (07.2025)** из-за SQL-injection (semicolon-delimited обход read-only). [Likely]
- Готовая конфигурация — `.mcp.json` в корне проекта.
- https://github.com/modelcontextprotocol/servers · https://github.com/crystaldba/postgres-mcp · https://www.npmjs.com/package/@upstash/context7-mcp

---

## 7. Docker / Compose (2026)

- Multi-stage build (builder → тонкий рантайм), `USER node`/non-root, named volume для Postgres.
- Healthcheck обязателен: `depends_on: { db: { condition: service_healthy } }` (иначе ждёт только старт контейнера, не готовность Postgres). Пример — `pg_isready`. [Certain]
- Для pgvector в проде: SSD-том, тюнинг `shared_buffers`, `ef_search` под HNSW.

---

## Итоговые решения стека
| Слой | Выбор | Уверенность |
|---|---|---|
| Агент-мозг | фреймворк hermes-agent v0.19.0 + модель `openai/gpt-5.6-terra` | [Certain] |
| Google Ads | офиц. Python SDK `google-ads` v24; WRITE-MCP свой | [Certain] |
| Telegram | платформа фреймворка (`gateway.platforms.telegram`) | [Certain] |
| Vector store | pgvector 0.8.0 (`pgvector/pgvector:pg17`), SQLAlchemy `Vector` — если не покрыто фреймворком | [Likely] |
| Эмбеддинги | `text-embedding-3-small` (1536) через OpenRouter | [Certain] |
| Observability | Langfuse (self-host MIT) + Usage Accounting | [Likely] |
| MCP dev | context7 + crystaldba/postgres-mcp + filesystem/git/fetch/sequential-thinking | [Certain] |

## Перепроверить перед фиксацией
1. Версия hermes-agent на живом VPS (`PIN.json host_matches: null` — замер V1 не снят).
2. Покрывает ли встроенная память фреймворка RAG бизнес-правил (иначе строим pgvector-RAG).
3. Точные аргументы `postgres-mcp` (restricted) по README.
4. Замеры V1–V22 (`OPERATIONS.md §12`) — fail-open/fail-closed хуков ещё не сняты.
