# Aimash — AI Agent for Google Ads via Telegram

Агентная платформа управления Google Ads через Telegram на базе Hermes Agent.

**Исполнитель с подтверждением каждого изменения.** Агент понимает задачу на естественном языке, анализирует аккаунт, предлагает изменения — но выполняет только после reply-подтверждения «да».

## Как это работает

```
Менеджер: «разберись почему упали заявки»
    ↓
Агент читает статистику → анализирует → пишет сводку → предлагает изменения
    ↓
Менеджер: reply «да»
    ↓
Агент выполняет → пишет audit-row «выполнено»
```

## Архитектура

- **Мозг:** Hermes Agent (openai/gpt-5.6-terra через OpenRouter)
- **Интерфейс:** Telegram (топик = клиент)
- **Google Ads:** Python SDK v24 через MCP-инструменты
- **Безопасность:** Confirm-гейт (Proposal → «да» реплай → execute), CAS claim, одноразовость
- **Автономная разработка:** Claude Code пишет новые MCP-инструменты → PR → human review → merge

## Стек

Python 3.12 · google-ads SDK v24 · FastMCP · PostgreSQL + pgvector · Docker Compose · Hermes Agent

## Быстрый старт

```bash
git clone git@github.com:kandibobe/aimash.git
cd aimash
cp .env.example .env   # заполнить ключи
docker compose up -d
```

## Документация

- **[ТЗ (agentic v3)](docs/TZ-Aimash-Agentic-v3.md)** — полное техническое задание
- **[CLAUDE.md](CLAUDE.md)** — контекст для AI-агентов (золотые правила, стек, пути)
- **[docs/archive/](docs/archive/)** — исторические доки (SPEC v2, HERMES_SPEC, аудит open-source)

## Статус

✅ READ-MCP (12 инструментов) · 🔨 WRITE-MCP · 🔨 PolicyEngine · 🔨 pgvector-память · 🔨 Evals