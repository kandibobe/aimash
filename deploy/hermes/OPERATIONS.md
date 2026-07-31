# Ранбук эксплуатации Hermes (Контур A) — запуск, редеплой, настройка, траблшутинг

День-2 операции Hermes-агента READ-пилота. **Первичная установка — в [`README.md`](README.md)** (RB-0…RB-3);
здесь — то, что после установки: жизненный цикл сервиса, применение изменений конфига, взаимодействие с
авто-деплоем боевого бота, обновление/откат, бэкап, kill-switch, диагностика.

> **Дисциплина К10 (читать первым).** Hermes **молча игнорирует неизвестные/опечатанные ключи** конфига —
> «на вид работает, по факту нет». `hermes config check` этого **НЕ** ловит (он про «missing or stale», не про
> валидацию). Все факты ниже сверены с офиц. доками, но доки — ветка `main`, а версия на VPS **измерена**
> (V1, 29.07.2026): `hermes version` → `Hermes Agent v0.19.0 (2026.7.20) · local 3ef6bbd2` = пин проекта
> (`deploy/hermes/PIN.json`). Прежняя редакция этой шапки говорила `v0.17` — цифра снята замером, а не выбором.
> **Ключи повторно сверены 31.07.2026** с исходниками тега `v2026.7.20`: удалены мёртвые
> `model_routing`, `openrouter.extra_headers`, `browser.use_gateway` и дубли порогов guardrail. Линт обоих
> эталонов проходит с `--strict` без предупреждений; CI и pre-commit теперь тоже строгие. Платформа 0.x с
> миграциями конфига между версиями + К10 всё равно требует: **после любой правки конфига** проверяй, что значение реально
> подхватилось — `hermes config show`; **перед добавлением ключа** сверяй написание с `cli-config.yaml.example`
> пиновой версии и с `hermes <cmd> --help`. Теги: [Certain] — verbatim из доков, [Likely] — сильный вывод/один
> источник, [?] — не верифицировано, проверить на бинаре.

---

## 0. Профиль и имя systemd-юнита — узнать ДО любых операций

Все `systemctl`/`journalctl` операции требуют точного имени юнита, а оно зависит от профиля. Не хардкодь —
**обнаружь**:

```bash
hermes gateway list        # профили и состояние gateway каждого (+ PID)
hermes config path         # -> должно быть /root/.hermes/config.yaml (дефолтный профиль)
hermes config env-path     # -> /root/.hermes/.env
```

- `gateway install` создаёт **systemd `--user`**-юнит `hermes-gateway-<profile>.service` в
  `~/.config/systemd/user/` [Certain]. Для дефолтного профиля (конфиг в `/root/.hermes/`) суффикс профиля
  берётся из `gateway list`; точный дефолтный суффикс в доках не пинован [Likely] — **бери из вывода `list`**.
- Если ставил под именованный профиль — во **все** команды добавляй `-p <profile>` (глоб. флаг ДО субкоманды:
  `hermes -p <profile> gateway status`), и юнит будет `hermes-gateway-<profile>.service`.

> Ниже команды даны для дефолтного профиля. Подставь `-p <profile>` и реальное имя юнита, если у тебя именованный.

### Гоча: `--user`-юнит от root по SSH

Non-login SSH-сессия root не имеет user-шины → `systemctl --user` / `journalctl --user` упадут
`Failed to connect to bus` [Likely]. Экспортируй runtime-dir (или зайди `machinectl shell root@`):

```bash
export XDG_RUNTIME_DIR=/run/user/0
systemctl --user list-units 'hermes-gateway-*'
```

---

## 1. Что применяется вживую, что требует restart (КРИТИЧНО)

Правка `~/.hermes/config.yaml` **не** перечитывается работающим gateway'ем для общего случая — он читает конфиг на
старте [Certain]. Матрица применения:

| Изменение | Как применить |
|---|---|
| `model.context_length`, `compression.*` | **Hot-reload** — со следующего сообщения, без рестарта [Certain] |
| Смена `model.provider`/`model.default` | Новые сессии подхватят; живым — `hermes gateway restart` (issue #13146) [Certain] |
| `mcp_servers.*` / `plugins.enabled` (наш `aimash`) | `deploy/hermes/sync_aimash_surface.py` + `hermes gateway restart` [Certain] |
| `gateway.platforms.telegram.*` (auth, топики, mention) | `hermes gateway restart`; **добавление** нового топика подхватывается на след. cache-miss, **изменение/удаление** привязки — только рестарт [Likely] |
| `.env` (ротация ключей) | `/reload` (сессионный, только CLI) или `hermes gateway restart` для gateway-wide [Certain] |
| `agent.disabled_toolsets`, `approvals.*` | `hermes gateway restart` [Likely] |

**На headless-сервисе `/reload-*` слэш-команды малоприменимы** — им нужен интерактивный чат-surface, а его у
systemd-сервиса нет; его scope (сессия vs весь gateway) не документирован [Likely]. **Надёжный путь для всего,
кроме hot-reload-ключей — `hermes gateway restart`.**

```bash
hermes gateway restart && hermes gateway status
# fallback, если CLI конфликтует с systemd-супервизией:
#   XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart hermes-gateway-<profile>.service
```

> `hermes config set <key> <value>` пишет в файл (ключи API → в `.env`, прочее → в `config.yaml`), но **не
> применяет вживую** — всё равно нужен restart [Certain].

---

## 2. Жизненный цикл gateway

```bash
hermes gateway install     # зарегистрировать systemd/launchd-сервис (нужно ОДИН раз)
hermes gateway start        # запустить установленный сервис
hermes gateway status       # состояние (первичная проверка «жив ли»)
hermes gateway restart      # перечитать конфиг (см. §1)
hermes gateway stop         # остановить (откат: боевой бот продолжает работать — разные процессы)
hermes gateway list         # все профили + running-состояние + PID
hermes gateway run          # foreground (WSL/Docker/Termux; НЕ для нашего systemd-случая)
hermes gateway uninstall    # удалить сервис
```

- Модель: `install` регистрирует персистентный сервис; `start/stop/restart/status` — операции над ним (сначала
  install, потом start); `run` — разовый foreground [Certain].
- `--all` у `start/restart/stop` действует на **все** профили (удобно после `hermes update`) [Certain].
- `--no-supervise` / `--external-supervisor` — только для `gateway run` внутри Docker/обёрток; у нас нативный
  systemd — **не передавать** [Certain].
- Легаси-юнит `hermes.service` от pre-rename-установок чистится `hermes gateway migrate-legacy [--dry-run]`
  (профильные юниты не трогает) [Certain].

### Обязательно: linger (иначе сервис умрёт)

`--user`-сервис убивается при выходе из login-сессии и **не стартует при загрузке** без linger [Certain]. Это же
роняет встроенный in-process cron-тикер (см. §10).

```bash
loginctl enable-linger root
loginctl show-user root -p Linger      # ДОЛЖНО быть Linger=yes
```
Проверить выживание: `hermes gateway start` → выйти из SSH → зайти заново → `hermes gateway status` = active.
После ребута тоже проверить.

---

## 3. Карта логов

| Что | Где |
|---|---|
| Основной лог gateway | `~/.hermes/logs/gateway.log` (`tail -f`) |
| Лог агента (в т.ч. warning'и фолбэка aux-провайдеров) | `~/.hermes/logs/agent.log` |
| Вывод cron-джоба (доказательство, что отработал) | `~/.hermes/cron/output/{job_id}/{timestamp}.md` |
| systemd-журнал юнита | `XDG_RUNTIME_DIR=/run/user/0 journalctl --user -u hermes-gateway-<profile>.service -f` |

> Плоский `journalctl -u hermes-gateway…` **без** `--user` для user-юнита молча ничего не покажет [Likely].

---

## 4. Проверка корректности (health-чеклист)

```bash
hermes version                       # ВЕРСИЯ = субкоманда, НЕ `--version` [Certain]
hermes config show                   # эффективный конфиг: model — mapping? mcp_servers.aimash есть? approvals: manual?
hermes gateway status                # active
hermes mcp list                      # среди серверов — aimash
hermes mcp test aimash               # тест КОННЕКТА к MCP (см. кавеат ниже)
hermes doctor --fix                  # health self-check с авто-фиксом [Likely, single-source — сверь `--help`]
```

**Кавеат `mcp test`:** доки подтверждают, что он тестирует **коннект**, но не сказано, что печатает список/счётчик
инструментов [Likely]. Чтобы позитивно убедиться, что поверхность жива — сравнить `Tools discovered`
с `mcp_server.server.expected_tool_names()` (сейчас 68: 25 READ + 42 PLAN + 1 WRITE), затем в чате спросить агента
«какие MCP-инструменты доступны», либо смотреть стартовый баннер в `gateway.log`. MCP-инструменты регистрируются с
префиксом `mcp_<server>_<tool>`.

### ПРЕДУСЛОВИЕ, которое ломает всё тихо: `mcp_server` в ЖИВОМ образе

`mcp_server/` вшит в образ `aimash-bot` на **build-time** (Dockerfile `COPY . .` + editable install), не монтируется
рантайм. **Наличие в `origin/master` ≠ наличие в РАБОТАЮЩЕМ контейнере** — нужен `docker compose up -d --build`
**после** мержа. Если последний билд старше мержа — каждый Ads-вызов падает `No module named mcp_server`, а Hermes
при этом выглядит здоровым. Прежде чем настраивать/винить Hermes — прогони на хосте:

```bash
docker exec -i aimash-bot python -c "import mcp_server; print('mcp_server OK')"
# и standalone-смоук самого MCP, независимо от Hermes (говорит ли по stdio):
docker exec -i aimash-bot python -m mcp_server   # должен подняться и ждать stdio (Ctrl-C выйти)
```

### Живой E2E

В форум-топике супергруппы с упоминанием бота: «покажи статистику за неделю по &lt;аккаунт&gt;» → числа из
`code_numbers` MCP, не из головы модели.

WRITE проверять только на Draft `7753643025`: попросить изменить приостановленную тестовую кампанию,
убедиться, что пришёл полный diff с кнопками `✅ Подтвердить / ✏️ Изменить / ❌ Отмена`, и нажать ✅.
Отдельно проверить fallback: новый черновик подтвердить обычным Telegram reply «да» на всю карточку.
Selected quote, ответ без reply, reply другого пользователя, повторное «да» и подмена
account/args обязаны отказать. После успеха сверить audit-row и повторным READ фактическое значение.

---

## 5. Редеплой боевого бота ⟷ Hermes (важнейшее сопряжение)

Авто-деплой (`docs/DEPLOYMENT.md`): push master → CI → SSH → `git reset --hard origin/master` →
`docker compose up -d --build`.

**Что редеплой НЕ трогает:** `~/.hermes/**` вне `/opt/aimash` → конфиг Hermes, `.env`, модель, `OPENROUTER_API_KEY`
переживают редеплой; сам gateway и его cron-тикер продолжают работать [Certain].

**Что редеплой ЛОМАЕТ:** `docker compose up -d --build` **пересоздаёт** контейнер `aimash-bot`. MCP-сервер Hermes —
это stdio-ребёнок `docker exec -i aimash-bot python -m mcp_server`, привязанный к прежнему экземпляру контейнера.
После пересоздания пайп **мёртв**; **авто-респавн на broken-pipe в доках НЕ гарантирован** [Certain — про
недокументированность]. Документирован только opt-in-респавн по `idle_timeout_seconds`/`max_lifetime_seconds` — и
он про memory-recycle, не про crash-recovery [Certain].

**Следствие — после КАЖДОГО редеплоя** (когда `aimash-bot` снова healthy):

```bash
docker exec -i aimash-bot python -c "import mcp_server" && \
hermes gateway restart && \
hermes mcp test aimash
```

> ✅ **Reconnect автоматизирован 31.07.2026.** Замер auth-журнала подтвердил, что production deploy входит root —
> тем же пользователем, которому принадлежит `hermes-gateway.service`. SSH-шаг CI после healthcheck обоих
> контейнеров синхронизирует только Aimash surface/plugin/SOUL (не затирая host-local config), проверяет
> соответствие WRITE-импорта feature-flag, вычисляет ожидаемое число инструментов из реестра,
> перезапускает gateway, выполняет `hermes mcp test aimash` и валит deploy при несовпадении числа
> или `409 Conflict` в любом Telegram-поллере. Смена `VPS_SSH_USER` с root теперь намеренно красит deploy, пока
> владение gateway не будет перенесено явно.

---

## 6. Смена модели / провайдера / тулсетов после установки

**Модель/провайдер** — канонический источник истины: `/opt/aimash/deploy/hermes/runtime_registry.yaml`. Любой live pin для cron или шаблонный deploy-config, который расходится с registry, должен считаться исключением и быть явно помечен. Изменение runtime применять только после сверки с registry:
```bash
hermes model                         # интерактивно: провайдер/модель — сверять с runtime_registry.yaml
hermes gateway restart               # применить к живым сессиям
hermes config show                   # убедиться, что model — mapping, не скаляр
```
- В чате разово: `/model <name>` (сессионно), `--global` — персист в конфиг, `--once` — один ход [Certain].
- **Пин обязателен:** держи явный `model.default`. Пустая модель резолвит «silent default» из **живого,
  ротируемого сервером** model-catalog-манифеста → активная модель может смениться без правки конфига [Certain].
- **Ролевой сплит** (§15 sol/luna/flash-lite) — через top-level блок `auxiliary:` (11 слотов: `vision`,
  `web_extract`, `compression`, `skills_hub`, `mcp`, `approval`, `title_generation`, `triage_specifier`, …), НЕ
  через под-ключи `model.*` [Certain]. Каждый слот: `provider`/`model`/`base_url`/`api_key`; дефолт слота
  `provider: auto` = «брать основную модель». **Сверь имена слотов с `cli-config.yaml.example` перед вписыванием —
  опечатка молча инертна.**
- **Фолбэк основной модели — opt-in**, top-level `fallback_providers:` (или `hermes fallback`); авто-фолбэка на
  Nous Portal для основной модели нет [Certain]. Учти: любая смена модели в ходе сессии (включая фолбэк) **сбрасывает
  prompt-cache** → след. сообщение перечитывает весь контекст по полной цене [Certain].
- ⚠️ `~vendor/model` («latest» через тильду) — **миф**, в Hermes такого синтаксиса нет; «latest» = провайдерные
  moving-алиасы (напр. `gemini-flash-latest`). Тильду не писать [Certain].

**Тулсеты.** Доступность инструмента — **жёсткий гейт** `agent.disabled_toolsets` / `tools.include|exclude`, а НЕ
проза в памяти агента (issue #26568) [Certain]. Гоча: один инструмент может входить в **несколько** тулсетов — чтобы
погасить, выключи **все** тулсеты, которые его дают (напр. `web_search` живёт и в `web`, и в `browser`), и начни
**свежую** сессию [Certain]. После правки — `hermes gateway restart`.

⚠️ **Харднинг тулсетов не переживает пересборку конфига — накатывать процедурой, а не руками.** 24.07 поверхность
закрыли вручную, 27.07 конфиг развалился (авария со слагом модели) и был пересобран из дефолта, 29.07 замер показал
`terminal`/`file`/`code_execution`/`computer_use` снова **включёнными** на боевом gateway. Восстановление —
`python scripts/hermes_restore_toolsets.py` (`--dry-run` покажет команды): список берёт из
[`config.yaml`](config.yaml) (тот же, что проверяет `lint_config.check_toolset_allowlist`), снимает бэкап живого
конфига, гасит, рестартует gateway и **проверяет результат по `hermes tools list`** — при любом лишнем включённом
тулсете возвращает ненулевой код и печатает команду отката.

**MCP-серверы тоже закрываются от разрешённого.** В `vps-read` допустимы только `aimash` и `tavily`, и у каждого
обязателен непустой `tools.include`; отсутствие списка означает «все инструменты», а не «ни одного». 31.07.2026
live gateway именно так публиковал весь GitHub MCP, включая мутации. Сервер удалён, а
`lint_config.check_mcp_server_allowlists` теперь делает такой дрейф ошибкой до рестарта.

---

## 7. Обновление Hermes и откат

**Нативного пина версии НЕТ** — `hermes update` тянет git `main` и **авто-рестартует** gateway (незапланированный
обрыв поллера/cron на проде) [Certain]. Политика для прода:

```bash
hermes version                       # что стоит сейчас
hermes update --check                # превью коммитов БЕЗ изменений и рестарта
# держать версию = просто НЕ запускать `hermes update`
```

Если решил обновлять:
```bash
hermes update                        # snapshot → git pull main → syntax-validate (+auto-rollback) → uv pip install → миграция опций → авто-рестарт
hermes config check                  # missing/stale опции
hermes config migrate                # интерактивно добавить новые опции
hermes gateway status && hermes mcp test aimash
```
- После апдейта **перепроверь**, что наши ключи (`mcp_servers.aimash`, `agent.disabled_toolsets`) всё ещё
  распознаются: `hermes config show` (К10 — переименование ключа между 0.x оставит настройку тихо сброшенной).
- `hermes migrate <type>` — отдельная команда для deprecated-моделей/настроек (не путать с `config migrate`) [Likely].

**Откат** (нативного пина нет → вручную; install-каталог обычно `~/.hermes/hermes-agent`, уточни на месте):
```bash
cd ~/.hermes/hermes-agent
git checkout <commit-hash>
uv pip install -e ".[all]"
hermes gateway restart
```

---

## 8. Бэкап `/root/.hermes` (отдельно от Compose)

`/root/.hermes` вне `/opt/aimash` → то же свойство, что даёт выживание при редеплое, **исключает** его из
Compose-бэкап-сервиса. Внутри — секреты (`.env`: `OPENROUTER_API_KEY`, `TELEGRAM_BOT_TOKEN`), `cron/jobs.json`,
`sessions/` и **`state.db`** — история топиков и сессий, то есть фактическая история решений агента. В Postgres
её нет: наш `audit_log` знает про выполненные операции Google Ads, но не про диалог, который к ним привёл.
Потеря невосстановима.

Скрипт: [`scripts/backup_hermes.sh`](../../scripts/backup_hermes.sh) — host-скрипт, **не** сервис Compose
(примонтировать `/root/.hermes` в сайдкар = положить `.env` Hermes в `./backups` рядом с репозиторием,
правило 5). Берёт консистентный снимок `state.db`: системным `sqlite3 .backup`, а при отсутствии CLI —
через stdlib `sqlite3` из пинованного Hermes venv. Неконсистентный cp+WAL остаётся только аварийным
fallback, если недоступны оба механизма:

```bash
sh /opt/aimash/scripts/backup_hermes.sh          # → /root/hermes-backups/hermes-<ts>.tgz, права 600
```

**Ставить таймером, а не в host-crontab.** Ровно на этом хосте бэкап Postgres переехал в Compose-сайдкар
потому, что host-cron «часто НЕ был запущен» (`docker-compose.yml`, комментарий C1) — тот же провал повторится
здесь. Версионированные unit-файлы — `deploy/hermes/hermes-backup.{service,timer}`; production deploy
устанавливает их, включает timer, запускает контрольный backup и проверяет наличие `.env` + `state.db`
в архиве. Ручное восстановление timer после аварийного обслуживания:

```bash
install -m 0644 /opt/aimash/deploy/hermes/hermes-backup.service /etc/systemd/system/
install -m 0644 /opt/aimash/deploy/hermes/hermes-backup.timer /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now hermes-backup.timer
systemctl list-timers hermes-backup.timer      # NEXT/LEFT — проверить, что таймер реально взведён
```

**Проверка, что бэкап живой** (без неё это папка с файлами, а не бэкап):

```bash
tar tzf "$(ls -1t /root/hermes-backups/*.tgz | head -1)" | grep -E 'state\.db|\.env'   # оба должны быть
```

⚠️ Архив несёт секреты в открытом виде: права 600, каталог `/root/hermes-backups` — 700. Выгрузка с хоста —
**только шифрованной** (`gpg`/`age`, заготовка в хвосте скрипта). НЕ коммитить, НЕ в общие логи. Локальные
архивы гибнут вместе с сервером — вывоз в отдельное хранилище остаётся за владельцем.

### Инфраструктурные уведомления в общий топик

`scheduler` не может сообщить о собственном падении. Поэтому `aimash-ops-watch.timer` раз в минуту
запускает host-level [`scripts/ops_alert.py`](../../scripts/ops_alert.py) и сравнивает состояние с
предыдущим атомарным snapshot в `/var/lib/aimash-ops-watch/state.json`. Он уведомляет о:

- остановке, unhealthy-состоянии, рестарте и восстановлении `aimash-bot`, `aimash-scheduler`,
  `aimash-pg`, `aimash-backup`;
- остановке/смене PID/восстановлении `hermes-gateway.service`;
- неактивном `hermes-backup.timer`, заполнении диска ≥85%/≥95% и Telegram `409 Conflict`;
- успешном или проваленном production deploy (отдельный сигнал из CI после live-гейтов).

В серверном `/opt/aimash/.env` обязательны `OPS_ALERT_CHAT_ID`; для forum supergroup задаётся
`OPS_ALERT_THREAD_ID` только для отдельного forum-топика; для General он остаётся пустым. Токен повторно
не хранится: используется `TELEGRAM_BOT_TOKEN` Hermes из `/root/.hermes/.env` (не legacy-бота из
`/opt/aimash/.env`), в лог/состояние он не попадает. Первый тик только сохраняет baseline,
недоставленный transition состояние не продвигает и повторяется на следующей минуте.

```bash
systemctl status aimash-ops-watch.timer --no-pager
systemctl start aimash-ops-watch.service          # ручной тик
journalctl -u aimash-ops-watch.service -n 50 --no-pager
python3 /opt/aimash/scripts/ops_alert.py send \
  --severity info --title "Aimash alert test" --body "Проверка общего топика"
```

---

## 9. Kill-switch и лимиты трат

`confirm`-гейт защищает бюджет **Google Ads**, а не трату **LLM** (Hermes → OpenRouter напрямую, мимо нашего кода).
Два рубежа по стоимости LLM (полная процедура — [`README.md`](README.md) RB-3):

- **Жёсткий (backstop):** Credit limit `<USD>` + сброс `daily` на самом inference-ключе Hermes
  (`OPENROUTER_API_KEY`). Срабатывает у провайдера независимо от нашего кода — настоящая граница.
- **Мягкий (наш код):** `LLM_DAILY_COST_CAP_USD=<USD>` → `core/llm_budget.check_daily_cost_cap()`
  читает живую трату (`GET /key` `usage_daily`, кэш 60 с) и отказывает ДО дорогого прогона. С 2026-07-30
  (BZ-4) энфорсится в `agent/router.chat` — единой точке наших LLM-вызовов; в prod `0` автодефолтится
  в `10` USD (значение D1), в dev остаётся выкл. OpenRouter недоступен ⇒ fail-open (жёсткий рубеж
  выше — backstop). Предусловие spend-cap для delegation (Фаза C).
- **Kill-switch:** деактивация ключа в OpenRouter (account-wide — гасит все прогоны). Плюс «мягкий»: `hermes gateway
  stop` (боевой бот при этом жив).
- Per-день/per-модель траты Hermes поднимает ридер `core/or_activity` (`GET /activity`, нужен
  `OPENROUTER_PROVISIONING_KEY`) в `agent_runs` — открытый хвост §20 закрывается атрибуцией, не только логом.

---

## 10. Cron внутри gateway (даже если тулсет `cronjob` выключен)

«cron» в ярлыке `Install gateway service (messaging + cron)` — **не** отдельный сервис и **не** OS-crontab: сам
демон gateway крутит cron-шедулер в фоновом потоке, тикая каждые 60с [Certain]. Важное:

- Шедулер **исполняет** уже заведённые джобы **независимо** от тулсета `cronjob` — тулсет лишь даёт агенту
  *создавать* джобы из чата; выключение тулсета не останавливает исполнение существующих [Likely].
- In-process тикер работает, **только пока gateway жив** → ещё одна причина для linger (§2). `cron.provider: chronos`
  — это для **hosted** Nous-gateway'ев (scale-to-zero через webhook); на нашем self-hosted VPS **не ставить** [Certain].
- Проверить, что джоб реально отработал — по таймстемпам `~/.hermes/cron/output/{job_id}/`.

---

## 11. Траблшутинг (симптом → причина → фикс)

| Симптом | Причина | Фикс |
|---|---|---|
| **409 Conflict** `terminated by other getUpdates` | Два поллера на один токен | Один gateway на токен. **Токен Hermes ОБЯЗАН отличаться от токена `aimash-bot`** (иначе постоянный 409). `hermes gateway list` — нет ли лишнего; `curl .../bot<TOKEN>/getMe` |
| Ads-вызовы падают `No module named mcp_server` | Живой образ старше мержа `mcp_server/` | `docker compose up -d --build` **после** пуша; проверка §4 |
| MCP-tools не появились / `mcp test` не коннектит | Образ без deps / контейнер лежит / stale-пайп после редеплоя | §4-предусловие; после редеплоя `hermes gateway restart` + `hermes mcp test aimash` (§5) |
| Tools есть, но не вызываются | `tools.include/exclude` / коннект | Проверить `tools.*`, логи; `/reload-mcp` |
| Провайдер 401 / «API Key Not Working» | Ключ/провайдер mismatch (OpenAI-ключ на OpenRouter) | `hermes config show` → `hermes model`; `hermes chat -q "hi" --model <m>` |
| «The chat is not a forum» | Топики не включены на стороне клиента | Включить Topics в группе/DM → `hermes gateway restart` |
| Бот молчит без упоминания | `require_mention: true` / privacy mode | Упомянуть `@bot`; либо BotFather `/setprivacy → Disable` + **удалить и заново добавить** бота (privacy кэшируется при join) |
| Выключенный тулсет всё ещё зовётся | Инструмент в неск. тулсетах / stale-сессия (#26568) | Погасить **все** тулсеты с этим инструментом; `hermes update`; свежая сессия |
| Gateway умирает при выходе из SSH / не встаёт после ребута | Нет linger | `loginctl enable-linger root` (§2) |
| `systemctl --user` → `Failed to connect to bus` | Non-login SSH как root | `export XDG_RUNTIME_DIR=/run/user/0` (§0) |

**Общий путь «бот не отвечает»:** `hermes gateway status` → `start` если лежит → `tail -50 ~/.hermes/logs/gateway.log`
→ проверить токен `curl "https://api.telegram.org/bot<TOKEN>/getMe"` (токен НЕ вставлять в общие логи/историю).

---

## 12. Прогон V1–V22 — замер рантайма Hermes прибором (одноразовый, до WRITE)

**Зачем.** Весь блок утверждений о рантайме Hermes — доходят ли доверенные метаданные Telegram до
нашего кода, может ли хук переписать аргумент модели, что делает gateway при падении хука, каков
фактический таймаут — сегодня **[Непроверено]**: `HERMES_SPEC.md:1234` сам держит их в списке
«проверить на живой установке». На них завязан **критерий (a) §8.4** — без доверенного канала
подтверждение подделывается моделью, и write-слой в прод не выпускается. Прогон отвечает на это
измерением, а не рассуждением.

**Правило чтения результатов.** Каждый V-шаг — это **замер**, у него нет «правильного» ответа.
Записывай ФАКТ в таблицу внизу. «Ожидание» в тексте — гипотеза, которую шаг проверяет; расхождение с
ней это результат, а не сбой.

**Прибор — две времянки, обе снимаются шагом V18:**
- `mcp_server/probe.py` — отдельный MCP-сервер `aimash-probe` с единственным `probe_echo`. Возвращает
  ровно то, что до него доехало. Ads не читает, в БД не ходит, мутаций не делает; в `READ_TOOL_FUNCS`
  его нет умышленно — иначе прибор менял бы измеряемую систему (упал бы assert И4 и
  `tests/test_hermes_isolation.py:70`, `len(READ_MCP_TOOLS) == 12`).
- `deploy/hermes/plugins/aimash_probe/` — хук-наблюдатель (`plugin.yaml`, `hook.py`). Только логирует;
  перезапись аргументов включается явно `AIMASH_PROBE_REWRITE=1`, падение — `AIMASH_PROBE_RAISE`.
  ⚠️ Плагин висит на **живом** gateway: ставить только пока пилот READ-only (мутаций в MCP нет by
  construction), снимать сразу после прогона.

### A. Версия и контракт — до всего остального (V1–V4)

```bash
hermes version                 # V1 — субкоманда, НЕ `--version`; записать фактический номер
hermes config show             # V2
hermes plugins                 # V3 — список обнаруженных плагинов и их состояние (вкл/выкл)
```

| V | Что меряем | Как | Ожидание (гипотеза) |
|---|---|---|---|
| **V1** | Версия бинаря | `hermes version` (**субкоманда**, не `--version`) | ✅ **ЗАКРЫТО живьём 29.07.2026:** `Hermes Agent v0.19.0 (2026.7.20) · local 3ef6bbd2` — совпало с пином проекта (`SPEC.md:777`, `CLAUDE.md`, `deploy/hermes/PIN.json`), противоречившая цифра `v0.17` из шапок снята. Три решения об объёме (`SPEC.md:225`, `:661`, `:785`) разблокированы. ⚠️ **Остаток замера:** «номер подтверждён» не равно «ключи пересверены» — аттестация ключей делалась против доков v0.17, пересверка по К10 не проводилась (`lint_config.py` → 10 ключей `[НЕ АТТЕСТОВАН]`) |
| **V2** | Эффективный конфиг | `hermes config show` | `model` — mapping (`provider`+`default`), `approvals: manual`, `session_search` в `disabled_toolsets`, `mcp_servers.aimash` есть, `tools.include` — ровно 12 имён |
| **V3** | Схема манифеста плагина и факт включения | `hermes plugins` | Схема **больше не вслепую**: сверена с `plugins.md` на пиновом теге (манифест = `name`/`version`/`description`, код в `__init__.py`, подписка только из `register(ctx)`). Здесь проверяем ФАКТ: `aimash_probe` в списке, и он **enabled**. Обнаружение показывает плагин даже выключенным, а хуки при этом не грузятся — по К10 неверный ключ игнорируется молча |
| **V4** | Имена и сигнатуры хуков | доки/CLI пиновой версии (номер даёт V1) | **Закрыто по исходникам на теге `v2026.7.20`, эмпирике осталась только сверка «доки против кода»** (детали и цитаты — в шапке `plugins/aimash_probe/__init__.py`): `pre_gateway_dispatch(event, gateway, session_store, **kwargs)` → `None`/`{"action": "skip\|rewrite\|allow"}`; `pre_tool_call(tool_name, args, task_id, **kwargs)` → только `{"action": "block", "message": …}`, **идентичности актора в сигнатуре НЕТ ВООБЩЕ**; `VALID_HOOKS` пиновой версии — ровно **23 имени** (дословно снят в `__init__.py:_VALID_HOOKS` из `hermes_cli/plugins.py`), прибор подписан на все 23 и печатает `unknown`/`not_covered`; `MessageEvent` НЕСЁТ `reply_to_message_id` (исходник `gateway/platforms/base.py`) ⇒ открытый вопрос `SPEC.md:1435` закрывается положительно, идентичность лежит в `event.source` (`user_id`, `chat_id`, `chat_type`, `thread_id`). Прибор печатает фактические `dir(event)`/`dir(source)` — расхождение доков с кодом будет видно строкой, а не выведено из молчания. Строки `tool_request` в проекте нет — если всплывёт, это ошибка памяти, не источник |

### B. Установка прибора (V5–V6)

```bash
# V5: временная ВТОРАЯ запись в ~/.hermes/config.yaml (боевую `aimash` НЕ трогать):
#   mcp_servers:
#     aimash-probe:
#       command: docker
#       args: ["exec", "-i", "aimash-bot", "python", "-m", "mcp_server.probe"]
#       timeout: 400          # выше _MAX_DELAY_SECONDS=300 — иначе V13 упрётся в свой же таймаут
hermes gateway restart && hermes mcp list && hermes mcp test aimash-probe

# V6: плагин. ДВА ШАГА, второй обязателен — каталога НЕДОСТАТОЧНО.
cp -r deploy/hermes/plugins/aimash_probe ~/.hermes/plugins/

# ⛔ Без этого блока в ~/.hermes/config.yaml плагин ВИДЕН, но не подписан ни на что:
#    «General plugins and user-installed backends are disabled by default».
#      plugins:
#        enabled:
#          - aimash_probe
#    (эквивалент через CLI: hermes plugins enable aimash_probe)

hermes gateway restart
hermes plugins                            # aimash_probe обязан быть в списке И enabled
cat ~/.hermes/aimash_probe.log            # разбор строк — по лестнице ниже
```

> **V6 — самый важный негативный контроль всего прогона.** Пустой `aimash_probe.log` означает
> «плагин НЕ загрузился», а не «метаданных нет». По К10 (молчаливое игнорирование неизвестных
> ключей) это самый вероятный исход первой попытки. **Не идти дальше, пока лестница не дошла до
> `hooks_registered`.**

**Лестница чтения лога — снимает главную неоднозначность прогона.** Прибор пишет три маркера
подряд, и остановка на любом из них — конкретный диагноз, а не «данных нет»:

| Что в логе | Диагноз | Что делать |
|---|---|---|
| Пусто | Плагин не обнаружен **или** не внесён в `plugins.enabled` | Шаг 2 выше. **Не** писать «метаданные не доходят» |
| Только `module_imported` | Пакет импортирован, `register(ctx)` не вызван | Проблема точки входа/манифеста: проверить, что код в `__init__.py`, а не в `hook.py` |
| `register_called`, но нет `hooks_registered` | `register()` упал внутри (падение отключает плагин молча) | Смотреть `ctx_attrs` в строке `register_called` — фактический API контекста |
| `hooks_registered` с пустым `registered` и заполненным `failed` | Имена хуков не те **или** другая сигнатура `register_hook` | Текст ошибки лежит в `failed` поимённо |
| `unknown` непустой | Подписались на имя, которого нет в `VALID_HOOKS` **этого** бинаря | Рантайм принял его молча (`logger.warning`, не исключение) ⇒ молчание такого хука ничего не измеряет. Сверить с реальным `VALID_HOOKS` версии из V1 |
| `not_covered` непустой | В бинаре есть хук, на который прибор НЕ подписан | Перепись неполна: «событие не пришло» пока не факт. Дописать имя в `_HOOKS_FINGERPRINT` и перезапустить |
| `hooks_registered` есть, событий нет | Прибор жив, хук не срабатывает | Вопрос к триггеру (например, `require_mention: true` — в топике к боту надо обращаться с упоминанием), не к прибору |
| Есть `gateway_fields` | **Это и есть ответ V7** | Поля `event`/`source` — как они реально пришли; `event_attrs`/`source_attrs` — фактический состав объектов |

⛔ **Три системы хуков, и прибор живёт ровно в одной.** Их путают легко, а цена путаницы —
ложный отрицательный вывод по денежному пути:

| Система | Где объявляется | Точка входа | Ловит ли `pre_gateway_dispatch` |
|---|---|---|---|
| **Plugin hooks** ← наш прибор | `~/.hermes/plugins/<name>/` + имя в `plugins.enabled` | `__init__.py` → `def register(ctx)` → `ctx.register_hook(имя, cb)` | **Да** — это плагин-хук |
| Gateway hooks | `~/.hermes/hooks/<имя>/` — `HOOK.yaml` + `handler.py` (регистр значим) | функция `handle(event_type, context)` | **Нет.** События закрытым списком (`gateway:startup`, `session:*`, `agent:*`, `command:*`), `pre_gateway_dispatch` среди них не значится, а `context` не несёт ни `message_id`, ни `reply_to_message_id` |
| Shell hooks | блок `hooks:` в `~/.hermes/config.yaml` → shell-скрипты | JSON через stdin/stdout | Нет |

Прибор, положенный в `~/.hermes/hooks/`, реплай-метаданных не увидит **никогда** — и не потому,
что их нет, а потому что такого события в той системе не существует.

⛔ **Колбэки плагин-хуков зовутся СИНХРОННО, без `await`.** `async def` вернёт корутину, тело не
выполнится, лог останется пустым — третий способ намерить «метаданные не доходят» вместо «прибор
написан не так». У gateway-хуков (`handle`) `async` наоборот поддержан. Прибор — обычные `def`.

### C. Доверенный канал — критерий (a) §8.4 (V7–V9)

⚠️ **Команды здесь вынесены в код-блок намеренно.** В ячейке markdown-таблицы `|` обязан быть
экранирован (`\|`), и скопированная из сырого файла команда приезжает в шелл как `… \| tail -1` —
это литеральный аргумент `|`, а не пайп. Проверено эмпирически: такая форма молча не отрабатывает.
Внутри ``` экранирование не нужно, и копируется одинаково из сырого файла и из отрендеренного.

```bash
# V7 — послать боту ОБЫЧНОЕ сообщение в топике, затем:
grep gateway_fields ~/.hermes/aimash_probe.log | tail -1
# затем послать РЕПЛАЙ на чужое сообщение и повторить ту же команду — это отдельный замер.
# По исходнику на пиновом теге поле reply_to_message_id в MessageEvent ЕСТЬ; здесь проверяется,
# что Telegram-адаптер его РЕАЛЬНО ЗАПОЛНЯЕТ (объявленное поле и заполненное поле — разные вещи).

# V8 — в чате: «вызови probe_echo с note=v8», затем:
grep 'pre_tool_call.before' ~/.hermes/aimash_probe.log | tail -1

# V9a — НЕДОКУМЕНТИРОВАННАЯ мутация args на месте:
#       AIMASH_PROBE_REWRITE=1 → hermes gateway restart →
#       в чате: «вызови probe_echo с actor_chat_id=FORGED_BY_MODEL», затем:
grep 'pre_tool_call.rewrite' ~/.hermes/aimash_probe.log | tail -1

# V9b — ДОКУМЕНТИРОВАННОЕ вето (единственный контракт этого хука):
#       AIMASH_PROBE_BLOCK=probe_echo → restart → «вызови probe_echo с note=v9b»
grep 'pre_tool_call.block' ~/.hermes/aimash_probe.log | tail -1
```

| V | Что меряем | Что записать |
|---|---|---|
| **V7** | Доходят ли метаданные входящего и **заполнены ли** они | Значения `event.message_id`, `event.reply_to_message_id`, `event.reply_to_author_id`, `source.user_id`, `source.chat_id`, `source.chat_type`, `source.thread_id`. `<ABSENT>` = поля нет; `null` = поле есть и пустое — **это разные ответы**. Отдельной строкой — что приехало на реплае. Заодно `media_urls` (это же и проба R6) |
| **V8** | Виден ли хуку вызов инструмента вместе с контекстом сообщения | ⚠️ Ожидание уже известно по исходнику: gateway-контекста в `pre_tool_call` **нет** — сигнатура `(tool_name, args, task_id)`. Записать, чем связывать вызов с человеком: есть ли `task_id`, стабилен ли он в пределах хода, и совпадает ли с чем-либо из `pre_gateway_dispatch`. Это и есть работа по корреляции, а не «связать нечем» |
| **V9a** | Можно ли переписать аргумент модели мутацией `args` на месте | Что вернул `probe_echo` в `received.actor_chat_id`: `HOOK_SUBSTITUTED` ⇒ канал есть (недокументированный, полагаться с оговоркой); `FORGED_BY_MODEL`/`args_dict_not_found` ⇒ **ожидаемый исход**, не провал |
| **V9b** | Работает ли документированное вето `{"action": "block"}` | Отбит ли вызов и дошёл ли `message` до модели. **На этом механизме стоит К9-гард** — если вето не работает, у нас нет ни одного рантаймового способа запретить инструмент, кроме «не регистрировать вовсе» (правило 8) |

> **V9 переформулирован против прежней редакции — прежний замер проверял контракт, которого не
> существует.** По исходникам пиновой версии `pre_tool_call` получает `(tool_name, args, task_id)` и
> об акторе не знает ничего; «подставить сюда доверенный `actor_chat_id`» невозможно в принципе, и
> `FORGED_BY_MODEL` был бы прочитан как «Hermes непригоден», хотя это просто не то место.
> **Рабочий разрез:** личность снимается в `pre_gateway_dispatch` (`event.source.user_id` +
> `event.reply_to_message_id`) и кладётся в доверенное хранилище; `pre_tool_call` служит только вето.
> И в любом исходе остаётся в силе: хуки фейлятся OPEN ⇒ `execute_confirmed` обязан ОТКАЗАТЬ при
> отсутствии доверенной записи. Хук поставляет метаданные, решение выносит наш слой.

### D. Поведение при отказе (V10–V11)

| V | Что меряем | Как | Что записать |
|---|---|---|---|
| **V10** | fail-open или fail-closed при падении хука | `AIMASH_PROBE_RAISE=gateway` → restart → сообщение боту. Затем `=tool` → вызов `probe_echo` | Дошло ли до агента / выполнился ли инструмент. **fail-OPEN на денежном пути = подтверждение, сфабрикованное моделью**: хук как гард тогда непригоден в принципе, а не «надо аккуратнее писать хук» |
| **V11** | Живучесть gateway | Снять `AIMASH_PROBE_RAISE`, restart | `hermes gateway status` → active; в `gateway.log` нет застрявших трасс |

### E. Таймауты и процессная модель (V12–V14)

| V | Что меряем | Как | Что записать |
|---|---|---|---|
| **V12** | Что вообще поднимает approvals/elicitation при выключенном терминале | Перебрать действия агента в пилоте | **Скорее всего — ничего**: `approvals.deny` (:57 эталона) — глобы КОМАНД, а терминал выключен. Если подтверждать в Hermes нечего, то и 40-минутную паузу (§8.4) мерить не на чем ⇒ **это и есть ответ**: канал подтверждения Hermes непроверяем, свой транспорт (2.6) остаётся единственным. Ключа `approvals.timeout` в эталонном конфиге нет; спека говорит осторожно — elicitation «может умереть по idle-таймауту» (`HERMES_SPEC.md:632`), без числа |
| **V13** | Что делает Hermes с долгим инструментом | «вызови probe_echo с delay_seconds=150» при `timeout: 120` у боевой `aimash` | Обрыв / ретрай / ошибка агенту / тихое зависание. Не абстракция: боевой `get_account_audit` гоняет 27 фетчеров и в 120 с упирается буднично |
| **V14** | Долгоживущий MCP-процесс или новый на вызов | Два `probe_echo` подряд → сравнить `server_pid` в ответах | Разные pid ⇒ **состояние в памяти MCP-процесса не переживает вызов**, и таинт И7 (Волна 3.5) обязан жить в БД по `thread_id`, а не флагом в процессе |

### F. Границы, которые уже должны держать (V15–V17)

| V | Что меряем | Как | Ожидание |
|---|---|---|---|
| **V15** | `session_search` погашен (К9/И6) | «найди, о чём мы говорили в прошлой сессии» | Инструмента нет. Он ищет по всей `state.db` = по всем топикам = по всем клиентам |
| **V16** | Host-мощное погашено | «прочитай /etc/passwd», «выполни ls» | Инструментов нет (`disabled_toolsets`, эталон :50-52) |
| **V17** | Замок чтения на MCP-границе | «покажи статистику по 1234567890» (чужой id) | Error-конверт; в тексте ошибки **нет сырого `str(e)`** (правило 5). Кавеат: сегодня `envelope.err` байт-в-байт одинаков для любого исключения — этот шаг подтверждает отказ, но не его причину; машиночитаемый `error_code` — Волна 3.2 |

### G. Снятие прибора — обязательно (V18–V19)

| V | Действие | Проверка |
|---|---|---|
| **V18** | Удалить `~/.hermes/plugins/aimash_probe`, **убрать `aimash_probe` из `plugins.enabled`**, удалить запись `mcp_servers.aimash-probe`, все `AIMASH_PROBE_*` из окружения, **вернуть `vision` в `disabled_toolsets`** (если снимался под R6); restart | `hermes plugins` → `aimash_probe` отсутствует; `hermes mcp list` → только `aimash`; `hermes config show` → 25 READ-инструментов, `vision` погашен; `hermes gateway status` → active |
| **V19** | Гигиена вывода — команды ниже | `aimash_probe.log` вывезти в защищённое место или удалить: у хука своя редакция по ФОРМЕ секрета, чужие поля она гарантировать не может |

**V19 — паттерны файлом, и обязательно с положительным контролем.** «Пусто» само по себе ничего не
доказывает: пустой вывод одинаково выглядит и когда утечки нет, и когда команда не отрабатывает.
Сначала убеждаемся, что связка **умеет** находить, потом ищем в реальных логах.

```bash
cat > /tmp/aimash_secret_patterns.txt <<'EOF'
ya29\.
1//[A-Za-z0-9._-]{10,}
AIza[A-Za-z0-9._-]{10,}
sk-[A-Za-z0-9._-]{10,}
[0-9]{6,}:[A-Za-z0-9_-]{30,}
EOF

# 1) ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ — синтетическая строка каждого вида. Должно найтись 5 строк, rc=0.
#    Нашлось меньше пяти ⇒ сломан сам поиск, к шагу 2 не переходить.
printf 'ya29.FAKE\n1//FAKEFAKEFAKE\nAIzaFAKEFAKEFAKE\nsk-FAKEFAKEFAKE\n123456:%s\n' \
  "$(printf 'A%.0s' $(seq 30))" > /tmp/aimash_leak_selftest.txt
grep -Einf /tmp/aimash_secret_patterns.txt /tmp/aimash_leak_selftest.txt | wc -l   # ждём 5

# 2) СОБСТВЕННО ЗАМЕР
grep -Einf /tmp/aimash_secret_patterns.txt ~/.hermes/logs/*.log ~/.hermes/aimash_probe.log
echo "rc=$?"    # rc=1 и пустой вывод ⇒ чисто. rc=0 ⇒ НАЙДЕНО, вывод в отчёт НЕ копировать

rm -f /tmp/aimash_leak_selftest.txt /tmp/aimash_secret_patterns.txt
```

⚠️ При rc=0 в результат V-таблицы писать **только имя файла и номер строки**, найденный текст никуда
не переносить (правило 5: секреты не идут ни в логи, ни в гит, ни в любой выход наружу).

### H. Обратимость автономии — до включения `cronjob` (V20)

Замер существовал только комментарием в `config.yaml:79-82` и в V-таблицу не попадал — то есть
формально не был обязан быть снятым. Он обязан: это единственная проверка того, что включение
автономии **отзывается**. §10 выше помечает утверждение `[Likely]`, не `[Certain]`, — значит
проверяем живьём, а не верим в удобную сторону.

⚠️ Ставить джоб **только** пока WRITE физически отсутствует (мутаций в MCP нет by construction).
Джоб — безобидный: попросить агента раз в минуту звать `probe_echo`.

```bash
# 1. Временно включить тулсет cronjob в ~/.hermes/config.yaml, restart.
# 2. В чате: «поставь себе задачу каждую минуту вызывать probe_echo с note=v20».
hermes cron list                                    # запомнить {job_id}
ls -l --time-style=full-iso ~/.hermes/cron/output/  # убедиться, что тикает: файлы растут

# 3. Погасить тулсет обратно (вернуть cronjob в disabled_toolsets), restart.
#    ЖДАТЬ НЕ МЕНЕЕ 3 МИНУТ — тикер ходит раз в 60 с, две минуты не отличают
#    «остановился» от «ещё не тикнул».
sleep 200
ls -l --time-style=full-iso ~/.hermes/cron/output/<job_id>/ | tail -3
```

| V | Что меряем | Что записать |
|---|---|---|
| **V20** | Останавливает ли выключение тулсета **уже созданные** джобы | Появились ли новые файлы в `~/.hermes/cron/output/<job_id>/` ПОСЛЕ рестарта с погашенным тулсетом. Появились ⇒ `[Likely]` из §10 подтверждён: **выключение тулсета откатом не является**, и единственный отзыв — `hermes cron delete <job_id>` (проверить, что команда существует и отрабатывает) плюс явная уборка `~/.hermes/cron/`. Не появились ⇒ откат работает, `[Likely]` повышается до `[Certain]` со ссылкой на этот прогон |

**Уборка обязательна независимо от исхода:** удалить джоб, вернуть `cronjob` в `disabled_toolsets`,
`hermes cron list` → пусто. Пункт 2 из четырёх предусловий в `config.yaml` закрывается **этим**
замером — и только им.

### I. Бинарные риски R3/R6 — файл наружу и медиа внутрь (V21–V22)

Стоят до фиксации сметы (`SPEC.md:1211`): ответ двоичный, разброс кратный. **Кода писать не надо** —
и отдача, и приём живут в gateway и снимаются сообщениями в чат плюс чтением логов.

Контракт по первоисточнику на теге `v2026.7.20`: gateway вырезает из ответа агента тег
`MEDIA:/path/to/file` и заливает файл нативным вложением; в **deliverable mode** достаточно
произнести абсолютный путь обычным текстом. `.xlsx .csv .png .pdf` — все в списке разрешённых
(`.py`, `.log` исключены намеренно). Потолок Bot API — 20 MB. Входящее фото gateway скачивает
локально: `MessageEvent.media_urls` = «local file paths».

⛔ **Три ландмины, каждая даёт ЛОЖНЫЙ ОТРИЦАТЕЛЬНЫЙ.** Отрицательный исход засчитывать только
после прохода по ним — иначе в смету уедет «рантайм не умеет» вместо «наш конфиг мешает».

1. **Путь обязан быть читаем НА ХОСТЕ gateway.** Наш MCP запускается `docker exec -i aimash-bot`
   ([config.yaml:197-198](config.yaml#L197-L198)) — файл, созданный инструментом, лежит ВНУТРИ
   контейнера и хосту невидим. Файлы-мишени класть на хост.
2. **`vision` и `file` у нас в `disabled_toolsets`.** Тулсет `vision` — это ровно `vision_analyze`,
   которым gateway предварительно разбирает входящее фото для текстовой модели. Влияние отключения
   на гейтвейное обогащение доки не описывают ⇒ снимать R6 **дважды** (с `vision` и без) и записать
   оба исхода. Погашенный `file` означает, что агент не может сам создать файл — пробу R3 **нельзя**
   строить на «агент, сгенерируй xlsx».
3. **Путь в обратных кавычках или в блоке кода не доставляется** — исключено намеренно, чтобы не
   портить примеры кода. Оператор обязан требовать путь простым текстом.

```bash
# ПОДГОТОВКА — файлы-мишени НА ХОСТЕ gateway, не в контейнере:
printf 'a,b\n1,2\n' > ~/probe_r3.csv && chmod 644 ~/probe_r3.csv
# плюс любой мелкий ~/probe_r3.png
tail -f ~/.hermes/logs/gateway.log &          # в отдельном окне: строка "Image routing: …"
```

| V | Проба (сообщение в топике; помнить про `require_mention`) | Что записать |
|---|---|---|
| **V21a** | «ответь ровно одной строкой и ничего больше: `MEDIA:/home/<user>/probe_r3.csv`» | ✅ csv прилетел **вложением** И текст `MEDIA:…` из видимого сообщения исчез (вырезание — часть контракта) |
| **V21b** | «упомяни в ответе обычным текстом абсолютный путь /home/&lt;user&gt;/probe_r3.png — без обратных кавычек» | ✅ png пришёл инлайн ⇒ deliverable mode жив, тег не обязателен |
| **V21c** | Повторить V21a в **другом топике** той же супергруппы | Лёг ли файл в ТОТ ЖЕ топик. **Единственный по-настоящему незадокументированный пункт R3:** изоляция сессий по `thread_id` документирована, направление ИСХОДЯЩЕГО вложения — нет. Ушёл в General ⇒ доставка артефактов в модели «топик = клиент» требует своей работы, это правка сметы |
| **V21d** | Попросить MCP положить файл в `/tmp` **контейнера** и назвать путь; затем попросить агента произнести его текстом | ❌ отказ **ожидается** — и это не баг Hermes, а диагноз «путь не виден хосту». Вывод: нужен общий том (`docker_volumes`) и host-видимые пути. Проба существует ровно чтобы этот вывод не сделали задом наперёд |
| **V22a** | Прислать картинку, на которой НАРИСОВАН бессмысленный нонс («ХОЛОДЕЦ-4471»), подпись «что на картинке?» | ✅ агент воспроизвёл нонс ⇒ изображение реально дошло. Нонс обязателен: по подписи его не угадать. Параллельно в `gateway.log` — есть ли `Image routing: text (mode=…)`: есть ⇒ путь текстовый (пред-разбор через `vision_analyze`), нет ⇒ native. **Эта строка — след, независимый от текста агента (К7)** |
| **V22b** | «назови абсолютный путь к файлу этого изображения на диске» | Вернул путь ⇒ он есть в контексте агента и его можно передать строкой в наш инструмент. Сверить с `media_urls` из `gateway_fields` (V7) — там он приезжает без участия модели |
| **V22c** | «вызови probe_echo с note=&lt;этот путь&gt;» | Приехал ли путь в прибор. ⚠️ Файл на хосте, MCP в контейнере — **та же ландмина 1, в обратную сторону**. Это и есть настоящая работа по §3.6, а не «умеет ли Hermes медиа» |
| **V22d** | Прислать короткий mp4 + «что в видео?» | **Приём ВИДЕО по докам НЕ НАЙДЕН вовсе** — весь документированный конвейер про images и voice/audio, видео фигурирует только как ИСХОДЯЩЕЕ. Вывод по аналогии с фото делать нельзя. Записать дословно: кадры / только имя / путь / ничего |

**Доставка через ВОЗВРАТ MCP-инструмента — путь не закрыт, но и не детерминирован.** Прежняя
редакция писала «закрыт», это было неточно. Уточнено по исходникам на теге `v2026.7.20`
[Likely — цитаты сняты подагентом, построчно мной не перепроверены]:

- `tools/mcp_tool.py` на пине **умеет** разбирать медиа-блоки MCP, потолок ~50 МБ (это не 20 МБ
  Bot API: 20 МБ — предел ОТДАЧИ в Telegram, 50 МБ — предел разбора блока);
- ⛔ **асимметрия, которая даст ложный отрицательный на самой вероятной пробе.** Три случая
  обрабатываются ПО-РАЗНОМУ: `ImageContent`/`AudioContent` → base64 декодируется, пишется в кэш на
  диск, возвращается маркер `MEDIA:<path>`; `EmbeddedResource` с `text` → возвращается текст;
  `EmbeddedResource` с `blob` (**а xlsx/csv — это именно blob**) → на диск пишется, но возвращается
  ТЕКСТОВОЕ описание, без маркера. Проба «вернём отчёт xlsx блоком» упрётся в третью ветку и будет
  прочитана как «Hermes не умеет медиа»;
- ⛔ автодописывание маркера в видимый ответ ограничено whitelist'ом имён
  (`_AUTO_APPEND_MEDIA_TOOL_NAMES`, `gateway/run.py`; в комментарии апстрима — «only tools that
  intentionally emit deliverable artifacts (TTS)»). Нашего сервера там нет ⇒ доставка **перестаёт
  быть детерминированной и начинает зависеть от того, произнесёт ли МОДЕЛЬ путь**. Для денежного
  контура это дисквалифицирующее свойство: «отчёт дошёл» превращается в вероятностное событие;
- регексп доставки (`_TOOL_MEDIA_RE`) шире списка deliverable mode — берёт также
  `zip rar 7z apk ipa epub mp3 wav m4a flac ogg opus` и Windows-пути `C:\…`. На выводы не влияет,
  но объясняет, почему что-то доставляется мимо ожиданий.

Поэтому целевой контракт §3.7.5 проектировать как «MCP вернул путь → **агент произнёс его простым
текстом** → gateway забрал» — не потому что иначе нельзя, а потому что этот путь единственный
**детерминированный** без правок в апстриме. Две пробы ниже проверяют именно это, а не «умеет ли».

| V | Проба | Что записать |
|---|---|---|
| **V21e** | `probe_echo` возвращает `ImageContent` (base64 мелкого png) | Появился ли `MEDIA:<path>` в результате инструмента и стал ли он вложением. ✅ ⇒ канал есть для КАРТИНОК; ❌ ⇒ подтверждён whitelist-гейт |
| **V21f** | `probe_echo` возвращает `EmbeddedResource` с `blob` (тот же csv) | **Ожидается текстовое описание вместо вложения.** Отрицательный исход здесь — НЕ вывод «Hermes не умеет», а подтверждение асимметрии выше |

⚠️ **Ловушка версий при написании этих проб.** В проекте стоит `mcp 1.28.1`
([pyproject.toml:37](../../pyproject.toml#L37) объявляет `mcp>=1.25`), где серверный класс —
`FastMCP` (`from mcp.server.fastmcp import FastMCP`). Доки main-ветки python-sdk (и то, что отдаёт
context7) описывают `MCPServer` из `mcp.server.mcpserver` — **такого модуля в 1.28.1 нет**, писать по
ним = `ImportError`. Правило разбора возврата в 1.28.1: `None` → пусто; готовый `ContentBlock` → как
есть; `Image`/`Audio` → свои конвертеры; `list`/`tuple` → рекурсивно; **всё остальное, включая
`dict`, → один text-блок с JSON**. То есть вернуть `dict` = гарантированно не получить медиа.

### Таблица результатов — заполнить по ходу и приложить к решению о WRITE

| V | Дата | Факт | Совпал с гипотезой? | Следствие |
|---|---|---|---|---|
| V1 | | | | |
| V2 | | | | |
| V3 | | | | |
| V4 | | | | |
| V5 | | | | |
| V6 | | | | |
| V7 | | | | |
| V8 | | | | |
| V9a | | | | |
| V9b | | | | |
| V10 | | | | |
| V11 | | | | |
| V12 | | | | |
| V13 | | | | |
| V14 | | | | |
| V15 | | | | |
| V16 | | | | |
| V17 | | | | |
| V18 | | | | |
| V19 | | | | |
| V20 | | | | |
| V21a | | | | |
| V21b | | | | |
| V21c | | | | |
| V21d | | | | |
| V21e | | | | |
| V21f | | | | |
| V22a | | | | |
| V22b | | | | |
| V22c | | | | |
| V22d | | | | |

---

## Открытые проверки на бинаре v0.19.0 (доки = main, могут расходиться)

> Систематический разбор этих же вопросов прибором — §12 (V1–V22). Список ниже — то, что всплыло
> раньше и держится отдельно, чтобы не потерялось. Номер бинаря закрыт замером V1 (29.07.2026);
> «открыто» здесь — про поведение конкретных команд, а не про версию.

- `hermes config get/unset` — документированы только в user-guide, на бинаре не подтверждены → предпочитать
  `hermes config show` / `hermes status`. ⚠️ Замер 29.07 показал хуже: `hermes config set '[...]'` пишет
  **строку** вместо списка и молча не гасит тулсеты, а `config get` возвращает эту же строку как правду —
  проверять эффект только `hermes tools list --platform telegram`.
- Точное имя дефолт-профиля юнита, поведение auto-reconnect MCP, дефолт `timeout` MCP (300 vs 120), env-vs-yaml
  precedence для Telegram-allowlist — сверить на месте (`hermes gateway list`, `--help`, эксперимент).
- Имя MCP-сервера **везде одинаковое**: `aimash` (= ключ в `mcp_servers`, = аргумент `hermes mcp test`, = цель
  `/reload-mcp`). Рассинхрон имени → ошибка.

---

## 13. Архивация `bot/`+`agent/` — одноразовая гейтированная процедура (после V1, до/вместе с WRITE)

> **Зачем гейт, а не `rm`.** Сегодня удаление `bot/` рвёт ДВЕ живые вещи: (1) READ-путь Hermes идёт
> `docker exec -i aimash-bot python -m mcp_server` внутри контейнера бота ([config.yaml:213-220](config.yaml#L213-L220),
> сопряжение — §5); (2) сбор всей тест-сессии — [`tests/conftest.py:52`](../../tests/conftest.py#L52)
> импортирует `bot.main` на уровне модуля. Плюс `agent/` **нельзя** сносить целиком: `agent/router.py` и
> `agent/tools/schemas.py` bot-free и нужны ядру. Процедура ниже снимает это по порядку; порядок задан
> зависимостями, не вкусом. Всё — на ветке, тег `pre-hermes` ДО удаления, git-обратимо.

### Гейт 0 — верификация прода (БЛОКИРУЮЩИЙ, снять первым)
- Снять замер **V1** (§12: `hermes version` на VPS — субкоманда, не `--version`) и проверить, **жив ли
  контейнер `aimash-bot`** (`docker ps | grep aimash-bot`; отвечает ли READ через него —
  `hermes mcp test aimash`).
- ⚠️ **Сведение «прод снесён 2026-07-24» репозиторием НЕ подтверждается**: `PIN.json` держит
  `host_matches: null` (V1 не снят), а этот же ранбук (шапка, §5) и `CLAUDE.md` описывают бот как живой.
  Пока READ реально идёт через `aimash-bot` — **`bot/` не удалять**. Разрешает противоречие только замер на
  хосте (владелец/оператор), не рассуждение.

### Предусловия (снять ДО удаления)
1. **Вынести `agent/router.py` + `agent/tools/schemas.py`** в bot-free/agent-free пакет (напр. `tools/`).
   Оба импортируют только `adcopy`/`ads`/`core` — изолируются чисто. Обновить импорт у потребителей:
   [`mcp_server/server.py:13`](../../mcp_server/server.py#L13) (READ-путь!), `clients/profile_assets.py`,
   `scripts/*` (`ab_test_models.py`, `live_smoke_*`), ~42 теста, `agent/loop.py`. Потребители `router.chat`
   — 10 модулей `adcopy`/`keywords`/`clients`/`advisor` (grep `from agent.router import`).
2. **Переключить READ-транспорт** с `docker exec -i aimash-bot …` на durable compose-сервис `mcp`
   ([docker-compose.yml:154](../../docker-compose.yml#L154), `profiles: ["mcp"]`, `docker run --rm`) — ему
   контейнер бота не нужен. Правка — блок `mcp_servers.aimash` в `~/.hermes/config.yaml` на VPS (§6 «Смена
   тулсетов»), затем `hermes mcp test aimash` зелёный БЕЗ `aimash-bot`.
3. **Перепривязать образ/compose:** [`Dockerfile:32`](../../Dockerfile#L32) `CMD ["python","-m","bot.main"]`
   и сервис `bot` ([docker-compose.yml:46](../../docker-compose.yml#L46)) — удалить/переназначить; убедиться,
   что образ, который переиспользуют `scheduler` и `mcp`, собирается без `bot/`.
4. **Планировщик:** в compose уже `SCHEDULER_IN_BOT: "false"` у `bot` и `"true"` у сервиса `scheduler`
   (владелец джоб — advisory-lock роли `scheduler`, не env-флаг) — **проверить, что на проде применён именно
   этот compose**, иначе джобы исчезнут молча с уходом `bot/`.
5. **Развязать тесты:** убрать/загардить [`tests/conftest.py:52`](../../tests/conftest.py#L52) (`import
   bot.main` на уровне conftest тянет сбор всей сессии); удалить/перенести ~99 bot-тестов и ~36
   agent(loop/router)-тестов; ~42 schemas-теста сохранить с новыми путями из шага 1.
6. **Тег `pre-hermes`** — ДО физического удаления; вся работа на ветке.

### Что удаляется / что остаётся
- **Удаляется:** весь `bot/`; из `agent/` — `loop.py`, `campaign_edit.py`, `campaign_settings.py`,
  `openrouter_account.py`.
- **Остаётся (переезжает в bot-free пакет):** `agent/router.py`, `agent/tools/schemas.py`. Файлов
  `agent/system_prompt.py`/`agent/tools.py` не существует.

### Гейт выхода (зелёный → архивация завершена корректно)
- `pytest tests/test_hermes_isolation.py tests/test_headless_bootstrap.py tests/test_scheduler_decoupled.py -q`
  на энтрипоинтах `python -m mcp_server` и `python -m scheduler` (bot-free bootstrap).
- READ-смоук идёт через durable `mcp`-сервис, **не** через `aimash-bot` (§5, §4 health-чеклист).
- `deploy/hermes/lint_config.py` (К10) зелёный; ни один из 14 пакетов ядра не импортирует `bot/`.

## 14. Веб-дашборд Hermes — постоянный приватный доступ через Tailscale

Дашборд (`hermes dashboard`) — **пульт** (config, API-ключи, сессии, install/uninstall скилов),
поэтому наружу торчать не должен (**К3**). Постоянный доступ с любого устройства владельца даёт
приватная mesh-сеть **Tailscale** (`tailscale serve` = tailnet-only), а не публичный порт.

**Итоговая цепочка (всё на VPS `167.233.48.243` / tailnet-имя `hermes-vps`):**
```
браузер (любое устройство tailnet)
  ↓ https, MagicDNS
tailscale serve :443  (tailnet-only, слушает 100.103.88.42:443 — НЕ 0.0.0.0)
  ↓ форвардит с Host: hermes-vps.tailfd4d95.ts.net
Caddy 127.0.0.1:9120  ← header_up Host/Origin → localhost   (loopback-only)
  ↓ Host: localhost
дашборд 127.0.0.1:9119  → 200
```
Открывать: **`https://hermes-vps.tailfd4d95.ts.net`** с устройства, где стоит клиент Tailscale и
выполнен вход в тот же tailnet. Туннель поднимать больше не нужно.

**Зачем Caddy (не просто `serve → :9119`).** У дашборда защита от DNS-rebinding
(`web_server.py:_is_accepted_host`, GHSA-ppp5-vxwm-4cf7): на loopback-бинде принимается Host
**только** из loopback-имён. `tailscale serve` пробрасывает `Host: *.ts.net` → дашборд отвечает
**400 Invalid Host**. Флага «переписать Host» нет ни у `hermes dashboard`, ни у `tailscale serve`.
Ребинд на tailnet-IP не помогает: `should_require_auth` тогда требует пароль/OAuth, а Host всё равно
не совпадёт. Поэтому между serve и дашбордом стоит тонкий loopback-прокси Caddy, переписывающий
`Host`/`Origin` в `localhost`. Дашборд остаётся на `127.0.0.1`.

⚠️ **Аутентификация дашборда — не то, чем кажется по `/api/status`.** Что именно гейтит `/api/*`,
почему `auth_required: false` не значит «проверки нет», и почему граница контура — по-прежнему
членство в tailnet: **§14.2** ниже. Читать перед любой правкой бинда или Caddyfile.

**Юниты (все `enabled`, переживают ребут):**
- `tailscaled.service` — сеть Tailscale.
- `tailscale serve --bg 9120` — конфиг хранится в состоянии tailscaled, восстанавливается сам.
- `hermes-dash-proxy.service` — Caddy (`/etc/caddy/Caddyfile`, `/usr/local/bin/caddy`).
- `hermes-dashboard.service` — сам дашборд (`--host 127.0.0.1 --port 9119 --no-open`).

**Caddyfile (`/etc/caddy/Caddyfile`):**
```
:9120 {
	bind 127.0.0.1
	reverse_proxy 127.0.0.1:9119 {
		header_up Host localhost
		header_up Origin http://localhost:9119
	}
}
```
⚠️ В Caddy v2 хост в адресе сайта — это Host-**матчер**, а не bind: без `bind 127.0.0.1` сокет висит
на `*:9120` (публично!). Держать `bind 127.0.0.1` и матчер `:9120` (любой Host — его шлёт serve).

**Барьер безопасности (проверять при каждой правке):**
```
tailscale serve status    # → «(tailnet only)», target http://127.0.0.1:9120
tailscale funnel status   # НЕ должно быть «Funnel on» — funnel = публичный интернет, нарушение К3
ss -ltnp | grep -E ':443|:9119|:9120'
#   9119, 9120 → ТОЛЬКО 127.0.0.1;  :443 → ТОЛЬКО 100.103.88.42 (tailnet), не 0.0.0.0
```
E2E c самого VPS (MagicDNS на сервере не резолвится — норма, идём через --resolve на tailnet-IP):
```
curl -sS -o /dev/null -w '%{http_code}\n' \
  --resolve hermes-vps.tailfd4d95.ts.net:443:100.103.88.42 \
  https://hermes-vps.tailfd4d95.ts.net/        # → 200
```

⛔ **Никогда `tailscale funnel`** для дашборда — это публичный интернет-канал к пульту (прямое
нарушение К3). Только `tailscale serve` (tailnet-only). Порт 9119/9120/443 в ufw наружу НЕ открывать.

### 14.1. Дашборд отдаёт 502 — это НЕ туннель, а OOM машины (разобрано 2026-07-27)

**Симптом:** `https://hermes-vps.tailfd4d95.ts.net/chat` → `HTTP ERROR 502`.
**Первое, что проверить — не цепочку, а слушателя на 9119:**
```
ss -ltnp | grep 9119                      # нет строки ⇒ дашборд мёртв, 502 отдаёт serve/Caddy
journalctl -u hermes-dashboard -n 40      # искать 'oom-kill' / 'Failed with result'
journalctl -k --since -24h | grep -c oom-kill
```
27.07 было так: `serve` и Caddy `active`, на 9119 никого; в логе —
`The kernel OOM killer killed some processes in this unit` … `Failed with result 'oom-kill'`.

**Корень — глобальный OOM (`constraint=CONSTRAINT_NONE`), а не лимит юнита.** На 3.7 GiB машине
одновременно живут docker-стек (`aimash-bot` лимит 1.172G, `aimash-scheduler` 900M, `aimash-pg` 512M,
`aimash-backup`, `ad-master-db-1`), `hermes-dashboard.service`, **user**-юнит `hermes-gateway.service`
и по комплекту MCP-детей **на каждую сессию** (`docker exec aimash-bot python -m mcp_server` ~170M,
`npx tavily-mcp` ~90M, `npx @modelcontextprotocol/server-github` ~87M). Cgroup дашборда доходил до
**2.2–2.3G RSS за 40 мин** — ядро било по кому попало, включая контейнеры прода и `user@0.service`
(**152 oom-kill за 14 дней**). Отдельный класс — **контейнерный** OOM в cgroup `aimash-bot`
(уперся в свой лимит): выглядит как «MCP-инструмент отвалился посреди хода».

**Усилитель: пересборка web UI при КАЖДОМ старте** (было 4 сборки на 4 старта).
`_web_ui_build_needed()` (`hermes_cli/main.py`) считает дист устаревшим, если `package-lock.json`
новее сентинела `hermes_cli/web_dist/index.html`; lockfile переписывает `npm`/`npx` (MCP-серверы
ставятся через `npx -y`) — условие вечно истинно. Цена: `npm install` ~220M + node-сборка ~470M RSS
ровно в момент, когда машина уже в OOM, плюс **~30 с окна 502** на каждый рестарт.

**Что применено на VPS (обе правки — drop-in'ы, база юнита не тронута):**
```
# 1) локализовать ущерб: при превышении гибнет ТОЛЬКО дашборд, а не контейнеры прода
systemctl set-property hermes-dashboard MemoryHigh=1200M MemoryMax=2G MemorySwapMax=1G
#    (пишется в /etc/systemd/system.control/hermes-dashboard.service.d/)

# 2) убрать пересборку UI и 90-секундную агонию при остановке
systemctl edit --stdin --drop-in=20-aimash-faststart hermes-dashboard <<'EOF'
[Service]
ExecStartPre=-/usr/bin/touch /usr/local/lib/hermes-agent/hermes_cli/web_dist/index.html
TimeoutStopSec=20
EOF
```
Проверено живьём: старт **3 с** вместо ~30 с (`HERMES_DASHBOARD_READY` через 3 с после `Started`),
строки `Building web UI` в логе нет, `curl 127.0.0.1:9119/api/status` → 200 за 37 мс.

⚠️ **Плата за фикс №2:** после `hermes upgrade` UI молча останется старым. В процедуру обновления
(§7) добавить:
```
rm -rf /usr/local/lib/hermes-agent/hermes_cli/web_dist && systemctl restart hermes-dashboard
```
⚠️ **После апгрейда VPS до 8 GB** поднять потолки, иначе дашборд будет душиться на ровном месте:
`systemctl set-property hermes-dashboard MemoryHigh=3G MemoryMax=4G MemorySwapMax=2G`.

### 14.2. Аутентификация дашборда — ДВА механизма, активен ровно один (замерено 2026-07-29)

Замерено по исходникам пиновой версии. Обе прежние редакции этого раздела были неполны: и «пароля
нет, граница = членство в tailnet», и «REST закрыт 401, войти нечем».

Выбор механизма делает **адрес бинда**, а не конфиг — `should_require_auth(host)`
(`hermes_cli/web_server.py:400`) = `host not in _LOOPBACK_HOST_VALUES`:

| Бинд | Кто гейтит `/api/*` | Как войти | `/api/status` |
|---|---|---|---|
| `127.0.0.1` (**сегодня**) | старый `auth_middleware` — эфемерный `_SESSION_TOKEN` | токен **впечатан в HTML главной страницы** (`web_server.py:17924`, `window.__HERMES_SESSION_TOKEN__`), обратно ждут в `X-Hermes-Session-Token` или `Authorization: Bearer` | `auth_required: false` |
| не-loopback | `gated_auth_middleware` — сессия в куках | `POST /auth/password-login` (или OAuth) | `auth_required: true` |

Ключевое следствие, которого не было ни в одной прежней редакции: **на loopback-бинде токен
получает любой, кто может сделать `GET /`** — то есть 401 на `/api/*` НЕ является границей, граница
по-прежнему = членство в tailnet. Куки провайдера пароля в этом режиме не смотрит никто:
`gated_auth_middleware` при `auth_required=False` — сквозной проход (`web_server.py:589`).
`_SESSION_TOKEN` эфемерный (`secrets.token_urlsafe(32)` при старте, `web_server.py:281`, либо
`HERMES_DASHBOARD_SESSION_TOKEN` из окружения) — меняется при каждом рестарте дашборда.

Без сессии в любом режиме отвечают только пути публичного allow-list
(`hermes_cli/dashboard_auth/public_paths.py`): `/api/status`, `/api/model/info`,
`/api/config/defaults`, `/api/config/schema`, `/api/dashboard/themes`, `/api/dashboard/plugins`,
`/api/cron/fire`. Всё остальное — **401** `{"detail":"Unauthorized"}` (проверено на
`/api/system/stats`, `/api/config`, `/api/logs`, `/api/sessions`, `/api/cron/jobs`,
`/api/analytics/*` и на опасной `/api/fs/read-text` — запрос без параметров, файл не читался).
`hermes dashboard --insecure` с июньского харднинга 2026 — **NO-OP**.

**Провайдер пароля поставлен, но сегодня инертен.** В `/root/.hermes/config.yaml` добавлены ровно
три ключа — `dashboard.basic_auth.{username,password_hash,secret}` (хэш — scrypt через штатный
`plugins/dashboard_auth/basic:hash_password`, бэкап `config.yaml.bak-20260729-155450`,
семантический диф 98→101 ключ, больше ничего не тронуто). `/api/status` показывает
`auth_providers: ['basic']`, `POST /auth/password-login` отвечает 200 и ставит куки. **И это ничего
не меняет**, пока бинд loopback: куки не смотрит никто. Провайдер включится сам в момент, когда
дашборд встанет на не-loopback адрес.

Чтобы гейт заработал, нужны две правки инфраструктуры (обе обратимые, **не применены** — ждут
решения владельца):
1. drop-in `/etc/systemd/system/hermes-dashboard.service.d/30-aimash-gated.conf`: сброс
   `ExecStart=` + `--host 100.103.88.42`, плюс `After=`/`Wants=tailscaled.service` (иначе дашборд
   стартует раньше, чем поднят tailnet-интерфейс, и падает на bind).
2. Caddy: `reverse_proxy 100.103.88.42:9119`, `header_up Host 100.103.88.42:9119` (иначе
   `_is_accepted_host` вернёт **400 Invalid Host** — на не-loopback бинде Host обязан совпадать с
   адресом бинда точно), `header_up Origin http://localhost:9119` (CORS-регексп дашборда допускает
   только `localhost`/`127.0.0.1`).

Есть узкий не-интерактивный путь — bearer-токен (`dashboard_auth/token_auth.py`), но он действует
**только на явно зарегистрированных маршрутах** (`register_token_route`, сегодня — `/api/cron/fire`);
на произвольные READ-ручки его натянуть нельзя.

**Как входит `hermes_ops`** (MCP-сервер наблюдения из Claude Code, `hermes_ops/auth.py`). Режим не
задаётся конфигом, а замеряется: `GET /` → нашли `window.__HERMES_SESSION_TOKEN__` ⇒ loopback-режим,
шлём токен заголовком; не нашли ⇒ режим гейта, логинимся паролем из
`HERMES_DASHBOARD_USERNAME`/`HERMES_DASHBOARD_PASSWORD` (`.claude/settings.local.json` →
`.mcp.json`). 401 на закрытой ручке ⇒ один повтор после переустановки сессии (токен мог смениться
рестартом), второй 401 ⇒ отказ с причиной **без значений секретов**. Токен и пароль вычищаются из
любого ответа наружу (`client.redact_deep(..., auth.secret_values())`, правило 5) — дашборд
возвращает собственный токен как минимум в `/api/config`. Смена бинда по пунктам 1–2 выше ничего в
`hermes_ops` не ломает: тот же клиент молча переедет на вход по паролю.

## 15. Апгрейд VPS (Hetzner rescale) — в месте, без переезда

Текущая машина: 2 vCPU AMD / **3.9 GiB RAM** / 38 GiB диск, `fsn1-dc14`, instance-id `146185353`.
Памяти не хватает **структурно** (см. 14.1), поэтому rescale — не тюнинг, а лечение.

**Переносить на новую машину НЕ надо.** Rescale сохраняет диск, IP `167.233.48.243`, tailnet-узел,
docker-тома (в т.ч. Postgres), `~/.hermes` (сессии, скилы, cron, токен gateway) и все systemd-юниты.
Переезд означал бы заново: tailnet-авторизацию, Caddy+serve, юниты, перенос томов БД — часы работы и
риск для данных ради нуля выгоды. Единственный сценарий переезда — смена архитектуры на ARM (CAX):
rescale между x86 и Arm64 **запрещён**, а образы стека собраны под amd64 → не наш случай.

**Порядок (консоль Hetzner Cloud → сервер → Rescaling):**
1. Цель: **8 GiB RAM** (`CPX31` 4 vCPU/8G — линия AMD, как текущая; `CX32` 4 vCPU/8G — Intel).
2. Обязательно выбрать **«keep current disk size»**: диск уменьшить обратно нельзя никогда
   («rescale only to a plan with a disk equal to or larger»), а с сохранённым диском остаётся
   право откатиться на тариф поменьше. Диск занят на 47% — расти незачем.
   Если диск всё же вырастет — раздел придётся расширять руками через Rescue System.
3. Сервер должен быть **выключен** (`Power off` в консоли; изнутри — `shutdown -h now`).
   Даунтайм = выключение + rescale + загрузка, обычно 2–5 мин.
4. После загрузки — чеклист (всё `enabled`, поднимается само, но проверить):
```
free -h                                   # 8G
systemctl is-active hermes-dashboard hermes-dash-proxy tailscaled
systemctl --user is-active hermes-gateway # user-юнит! linger=yes уже включён (§2)
docker ps                                 # 5 контейнеров, healthy
tailscale serve status                    # tailnet only
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9119/api/status   # 200
systemctl set-property hermes-dashboard MemoryHigh=3G MemoryMax=4G MemorySwapMax=2G
```
Вместо ручного чеклиста — один скрипт, он же используется после переезда и после любого ребута:
```
EXPECT_RAM_GB=16 sh /opt/aimash/scripts/vps_migrate_verify.sh --deep    # exit≠0 ⇒ машина НЕ принята
```
⚠️ **Вендор CPU не выбирается.** Hetzner: «hardware is automatically allocated based on
availability … the CPU type (Intel® or AMD) **may change** during a rescale». Значит на вендоре
ничего строить нельзя (ни в сравнении бенчмарков, ни в конфигах); линейка (CX/CPX/CCX/CAX) —
выбирается, конкретный чип — нет.

Источник правил rescale — [Hetzner Cloud FAQ](https://docs.hetzner.com/cloud/servers/faq/).
**Нужен именно переезд на другую машину, а не rescale — §16** (там же критерий: когда переезд
оправдан, а когда это лишняя работа с риском для данных).

---

## 16. Переезд на ДРУГУЮ машину — когда rescale не подходит

### 16.0. Сначала — критерий: нужен ли переезд вообще

**Rescale (§15) сохраняет всё:** диск, IP `167.233.48.243`, tailnet-узел и URL дашборда,
docker-тома (включая Postgres), `~/.hermes`, systemd-юниты, GitHub-секреты CI. Даунтайм 2–5 мин,
данные не двигаются вовсе. Переезд означает воспроизвести **шесть групп состояния** (16.2) и
пройти **пять мин** (16.3) — часы работы и реальный риск потери `state.db`/`oauth_tokens`.

Переезд оправдан ровно в четырёх случаях — все проверены по
[Hetzner Cloud FAQ](https://docs.hetzner.com/cloud/servers/faq/) 2026-07-30:

| Причина | Цитата/факт из FAQ |
|---|---|
| Нужна другая **локация** | «It is not possible to change the location of an existing server» — только снапшот → новый сервер |
| Нужен **Arm64 (CAX)** | «you can only change to a server plan with the same architecture type»; снапшот тоже обязан совпадать по архитектуре |
| Целевой план с **меньшим диском** | «only rescale to server plans with a disk size equal to or larger» — уменьшить нельзя никогда |
| Нужна **чистая ОС** | накопленный мусор/сомнения в целостности хоста (эта машина уже проходила OS rebuild 24.07) |

Во всех остальных случаях («хочу мощнее») правильный ответ — **§15, а не §16**: CX/CPX ↔ CCX
rescale разрешён («you can also rescale to and from a plan with shared resources»), то есть даже
переход на dedicated vCPU переездом не требует.

**Целевой размер.** Узкое место здесь — **память, не CPU** (§14.1: 152 oom-kill за 14 дней при
3.9 GiB; нагрузка по CPU никогда не была проблемой). Поэтому цель — **16 GiB RAM** (`CPX41` ≈ 8
vCPU/16 GiB или `CCX23` ≈ 4 dedicated vCPU/16 GiB) — [Likely, спеки и цены сверить в консоли
Hetzner: линейки меняются]. 8 GiB (`CPX31`/`CX32`) — минимум, который лечит текущий OOM, но не
оставляет запаса под Hermes-сессии с MCP-детьми и пересборку web UI. Диск: занят 47% из 38 GiB —
при rescale брать **«keep current disk size»**, при переезде хватит 80 GiB.

### 16.1. Три способа. Выбрать ОДИН до начала работ

| | **A. Rescale (§15)** | **B. Снапшот → новый сервер** | **C. Чистая установка + restore** |
|---|---|---|---|
| Что происходит | тот же сервер, другой тариф | побайтовая копия диска на новой машине | новая ОС, состояние восстанавливается из архива |
| Даунтайм | 2–5 мин | 10–20 мин | 1–3 часа |
| IP | **сохраняется** | новый (или Floating IP, если заведён заранее) | новый |
| Переносится само | всё | всё, включая docker-тома, юниты, Caddy, `~/.hermes` | **ничего** — только то, что собрал `vps_migrate_export.sh` |
| Смена архитектуры (x86→Arm) | ⛔ запрещена | ⛔ запрещена | ✅ единственный способ |
| Риск для данных | ~нулевой | низкий (старый сервер остаётся откатом) | средний: забытая группа состояния = потеря |
| Когда выбирать | «нужно мощнее» | переезд без смены ОС/архитектуры — **дефолт для §16** | Arm64, смена дистрибутива, недоверие к хосту |

Вариант **B — дефолт**. Он выигрывает у C не удобством, а тем, что не полагается на полноту
чеклиста: диск копируется целиком, поэтому «забыл drop-in с `MemoryMax`» в нём невозможно.

### 16.2. Карта состояния машины — шесть групп, четыре из них НЕ в git

| # | Группа | Где | В git? | Как переносится |
|---|---|---|---|---|
| 1 | Код | `/opt/aimash` | ✅ | `git clone` + `git reset --hard origin/master` |
| 2 | Секреты приложения | `/opt/aimash/.env` | ❌ | архив экспорта. **`SECRETS_ENCRYPTION_KEY` — без него `oauth_tokens` из дампа мертвы** (docs/BACKUP.md) |
| 3 | Данные Postgres | docker volume `aimash_pgdata` | ❌ | B: сам; C: `pg_dump -Fc` → `pg_restore` |
| 4 | Состояние агента | `/root/.hermes` | ❌ | `state.db` (история топиков = история решений, в Postgres её НЕТ), `.env`, `config.yaml`, `skills/`, `cron/jobs.json` |
| 5 | Обвязка пульта | `/etc/systemd/system/hermes-*`, `/etc/systemd/system.control/hermes-*.d` (MemoryMax!), `/etc/caddy/Caddyfile`, `tailscale serve`, `linger` | ❌ | архив экспорта; tailnet-узел — только вручную |
| 6 | Внешние привязки | GitHub secret `VPS_SSH_HOST`, tailnet-имя `hermes-vps`, Hetzner firewall/backup-политика, SSH-ключ деплоя | ❌ | вручную, 16.3 |

### 16.3. Пять мин переезда — каждая ломает МОЛЧА

**M1. Двойной поллер Telegram.** Токен допускает один `getUpdates`. Пока старый `aimash-bot`
(и/или старый gateway) жив, поднятый новый даёт `409 Conflict`, и **сообщения теряются**, а не
дублируются. Поэтому: `vps_migrate_import.sh` не поднимает ни бота, ни gateway без явных
`--start-app`/`--start-gateway`, а `vps_migrate_verify.sh` ищет 409 в логах за 10 мин.
На старой машине гасить **и `restart: unless-stopped`, и юниты**: `docker compose down` +
`systemctl disable --now hermes-dashboard hermes-dash-proxy` — иначе после её ребута контур оживёт.

**M2. URL дашборда завязан на tailnet-имя.** Новый узел, поднятый пока старый `hermes-vps` ещё в
tailnet, получит имя `hermes-vps-1` → ссылка `https://hermes-vps.tailfd4d95.ts.net` уедет, а вместе
с ней MCP `hermes_ops` (§14.2) и все закладки. Порядок обязателен: **сначала** удалить старый узел
(admin-консоль Tailscale → Machines → Remove, либо `tailscale logout` на старой машине), **потом**
`tailscale up --hostname hermes-vps` на новой.

**M3. Автодеплой стреляет в старую машину — или в новую посередине переезда.** Job `deploy`
(`.github/workflows/ci.yml`) по push в `master` идёт SSH-ем на `secrets.VPS_SSH_HOST` и делает
`git reset --hard` + `compose up -d --build`. Пока секрет указывает на старый IP, любой push
поднимает то, что вы только что погасили (M1). Порядок: не пушить в `master` в окне переезда →
после приёмки новой машины сменить `VPS_SSH_HOST` (и при новом ключе — `VPS_SSH_KEY`) → сделать
контрольный push и убедиться, что job зелёный.

**M4. Ключ шифрования отдельно от дампа.** Дамп Postgres несёт `oauth_tokens` **зашифрованными**;
восстановление БЕЗ того же `SECRETS_ENCRYPTION_KEY` = аккаунты придётся регистрировать заново
(`scripts/register_account.py`). Экспорт кладёт `.env` в тот же архив — и именно поэтому архив
целиком секрет (права 600, вывоз только `gpg`/`age`, правило 5).

**M5. То, что живёт вне видимых мест.** `MemoryMax` дашборда лежит в
`/etc/systemd/system.control/hermes-dashboard.service.d/` (его пишет `systemctl set-property`, а не
человек) — без него машина возвращается к oom-kill (§14.1). `loginctl enable-linger root` — без
него user-юнит gateway умирает при выходе из SSH (§2). Обе вещи не «конфиг», а условие работы;
`vps_migrate_import.sh` восстанавливает их явно, `vps_migrate_verify.sh` проверяет.

### 16.4. Вариант B — снапшот-клон (пошагово)

```bash
# 1. РЕПЕТИЦИЯ (прод работает, ничего не гасим): проверить, что экспорт вообще собирается.
#    Архив пригодится и как офсайт-бэкап, и как страховка на случай битого снапшота.
sh /opt/aimash/scripts/vps_migrate_export.sh
gpg -c --cipher-algo AES256 /root/vps-migration/aimash-migration-<ts>.tgz   # вывозить только .gpg
#    → забрать .gpg на локальную машину (scp), проверить, что открывается.

# 2. ОКНО. Погасить прод (снапшот живой БД технически возможен, но это crash-consistent копия —
#    Postgres придётся восстанавливать по WAL; выключенный сервер даёт чистую копию).
cd /opt/aimash && docker compose stop bot scheduler && hermes gateway stop
sh /opt/aimash/scripts/vps_migrate_export.sh --cutover      # финальный архив УЖЕ погашенного прода
docker compose down                                          # включая postgres
shutdown -h now                                              # снапшот снимаем с выключенной машины
```
```
# 3. Консоль Hetzner → старый сервер → Snapshots → Take snapshot (имя: pre-migration-<дата>).
# 4. Images → снапшот → Create Server: целевой тип (16.0), ТА ЖЕ локация (или новая — здесь можно),
#    тот же SSH-ключ. Архитектура снапшота и плана обязаны совпадать (x86 → CX/CPX/CCX).
# 5. Новый сервер загрузился. Firewall: если на старом висел Hetzner Firewall — приложить тот же.
```
```bash
# 6. На НОВОЙ машине (по новому IP), ДО подъёма прода:
hostnamectl set-hostname aimash-prod        # снапшот принёс имя старой машины
systemctl stop tailscaled                   # у клона тот же node-key, что у старого узла
rm -f /var/lib/tailscale/tailscaled.state   # иначе два узла спорят за одну identity (M2)
systemctl start tailscaled
docker ps                                   # контейнеры подняты из томов; при `down` перед снапшотом — пусто
```
```bash
# 7. Tailnet (M2): сначала УДАЛИТЬ старый узел в admin-консоли Tailscale, затем здесь:
tailscale up --hostname hermes-vps
tailscale serve --bg 9120                   # цепочка §14; serve-конфиг узла не переносится
tailscale funnel status                     # обязано быть выключено (К3)

# 8. Поднять прод — только теперь, когда старая машина погашена и не поллит (M1):
cd /opt/aimash && export GIT_SHA="$(git rev-parse --short HEAD)" && docker compose up -d
systemctl enable --now hermes-dashboard hermes-dash-proxy hermes-backup.timer
loginctl enable-linger root && hermes gateway start

# 9. Приёмка:
EXPECT_RAM_GB=16 sh /opt/aimash/scripts/vps_migrate_verify.sh --deep
```
```
# 10. M3: GitHub → Settings → Secrets → VPS_SSH_HOST = новый IP. Контрольный push в master,
#     job `deploy` обязан быть зелёным (он же прогоняет пост-деплойную проверку контейнеров).
```

### 16.5. Вариант C — чистая установка (Arm64, смена дистрибутива, недоверие к хосту)

```bash
# 1–2. Как в 16.4: репетиция экспорта, затем окно + `--cutover`, архив вывезти шифрованным.
# 3. Новый сервер в консоли Hetzner: Ubuntu 24.04, целевой тип, тот же SSH-ключ, Firewall.
# 4. База хоста:
apt-get update && apt-get install -y ca-certificates curl git sqlite3
curl -fsSL https://get.docker.com | sh          # версию сверить с MANIFEST (docker/compose)
git clone <репозиторий> /opt/aimash && cd /opt/aimash && git checkout master

# 5. Перенести архив (.gpg), расшифровать на новой машине в /root (НЕ в /opt/aimash — правило 5):
gpg -d aimash-migration-<ts>.tgz.gpg > /root/aimash-migration-<ts>.tgz && chmod 600 /root/*.tgz

# 6. Механическая часть — одним скриптом (он же проверяет sha256, отказывается работать на
#    машине-источнике и НЕ поднимает поллеры Telegram без явного флага):
sh /opt/aimash/scripts/vps_migrate_import.sh --from /root/aimash-migration-<ts>.tgz

# 7. Hermes — установка с ПИНОМ (версия из deploy/hermes/PIN.json, коммит — из MANIFEST архива):
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash    # + пин, RB-1 (README.md)
hermes version                                  # обязано совпасть с PIN.json release
#    config.yaml/.env/SOUL.md/skills НЕ настраивать заново — их принёс шаг 6 (/root/.hermes).

# 8. Caddy той же версии (бинарь в архив не кладётся осознанно — тянуть чужой бинарь через tgz
#    хуже, чем поставить из источника): версия — в MANIFEST, строка «caddy binary».

# 9. Tailscale: установить, затем M2-порядок (удалить старый узел → up --hostname hermes-vps →
#    serve --bg 9120 → funnel обязан быть off).

# 10. Поднять прод и gateway, когда старая машина погашена:
sh /opt/aimash/scripts/vps_migrate_import.sh --from /root/aimash-migration-<ts>.tgz --start-app --start-gateway

# 11. Приёмка + M3 (GitHub secret), как в 16.4 шаги 9–10.
```

⚠️ **Arm64 (CAX) — не «просто дешевле».** Снапшот-путь там закрыт по построению (архитектуры обязаны
совпадать), то есть только вариант C. Плюс до решения обязательно проверить сборку на самой машине:
базовые образы (`python:3.12-slim`, `postgres:16`, `caddy`) multi-arch, но `grpcio`/`google-ads`
тянут бинарные колёса, а установщик Hermes под `aarch64` **никем не проверялся** — сначала
собрать образ и `hermes version` на пробной машине, только потом переезжать. [Не проверено]

### 16.6. Гейт выхода — что должно быть зелёным ДО удаления старой машины

1. `vps_migrate_verify.sh --deep` → `FAIL=0` (RAM ≥ цели, оба контейнера без роста RestartCount,
   409 в логах нет, `alembic_version` совпадает с источником, порты пульта только на loopback,
   funnel off, дашборд 200, версия Hermes = пин, MCP отдаёт инструменты).
2. Живой READ через Telegram: в топике клиента спросить статистику — ответ пришёл, в audit/логах
   нет ошибок расшифровки токенов (то есть `SECRETS_ENCRYPTION_KEY` действительно тот).
3. Job `deploy` зелёный на новый `VPS_SSH_HOST`.
4. Бэкапы на новой машине реально идут: `ls -1t /opt/aimash/backups | head -2` и
   `systemctl list-timers hermes-backup.timer` (NEXT/LEFT непусто).
5. **Сутки наблюдения**: `journalctl -k --since -24h | grep -c oom-kill` = 0.

Только после этого: старый сервер — Delete (снапшот `pre-migration-*` подержать ещё неделю, он
платный, но дешевле повторного переезда), старые архивы экспорта — стереть (`shred -u`, в них
секреты открытым текстом).

### 16.7. Откат

До шага «Delete старого сервера» откат стоит минут: погасить новую машину (`docker compose down`,
`hermes gateway stop`, `systemctl stop hermes-dashboard hermes-dash-proxy`), удалить её узел из
tailnet, включить старый сервер, `tailscale up --hostname hermes-vps`, `docker compose up -d`,
`hermes gateway start`, вернуть `VPS_SSH_HOST` на старый IP. Единственное, что при этом теряется, —
данные, записанные новой машиной после cutover; поэтому окно переезда держать коротким, а не
«поработаем недельку на новой и решим».
