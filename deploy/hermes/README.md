# Ранбук: поднять Hermes (Контур A) рядом с боевым ботом на VPS

READ-пилот пивота Aimash → Hermes (`docs/HERMES_SPEC.md`). Hermes-агент отвечает в Telegram и через
MCP-сервер `aimash` (пакет `mcp_server/`, 12 READ-инструментов) читает Google Ads. Денежное ядро не
затрагивается: слой **READ-only by construction** — WRITE-инструментов физически нет.

**Топология.** Текущий aiogram-бот (`aimash-bot` в Docker Compose) остаётся жив — он же окружение для
MCP-сервера. Hermes ставится рядом отдельным systemd user-сервисом и зовёт MCP через `docker exec` в
контейнер бота (паритет окружения: `DATABASE_URL=@postgres:5432`, `GOOGLE_ADS_*`,
`SECRETS_ENCRYPTION_KEY`, OAuth-кэш — всё из контейнера, без host-venv).

**Артефакты рядом:** `config.yaml` (эталон `~/.hermes/config.yaml`), `hermes.env.example` (шаблон
`~/.hermes/.env`).

---

## RB-0. Зайти на VPS

```bash
# host — из docs/DEPLOYMENT.md (167.233.48.243, секрет CI VPS_SSH_HOST); user = VPS_SSH_USER
# (root или deploy); если задан VPS_SSH_PORT — добавьте -p <порт>.
ssh root@167.233.48.243
cd /opt/aimash
docker compose ps          # aimash-bot / aimash-pg должны быть Up
```

Команды RB-1…RB-3 выполняются на сервере в этой сессии (шаги в Telegram/BotFather/OpenRouter — в UI).

---

## RB-1. Установить и запустить Hermes (профиль `aimash`)

```bash
# 1. Установка с жёстким пином версии (0.x релизится часто; на проде автообновление НЕ включаем).
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
hermes --version

# 2. Секреты → ~/.hermes/.env (шаблон — deploy/hermes/hermes.env.example).
hermes config env-path             # путь ~/.hermes/.env
#   заполнить: OPENROUTER_API_KEY, TELEGRAM_BOT_TOKEN (новый бот, RB-2),
#              TELEGRAM_ALLOWED_USERS, TELEGRAM_GROUP_ALLOWED_CHATS

# 3. Провайдер модели — через интерактивный мастер (он же запускается при первом старте):
hermes model
#   Select provider   → OpenRouter (НЕ подсвеченный Nous Portal: у нас рабочий ключ OpenRouter
#                       и слаги §15 — openai/gpt-5.6-*). Далее: ключ sk-or-… (или подхватит
#                       OPENROUTER_API_KEY из ~/.hermes/.env), затем модель openai/gpt-5.6-terra.
#   Мастер пишет в config.yaml ТОЛЬКО блок  model: {provider: openrouter, default: <slug>}
#   (форма mapping, не скаляр — иначе К10 молча откатит на Nous Portal).
#   Select terminal backend → Keep current (local): терминал у Контура A и так гасится (см. тулсеты).
#   Select platforms        → только Telegram (SPACE, ENTER).
#   Tools for CLI (тулсеты) → эталон = минимум (skills/todo/session_search/clarify + наш MCP).
#     Снять ОБЯЗАТЕЛЬНО: Computer Use (контроль рабочего стола VPS) и Cron Jobs (автономный запуск,
#     против золотого правила №3). Terminal/Code/File/Browser держать под approvals: manual и
#     вычитывать каждую команду; обкатка — только на Draft 7753643025.

# 4. Долить НЕ-модельные блоки из эталона репо (мастер их не трогает):
#    mcp_servers.aimash (без него нет доступа к Google Ads), gateway…group_topics (топик→скил),
#    approvals: manual. Эталон: /opt/aimash/deploy/hermes/config.yaml — подставить REPLACE_* (user
#    id, id супергруппы, thread_id топиков). НЕ мёржить agent.disabled_toolsets из эталона, если на
#    экране тулсетов выбрал более широкий набор — иначе он снова погасит выбранное.
hermes config edit
#   (альтернатива «с нуля из файла»: cp /opt/aimash/deploy/hermes/config.yaml ~/.hermes/config.yaml,
#    затем hermes model — мастер перезапишет только model-блок.)

# 5. Конфиг-линт К10 (обязательно, до старта). Hermes молча игнорирует неизвестные ключи.
hermes config check
#   + вручную сверить каждый ключ config.yaml с cli-config.yaml.example пиновой версии.

# 6. systemd user-сервис (переживает выход из SSH).
hermes -p aimash gateway install
sudo loginctl enable-linger $USER  # ОБЯЗАТЕЛЬНО: иначе сервис умрёт при logout
hermes -p aimash gateway start
hermes -p aimash gateway status    # active; в логах — коннект MCP aimash + список 12 tools
#   логи:  journalctl --user -u hermes-gateway -f   (имя юнита сверить `gateway status`)

# 7. Быстрая проверка MCP-коннекта до Telegram.
hermes mcp test aimash             # должен отдать 12 инструментов
```

> Юзер systemd-сервиса Hermes должен иметь доступ к `docker` (группа `docker` или root) — иначе
> `docker exec aimash-bot …` из MCP-конфига упадёт с правами.

**Откат:** `hermes -p aimash gateway stop`. Текущий бот продолжает работать (разные процессы).

---

## RB-2. Telegram: супергруппа с форум-топиками (как на скриншоте)

Топики и privacy программно не переключаются — только владелец в UI/BotFather.

1. **Новый бот для Hermes** (текущий бот не трогаем — обратимость; один токен = один поллер, иначе
   **409**): `@BotFather → /newbot` → скопировать `TELEGRAM_BOT_TOKEN` в `~/.hermes/.env`.
   _Альтернатива «переиспользовать токен»_: сначала `docker compose stop bot` (освободить токен) и
   переключить MCP на compose-`mcp`-сервис (см. ниже) — иначе MCP останется без контейнера.
2. **Группа → супергруппа с топиками:** создать группу → включить **Topics** (форум). Без этого в
   логах `The chat is not a forum`.
3. **Privacy mode** (по умолчанию бот не видит сообщения без `/` и упоминания):
   `@BotFather → /mybots → бот → Bot Settings → Group Privacy → Turn off` → **удалить бота из группы и
   добавить заново** (Telegram кэширует настройку). Альтернатива — сделать бота админом.
4. **id и thread_id** (в `config.yaml`): id супергруппы отрицательный (`-100…`, @get_id_bot);
   `thread_id` топика — из URL `https://t.me/c/<id>/<thread_id>`; свой user id — @userinfobot.
5. **Перечитать конфиг:** `hermes -p aimash gateway restart`.

**Проверка П1:** в топик без упоминания — тишина; с упоминанием «покажи статистику за неделю по
<аккаунт>» — ответ с живыми цифрами (числа из `code_numbers`, не из головы модели).

---

## RB-3. OpenRouter: дневной потолок + kill-switch (рекомендация, не блокер)

Ключ уже рабочий → пилот стартует на нём. confirm-гейт защищает бюджет **Google Ads**, а не трату
**LLM** (Hermes → OpenRouter напрямую, мимо кода) — потолок только платформенный:

1. OpenRouter → **Provisioning API keys** → ключ профиля `aimash` с `limit: <USD>` +
   `limit_reset: daily`. В `~/.hermes/.env` (не в git).
2. **Kill-switch:** деактивация ключа в OpenRouter (account-wide — гасит все прогоны разом).
3. Открытый хвост: `usage.cost`/resolved `model` в наш audit-row не попадают (LLM-трафик мимо кода,
   §20). Конкретные $-потолки — вопрос заказчику.

---

## MCP-интеграция: откуда MCP-сервер берёт окружение

- **Дефолт (работает сразу): `docker exec` в контейнер бота** — путь в `config.yaml`. Наследует env
  контейнера, deps, OAuth-кэш. Работает против уже задеплоенного `aimash-bot` без нового деплоя.
- **Durable (чище по §20, требует деплоя): compose-сервис `mcp`** под `profiles: ["mcp"]` в
  `docker-compose.yml` (штатный `up -d` его не трогает). Hermes зовёт:
  `docker compose -f /opt/aimash/docker-compose.yml --profile mcp run --rm --no-deps -T mcp` —
  свежий контейнер на сессию, отдельно от Telegram-поллера. Переключиться при первом же деплое,
  заменив `args` MCP-сервера в `config.yaml`.
- **Отклонено — host-venv** (`command: python` на хосте): дубль Python-env вне Docker, второй стор
  секретов, `DATABASE_URL=127.0.0.1:5433`, яма `.env`-от-cwd (`core/config.py:29`).

---

## Что прогнано, что нет

MCP READ-слой (`mcp_server/`) прогнан вживую на Draft `7753643025` локально (все 12 инструментов —
чистые редактированные конверты). Шаги на VPS (RB-0…RB-3) выполняет владелец — SSH к боевому серверу
и `git push` в среде агента недоступны. Артефакты этого каталога приезжают на сервер штатным
авто-деплоем (push master → CI green → `git reset --hard` → `docker compose up -d --build`).
