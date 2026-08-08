# Ранбук: поднять Hermes (Контур A) рядом с боевым ботом на VPS

READ-пилот пивота Aimash → Hermes. Hermes-агент отвечает в Telegram и через
MCP-сервер `aimash` (пакет `mcp_server/`) всегда отдаёт 27 READ-инструментов + 1 META. При явном
`HERMES_WRITE_ENABLED=true` он добавляет 58 agent-first PLAN/state/action + 1 approval execute через HMAC trusted Telegram transport;
при false модуль WRITE физически не импортируется.

**Где что написано:** нормативный канон — три исходных DOCX заказчика; их текстовое зеркало —
[`/ТЗ.md`](../../ТЗ.md). Implementation profile и границы автономии определяются живым кодом,
конфигурацией и runbooks. Этот файл — только про установку.

**Топология v3.** Telegram принимает Hermes gateway. Aimash предоставляет отдельные контейнеры
`scheduler` и MCP; legacy aiogram poller отсутствует. Hermes запускает MCP через
`docker compose --profile mcp run --rm --no-deps -T mcp`, используя то же окружение Google Ads/БД
без зависимости от постоянно работающего bot-контейнера.

**Артефакты рядом:** `config.yaml` (эталон `~/.hermes/config.yaml`), `SOUL.md` (идентичность агента —
слот №1 системного промпта, эталон `~/.hermes/SOUL.md`), `hermes.env.example` (шаблон `~/.hermes/.env`),
`lint_config.py` (конфиг-линт К10 — **неизвестные ключи Hermes игнорирует молча**), `host-a/` (эталон
живого хоста + RUNBOOK), `RISK_REGISTER.md`, `plugins/aimash_probe` (проб доверенного канала метаданных
гейта), `PILOT_ROLLBACK.md` (границы capability pilot и проверяемый откат).

**Этот файл — только установка (RB-0…RB-3).** День-2 эксплуатация (что применяется вживую vs требует
restart, редеплой↔MCP-reconnect, логи, обновление/откат, бэкап, kill-switch, траблшутинг) — в
[`OPERATIONS.md`](OPERATIONS.md).

---

## RB-0. Зайти на VPS

```bash
# host — из CI-секрета VPS_SSH_HOST (текущее значение сверять перед подключением); user = VPS_SSH_USER
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

# 3. Первичная авторизация Codex — через интерактивный мастер (он же запускается при первом старте):
hermes model
#   Select provider/model — `gpt-5.6-terra` via `openai-codex`; следующий surface sync закрепит
#   это значение из `/opt/aimash/deploy/hermes/runtime_registry.yaml`.
#   Select terminal backend → Keep current (local).
#   Select platforms        → только Telegram (SPACE, ENTER).
#   Tools for CLI (тулсеты) → не снимать рабочие native tools. Terminal/Code/File/Browser остаются
#     доступны в CLI через `approvals: manual`. Surface sync задаёт отдельный узкий Telegram surface:
#     без host/code mutation tools, но с живыми typed Aimash MCP tools, web и делегированием.

# 4. Долить host-local настройки, которых нет в репозитории (dashboard/secrets); не заменять live YAML
#    шаблоном целиком. Model/delegation/tool policy, mcp_servers.aimash, plugin, SOUL и topic skill
#    выборочно и атомарно закрепляет sync_aimash_surface.py.
hermes config edit
#   Aimash surface/plugin/SOUL синхронизируются выборочно:
/usr/local/lib/hermes-agent/venv/bin/python /opt/aimash/deploy/hermes/sync_aimash_surface.py

# 4b. Идентичность агента → ~/.hermes/SOUL.md (слот №1 системного промпта, автозагрузка Hermes;
#     head/tail-усечение на context_file_max_chars, дефолт 20000 — эталон умещается с запасом).
#     Плейсхолдеров нет, секретов нет: копируется как есть, применяется со следующей сессии агента.
cp /opt/aimash/deploy/hermes/SOUL.md ~/.hermes/SOUL.md
#   Обычно вручную не нужно: sync_aimash_surface.py делает атомарную копию и backup.

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
hermes mcp test aimash             # 25 в READ-режиме, 65 при принятом WRITE-cutover
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
6. `require_mention: false`: обычный текст от пользователя из `group_allow_from` считается
   обращением к агенту без reply и `@username`. Это не открывает группу целиком: авторизация sender
   остаётся обязательной. `mention_patterns` для этого режима не нужен.

**Проверка П1:** allowlisted пользователь пишет в топик обычный текст «покажи статистику за неделю
по <аккаунт>» без reply/упоминания — агент отвечает живыми цифрами. Сообщение пользователя вне
`group_allow_from` остаётся без ответа.

---

## RB-3. OpenRouter: дневной потолок трат + атрибуция + kill-switch

Для READ-пилота — **рекомендация**; для delegation/WRITE (Фаза C, fan-out субагентов) **жёсткий потолок
— предусловие**, без него агентский цикл может сжечь бюджет без confirm-гейта (гейт защищает деньги
**Google Ads**, не трату **LLM**). Два ключа OpenRouter с РАЗНОЙ ролью — не путать:

1. **Жёсткий потолок (backstop, руки владельца — это и есть настоящая граница).** Тот inference-ключ,
   которым платит Hermes (`OPENROUTER_API_KEY`), в OpenRouter → **Settings → Keys** получает **Credit
   limit** `<USD>` + сброс **daily**. Срабатывает у провайдера независимо от нашего кода — переживает
   любой наш сбой. Число задаёт владелец. Рекомендация: бот и Hermes делят ОДИН ключ (единый спенд-пул),
   иначе софт-потолок (п.2) и `/activity` видят не всю трату.
2. **Софт-потолок (наш код, ниже агента).** `LLM_DAILY_COST_CAP_USD=<USD>` в env бот-процесса —
   `core/llm_budget.check_daily_cost_cap()` читает ЖИВУЮ дневную трату (`GET /key` `usage_daily`,
   кэш 60 с) и отказывает ДО дорогого прогона; с 2026-07-30 (BZ-4) энфорсится в `agent/router.chat` —
   единой точке наших LLM-вызовов. `0`/пусто ⇒ выкл в dev; **в prod автодефолт `10` USD** (значение D1 —
   замер 29.07 показал `limit=null` на живом ключе, серверного лимита не было). ⚠️ OpenRouter недоступен ⇒
   **fail-open** (пропускает с warning): это бюджетный рубеж, не security-гейт, жёсткий backstop — п.1.
   Ставить НИЖЕ числа из п.1.
3. **Атрибуция трат Hermes (opt-in, закрывает открытый хвост §20).** LLM-трафик Hermes идёт мимо нашего
   процесса — чтобы поднять его per-день/per-модель в `agent_runs`, заведи ОТДЕЛЬНЫЙ **management-ключ**:
   OpenRouter → **Provisioning API keys** → новый ключ → `OPENROUTER_PROVISIONING_KEY=<mgmt>` (обычный
   inference-ключ на `GET /activity` даёт 403). Опционально `OPENROUTER_KEY_HASH=<sha256 ключа Hermes>`
   — сузить `/activity` до конкретного ключа, если пул общий с ботом. Пусто ⇒ ридер тихо деградирует на
   `/key` (fail-soft), сшивка `origin='hermes'` спит.
4. **Kill-switch:** деактивация ключа в OpenRouter (account-wide — гасит все прогоны разом). Мягкий:
   `hermes gateway stop` (боевой бот жив).

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

MCP READ-слой (`mcp_server/`) отдаёт чистые редактированные конверты на всех 27 READ-инструментах, но
прогоны РАЗНОЙ силы, и разница существенна: 15 инструментов прогнаны **вживую** на Draft `7753643025`
локально, 8 добавленных 30.07 (кампании · таргетинг · настройки · стратегия ставок · разбивки ·
аудитории · квота) — **только офлайн** (`tests/test_mcp_read_smoke.py`, SDK подменён). Живой Draft-прогон
этих восьми не делался: конверт и сериализация доказаны, ответ реального API — нет.
Артефакты этого каталога приезжают на сервер штатным авто-деплоем
(push master → CI green → `git reset --hard` → `docker compose up -d --build` → selective Hermes sync).
