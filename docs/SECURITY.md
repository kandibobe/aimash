# Безопасность Aimash — проверенные гарантии

Бот управляет **чужими деньгами** в Google Ads, поэтому safety-инварианты — не пожелания, а
проверяемые свойства кода. Этот документ показывает, **где** в коде реализовано каждое из 10
золотых правил ([CLAUDE.md](../CLAUDE.md#-золотые-правила)) и **чем** оно покрыто в тестах. Это
одностраничное доказательство для заказчика/ревьюера: гарантии живут в коде и в CI, а не в промпте.

> Имена функций/классов (а не номера строк) использованы для часто меняющихся модулей — чтобы
> ссылки не «протухали». Точные строки даны только для стабильных файлов.

## Текущая фаза (важно для модели угроз)
**МУТАЦИИ** по умолчанию разрешены на **одном** аккаунте — `Aimash (Draft)` = `7753643025` — и
**только TEST MCC** при разработке. Расширяются **управляемым конфигом** (`GOOGLE_ADS_ALLOWED_CUSTOMER_IDS`),
но только среди аккаунтов, **видимых** боту на чтение — эффективный потолок `allowed_ceiling()` не
пропустит чужой боевой id (защита от опечатки), а код-минимум `ALLOWED_CEILING = {Draft}` нельзя
понизить через `.env` (см. правило 9 и [DEPLOYMENT.md §2.1](DEPLOYMENT.md)). **ЧТЕНИЕ** — шире:
раздельный чокпойнт `ensure_read_allowed` пускает мутационный аккаунт ∪ `GOOGLE_ADS_READ_CUSTOMER_IDS`
∪ дочерние, обнаруженные обходом MCC на старте (§8 read реализован: `/mcc`, per-currency сводки,
per-child таймзоны). Чтение дочернего **не** делает его мутируемым: мутация всё равно требует членства
в `allowed_customer_ids` (инварианты `test_mutation_lock_unchanged_by_read_allowlist`,
`test_grant_does_not_open_mutations`).

---

## Карта: правило → реализация → тесты

| # | Золотое правило | Где в коде | Покрытие тестами |
|---|---|---|---|
| 1 | **Confirm-гейт**: мутация только после «да»; proposal отделён от исполнения | `confirm/store.py::ConfirmStore.claim` (атомарный compare-and-set), `ads/mutations.py::_require_confirmation` | `test_write_layer` (`test_all_apply_reject_without_confirmation`, replay), `test_safety_core` |
| 2 | **`confirmation_id` обязателен** в каждой мутации | каждая `ads/mutations.py::apply_*` принимает `confirmation_id`; `claim` сверяет `status='confirmed'` **и** операцию | `test_write_layer` (`test_apply_rejects_wrong_operation_confirmation`, single-use/replay) |
| 3 | **Бюджет/ставка — только по прямой команде** (`user_initiated`) | `apply_update_budget` / `apply_update_bid` проверяют `proposal.user_initiated`; дефолт `False`; `agent/loop.py` никогда не ставит `True`; `scheduler/*` не мутирует | `test_safety_core` (`test_budget_blocked_when_not_user_initiated`), `test_write_layer` |
| 4 | **Длину RSA считает КОД** (кириллица = 1, CJK = 2; по code points) | `adcopy/validate.py::char_width` / `rsa_len` / `validate`; валидация **до** `claim` | `test_safety_core` (`test_cyrillic_counts_as_one`, `test_cjk_counts_as_two`), `test_write_layer` |
| 5 | **Секреты — никогда в логи/Telegram/гит** | `core/logging.py::redact_text` + `RedactionFilter`; `bot/ux.py::err_text`; `core/config.py` (`SecretStr`, `database_url`); `core/secrets.py` (шифрование at-rest); глобальный `dp.errors`-хендлер логирует редактированно | `test_logging_redaction`, `test_ux_helpers` |
| 6 | **Модель не трогает SDK напрямую** (Pydantic → валидация → diff → «да» → SDK) | `agent/tools/schemas.py` (типизированные схемы) → `ads/mutations.py` (валидация диапазонов) → confirm → SDK; capability-guard `ads/service.py` | `test_write_layer` (capability-guard), `test_bot_integration` |
| 7 | **Только TEST MCC при разработке** | `ENV=dev` по умолчанию; замок аккаунта на `7753643025` (правило 9) | покрыто правилом 9 + `test_config_failfast` |
| 8 | **Жёсткий allow-list операций** | `ads/service.py` исполняет только поддержанные операции (отклоняет неизвестную ДО кнопок и в `execute_confirmed`) | `test_write_layer` (отклонение неподдержанной операции) |
| 9 | **Замок аккаунта** `7753643025` (мутации) + раздельное чтение (§8) | `ads/client.py::ensure_allowed` (мутации), `ensure_read_allowed` (per-account чтение = мутационный ∪ read-env ∪ дочерние обхода), `ensure_manager_allowed` (обход MCC); потолок `ALLOWED_CEILING` зашит в коде | `test_safety_core` (потолок/fail-closed/чужой), `test_mutation_lock_unchanged_by_read_allowlist`, `test_discovered_child_readable_but_not_mutable`, `test_ads_resolve`, `test_ads_read` |
| 10 | **Fail-closed везде** (никогда fail-open) | `bot/main.py::WhitelistMiddleware` → `core.access.is_whitelisted` (источник = env `TELEGRAM_WHITELIST_CHAT_IDS` **∪ таблица `whitelist`**; блок при пустом объединении; сбой БД ⇒ пустой БД-набор, не fail-open); `core/config.py` prod fail-fast (нет ключа/пустой env-whitelist → `ValueError`); `user_initiated` дефолт `False`; пустой allow-list → отказ | `test_whitelist`, `test_runtime_whitelist`, `test_config_failfast`, `test_safety_core` |

---

## Ключевые механизмы (слоями)

### Confirm-гейт = атомарный одноразовый `claim`
Подтверждение и исполнение **разделены**. Агент лишь СОЗДАЁТ черновик (`proposal`); исполняет —
код после явного «да». `ConfirmStore.claim` делает атомарный `UPDATE ... WHERE status='confirmed'
AND operation=...` с проверкой `rowcount==1` → одно подтверждение можно «потратить» **ровно один
раз** (защита от replay) и **только** под ту операцию, для которой оно выдано. Нет валидного
claim → `PermissionError`, SDK не вызывается.

### Замок аккаунта МУТАЦИЙ — слои ([`ads/client.py`](../ads/client.py))
1. **Код-минимум** `ALLOWED_CEILING = {7753643025}` — `.env` не может его **понизить** (Draft всегда в
   потолке). **Эффективный** потолок `allowed_ceiling()` = этот минимум **∪ видимые боту аккаунты**
   (env `GOOGLE_ADS_READ_CUSTOMER_IDS` ∪ дочерние обхода MCC).
2. **Fail-closed** — пустой `allowed_customer_ids` ⇒ отказ (а не «разрешено всё»).
3. **Членство** — мутационный `customer_id` обязан быть в `allowed_customer_ids` (opt-in владельца через
   env) **и** `allowed_customer_ids ⊆ allowed_ceiling()` — env включает мутации только на **видимом**
   аккаунте, чужой боевой id вне видимости не пройдёт (защита от опечатки).

`ensure_allowed` — точка проверки **всех мутаций**; per-account **ЧТЕНИЕ** идёт через отдельный,
более широкий `ensure_read_allowed`; `ensure_manager_allowed` отдельно гейтит обход MCC. По умолчанию
`allowed_customer_ids = {Draft}` ⇒ мутируется только Draft; расширение — **управляемый список** видимых
аккаунтов (см. [DEPLOYMENT.md §2.1](DEPLOYMENT.md)), понизить код-минимум можно лишь правкой `ads/client.py`.

### Редакция секретов — три рубежа
1. **Логи**: `RedactionFilter` на каждой записи + повторная редакция в форматтере (`core/logging.py`).
2. **Telegram**: `bot/ux.py::err_text` всегда прогоняет `str(e)` через `redact_text` — сырой текст
   исключения (от google-ads / google.auth / OpenAI может нести токен) пользователю не уходит.
3. **At-rest**: refresh-токены в БД шифруются Fernet (`core/secrets.py`, `oauth_tokens.refresh_token_enc`);
   секреты в конфиге обёрнуты в `SecretStr`.

### Fail-fast в проде
`ENV=prod` без валидного `SECRETS_ENCRYPTION_KEY` или с пустым `TELEGRAM_WHITELIST_CHAT_IDS` →
приложение **не стартует** (`core/config.py`). env-whitelist — это **бутстрап первого админа**
(обязателен в prod); дальше операторы добавляются в рантайме в таблицу `whitelist` (`/adduser`), но
env всё равно должен быть непустым на старте. Пустое объединение (env ∪ БД) означало бы «отвечаю
всем» — это запрещено (fail-closed).

### Защита от GAQL-инъекции
Резолв по имени (`ads/resolve.py::_gaql_escape`) экранирует `'` и `\` перед подстановкой в
`WHERE name = '...'`, чтобы имя кампании нельзя было превратить в инъекцию. Покрыто в
`test_ads_resolve` (в т.ч. кейс с попыткой вырваться из литерала).

### §20 «Клиенты» — отдельный memory-домен за тем же гейтом
Изменения профиля клиента (save/update/clear) — это мутации **локальной БД**, не Google Ads. Они
идут через **тот же** confirm-гейт («было→станет» + «да»), но исполняются отдельным исполнителем
`clients/execute.py::execute_confirmed_memory` (множество `MEMORY_OPERATIONS`), а НЕ через
`ads.mutations`. Cross-domain инвариант: memory-операция, поданная в ads-исполнитель, и наоборот →
`PermissionError` (`test_client_confirm`). Замок аккаунта Draft к профилям неприменим (это не деньги).
PII клиентов (телефоны/e-mail) в БД: в контекст генерации НЕ кладутся (`profile_context_text`), в
`crawl_jobs.error` редактируются; **бэкапы БД содержат PII** — хранить защищённо ([BACKUP.md](BACKUP.md)).

### Анти-спам (rate limiting)
`bot/throttle.py` ограничивает частоту сообщений на chat_id (ТЗ §12) — защита от флуда и случайного
цикла кнопок. Покрыто `test_throttle`.

---

## Двухслойный доступ ЧТЕНИЯ (2026-07, мультиаккаунт-подготовка)

Доступ оператора к аккаунту на чтение = **глобальный read-замок** (`ensure_read_allowed`:
allow ∪ env read-list ∪ discovered дочерние) × **пер-пользовательский грант** (`account_access`,
`core.access.ensure_account_allowed_for_user`) — enforced на ВСЕХ путях чтения (/status /report
/export /sheets /account, пикеры, get_stats агента, §20). Режимы (`ACCOUNT_ACCESS_MODE`):
`auto` (дефолт: пустая таблица грантов = legacy-проход, первый `/grant` включает enforcement),
`enforced`, `legacy`. Draft доступен всем whitelisted в любом режиме. Гранты выдаёт админ
(`ADMIN_CHAT_IDS`, fail-closed: пусто = команды недоступны). **Грант чтения НЕ открывает
мутации** — их потолок `ALLOWED_CEILING` отдельный (тест `test_grant_does_not_open_mutations`).
Исполнение мутации привязано к `proposal.customer_id` с повторным `ensure_allowed`
(`tests/test_execute_account_binding.py`).

## PII клиентов (§20) — egress

Телефоны/e-mail извлекаются краулером ДЕТЕРМИНИРОВАННО (regex) и **НЕ включаются** в текст,
уходящий во внешний LLM (`CrawlResult.combined_text`); соцсети (публичные хэндлы) включаются.
Residual: контакт, встречающийся в самом тексте страницы, может попасть в LLM-payload —
осознанное ограничение. PII хранится в БД (`client_contacts`) — бэкапы содержат PII, храните
защищённо (см. HANDOVER).

## Что НЕ покрыто / границы (честно)
- **Доверие к LLM ограничено, но не нулевое.** Модель может предложить любой черновик; защита —
  confirm-гейт + замок аккаунта + allow-list операций + валидация диапазонов кодом. Промпт-инъекция
  не может выполнить мутацию без «да» пользователя и не выйдет за разрешённый аккаунт.
- **Ротация Fernet-ключа — ручная** (перешифровать `oauth_tokens` старым→новым ключом; см.
  [DEPLOYMENT.md](DEPLOYMENT.md#генерация-fernet-ключа)).
- **Мутации — по умолчанию один аккаунт (тест-фаза).** Чтение дочерних MCC (сводный отчёт `/mcc`,
  нормализация валют/таймзон) уже реализовано (§8 read); **мутации** на дочерних по умолчанию
  заблокированы — включаются осознанно, по одному, управляемым `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS`
  среди **видимых** аккаунтов (в рамках `allowed_ceiling()`; см. [DEPLOYMENT.md §2.1](DEPLOYMENT.md)).
- **Read-путь зависит от живого SDK** в проде; офлайн он покрыт юнит-тестами на фейковом клиенте
  (`test_ads_read`, `test_ads_resolve`), но не заменяет smoke-проверку доступа
  (`scripts/check_access.py`, read-only) на реальном TEST-аккаунте.

## Проверить самому
```bash
pytest -q                       # весь офлайн-набор (вкл. security-тесты ниже)
pytest -q tests/test_safety_core.py tests/test_whitelist.py \
          tests/test_config_failfast.py tests/test_logging_redaction.py \
          tests/test_ads_resolve.py tests/test_ads_read.py
```
Подробности гейтов — скилы `.claude/skills/confirm-gate-audit` и `new-mutation`; деплой и ротация
секретов — [DEPLOYMENT.md](DEPLOYMENT.md).

## 2FA и модель угроз «захват Telegram-аккаунта» (2.12, аудит 2026-07-06)

**Что есть.** Опциональный второй фактор для ОПАСНЫХ операций (`core/twofa.py`): при
`TWO_FACTOR_ENABLED=true` подтверждение ✅ денежных/необратимых операций (`TWO_FACTOR_OPS_CSV`,
дефолт: удаление кампании/группы, бюджет, ставка, стратегия) требует ввода PIN. Fail-closed:
2FA включён, но PIN не задан ⇒ такие операции блокируются, а не проходят без проверки. PIN
сверяет КОД (constant-time `hmac.compare_digest`), сырьё не логируется (`SecretStr`).
Настройка — [DEPLOYMENT.md §2.2](DEPLOYMENT.md); диагностика готовности аккаунта — `/mutready`.

**Границы (остаточный риск).** Единственный фактор доступа к боту — членство `chat_id` в
whitelist; confirm-гейт («да») — подтверждение намерения, НЕ аутентификация: скомпрометированный
Telegram-аккаунт оператора нажмёт ✅ сам. Что это значит на практике:

- **Мутации** ограничены замком аккаунта (по умолчанию только Draft; боевые — управляемым
  списком) + опциональным 2FA-PIN — включайте 2FA ДО включения мутаций на боевых аккаунтах.
- **Чтение** финансовых данных всех дочерних MCC при захвате Telegram-аккаунта оператора
  утечёт — это принципиальная граница Telegram-бота, кодом не закрывается.

**Организационные требования к операторам (обязательны для прода):**
1. Включённая двухэтапная аутентификация Telegram (Settings → Privacy → Two-Step Verification —
   cloud password) у КАЖДОГО whitelisted-оператора и админа.
2. Не пересылать чат с ботом и не добавлять бота в группы с посторонними.
3. При подозрении на компрометацию: немедленно `/removeuser <chat_id>` (админ) — доступ
   закрывается без рестарта; затем ревизия `/journal` и `/diag`.
