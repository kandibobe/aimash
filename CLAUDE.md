# Aimash — AI Agent for Google Ads via Telegram + Hermes

## Стек
- **Python 3.12** + google-ads SDK v24 + FastMCP
- **PostgreSQL** + pgvector (векторная память)
- **Hermes Agent** (gateway: Telegram; модель: `openai/gpt-5.6-terra` через OpenRouter)
- **Claude Code** (автономная разработка MCP-инструментов)
- **Docker Compose** (4 контейнера: bot, pg, scheduler, backup)

## Золотые правила (не нарушать — это про чужие деньги)

1. **Мутация только после «да» реплаем** — confirm-гейт (`confirm/store.py` CAS claim)
2. **confirmation_id обязателен, одноразовый** — атомарный `ConfirmStore.claim`
3. **Бюджет/ставка — только по команде человека** — два бита провенанса (`core/provenance.py`)
4. **Длину символов считает КОД** — RSA: headline≤30, desc≤90, path≤15; кириллица=1 символ
5. **Секреты — никогда в промпт/логи/git** — Fernet at-rest, три рубежа редакции
6. **Модель НЕ трогает Google Ads SDK напрямую** — только через MCP-инструменты
7. **Разработка — только на Draft-аккаунте** (7753643025) — никогда на боевых

## Архитектура

```
Пользователь (Telegram) → Hermes gateway → MCP (docker exec aimash-bot) → Google Ads API
                                                      ↓
                                              confirm-гейт (Proposal → «да» реплай → execute)
```

- **Модель решает:** ЧТО читать, КАК анализировать, ЧТО предложить, КАКИЕ скилы писать
- **Код исполняет:** валидацию, confirm-гейт, вызов API, audit-row, факт-гард

## Ключевые пакеты

| Пакет | Назначение | Трогать? |
|---|---|---|
| `ads/mutations.py` | Вызов Google Ads API | ❌ НИКОГДА (денежное ядро) |
| `confirm/store.py` | CAS claim, TTL, one-shot | ❌ НИКОГДА |
| `confirm/gate.py` | Proposal, build_summary | ❌ НИКОГДА |
| `core/secrets.py` | Fernet-шифрование токенов | ❌ НИКОГДА |
| `core/guards.py` | Construction-time гарды | ❌ НИКОГДА |
| `mcp_server/` | MCP-инструменты (READ + WRITE) | ✅ Расширять |
| `tests/` | 2156 тестов | ✅ Добавлять |

## Google Ads аккаунты

| Аккаунт | ID | Валюта | Статус |
|---|---|---|---|
| **Draft (тест)** | 7753643025 | AUD | Только для разработки |
| Aimash | 6764040266 | UAH | Боевой |
| Irisboutique | 7990205915 | CZK | Боевой |
| Rozowy Słoń | 8325477566 | PLN | Боевой |
| Art Or | 9889330611 | USD | Боевой |
| MCC | 6283738601 | — | Управляющий |

## Текущий статус пивота

- ✅ READ-MCP: 12 инструментов, работает
- 🔨 WRITE-MCP: `propose_budget_change`, `execute_approved_action`, ...
- 🔨 PolicyEngine: Δ≤20% за шаг, дневные лимиты
- 🔨 Память: pgvector RAG бизнес-правил
- 🔨 Evals: golden dataset ≥20 сценариев

## Команды

```bash
# Dev
docker compose up -d                    # Запустить всё
docker compose logs -f aimash-bot       # Логи бота

# Тесты
docker exec aimash-bot python -m pytest tests/ -x -q

# Hermes
hermes gateway status                   # Статус гейтвея
hermes config                           # Конфигурация
hermes skills list                      # Скилы
```

## Важно

- Hermes должен работать с `provider_routing.require_parameters: true`
- Терминал/файлы/браузер включены ТОЛЬКО для агентной разработки
- Production-конфиг эталона: `deploy/hermes/config.yaml`
- Полные доки (архитектура, безопасность, смета): `docs/archive/`