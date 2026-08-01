# Aimash 3.0 — Bias for Action

> **Статус:** утверждённая владельцем архитектурная редакция
> **Дата:** 01.08.2026
> **Область:** runtime, UX и реализация Aimash

Три исходных DOCX заказчика — `Aimash_Technical_Specification.docx`,
`Aimash_Flow_Google_Search_4.docx`, `Информация о клиентах_1.docx` — и их зеркало `ТЗ.md`
сохранены как исторический договорный baseline.
Для реализации runtime требования этой редакции заменяют прежнюю FSM/confirm-first архитектуру.
`SPEC.md` — ненормативная инженерная интерпретация этого baseline с утверждённым v3 runtime-cutover;
при расхождении предметных требований исходные DOCX сохраняют traceability.

## 1. Цель

Aimash — автономный Hermes-агент для управления Google Ads через Telegram. Пользователь описывает
цель естественным языком; Hermes самостоятельно исследует аккаунт, выбирает typed tools, делегирует
тяжёлую аналитику и выполняет операционные действия.

Основной принцип:

> **Сначала действие и исследование. Backend ограничивает только фактически недопустимую операцию.**

## 2. Что удаляется

- legacy aiogram poller и тяжёлые Telegram handlers;
- FSM/StatesGroup для кампаний, RSA, ключей, профилей и навигации;
- обязательные inline-кнопки для промежуточных шагов;
- покнопочное подтверждение каждого заголовка, описания и микродействия;
- второй LLM-командный цикл, дублирующий Hermes;
- prompt-микроменеджмент и длинные списки запретов;
- прямой запуск MCP внутри контейнера legacy-бота;
- тесты, проверяющие удалённый wizard/callback UX вместо поведения Hermes tools.

## 3. Runtime topology

```text
Telegram
  → Hermes gateway
      → Hermes orchestrator
          → Aimash MCP typed tools
              → Google Ads / PostgreSQL / reports / crawling
          → Analysis sub-agent
  → scheduler (отдельный процесс для cron/alerts/reconciliation)
```

### 3.1. Telegram

Telegram — тонкий транспорт:

- принимает свободный текст и файлы;
- передаёт actor/chat/topic context Hermes;
- показывает короткий результат и структурированные отчёты;
- доставляет XLSX и другие артефакты;
- не реализует бизнес-процесс через FSM.

### 3.2. Hermes orchestrator

Hermes:

- держит контекст диалога;
- определяет цель и следующий шаг;
- выбирает Function Calling tools;
- сначала выполняет READ и исследование;
- делегирует тяжёлый анализ отдельному analyst sub-agent;
- собирает результат и принимает операционные решения;
- самостоятельно исправляет retryable tool errors.

### 3.3. Analyst sub-agent

Аналитик вызывается как инструмент и получает task envelope:

```json
{
  "objective": "Найди главный источник потерь",
  "account": "customer reference",
  "period": {"date_from": "YYYY-MM-DD", "date_to": "YYYY-MM-DD"},
  "filters": {}
}
```

Внутри analyst-контура выполняются GAQL, обработка больших JSON и расчёты. Hermes получает digest:

```json
{
  "summary": "...",
  "metrics": {},
  "findings": [],
  "recommendations": []
}
```

Сырые выгрузки не засоряют основной контекст Hermes.

## 4. Bias for Action

Нормальный цикл:

```text
понять цель
→ прочитать необходимые данные
→ проверить гипотезы
→ выбрать действие
→ вызвать typed tool
→ оценить structured result
→ при необходимости исправить аргументы и повторить
→ сообщить фактический результат
```

- READ, аудит, research, crawling, аналитика, генерация и подготовка выполняются автономно.
- Оперативные изменения по прямой пользовательской команде выполняются без legacy wizard.
- Статусы, ставки и минус-слова меняются прямым Function Calling.
- Связанный пакет создания кампании собирается и применяется за один агентный цикл.
- Backend возвращает `APPROVAL_REQUIRED`, только когда операция классифицирована как критическое
  изменение глобального бюджета; Hermes продолжает после одного approval.

## 5. Tool design

Tool schema содержит только параметры, которые действительно выбирает агент. Текущие resource names,
стратегии, валюту и живые значения backend получает самостоятельно из БД или Google Ads API.

Пример:

```python
set_target_cpa(campaign_ref: str, target_cpa_micros: MoneyMicros)
```

Вместо вложенного JSON с `customer_id`, `ad_group_id`, текущей bidding strategy, подписью и dry-run
флагом агент передаёт ссылку на объект и новое значение.

## 6. Structured JSON и Self-Healing

Успех:

```json
{
  "ok": true,
  "data": {},
  "message": null
}
```

Исправимая ошибка:

```json
{
  "ok": false,
  "error_type": "AMBIGUOUS_CAMPAIGN",
  "message": "Найдено 3 кампании с таким названием.",
  "suggested_action": "Вызови list_campaigns и повтори запрос с точным reference."
}
```

Connector:

- перехватывает `GoogleAdsException`, timeout, validation и partial failure;
- редактирует техническую ошибку;
- классифицирует retryability;
- предлагает конкретный следующий tool/аргумент;
- возвращает частично применённые и отклонённые позиции отдельно;
- позволяет Hermes самостоятельно повторить исправленный вызов.

## 7. Mutation execution

Внутренний pipeline:

```text
resolve live object
→ build operations
→ validate_only
→ classify backend result
→ apply
→ readback
→ structured result
```

Backend реализует `MoneyMicros`, budget limits, blast-radius, CAS для критического approval,
kill-switch, request throttling и audit. Эти механизмы работают в Python/MCP middleware и не
дублируются длинными инструкциями модели.

## 8. Agent loop и контекст

- Цикл tool calling контролируется Hermes runtime.
- После tool result модель оценивает прогресс и выбирает следующий tool либо финальный ответ.
- Повтор одинакового tool call без новых данных останавливается структурированной ошибкой.
- Тяжёлые tool outputs заменяются digest/artifact reference.
- Контекст компактируется после завершения этапа или примерно каждые 10–15 tool steps.
- Сохраняются цель, выбранный account, подтверждённые факты, незавершённые действия и artifact refs.

Перед tool call допустим краткий action rationale в `<thought>`: цель вызова, ожидаемый результат и
следующий шаг. Parser удаляет тег из Telegram-ответа и пишет rationale в trace/Langfuse. Скрытая
внутренняя цепочка рассуждений не является частью публичного контракта.

## 9. Google Ads

- официальный Google Ads API v25;
- Python SDK `google-ads` 31.2.x;
- GAQL и MCC reads;
- `KeywordPlanIdeaService`;
- Search, GDN, Demand Gen, Video и App campaign tools;
- budgets, bids, keywords, negatives, RSA и assets;
- `validate_only`, partial failure и readback;
- change events и reconciliation.

Версия API задаётся конфигом. Номер `vNN` не хардкодится в сервисных модулях.

## 10. Monitoring

Standalone scheduler выполняет:

- anomaly watchdog;
- отчёты и алерты;
- reconciliation локального состояния с Google Ads;
- change-event monitoring;
- очистку временных артефактов;
- heartbeat.

Scheduler возвращает наблюдения и рекомендации; исправление retryable API-вызова использует тот же
Structured JSON contract.

## 11. Acceptance criteria

- production topology не содержит legacy `bot.main` poller;
- MCP запускается независимо от aiogram-контейнера;
- Telegram принимает свободные команды через Hermes;
- Hermes самостоятельно выбирает READ/analysis/mutation tools;
- тяжёлая аналитика выполняется analyst sub-agent;
- стандартные ошибки возвращаются как Self-Healing JSON;
- retryable ошибка исправляется следующим tool call без участия разработчика;
- оперативные mutations не открывают FSM/inline wizard;
- критический глобальный budget change использует один approval;
- scheduler работает отдельным процессом;
- полный актуальный test suite проходит без legacy bot/FSM tests.
