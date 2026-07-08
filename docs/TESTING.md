# Тестирование Aimash

Весь набор — **офлайн**: без живого Google Ads, без сети, без боевого Postgres. Это намеренно —
safety-инварианты (см. [SECURITY.md](SECURITY.md)) должны проверяться в CI быстро и детерминированно.

## Запуск
```bash
make test                 # = pytest -q   (весь набор)
pytest -q tests/test_safety_core.py            # один файл
pytest -q -k "ensure_allowed or whitelist"     # по имени
ruff check . && ruff format --check .          # линт/формат (как в pre-commit/CI)
```
Линт/формат и mypy идут в pre-commit (`make hooks`) и CI; gitleaks блокирует коммит секретов.

## Как устроено

### Изоляция БД (conftest)
[`tests/conftest.py`](../tests/conftest.py) выставляет `DATABASE_URL` на временный SQLite **до**
импорта `core.config`/`db.session`, поэтому write-путь (proposal → confirm → audit) тестируется
офлайн. Файл БД пересоздаётся в начале сессии тестов.

**Флак-изоляция (устранённые классы, не заплатки).** Файл БД — ПЕР-ПРОЦЕССНЫЙ
(`aimash_pytest_<pid>.db`, [conftest.py:20](../tests/conftest.py#L20)): общее имя на Windows роняло
`unlink` OSError, если файл держал недобитый прошлый pytest-процесс → сессия стартовала на грязной
базе (флак «профиль уже есть» / лишние audit-строки). Плюс autouse-фикстура чистит МОДУЛЬНЫЙ кэш
обнаруженных дочерних MCC до и после каждого теста ([conftest.py:45](../tests/conftest.py#L45)): на
машине с живым `.env` ленивый само-обход наполнял его реальными аккаунтами, и он тёк в соседние
тесты. Оба — офлайн в CI (без кредов обход пуст). Видишь флак «зелено в изоляции, красно в полном
прогоне» — ищи разделяемое МОДУЛЬНОЕ состояние, а не ретрай SQLite-лока.

### Фейк Google Ads SDK (без сети)
Живой клиент не нужен — ответы SDK подменяются. Устоявшийся паттерн (см.
[`tests/test_write_layer.py`](../tests/test_write_layer.py), [`tests/test_keyword_plan.py`](../tests/test_keyword_plan.py),
[`tests/test_ads_read.py`](../tests/test_ads_read.py)):
- строки ответа — `types.SimpleNamespace` с той же формой полей, что читает код
  (напр. `row.campaign.status.name`);
- фейковый клиент отдаёт сервис, у которого `search(*, customer_id, query)` возвращает список строк:
```python
class _FakeGA:
    def __init__(self, rows): self._rows = rows
    def search(self, *, customer_id, query): return list(self._rows)

class _FakeClient:
    def __init__(self, rows): self._ga = _FakeGA(rows)
    def get_service(self, name): return self._ga
```
- SDK-исполнители мутаций (`_*_via_sdk`) подменяются через `monkeypatch`/локальный `patched(...)`.

### Настройка замка аккаунта в тестах
`ensure_allowed` fail-closed при пустом allow-list, поэтому happy-path оборачивают в контекст-
менеджер, временно задающий настройки (паттерн `allowed_ids(...)` в `test_safety_core`,
`test_ads_resolve`, `test_ads_read`; для обхода MCC — `login_customer_id(...)`). Всегда
восстанавливай `settings.*` в `finally`, чтобы не протекало между тестами.

## Где что покрыто (security-критичное)
| Свойство | Файл теста |
|---|---|
| Замок аккаунта (потолок/fail-closed/чужой) | `test_safety_core`, `test_ads_resolve`, `test_ads_read` |
| Confirm-гейт + replay + неверная операция | `test_write_layer`, `test_safety_core` |
| Бюджет/ставка только `user_initiated` | `test_safety_core`, `test_write_layer` |
| RSA-длина (кириллица=1, CJK=2) | `test_safety_core`, `test_write_layer`, `test_adcopy_generate` |
| Whitelist fail-closed (message + callback) | `test_whitelist` |
| Редакция секретов (логи + Telegram) | `test_logging_redaction`, `test_ux_helpers` |
| Prod fail-fast (ключ/whitelist) | `test_config_failfast` |
| GAQL-инъекция (`_gaql_escape`) | `test_ads_resolve` |
| §19 визард (этапы/состояние/composite/откат/micros) | `test_cc_stage_flow`, `test_campaign_wizard_state`, `test_cc_composite_create`, `test_search_campaign`, `test_predelivery_fixes` |
| §20 клиенты (профиль/краул/confirm/cross-domain) | `test_client_store`, `test_client_extract`, `test_client_crawler`, `test_client_crawl_orchestration`, `test_client_confirm`, `test_client_wizard` |
| Квота API / анти-спам throttle | `test_quota`, `test_throttle` |
| Предсдаточные фиксы (B1–B15: откат/micros/❌-строки/charset/пагинация/`/cancel`/inflight) | `test_predelivery_fixes` |

## Добавляешь новую мутацию — добавь тесты
Минимальный набор (зеркаль `test_write_layer`), скил `new-mutation` ведёт по шаблону:
1. **happy-path** — оба гейта пройдены (`ensure_allowed` + валидный `confirmation_id`), SDK-исполнитель
   вызван с ожидаемыми аргументами, `store.finalize` записал audit;
2. **без подтверждения** → `PermissionError` (SDK не вызван);
3. **чужой аккаунт** → `PermissionError` ещё до валидации;
4. если операция — деньги (бюджет/ставка): **`user_initiated=False`** → блок;
5. валидация входа (длина/диапазон) срабатывает **до** `claim` (одноразовый id не «сгорает» на плохих данных).

## Smoke на реальном доступе (вне набора)
[`scripts/check_access.py`](../scripts/check_access.py) (`make check-access`) — read-only проверка
доступа к Google Ads на разрешённом аккаунте. Не входит в `pytest` (требует реальных кредов);
гонять вручную после настройки [OAuth](OAUTH_SETUP.md).
