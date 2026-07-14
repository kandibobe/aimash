# Ранбук: переменные окружения на проде (что где живёт и как менять)

Практические шаги владельца: **добавить/поменять настройку на боевом сервере**, ничего не сломав
и не заморозив прод на вчерашнем значении. Термины: **`.env.defaults`** — отслеживаемый git файл
прод-ОТКЛОНЕНИЙ (катится автодеплоем); **`/opt/aimash/.env`** — untracked файл секретов на VPS
(правится ТОЛЬКО руками); **код-дефолт** — значение поля в [`core/config.py`](../core/config.py).
Полный список всех ручек с пояснениями — [`.env.example`](../.env.example).

## 0. Три слоя конфигурации (порядок важен)

```
docker-compose.yml → bot.env_file: [.env.defaults, .env]
  1) код-дефолт   core/config.py            ← источник истины, катится с кодом
  2) .env.defaults  в git, деплоится CI     ← ТОЛЬКО осознанные прод-отклонения
  3) /opt/aimash/.env  untracked, руками    ← секреты + инстанс-специфика; ПОБЕЖДАЕТ всех
  (+ environment: в compose бьёт вообще всё — там DATABASE_URL и TZ=UTC)
```

⚠️ **Главная грабля (кусала на проде 2026-07):** любое значение, вписанное в слой 2 или 3,
**перебивает код навсегда**. `.env.defaults` держал `CRAWL_MAX_PAGES=70`, код давно просил 1000 —
обход сайта §20 молча резался втрое, и никто не знал. Отсюда правило:

> **В `.env.defaults` и в серверный `.env` пишем ТОЛЬКО то, что ОТЛИЧАЕТСЯ от код-дефолта.**
> Совпало с дефолтом — не пиши. Иначе завтрашний бамп в коде до прода не доедет.

Правило держит [`tests/test_env_defaults_drift.py`](../tests/test_env_defaults_drift.py): дубликат
дефолта = красный тест; уехал код-дефолт под существующим оверрайдом (`# code-default: X`) = тоже
красный, с требованием пересмотреть оверрайд.

## 1. Куда класть новую переменную

| Что это | Куда | Как доедет до прода |
|---|---|---|
| Секрет (токен, ключ, пароль, DSN) | серверный `.env` | руками по SSH + `up -d --force-recreate bot` |
| Инстанс-специфика (chat_id админов, страна ГЕО, id аккаунтов) | серверный `.env` | то же |
| Прод-отклонение операционной ручки (расписание, формат логов) | `.env.defaults` в git | коммит → push → зелёный CI → автодеплой |
| Значение = код-дефолт | **никуда** | код и так его даёт |

## 2. Добавить переменные в серверный `.env`

### Шаг 1 — посмотреть, чего не хватает (секреты не печатаем)

```bash
ssh root@167.233.48.243
cd /opt/aimash
# имена ключей, которых нет НИ в .env, НИ в .env.defaults (значения не светятся):
comm -23 <(grep -oE '^[A-Za-z_][A-Za-z0-9_]*' .env.example | sort -u) \
         <(cat .env .env.defaults | grep -oE '^[A-Za-z_][A-Za-z0-9_]*' | sort -u)
```
Вывод — это НЕ список «что дописать». Почти всё там берёт правильный код-дефолт
(`CRAWL_*`, `DOSSIER_*`, `LLM_MAX_TOKENS_*`, `PROFILE_CTX_CHARS`, ретеншны…). Дописывать нужно
только то, чего код знать не может (см. Шаг 2).

### Шаг 2 — дописать инстанс-специфику

```bash
cp .env .env.bak.$(date +%Y%m%d-%H%M%S)   # откат в один шаг
nano .env
```

```dotenv
# Админы бота: chat_id узнать командой /whoami. БЕЗ них ERROR_ALERT_INTERVAL_MINUTES=15 — no-op,
# не приходит readiness-пинг после деплоя, а /grant /adduser /addadmin недоступны никому.
ADMIN_CHAT_IDS=<chat_id>,<chat_id>

# Страна ГЕО по умолчанию (агентство льёт на Уганду). Пусто ⇒ кампания без явного гео в брифе
# не создастся: код осознанно НЕ угадывает страну.
DEFAULT_GEO_COUNTRY_CODE=UG
# Пусто ⇒ язык названий локаций выводится из страны (UG → en) и совпадает с гео-таргетами Google.
# Код-дефолт здесь «ru», поэтому пустое значение задаём ЯВНО.
DEFAULT_GEO_LOCALE=
```

### Шаг 3 — проверить, что уже лежит в `.env` (эти строки бьют код!)

```bash
grep -E '^(LLM_|CRAWL_|DOSSIER_|GOOGLE_ADS_ALLOWED|LLM_DAILY)' .env
```
- `LLM_COPY=` / `LLM_KEYWORDS=` с пином старой модели — **удалить строку** (код-дефолт обоих
  `anthropic/claude-opus-4.8`, решение владельца 2026-07). Править значение = снова пин.
- любые `CRAWL_*` — перебивают и код, и `.env.defaults`. Удалить, если это не осознанный оверрайд.

### Шаг 4 — применить (без даунтайма БД)

```bash
docker compose config -q     # резолвит ${POSTGRES_PASSWORD:?} — падает, если пусто
# сухая проверка ВСЕГО конфига, живой бот не трогается (прогоняет prod fail-fast валидаторы):
docker compose run --rm --no-deps bot python -c "from core.config import settings as s; \
  print(s.env, s.crawl_max_pages, s.crawl_time_budget_s, s.llm_copy, s.llm_keywords, \
        s.admin_chat_ids, s.default_geo_country_code, s.llm_daily_calls_per_user)"
docker compose up -d --force-recreate bot
```
Ожидаемо: `prod 1000 240.0 anthropic/claude-opus-4.8 anthropic/claude-opus-4.8 [<ids>] UG 500`
(`llm_daily_calls_per_user`=500 — прод-валидатор сам поднимает 0 → 500, это не опечатка).

⚠️ `docker compose restart bot` **НЕ перечитывает** env-файлы — только `up -d --force-recreate`.

### Шаг 5 — убедиться, что бот поднялся

```bash
docker compose ps                                                     # aimash-bot = Up (healthy)
docker inspect -f '{{.State.Status}} {{.RestartCount}}' aimash-bot    # RestartCount не растёт
docker compose logs --tail=60 bot     # «миграции применены» + «Aimash bot запущен (polling)»
```
В Telegram: пришёл readiness-пинг «✅ Aimash запущен…» (он же доказывает, что `ADMIN_CHAT_IDS`
прочитан), `/diag` без инцидентов, `/whoami` отвечает.

**Откат:** `cp .env.bak.<ts> .env && docker compose up -d --force-recreate bot`.

## 3. Изменить операционную ручку для прода (`.env.defaults`)

1. В `.env.defaults` добавить оверрайд **и строку `# code-default: <текущее значение из кода>`**
   над ним (без неё тест красный).
2. `pytest tests/test_env_defaults_drift.py -q` — зелёный.
3. Коммит → push → **зелёный CI** → автодеплой (`git reset --hard origin/master` +
   `docker compose up -d --build` — контейнер пересоздаётся, env перечитывается сам).

⚠️ Красный CI ⇒ job `deploy` **пропускается молча** (это `needs: [lint-test, secret-scan]`), файл
на сервер не доедет. Проверять: `gh run list --limit 3`.

⚠️ Если та же переменная есть в серверном `.env` — она победит, и правка `.env.defaults` не
подействует. Сначала удалить строку из `.env` (Шаг 3 выше).

## 4. Быстрая диагностика

```bash
# какое значение реально видит бот (после слияния всех трёх слоёв):
docker compose exec bot python -c "from core.config import settings as s; print(s.crawl_max_pages)"
# откуда оно пришло — ищем ключ в обоих файлах (.env главнее):
grep -n '^CRAWL_MAX_PAGES' .env .env.defaults
# секреты живого контейнера НЕ дампить (docker compose exec bot env | ...) — там токены.
docker compose logs --tail=100 bot | grep -iE 'warn|error'
```

| Симптом | Причина | Лечение |
|---|---|---|
| Правка `.env` не подействовала | сделан `restart`, а не `--force-recreate` | `docker compose up -d --force-recreate bot` |
| Правка `.env.defaults` не подействовала | тот же ключ есть в серверном `.env` | удалить строку из `.env` |
| Новая фича «работает по-старому» | старый оверрайд замораживает дефолт | `grep` ключ в обоих файлах; удалить лишнее |
| Коммит с `.env.defaults` не доехал | CI красный ⇒ deploy пропущен | `gh run list --limit 3`, чинить lint/тесты |
| Алерты об ошибках не приходят | пуст `ADMIN_CHAT_IDS` | вписать chat_id (`/whoami`) в `.env` |
| Бот не стартует после правки | prod fail-fast (`core/config.py`) | `docker compose logs bot` — там текст, какого ключа не хватает |

Связанное: [DEPLOYMENT.md](DEPLOYMENT.md) (полная таблица переменных, prod-чеклист),
[RUNBOOK_ACCESS.md](RUNBOOK_ACCESS.md) (доступы к аккаунтам и админка),
[SECURITY.md](SECURITY.md) (fail-closed, замок аккаунта, редакция секретов).
