# Передача Aimash заказчику (handover)

Раннбук передачи бота заказчику: перенос владения ключами, развёртывание на своём сервере,
проверки перед сдачей. Технические детали деплоя — в [DEPLOYMENT.md](DEPLOYMENT.md); схема БД и
миграции — в [DATABASE.md](DATABASE.md); золотые правила — в [../CLAUDE.md](../CLAUDE.md).

> ⚠️ Бот управляет **чужими деньгами** в Google Ads. Перед боевым запуском пройди весь
> [pre-delivery чек-лист](#4-чек-лист-перед-сдачей-pre-delivery-gate) и
> [prod-чеклист DEPLOYMENT.md §6](DEPLOYMENT.md#6-prod-чеклист).

## Модель передачи (зафиксировано)
- **Заказчик владеет всеми ключами** — секреты не передаются, заказчик заводит свои (раздел 1).
- **Деплой — свой VPS + Docker Compose** (раздел 2).
- **Прод-аккаунт — полный MCC** (много дочерних) → требует спринта многоаккаунтности (раздел 5).
- **Интерфейс — RU/EN.**

---

## 1. Перенос владения ключами (заказчик заводит свои)

Ни один секрет разработчика не переезжает в прод. Заказчик создаёт и владеет:

| Ключ (`.env`) | Где завести | Примечание |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather (свой бот) | свой бот — нельзя «унаследовать» дев-бота |
| `OPENROUTER_API_KEY` | openrouter.ai (свой биллинг) | рантайм LLM — счёт заказчика (~$10–50/мес) |
| `GOOGLE_ADS_DEVELOPER_TOKEN` | Google Ads API Center (свой MCC) | Basic-уровня достаточно |
| `GOOGLE_ADS_CLIENT_ID` / `..._SECRET` | Google Cloud → OAuth-клиент (свой проект) | desktop-app OAuth |
| `GOOGLE_ADS_REFRESH_TOKEN` | `python scripts/get_refresh_token.py` | scope `adwords` **и** `drive.file` (для `/sheets`) |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | ID менеджерского (MCC) аккаунта | контекст авторизации |
| `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS` | — | круг разрешённых аккаунтов (см. раздел 5) |
| `SECRETS_ENCRYPTION_KEY` | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` | Fernet-ключ; хранить в секрет-сторе |

Передаём заказчику **инструкции** (этот файл + `DEPLOYMENT.md`), а не значения.

---

## 2. Развёртывание на VPS (Docker Compose)

```bash
git clone <repo> /opt/aimash && cd /opt/aimash
cp .env.example .env            # заполнить ключами из раздела 1
chmod 600 .env                  # секреты доступны только владельцу
# ENV=prod включает fail-fast: пустой Fernet-ключ или пустой whitelist уронят старт.
docker compose up -d --build    # postgres + bot; миграции применяются АВТОМАТИЧЕСКИ при старте
docker compose logs -f bot      # убедиться: "alembic upgrade head" прошёл, бот "запущен (polling)"
```

Миграции БД накатываются автоматически (`docker-entrypoint.sh` → `alembic upgrade head` до старта
бота; падение миграции = контейнер не поднимается, fail-fast). Бот — long-poll (HTTP-порта нет).

### Whitelist — это `.env`, а не таблица БД
Доступ к боту гейтится по `TELEGRAM_WHITELIST_CHAT_IDS` (env), а **не** по таблице `whitelist` в БД
(`WhitelistMiddleware` и `scheduler` читают `settings.whitelist`). Чтобы пустить пользователей —
впиши их `chat_id` в `.env` и перезапусти бота. В prod пустой whitelist роняет старт (fail-closed).

> Таблицы `whitelist` и `oauth_tokens` сейчас **не используются рантаймом** (env — источник истины
> по доступу; refresh-токен — из `.env`). Это задел под мультиюзер/мультиаккаунт; см. раздел 5.

---

## 3. О «миграции данных» (важно)

**Дев/тест-БД заказчику НЕ переносится.** `audit_log`/`proposals` тест-аккаунта — артефакты
разработки. Прод стартует с **чистой** Postgres; переносится только **схема** (через Alembic),
не данные. «Миграция» к заказчику =
1. развернуть код на его VPS;
2. вписать его ключи в `.env`;
3. `docker compose up` (миграции накатятся сами);
4. вписать `chat_id` в `TELEGRAM_WHITELIST_CHAT_IDS`.

### Бэкапы (обязательны до боевого запуска)
`audit_log` необратим при потере. Поставить `scripts/backup_db.sh` в cron (пример внутри скрипта),
выгружать дампы **наружу** (S3/Backblaze/rclone) и **протестировать восстановление** на staging.

---

## 4. Чек-лист перед сдачей (pre-delivery gate)

- [ ] **Живой smoke** на разрешённом аккаунте: `python scripts/check_access.py` (замок + обход MCC)
      и `python scripts/live_smoke_test.py` (обратимый pause↔resume через полный confirm-гейт). `exit 0`.
- [ ] `pytest -q` зелёный; `ruff check . && ruff format --check .` чисто; `mypy` просмотрен.
- [ ] `gitleaks` чист; `git log -p -- .env` пуст (`.env` никогда не коммитился).
- [ ] **Prod fail-fast:** `ENV=prod` с пустым `SECRETS_ENCRYPTION_KEY` → старт падает; пустой
      whitelist → падает.
- [ ] `alembic heads` == один (`0005`); `upgrade head` чистый; `downgrade -1` → `upgrade` обратимы.
- [ ] **Autogenerate-дрейф = 0** на чистой Postgres (`alembic revision --autogenerate` ничего не
      предлагает — ни DROP, ни ADD).
- [ ] Бэкап настроен и **restore протестирован**.
- [ ] `/lang en` переключает **весь** интерфейс (после завершения EN-локализации).
- [ ] Прод-чеклист [DEPLOYMENT.md §6](DEPLOYMENT.md#6-prod-чеклист) пройден полностью.

---

## 5. Перед боевыми деньгами на полном MCC (открытый объём)

**Замки чтения и мутаций РАЗДЕЛЕНЫ** (golden rule #9): мутации — `ensure_allowed` + код-потолок
`ALLOWED_CEILING = {"7753643025"}` ([../ads/client.py](../ads/client.py), `.env` не расширит);
чтение — `ensure_read_allowed` + env `GOOGLE_ADS_READ_CUSTOMER_IDS` (fail-closed, по умолчанию пуст).
Это значит: дочерние можно дать **на чтение** под §8, не открывая мутаций на них.

Сделано (read-only фундамент §8):
- ✅ раздельные замки чтения/мутаций (`ensure_read_allowed`), инвариант покрыт тестом;
- ✅ обнаружение дочерних — `ads.read.list_child_accounts` (за `ensure_manager_allowed`).

Осталось для боевого MCC (≈ остаток спринта, всё read-only кроме п.5):
1. сводный отчёт по дочерним (ТЗ §8): `reports/service` строит отчёт на один аккаунт → обход
   `list_child_accounts` + агрегат + команда бота;
2. **нормализация валют** (разные дочерние — разные валюты; нельзя суммировать «в лоб»);
3. **таймзоны per-child** в отчётных периодах;
4. заполнить `GOOGLE_ADS_READ_CUSTOMER_IDS` дочерними MCC заказчика;
5. (отдельно, осознанно) если нужны мутации на дочерних — расширить `ALLOWED_CEILING` в коде +
   живой прогон под confirm-гейтом; (опц.) подключить `oauth_tokens` для токенов по аккаунту.

До этого бот: **читает** только перечисленные аккаунты, **меняет** только `7753643025`.
