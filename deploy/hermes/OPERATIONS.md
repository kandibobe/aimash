# Ранбук эксплуатации Hermes (Контур A) — запуск, редеплой, настройка, траблшутинг

День-2 операции Hermes-агента READ-пилота. **Первичная установка — в [`README.md`](README.md)** (RB-0…RB-3);
здесь — то, что после установки: жизненный цикл сервиса, применение изменений конфига, взаимодействие с
авто-деплоем боевого бота, обновление/откат, бэкап, kill-switch, диагностика.

> **Дисциплина К10 (читать первым).** Hermes **молча игнорирует неизвестные/опечатанные ключи** конфига —
> «на вид работает, по факту нет». `hermes config check` этого **НЕ** ловит (он про «missing or stale», не про
> валидацию). Все факты ниже сверены с офиц. доками, но доки — ветка `main`, а на VPS стоит **v0.17**; команда/
> ключ из доков может отличаться от бинаря. Поэтому: **после любой правки конфига** проверяй, что значение реально
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
| `mcp_servers.*` (наш `aimash`) | `hermes gateway restart` (или `/reload-mcp` в чате) [Certain] |
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
инструментов [Likely]. Чтобы позитивно убедиться, что 12 READ-tools живые — в чате `/reload-mcp` и спросить агента
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

> ⚠️ **Дырка в автоматике (закрыть решением владельца).** Деплой-скрипт (SSH-шаг CI) **не** делает reconnect Hermes
> после билда — значит каждый деплой тихо рвёт MCP до ручного вмешательства. Правильно — дописать в деплой-шаг
> `hermes gateway restart` **после** healthcheck'а `aimash-bot`. **Но:** это связывает Контур B (деплой) с Контуром
> A и зависит от того, под каким юзером ходит `VPS_SSH_USER` — если это не root, он **не достучится** до
> root-овского `--user`-gateway (`systemctl --user`/`hermes gateway` бьют по сервису своего юзера). Поэтому не
> вписываю в `ci.yml` вслепую — решение и юзер за владельцем. До автоматизации reconnect ручной (команда выше).

---

## 6. Смена модели / провайдера / тулсетов после установки

**Модель/провайдер** — канонически через мастер, он пишет `model.provider`+`model.default` [Certain]:
```bash
hermes model                         # интерактивно: провайдер (OpenRouter), ключ, модель (§15 openai/gpt-5.6-*)
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
`sessions/`. Потеря невосстановима. Заведи отдельный бэкап (шифрованный — там секреты, правило 5):

```bash
tar czf /root/hermes-backup-$(date +%F).tgz -C /root .hermes
#  и вывези с хоста в защищённое хранилище; НЕ коммить, НЕ в общие логи.
```

---

## 9. Kill-switch и лимиты трат

`confirm`-гейт защищает бюджет **Google Ads**, а не трату **LLM** (Hermes → OpenRouter напрямую, мимо нашего кода).
Потолок — только платформенный (см. также [`README.md`](README.md) RB-3):

- **Дневной потолок:** OpenRouter Provisioning-ключ профиля с `limit: <USD>` + `limit_reset: daily`.
- **Kill-switch:** деактивация ключа в OpenRouter (account-wide — гасит все прогоны). Плюс «мягкий»: `hermes gateway
  stop` (боевой бот при этом жив).
- `usage.cost`/resolved `model` в наш audit-row **не** попадают — LLM-трафик мимо кода (§20 открытый хвост).

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

## Открытые проверки на бинаре v0.17 (доки = main, могут расходиться)

- `hermes config get/unset` — документированы только в user-guide, для v0.17 не подтверждены → предпочитать
  `hermes config show` / `hermes status`.
- Точное имя дефолт-профиля юнита, поведение auto-reconnect MCP, дефолт `timeout` MCP (300 vs 120), env-vs-yaml
  precedence для Telegram-allowlist — сверить на месте (`hermes gateway list`, `--help`, эксперимент).
- Имя MCP-сервера **везде одинаковое**: `aimash` (= ключ в `mcp_servers`, = аргумент `hermes mcp test`, = цель
  `/reload-mcp`). Рассинхрон имени → ошибка.
