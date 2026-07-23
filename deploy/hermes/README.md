# Ранбук: поднять Hermes (Контур A) рядом с боевым ботом на VPS

READ-пилот пивота Aimash → Hermes. Hermes-агент отвечает в Telegram и через
MCP-сервер `aimash` (пакет `mcp_server/`, 12 READ-инструментов) читает Google Ads. Денежное ядро не
затрагивается: слой **READ-only by construction** — WRITE-инструментов физически нет.

**Где что написано:** требования и приёмка — [`/SPEC.md`](../../SPEC.md); архитектура (топология,
И1–И8, К1–К10, реестр инструментов) — [`HERMES_SPEC.md`](HERMES_SPEC.md); почему так —
[`AGENTIC_VS_TZ.md`](AGENTIC_VS_TZ.md). Этот файл — **как поставить**, а не что и почему.

**Топология.** Текущий aiogram-бот (`aimash-bot` в Docker Compose) остаётся жив — он же окружение для
MCP-сервера. Hermes ставится рядом отдельным systemd user-сервисом и зовёт MCP через `docker exec` в
контейнер бота (паритет окружения: `DATABASE_URL=@postgres:5432`, `GOOGLE_ADS_*`,
`SECRETS_ENCRYPTION_KEY`, OAuth-кэш — всё из контейнера, без host-venv).

**Артефакты рядом:** `config.yaml` (эталон `~/.hermes/config.yaml`), `hermes.env.example` (шаблон
`~/.hermes/.env`), `lint_config.py` (конфиг-линт К10 — **неизвестные ключи Hermes игнорирует молча**),
`host-a/` (эталон живого хоста + RUNBOOK), `RISK_REGISTER.md`, `plugins/aimash_probe` (проб
доверенного канала метаданных гейта).

**Этот файл — только установка (RB-0…RB-3).** День-2 эксплуатация (что применяется вживую vs требует
restart, редеплой↔MCP-reconnect, логи, обновление/откат, бэкап, kill-switch, траблшутинг) — в
[`OPERATIONS.md`](OPERATIONS.md).

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

## RB-1. Установить и запустить Hermes (ДЕФОЛТНЫЙ профиль)

> ⚠️ **Профиль `aimash` не создавать.** Профиль в Hermes — это отдельный home-каталог
> `~/.hermes/profiles/<имя>/` со своими `config.yaml`, `.env`, `SOUL.md`, памятью и gateway-юнитом.
> `hermes profile create aimash` заводит **пустой** профиль и наш конфиг из `~/.hermes/` туда не
> попадает; а `hermes -p aimash …` без создания падает с `Profile 'aimash' does not exist`
> **[Проверено на VPS 21.07]**. Весь ранбук — на дефолтном профиле, команды **без `-p`**.

```bash
# 1. Установка с жёстким пином версии (0.x релизится часто; на проде автообновление НЕ включаем).
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
hermes version                     # версия = субкоманда, НЕ `--version`

# 2. Секреты → ~/.hermes/.env (шаблон — deploy/hermes/hermes.env.example).
hermes config env-path             # путь ~/.hermes/.env
#   заполнить: OPENROUTER_API_KEY, TELEGRAM_BOT_TOKEN (новый бот, RB-2), TELEGRAM_ALLOWED_USERS.
#   ⚠️ TELEGRAM_GROUP_ALLOWED_CHATS не заполнять — ни здесь, ни в config.yaml. Прошлая редакция
#      этой строки отправляла его в `gateway.platforms.telegram.extra.group_allowed_chats`, и это
#      было неверно дважды: (1) из `extra` гейты доступа не читает никто, решение принимает
#      `gateway/authz_mixin.py::_is_user_authorized` и только по env; (2) сам чат-лист авторизует
#      группу ЦЕЛИКОМ и проверяется ПЕРВЫМ (authz_mixin.py:344-358, `return True` до проверки
#      личности) — то есть отменяет перечисление людей, а не дополняет его.
#      Гейт группы = `gateway.platforms.telegram.group_allow_from` НА УРОВНЕ БЛОКА (не в `extra`),
#      он же → TELEGRAM_GROUP_ALLOWED_USERS. Разбор со ссылками: deploy/hermes/host-a/config.yaml.
#   ⚠️ Тулсет `web` без ключа поискового провайдера (EXA/TAVILY/BRAVE/FIRECRAWL/SEARXNG) мёртв —
#      либо ключ, либо `web` в disabled_toolsets, иначе агент дёргает нерабочий инструмент.

# 3. Провайдер модели — через интерактивный мастер (он же запускается при первом старте):
hermes model
#   Select provider   → OpenRouter (НЕ подсвеченный Nous Portal: у нас рабочий ключ OpenRouter
#                       и слаги §15 — openai/gpt-5.6-*). Далее: ключ sk-or-… (или подхватит
#                       OPENROUTER_API_KEY из ~/.hermes/.env), затем модель openai/gpt-5.6-terra.
#   Мастер пишет в config.yaml ТОЛЬКО блок  model: {provider: openrouter, default: <slug>}
#   (форма mapping, не скаляр — иначе К10 молча откатит на Nous Portal).
#   Select terminal backend → Keep current (local): терминал у Контура A и так гасится (см. тулсеты).
#   Select platforms        → только Telegram (SPACE, ENTER).
#   Tools for CLI (тулсеты) → эталон = минимум (skills/todo/clarify + наш MCP). `session_search`
#     в эталоне ПОГАШЕН (К9/И6): он ищет по всей ~/.hermes/state.db, то есть по всем топикам, а
#     топик у нас = клиент — кросс-клиентное чтение переписки в обход нашего замка.
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

# 5. Проверка конфига до старта. ВАЖНО: `hermes config check` про «missing/stale», он НЕ ловит
#    неизвестные/опечатанные ключи — Hermes их молча игнорирует (К10). Единственный надёжный контроль:
hermes config check                # ключи .env: OPENROUTER_API_KEY / TELEGRAM_BOT_TOKEN /
                                   #   TELEGRAM_ALLOWED_USERS должны быть ✓, а не ○
#   ⚠️ `hermes config show` печатает ФОРМАТИРОВАННЫЙ вид, а не сырой YAML: `config show | grep
#      mcp_servers` даёт пусто даже на рабочем конфиге. Проверять сам файл:
grep -n -E "gpt-5.6|group_allowed_chats|disabled_toolsets|allow_from" ~/.hermes/config.yaml
#   + вручную сверить каждый ключ config.yaml с cli-config.yaml.example пиновой версии.

# 6. systemd user-сервис (переживает выход из SSH). БЕЗ `-p` — см. врезку про профили выше.
hermes gateway install
sudo loginctl enable-linger $USER  # ОБЯЗАТЕЛЬНО: иначе сервис умрёт при logout
hermes gateway start
hermes gateway status              # active; в логах — коннект MCP aimash + список 12 tools
#   логи:  hermes gateway list  → имя юнита дефолтного профиля;
#          journalctl над root-SSH требует user-шины:
#          export XDG_RUNTIME_DIR=/run/user/0 && journalctl --user -u <имя-юнита> -f
#          (подробнее про логи/linger — OPERATIONS.md §0/§3)

# 7. Быстрая проверка MCP-коннекта до Telegram.
hermes mcp test aimash             # должен отдать 12 инструментов
#   (`aimash` здесь — имя MCP-сервера из config.yaml, НЕ профиля)
```

> Юзер systemd-сервиса Hermes должен иметь доступ к `docker` (группа `docker` или root) — иначе
> `docker exec aimash-bot …` из MCP-конфига упадёт с правами.

**Откат:** `hermes gateway stop`. Текущий бот продолжает работать (разные процессы).

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
5. **Перечитать конфиг:** `hermes gateway restart`.
6. **Короткий алиас вместо длинного `@username`** — `gateway.platforms.telegram.extra.mention_patterns`,
   список **регулярок** (эталон из тестов сборки: `["^\s*гермес\b"]`), работает вместе с
   `require_mention: true`, не вместо него.
   ⚠️ **На VPS 21.07 алиас не сработал** — ни «гермес», ни «бот» бота не будили. Ключ адаптеру известен
   (`plugins/platforms/telegram/adapter.py:7419` читает `config.extra["mention_patterns"]`), причина не
   установлена: в `_message_matches_mention_patterns` (:7631) соседствуют `_mention_patterns` и
   `_mention_pattern` — похоже на опечатку сборки, тогда wake-word падает в `AttributeError`.
   Рабочие обходные пути: **reply** на сообщение бота считается обращением; в группе с одним ботом
   слэш-команды идут без `@суффикса`. Сам `@username` BotFather менять не даёт — только новый бот
   (новый токен, заново privacy off, заново добавить в группу).

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
