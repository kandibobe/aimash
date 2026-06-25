# Aimash — ИИ-агент управления Google Ads через Telegram

Telegram-бот, который по командам на естественном языке управляет рекламными кампаниями Google Ads (уровень MCC). **Исполнитель, не автономный оптимизатор:** перед любым изменением показывает «было → станет» и ждёт подтверждения «да». Полный бриф и план — в `CLAUDE.md` и план-файле.

## Принцип безопасности
Мутация и подтверждение **разделены**: агент создаёт черновик изменения (proposal), а выполняет его код — только после явного «да» пользователя, с записью в audit-журнал. Бюджет меняется только по прямой команде. См. золотые правила в `CLAUDE.md`.

## Статус
**Фаза −1 (де-риск):** A/B-тест моделей + спайк каркаса. Дальше: read-MVP → confirm-гейт+запись Search → отчёты/тексты → keyword research/scheduler.

---

## Что нужно получить (ключи) — пока ничего нет
| Ключ | Где взять | Зачем | Стоимость |
|---|---|---|---|
| **Test MCC + тест-аккаунты** | ads.google.com → создать менеджерский тест-аккаунт | Безопасная разработка без денег | бесплатно |
| **developer token (Basic)** | у Антона (уже есть) | Доступ к Google Ads API (работает и на тесте) | — |
| **OpenRouter API key** | openrouter.ai | A/B-тест моделей + рантайм (один ключ ко всем моделям) | pay-as-you-go, копейки |
| **Telegram bot token** | @BotFather | Сам бот | бесплатно |

> ⚠️ Логин Claude Code (Max 20x) покрывает **разработку**, но **не рантайм** бота — для прода нужен API-ключ (OpenRouter/Anthropic), это счёт клиента (~$10–50/мес).

---

## Установка
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   |  *nix: source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # затем заполнить .env
```

## A/B-тест моделей (Фаза −1, первый шаг)
Сравнить DeepSeek / Hermes / Claude на реальных русских командах и текстах, выбрать самую дешёвую, что проходит.
```bash
# нужен только OPENROUTER_API_KEY в .env
python scripts/ab_test_models.py
```
Скрипт прогоняет сценарии (парсинг команд → правильная функция+аргументы; «на 20%» vs «до 20%»; типы соответствия; неоднозначное → должен УТОЧНИТЬ; генерация RSA-текстов с лимитами) и печатает таблицу результатов по моделям.

## Структура
```
core/      config, secrets (шифрование), logging
bot/       aiogram handlers, inline-кнопки, whitelist
agent/     router (OpenRouter), system_prompt, tools (Pydantic), loop
ads/       auth, read (GAQL), mutations (требуют confirmation_id), keyword_plan
confirm/   proposal (diff), gate (логика «да»), audit
db/        SQLAlchemy модели + Alembic
scripts/   ab_test_models.py — A/B-тест моделей
```

## Правила разработки
- Только **TEST MCC** при разработке (`ENV=dev`).
- Любая мутация — через `confirm`-гейт с `confirmation_id`; без него код отклоняет.
- Длину текста считает код (кириллица = 1 символ). Секреты — не в код/логи/гит.
