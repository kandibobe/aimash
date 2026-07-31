# REUSE-MAP: что даёт фреймворк / что переиспользуем / что строим

> Карта привязки требований к реальным модулям проекта `ai mash tg bot`. Три источника
> реализации: **[Ф] фреймворк** `NousResearch/hermes-agent` даёт из коробки · **[R] переиспользуем**
> существующий Python-код (пути указаны) · **[B] строим** заново. Пути — от корня `ai mash tg bot`.

## Ключевой принцип разреза (SPEC §0.6, AGENTIC_VS_TZ §4)
«Автономия применяется к ПОНИМАНИЮ, ПЛАНИРОВАНИЮ и НАПИСАНИЮ СКИЛОВ. Confirm-гейт применяется
к ИСПОЛНЕНИЮ. Скил может влиять на то, ЧТО агент предложит. Скил не может влиять на то, МОЖНО ЛИ
это выполнить.» Граница «модель/код» проходит по типизированному вызову (MCP-инструменту).

---

## Что НЕ трогается (лучшая часть кодовой базы — HERMES_SPEC §Прочтение A)
`ads/mutations.py` · `ads/client.py` · `confirm/**` · `core/secrets.py` — денежное ядро, отлаженное и покрытое инвариант-тестами. Переносится как есть.

## Что архивируется (пивот, SPEC §5.2)
`bot/` целиком (~151 хендлер, клавиатуры, визарды, i18n 3139 строк), детерминированная маршрутизация aiogram, слэш-команды, inline-кнопки визардов. **Тянет за собой 110 из 214 тестовых файлов.**
Из `agent/` архивируется **только** свой агент-цикл: `loop.py` (в нём же `SYSTEM`-промпт), `campaign_edit.py`, `campaign_settings.py`, `openrouter_account.py`. **НЕ архивируются** `agent/router.py` и `agent/tools/schemas.py` — они bot-free/agent-free и переиспользуются (см. карту фаз ниже), при архивации `agent/` **переезжают** в bot-free пакет. Файлов `agent/system_prompt.py` и `agent/tools.py` **не существует** (в `agent/tools/` только `schemas.py`).

> Это целевая судьба каталогов, не разрешение удалить их сейчас. Волна 1 шаг 1 уже выполнена,
> но архивация допускается только после bot-free runtime MCP, приёмки gateway/READ и WRITE/reply,
> удаления тестовых импортов `bot.main`, тега `pre-hermes` и прохождения runbook из
> `deploy/hermes/OPERATIONS.md`.

---

## Карта по 7 фазам

### Фаза 1 — Инфраструктура / телеметрия / retry / БД
| Требование | Источник | Где |
|---|---|---|
| Агент-цикл, диспетчеризация, ретраи модели, трейс шагов | **[Ф]** | hermes-agent (SPEC §5.6) |
| Retry+backoff+семафор+квота Google Ads | **[R]** | `core/resilience.py` (`run_ads_call`/`run_ads_read_call`/`call_llm`) |
| Структурные логи + корреляция | **[R]** | `core/logging.py` (`ContextFilter`, `RedactionFilter`) |
| Учёт токенов/стоимости | **[R]** (per-process) | `core/usage.py` (`record`/`snapshot`) |
| SQLAlchemy 2.0 + Alembic (цепочка ревизий — docs/DATABASE.md) | **[R]** | `db/models.py`, `db/session.py`, `migrations/` |
| Sentry (опц.) | **[R]** | `core/observability.py` |
| Per-iteration трейсинг (Langfuse/Helicone), токены→шаг/run_id | **[B]** | `observability/` (новое) |
| Новые таблицы (agent-runs, message-history, vector-store) | **[B]** | SPEC §9.2 |

### Фаза 2 — Оркестрация / Router pattern
| Требование | Источник | Где |
|---|---|---|
| Маршрутизация интентов, суб-агенты, state machine | **[Ф]** | hermes-agent (SPEC §5.5, §5.6) — **не пишем сами** |
| Реестр инструментов как контракт | **[R]** | `agent/tools/schemas.py` (Pydantic→OpenAI schema) |
| Обёртка вызова модели (`chat`/`finish_reason`/выбор модели по роли) | **[R]** | `agent/router.py` — импортируется на уровне модуля 10 модулями сохраняемых пакетов `adcopy`/`keywords`/`clients`/`advisor`; при архивации `agent/` переезжает в bot-free пакет вместе с `schemas.py`, **удалить нельзя** (рвёт импорт ядра) |
| Маппинг интент→скил/топик | **[B]** | конфиг гейтвея `group_topics`, скилы |

### Фаза 3 — Tool design / self-correction
| Требование | Источник | Где |
|---|---|---|
| 25 READ-инструментов (24 Google Ads + `recall_client`), envelope+error-codes, redaction | **[R]** | `mcp_server/` (`server.py`, `tools_read.py`, `envelope.py`, `redact.py`) |
| ~41 Google Ads skill, резолверы, post-apply verify | **[R]** | `ads/service.py` (`SUPPORTED_OPERATIONS`), `ads/mutations.py`, `ads/resolve.py` |
| WRITE-MCP: два `propose_*` + reply/execute facade ready-dark; полный реестр и live-регистрация | **[R]+[B]** | готовое — `mcp_server/tools_write.py`, `confirm/store.py`, `ads/service.py`; остальное — `mcp_server/` поверх `execute_confirmed` |
| Self-correction (ошибка API → JSON модели, цикл продолжается) | **[Ф]+[R]** | цикл — фреймворк; понятный JSON ошибки — `mcp_server/envelope.err` |

### Фаза 4 — Guardrails / PolicyEngine
| Требование | Источник | Где |
|---|---|---|
| Замки аккаунтов (`ensure_allowed`/`ensure_read_allowed`/`ensure_manager_allowed`/`allowed_ceiling`) | **[R]** | `ads/client.py` |
| Capability-ceiling | **[R]** | `ads/service.py` (`SUPPORTED_OPERATIONS`) |
| Денежные диапазоны/кратность валюты/потолок суммы | **[R]** | `core/limits.py`, `agent/tools/schemas.py` |
| Двухбитовый денежный гейт (user_initiated + origin_human_turn) | **[R]** | `ads/mutations.py`, `core/provenance.py` |
| Freshness/TOCTOU | **[R]** | `ads/service.py` (`_verify_freshness`), `ads/freshness.py` |
| 2FA опасных операций | **[R]** | `core/twofa.py` |
| Construction-time гард (READ без мутаций) | **[R]** | `core/guards.py` |
| **Единый `PolicyEngine`** (middleware перед WRITE) | **[B]** | `guardrails/policy.py` (новое) |
| **Бизнес-лимиты:** Δбюджета ≤20%, D5-порог, дневной потолок/лимит мутаций, опц. no-delete | **[B]** | `guardrails/policy.py` |

### Фаза 5 — Human-in-the-loop / approval
| Требование | Источник | Где |
|---|---|---|
| `Proposal` + `build_summary` + `confirmation_id` | **[R]** | `confirm/gate.py` |
| `ConfirmStore` (CAS claim/confirm/finalize, TTL-в-CAS, one-shot, needs_review, `confirm_by_reply`) | **[R]** | `confirm/store.py` |
| Исполнение после доверенного reply | **[R]** | `ads/service.py` (`confirm_and_execute_by_reply` → `execute_confirmed`) |
| Провенанс `origin_human_turn` для агентного actor | **[B]** | мост в `confirm/store.py` / `core/provenance.py` |
| Карточка 🎯/📊/⚠️ + reply-подтверждение (кнопки архив.) | **[B]** | скил + `gateway.platforms.telegram` |

### Фаза 6 — Память
| Требование | Источник | Где |
|---|---|---|
| `memory`/`context_engine` toolsets (сейчас погашены) | **[Ф]** | hermes-agent (включать осознанно) |
| Артефактная память «помнит всё, что писал/загружал» | **[Ф]** | HERMES_SPEC §17 |
| Скилы как процедурная память | **[Ф]** | HERMES_SPEC §10, §21 |
| Структурная БЗ клиента (профиль, досье map-reduce) | **[R]** | `db/models.py` (`client_profiles`/`client_dossiers`), `clients/` |
| Recall applied-действий | **[R]** | `db/history.py` |
| **RAG бизнес-правил на pgvector** + `save_rule_to_memory` + retrieval | **[B]** (если фреймворк не покрывает) | `memory/` (новое) |
| **Компрессия/саммаризация длинного диалога** (episodic) | **[B]** | `memory/` |
| Таблица истории сообщений | **[B]** | SPEC §9.2 |

### Фаза 7 — Evals
| Требование | Источник | Где |
|---|---|---|
| Guardrail-инварианты (pytest) | **[R]** | `tests/test_invariants_core.py`, `test_safety_core.py`, `test_money_checks_f6.py`, `test_reject_cas.py` |
| Оценщик галлюцинаций (fact-guard) | **[R]** | `audit/factguard.py` (`narrative_facts_preserved`) |
| Скелет A/B моделей | **[R]** | `scripts/ab_test_models.py` |
| **`tests/evals/` + golden-датасет (≥20)** | **[B]** | новое |
| **Shadow Mode** (план в лог, без исполнения) | **[B]** | новое |
| **Eval-раннер** (regression на промптах/моделях) | **[B]** | расширить `ab_test_models.py` |

---

## Топ гэпов (то, что реально строим)
1. **Завершить WRITE-MCP** — два `propose_*` и reply/execute facade уже ready-dark; нужны остальные обёртки, live-регистрация и доверенный Telegram-транспорт мимо LLM.
2. **`PolicyEngine`** — единый слой + бизнес-лимиты (≤20% за шаг, D5-порог, дневные лимиты, опц. no-delete).
3. **Провенанс-мост** для агентного actor (иначе денежные `apply_*` заблокируются fail-closed).
4. **RAG бизнес-правил + компрессия диалога** (если не покрыто фреймворком) и таблица истории сообщений.
5. **Per-iteration observability** (Langfuse/Helicone), токены/латентность → шаг/run_id.
6. **Evals**: golden-датасет + Shadow Mode + eval-раннер.
7. **Конфигурация фреймворка** (config.yaml гейтвея, скилы, топики) и **VPS-развёртывание** (двуххостовая схема, пины, kill-switch).
