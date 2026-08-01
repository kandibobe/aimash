# Aimash — Hermes-агент для Google Ads

Aimash — приватный Telegram-агент владельца и доверенных сотрудников агентства. Hermes понимает
свободный запрос, выбирает инструменты, читает Google Ads, исследует, анализирует и выполняет действия.
Python-код — узкий typed gateway к Google Ads со Structured JSON errors, Self-Healing,
`validate_only`, лимитами и повторной API-проверкой.

## Источник истины

Договорный продуктовый канон — три оригинальных документа заказчика:
[`Aimash_Technical_Specification.docx`](Aimash_Technical_Specification.docx),
[`Aimash_Flow_Google_Search_4.docx`](Aimash_Flow_Google_Search_4.docx) и
[`Информация о клиентах_1.docx`](Информация%20о%20клиентах_1.docx).
[`ТЗ.md`](ТЗ.md) — их проверяемое текстовое зеркало. [`SPEC.md`](SPEC.md) фиксирует утверждённую
архитектурную редакцию v3.0; исходные DOCX сохранены как исторический договорный baseline.

## Целевой UX

- Менеджер пишет обычным русским или английским текстом.
- Hermes сам решает, что прочитать и в какой последовательности вызвать primitive tools.
- Жёсткого восьмиэкранного wizard и обязательной покнопочной RSA-курации в целевом UX нет.
- Чтение, аудит, отчёты, исследования, выбор tools и подготовка пакета выполняются автономно.
- Оперативные изменения по прямой команде выполняются сразу через typed Function Calling.
- Критические глобальные бюджетные изменения backend переводит в отдельный подтверждаемый шаг.
- Ошибка инструмента возвращает Hermes JSON с причиной и следующим действием для самостоятельного retry.

`aimash_trusted_transport` — тонкий Telegram transport для actor/chat context, файлов и критических
budget approvals; бизнес-логику и выбор следующего tool держит Hermes.

## Текущий production

| Контур | Назначение |
|---|---|
| `@Google_Hermes_AI_Manager_bot` | Целевой agent-first интерфейс Hermes |
| `mcp_server/` | 24 READ-инструмента + 1 META + 55 agent-first PLAN/state + 1 WRITE |
| `scheduler/` | Отчёты, алерты, delivery и фоновые задания |

Hermes MCP запускается отдельным ephemeral-контейнером `aimash-mcp`; scheduler и миграции также
отделены от Telegram gateway. Точная эксплуатационная процедура —
[`deploy/hermes/OPERATIONS.md`](deploy/hermes/OPERATIONS.md).

## Граница автономии

Hermes делает сам:

- понимание задачи и контекста;
- выбор tools, модели, скилов и порядка анализа;
- web/research, диагностику, кластеризацию, формулировку рекомендаций;
- подготовку отчётов, рекламных текстов, профилей и черновиков;
- память, cron, delegation и повторное использование workflow.

Код делает детерминированно:

- typed validation, деньги, проценты, billing units и RSA 30/90/15;
- account ceiling, allowlist, provenance, freshness, kill-switch и quota;
- `validate_only`, лимиты и structured recovery hints на mutation boundary;
- Google Ads SDK mutation, audit и post-verify;
- доверенную доставку файлов и привязку входящих Telegram media.

Классификация операций находится в [`confirm/policy.py`](confirm/policy.py). Неизвестная операция
fail-closed считается требующей подтверждения.

## Локальный запуск

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
python -m pytest -q
python -m mcp_server
```

Hermes-конфиг, установка gateway, production env и OAuth-проверки описаны в
[`deploy/hermes/README.md`](deploy/hermes/README.md) и
[`deploy/hermes/OPERATIONS.md`](deploy/hermes/OPERATIONS.md). Шаблон переменных — [`.env.example`](.env.example).

Живая проверка новой App mutation выполняется только явным запуском оператора на allowlisted Draft:

```powershell
python scripts/live_smoke_app.py --app-id com.example.real
```

Скрипт проходит production confirm/audit path, перечитывает `PAUSED` App campaign из API и не запускается
автоматически тестами. Для Apple используйте `--store apple_app_store` и числовой App ID.

## Runtime guarantees

- Разработка и UAT мутаций — только Draft `7753643025`.
- Пустой Telegram allowlist и пустой mutation allowlist блокируют доступ.
- Оперативные mutation-команды исполняются из текущего пользовательского запроса без legacy wizard.
- Критический глобальный бюджетный diff исполняется после отдельного approval.
- Секреты не попадают в prompt, Telegram, логи или git.
- `skills.inline_shell: false` сохраняется.
- «Выполнено» строится из audit-row и API-readback, а не из уверенного текста модели.

Подробности: [`deploy/hermes/RISK_REGISTER.md`](deploy/hermes/RISK_REGISTER.md),
[`deploy/hermes/OPERATIONS.md`](deploy/hermes/OPERATIONS.md) и разделы 15–17 текстового зеркала
[`SPEC.md`](SPEC.md).

## Структура

```text
ads/             Google Ads read/mutations/service/freshness
confirm/         policy, proposal, CAS, audit
mcp_server/      typed Hermes tools и trusted envelopes
reports/         Telegram/XLSX/Google Sheets
keywords/        Planner, relevance, clustering, negatives, export
adcopy/          RSA generation + deterministic validation
clients/         account-scoped profiles and crawl
scheduler/       background jobs and delivery
deploy/hermes/   gateway config, plugin, operations
```

## Живые документы Hermes

- [`deploy/hermes/README.md`](deploy/hermes/README.md) — установка и топология.
- [`deploy/hermes/OPERATIONS.md`](deploy/hermes/OPERATIONS.md) — эксплуатация, deploy и verification.
- [`deploy/hermes/SAFE_RESTART.md`](deploy/hermes/SAFE_RESTART.md) — безопасный restart.
- [`deploy/hermes/host-a/RUNBOOK.md`](deploy/hermes/host-a/RUNBOOK.md) — runbook текущего host.
- [`deploy/hermes/SOUL.md`](deploy/hermes/SOUL.md) — системные инструкции агента.
- [`deploy/hermes/DRIFT_AUDIT.md`](deploy/hermes/DRIFT_AUDIT.md) — контроль дрейфа поверхности.
- [`deploy/hermes/RISK_REGISTER.md`](deploy/hermes/RISK_REGISTER.md) — открытые риски.
- [`deploy/hermes/OPEN_DECISIONS.md`](deploy/hermes/OPEN_DECISIONS.md) — операционные решения.
- [`deploy/hermes/skills/ad-master/ad-master-agent/SKILL.md`](deploy/hermes/skills/ad-master/ad-master-agent/SKILL.md) — канонический skill аудита и исследования.
- [`deploy/hermes/skills/ad-master/google-ads-worker/SKILL.md`](deploy/hermes/skills/ad-master/google-ads-worker/SKILL.md) — канонический Google Ads skill.
