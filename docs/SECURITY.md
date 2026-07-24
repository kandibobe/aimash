# Безопасность Aimash — проверенные гарантии

Бот управляет **чужими деньгами** в Google Ads, поэтому safety-инварианты — не пожелания, а
проверяемые свойства кода. Этот документ показывает, **где** в коде реализовано каждое из 10
золотых правил ([CLAUDE.md](../CLAUDE.md#-золотые-правила)) и **чем** оно покрыто в тестах. Это
одностраничное доказательство для заказчика/ревьюера: гарантии живут в коде и в CI, а не в промпте.

> Имена функций/классов (а не номера строк) использованы для часто меняющихся модулей — чтобы
> ссылки не «протухали». Точные строки даны только для стабильных файлов.

## Модель угроз (решение владельца 2026-07: мутации на всех видимых аккаунтах)
**Draft-only доктрина снята.** В prod **МУТАЦИИ** по умолчанию разрешены на **всех** аккаунтах,
ВИДИМЫХ боту под его MCC (`GOOGLE_ADS_ALLOWED_CUSTOMER_IDS=all`, прод-дефолт; эффективный потолок
`allowed_ceiling()` = Draft ∪ read-list ∪ дочерние обхода). При разработке — **только TEST MCC**
(в dev/test пустой список = fail-closed, мутаций нет). Радиус поражения расширился с одного Draft до
всех дочерних (у клиента — 7 боевых аккаунтов), поэтому безопасность держится на **компенсирующих
контролях**, каждый из которых сам по себе несменяем:
1. **Confirm-гейт** — ни одна мутация не исполняется без «да» + одноразового `confirmation_id`
   (`ensure_allowed` перепроверяется на исполнении по `proposal.customer_id`). Автоматика (scheduler/
   advisor) физически не может импортировать `ads.mutations` (AST-гарды).
2. **Потолок видимости** — мутировать можно ТОЛЬКО аккаунт, который бот РЕАЛЬНО видит под своим MCC;
   чужой боевой id вне MCC невидим ⇒ немутируем даже при `all` (`allowed_ceiling()`, защита от
   опечатки). Сбой discovery ⇒ деградация до пола потолка `{Draft}`, не эскалация.
3. **Провенанс хода, два независимых бита** — деньги (бюджет/ставка/стратегия/создание) требуют
   прямой команды человека: `user_initiated` (аргумент `save_proposal`) **и** `origin_human_turn`
   (аргументом не задаётся, стор берёт из `core.provenance`; поднимает только `WhitelistMiddleware`,
   `request_scope` фоновых входов — опускает). Бит ставится при **создании** черновика и не
   повышается подтверждением. Подробности — правило 3 в таблице ниже и `docs/MUTATIONS.md`.
4. **Whitelist + 2FA** — доступ к боту закрыт (`TELEGRAM_WHITELIST_CHAT_IDS` ∪ БД, fail-closed); при
   мутабельных боевых аккаунтах **настоятельно** рекомендуется 2FA операторам (см. ниже, раздел
   «2FA и захват Telegram»), чтобы захват аккаунта оператора не давал right-away денежных изменений.
5. **Универсальный баннер аккаунта** — карточка подтверждения ВСЕГДА показывает «⚠️ Аккаунт
   изменения: Имя · id» (Draft — «🧪 …»): оператор видит, на чьи деньги правка, ДО ✅; при
   неоднозначном аккаунте бот сначала ЗАСТАВЛЯЕТ выбрать аккаунт (не угадывает).

**ЧТЕНИЕ** — отдельный, более широкий замок `ensure_read_allowed` (мутационный набор ∪
`GOOGLE_ADS_READ_CUSTOMER_IDS` ∪ дочерние обхода MCC; §8: `/mcc`, per-currency сводки, per-child
таймзоны). Грант чтения оператору **не** открывает мутации — потолок мутаций отдельный (инварианты
`test_mutation_lock_unchanged_by_read_allowlist`, `test_grant_does_not_open_mutations`,
`test_discovered_child_readable_but_not_mutable`); сквозной гейт на боевом аккаунте —
`test_mutations_all_accounts.py`.

---

## Карта: правило → реализация → тесты

| # | Золотое правило | Где в коде | Покрытие тестами |
|---|---|---|---|
| 1 | **Confirm-гейт**: мутация только после «да»; proposal отделён от исполнения | `confirm/store.py::ConfirmStore.claim` (атомарный compare-and-set), `ads/mutations.py::_require_confirmation` | `test_write_layer` (`test_all_apply_reject_without_confirmation`, replay), `test_safety_core` |
| 2 | **`confirmation_id` обязателен** в каждой мутации | каждая `ads/mutations.py::apply_*` принимает `confirmation_id`; `claim` сверяет `status='confirmed'` **и** операцию | `test_write_layer` (`test_apply_rejects_wrong_operation_confirmation`, single-use/replay) |
| 3 | **Бюджет/ставка — только по прямой команде**, ДВА бита провенанса | `ads/mutations.py::_require_user_command` требует `proposal.user_initiated` **и** `proposal.origin_human_turn`; оба дефолтят `False`; второй бит штампует `ConfirmStore.save_proposal` из `core.provenance` (аргументом не задаётся — тест на сигнатуру), поднимает только `bot/main.py::WhitelistMiddleware`, `core.context.request_scope` фоновых входов опускает; `agent/loop.py` никогда не ставит `True`; `scheduler/*` не мутирует | `test_provenance_gate` (выпускной гейт И3: машинный черновик + ✅ человека ⇒ `PermissionError`; allow-list call-site'ов `human_turn`), `test_safety_core` (`test_budget_blocked_when_not_user_initiated`), `test_invariants_core` (`test_money_gate_requires_both_provenance_bits`), `test_write_layer` |
| 4 | **Длину RSA считает КОД** (кириллица = 1, CJK = 2; по code points) | `adcopy/validate.py::char_width` / `rsa_len` / `validate`; валидация **до** `claim` | `test_safety_core` (`test_cyrillic_counts_as_one`, `test_cjk_counts_as_two`), `test_write_layer` |
| 5 | **Секреты — никогда в логи/Telegram/гит** | `core/logging.py::redact_text` + `RedactionFilter`; `bot/ux.py::err_text`; `core/config.py` (`SecretStr`, `database_url`); `core/secrets.py` (шифрование at-rest); глобальный `dp.errors`-хендлер логирует редактированно | `test_logging_redaction`, `test_ux_helpers` |
| 6 | **Модель не трогает SDK напрямую** (Pydantic → валидация → diff → «да» → SDK) | `agent/tools/schemas.py` (типизированные схемы) → `ads/mutations.py` (валидация диапазонов) → confirm → SDK; capability-guard `ads/service.py` | `test_write_layer` (capability-guard), `test_bot_integration` |
| 7 | **Только TEST MCC при разработке** | `ENV=dev` по умолчанию; замок аккаунта на `7753643025` (правило 9) | покрыто правилом 9 + `test_config_failfast` |
| 8 | **Жёсткий allow-list операций** | `ads/service.py` исполняет только поддержанные операции (отклоняет неизвестную ДО кнопок и в `execute_confirmed`) | `test_write_layer` (отклонение неподдержанной операции) |
| 9 | **Замок аккаунта** (мутации — все ВИДИМЫЕ, прод-дефолт `all`) + раздельное чтение (§8) | `ads/client.py::ensure_allowed` (мутации, набор = потолок при `all` / явный список), `ensure_read_allowed` (per-account чтение = мутационный ∪ read-env ∪ дочерние обхода), `ensure_manager_allowed` (обход MCC); потолок видимости `ALLOWED_CEILING`={Draft} зашит в коде | `test_safety_core` (сентинел/потолок/fail-closed/чужой), `test_mutations_all_accounts` (сквозной гейт на боевом), `test_mutation_lock_unchanged_by_read_allowlist`, `test_discovered_child_readable_but_not_mutable`, `test_ads_resolve`, `test_ads_read` |
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
   потолке; это МИНИМУМ, а не «только Draft»). **Эффективный** потолок `allowed_ceiling()` = этот
   минимум **∪ видимые боту аккаунты** (env `GOOGLE_ADS_READ_CUSTOMER_IDS` ∪ дочерние обхода MCC).
2. **Мутационный набор** — сентинел `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS=all`/`*` (`allow_all_visible`,
   **прод-дефолт**) ⇒ набор = ВЕСЬ `allowed_ceiling()`; явный список id ⇒ способ СУЗИТЬ; в dev/test
   пусто ⇒ **fail-closed** (отказ, а не «разрешено всё»).
3. **Потолок видимости** — набор ⊆ `allowed_ceiling()`: мутировать можно только **видимый** аккаунт,
   чужой боевой id вне видимости не пройдёт даже при `all` (защита от опечатки). Сбой discovery ⇒
   набор `all` схлопывается до `{Draft}` (безопасная деградация, не эскалация).

`ensure_allowed` — точка проверки **всех мутаций**; per-account **ЧТЕНИЕ** идёт через отдельный,
более широкий `ensure_read_allowed`; `ensure_manager_allowed` отдельно гейтит обход MCC. В prod по
умолчанию мутируются **все видимые** аккаунты (решение владельца 2026-07); сузить — явным списком id
(см. [DEPLOYMENT.md §2.1](DEPLOYMENT.md)), понизить код-минимум можно лишь правкой `ads/client.py`.
Каждая мутация всё равно проходит confirm-гейт + гард `user_initiated` — «all» расширяет НАБОР
аккаунтов, но не отменяет подтверждение.

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

- **Мутации** ограничены замком аккаунта (в prod по умолчанию — ВСЕ видимые аккаунты, решение
  владельца 2026-07) + confirm-гейтом + опциональным 2FA-PIN. Поскольку мутабельны все боевые
  аккаунты, 2FA из «рекомендуется» становится **практически обязательным**: без него захваченный
  Telegram оператора может провести денежное изменение на любом боевом аккаунте одним ✅ (потолок
  видимости не спасает — эти аккаунты видимы и мутабельны). Замок сужается явным списком id, если
  часть аккаунтов не должна быть мутабельной.
- **Чтение** финансовых данных всех дочерних MCC при захвате Telegram-аккаунта оператора
  утечёт — это принципиальная граница Telegram-бота, кодом не закрывается.

**Организационные требования к операторам (обязательны для прода):**
1. Включённая двухэтапная аутентификация Telegram (Settings → Privacy → Two-Step Verification —
   cloud password) у КАЖДОГО whitelisted-оператора и админа.
2. Не пересылать чат с ботом и не добавлять бота в группы с посторонними.
3. При подозрении на компрометацию: немедленно `/removeuser <chat_id>` (админ) — доступ
   закрывается без рестарта; затем ревизия `/journal` и `/diag`.
