# Безопасность Aimash — проверенные гарантии

Бот управляет **чужими деньгами** в Google Ads, поэтому safety-инварианты — не пожелания, а
проверяемые свойства кода. Этот документ показывает, **где** в коде реализовано каждое из 10
золотых правил ([CLAUDE.md](../CLAUDE.md#-золотые-правила)) и **чем** оно покрыто в тестах. Это
одностраничное доказательство для заказчика/ревьюера: гарантии живут в коде и в CI, а не в промпте.

> Имена функций/классов (а не номера строк) использованы для часто меняющихся модулей — чтобы
> ссылки не «протухали». Точные строки даны только для стабильных файлов.

## Текущая фаза (важно для модели угроз)
Сейчас разрешён **ровно один** аккаунт — `Aimash (Draft)` = `7753643025` — и **только TEST MCC** при
разработке. Это осознанная тест-фазная позиция; продуктовая цель (полный MCC, ТЗ §8) включается
**осознанным** расширением кода, не строкой в `.env` (см. правило 9).

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
| 9 | **Замок единственного аккаунта** `7753643025` | [`ads/client.py:44` `ensure_allowed`](../ads/client.py#L44) и [`:73` `ensure_manager_allowed`](../ads/client.py#L73); потолок [`ALLOWED_CEILING` (`:24`)](../ads/client.py#L24) | `test_safety_core` (потолок/fail-closed/чужой), **`test_ads_resolve` + `test_ads_read`** (резолв и чтение отклоняют чужой аккаунт) |
| 10 | **Fail-closed везде** (никогда fail-open) | `bot/main.py::WhitelistMiddleware` (`if uid not in wl` — блок при пустом); `core/config.py` prod fail-fast (нет ключа/whitelist → `ValueError`); `user_initiated` дефолт `False`; пустой allow-list → отказ | `test_whitelist`, `test_config_failfast`, `test_safety_core` |

---

## Ключевые механизмы (слоями)

### Confirm-гейт = атомарный одноразовый `claim`
Подтверждение и исполнение **разделены**. Агент лишь СОЗДАЁТ черновик (`proposal`); исполняет —
код после явного «да». `ConfirmStore.claim` делает атомарный `UPDATE ... WHERE status='confirmed'
AND operation=...` с проверкой `rowcount==1` → одно подтверждение можно «потратить» **ровно один
раз** (защита от replay) и **только** под ту операцию, для которой оно выдано. Нет валидного
claim → `PermissionError`, SDK не вызывается.

### Замок аккаунта — три независимых слоя
[`ads/client.py`](../ads/client.py):
1. **Код-потолок** `ALLOWED_CEILING = {7753643025}` — `.env` не может его расширить.
2. **Fail-closed** — пустой allow-list ⇒ отказ (а не «разрешено всё»).
3. **Членство** — `customer_id` обязан быть в `allow-list ⊆ потолок`.

`ensure_allowed` — единственная точка для per-account чтения **и** всех мутаций; `ensure_manager_
allowed` отдельно гейтит обход MCC. Расширение круга = **осознанная правка `ads/client.py`**.

### Редакция секретов — три рубежа
1. **Логи**: `RedactionFilter` на каждой записи + повторная редакция в форматтере (`core/logging.py`).
2. **Telegram**: `bot/ux.py::err_text` всегда прогоняет `str(e)` через `redact_text` — сырой текст
   исключения (от google-ads / google.auth / OpenAI может нести токен) пользователю не уходит.
3. **At-rest**: refresh-токены в БД шифруются Fernet (`core/secrets.py`, `oauth_tokens.refresh_token_enc`);
   секреты в конфиге обёрнуты в `SecretStr`.

### Fail-fast в проде
`ENV=prod` без валидного `SECRETS_ENCRYPTION_KEY` или с пустым `TELEGRAM_WHITELIST_CHAT_IDS` →
приложение **не стартует** (`core/config.py`). Пустой whitelist означал бы «отвечаю всем» — это
запрещено на старте, а не в рантайме.

### Защита от GAQL-инъекции
Резолв по имени (`ads/resolve.py::_gaql_escape`) экранирует `'` и `\` перед подстановкой в
`WHERE name = '...'`, чтобы имя кампании нельзя было превратить в инъекцию. Покрыто в
`test_ads_resolve` (в т.ч. кейс с попыткой вырваться из литерала).

---

## Что НЕ покрыто / границы (честно)
- **Доверие к LLM ограничено, но не нулевое.** Модель может предложить любой черновик; защита —
  confirm-гейт + замок аккаунта + allow-list операций + валидация диапазонов кодом. Промпт-инъекция
  не может выполнить мутацию без «да» пользователя и не выйдет за разрешённый аккаунт.
- **Ротация Fernet-ключа — ручная** (перешифровать `oauth_tokens` старым→новым ключом; см.
  [DEPLOYMENT.md](DEPLOYMENT.md#генерация-fernet-ключа)).
- **Один аккаунт — тест-фаза.** Полный MCC (несколько дочерних, сводный отчёт, нормализация
  валют/таймзон) — позже, осознанным расширением `ALLOWED_CEILING`.
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
