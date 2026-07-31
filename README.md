# Aimash — Hermes-агент для Google Ads

Aimash — приватный Telegram-агент владельца и доверенных сотрудников агентства. Hermes понимает
свободный запрос, выбирает инструменты, читает Google Ads, исследует, анализирует и готовит действия.
Наш Python-код не заменяет агента: это узкий typed gateway к Google Ads, валидация денег/лимитов,
одноразовое подтверждение финансового риска, audit и повторная API-проверка.

## Один источник истины

Единственный нормативный продуктовый документ — [`SPEC.md`](SPEC.md). Оригинальные `.docx` и
[`ТЗ.md`](ТЗ.md) сохранены как contract evidence. Старые продуктовые спеки лежат в
[`docs/archive/pre-single-spec-2026-07/`](docs/archive/pre-single-spec-2026-07/) и не задают текущее
поведение. Runbooks и security/API references подчинены `SPEC.md`.

## Целевой UX

- Менеджер пишет обычным русским или английским текстом.
- Hermes сам решает, что прочитать и в какой последовательности вызвать primitive tools.
- Жёсткого восьмиэкранного wizard и обязательной покнопочной RSA-курации в целевом UX нет.
- Чтение, аудит, отчёты, исследования и явно разрешённые неденежные операции выполняются автономно.
- Для spend-affecting операции показывается один diff и кнопки `✅ Да` / `❌ Нет`; reply на всю
  карточку остаётся fallback.
- Код исполняет typed mutation, пишет audit и перечитывает результат из Google Ads API.

`aimash_trusted_transport` — не второй агент и не бизнес-логика. Это узкий мост, который доказывает,
что финансовое подтверждение действительно пришло от разрешённого Telegram user/chat/message, а не
было придумано моделью или текстом сайта.

## Текущий production

| Контур | Назначение |
|---|---|
| `@Google_Hermes_AI_Manager_bot` | Целевой agent-first интерфейс Hermes |
| `@Aimash_Google_Ads_AI_Manager_bot` | Legacy aiogram fallback до завершения cutover |
| `mcp_server/` | 38 READ-инструментов + 1 META + 53 agent-first PLAN/state + 1 WRITE |
| `scheduler/` | Отчёты, алерты, delivery и фоновые задания |

Hermes MCP всё ещё запускается внутри контейнера `aimash-bot`; поэтому `bot/` нельзя удалять до
bot-free cutover. Точная эксплуатационная процедура — [`deploy/hermes/OPERATIONS.md`](deploy/hermes/OPERATIONS.md).

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
- подтверждение финансового риска через Telegram anchor + CAS;
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
python -m bot.main
```

Hermes-конфиг и установка gateway: [`deploy/hermes/README.md`](deploy/hermes/README.md). Переменные
production: [`docs/RUNBOOK_ENV.md`](docs/RUNBOOK_ENV.md). OAuth Google Ads:
[`docs/OAUTH_SETUP.md`](docs/OAUTH_SETUP.md).

## Безопасность

- Разработка и UAT мутаций — только Draft `7753643025`.
- Пустой Telegram allowlist и пустой mutation allowlist блокируют доступ.
- Бюджет/ставка/стратегия/launch требуют прямой команды человека и одного подтверждения.
- Секреты не попадают в prompt, Telegram, логи или git.
- `skills.inline_shell: false` сохраняется.
- «Выполнено» строится из audit-row и API-readback, а не из уверенного текста модели.

Подробности: [`docs/SECURITY.md`](docs/SECURITY.md), [`docs/BACKUP.md`](docs/BACKUP.md),
[`docs/HERMES_PRODUCTION_GAP.md`](docs/HERMES_PRODUCTION_GAP.md).

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
bot/             временный legacy runtime/fallback
```

Документационный индекс — [`docs/README.md`](docs/README.md).
