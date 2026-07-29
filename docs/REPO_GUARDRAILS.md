# Репо-рубежи: защита денежного пути от агента-билдера

**Зачем.** Сегодня `merge в master` == авто-деплой в прод: `.github/workflows/ci.yml:82` на push в `master` делает `git reset --hard origin/master` + `docker compose up -d --build` на VPS. Значит право мержить == право снять confirm-гейт (`confirm/**`, `core/provenance.py`, `ads/client.py`) и потом «подтвердить себе» мутацию. Контур B (агент-билдер, SPEC §5 / HERMES_SPEC §19) приносит **Pull Request**, а не merge; принимает — человек.

**Статус:** `.github/CODEOWNERS` — в репозитории. Но **сам по себе он ничего не энфорсит** — вступает в силу только вместе с ruleset ниже. Настройка — в UI GitHub, выполняет владелец репо (Settings → Rules → Rulesets). Ниже — что именно включить.

---

## 1. Ruleset на `master`

Settings → Rules → Rulesets → **New branch ruleset**.

| Настройка | Значение | Почему |
|---|---|---|
| Enforcement status | **Active** | Иначе правило висит, но не действует |
| Target branches | `master` | Единственная линия. `main` была orphan-веткой (пустой merge-base), удалена 29.07 под тегом `archive/main-v3.2`; триггер `ci.yml` больше её не слушает |
| **Require a pull request before merging** | ✅ | Прямой push в master запрещён — только через PR |
| — Required approvals | **≥ 1** | Человек читает диф |
| — **Require review from Code Owners** | ✅ | Пути из `CODEOWNERS` требуют ревью владельца |
| — Dismiss stale approvals on new commits | ✅ | Одобрили одно, дописали другое — переодобрить |
| **Require status checks to pass** | ✅ `lint-test`, `secret-scan` | Не катим сломанное/с секретом (`ci.yml`) |
| **Block force pushes** | ✅ | Нельзя переписать историю master |
| Restrict deletions | ✅ | Нельзя снести ветку |
| **Bypass list** | **ПУСТО** | ⚠️ Ключевое. Любой bypass-актор = дыра: он обходит и ревью, и статусы. Ни владелец, ни machine-аккаунт сюда не добавляются |

**Проверка:** после включения `git push origin master` напрямую (даже владельцем) должен отклоняться с «protected branch»; PR без ревью Code Owner — не мержиться.

---

## 2. Machine-аккаунт билдера — без права merge

Контур B пушит **ветки**, открывает PR, и на этом его права кончаются.

- Отдельный GitHub-аккаунт (или fine-grained PAT) с `Contents: write` на репозиторий — этого хватает, чтобы пушить ветку и открыть PR.
- **НЕ добавлять** его в bypass-list ruleset'а и **НЕ давать** ему admin/maintain. Под активным ruleset'ом даже write-токен физически не может пушить в master напрямую — только PR.
- Токен билдера живёт в **песочнице Контура B**, не рядом с боевыми OAuth Google Ads (SPEC §5: ноль общих секретов).
- `VPS_SSH_*`, `OPENROUTER_API_KEY`, `SECRETS_ENCRYPTION_KEY` билдеру недоступны — деплой добывает человек через merge.

---

## 3. Новый модуль мутаций — отдельный человеческий гейт

Диф трогает `confirm/**`, `ads/mutations.py`, новый write-модуль или `pyproject.toml`/lock → сверх Code-Owner-ревью прогнать **скилом `confirm-gate-audit`** руками (SPEC §14). Причина: класс правил 1/2/9 (гейт, провенанс, замок) обходится через новый файл, если его никто не проверил прицельно.

Supply-chain: диф добавляет зависимость → проверить, что пакет существовал до сегодня (slopsquatting — агент ставит галлюцинированное имя, злоумышленник регистрирует заранее). `pip-audit` ловит известные CVE, но не это.

---

## 4. `/opt/aimash` на VPS — deploy-чекаут, а не рабочее дерево

Причина расхождения линий 27–29.07 была механической, а не человеческой невнимательностью: на сервере лежит полноценный git-чекаут с правом коммитить, а деплой делает `git reset --hard origin/master` **по текущей ветке, какой бы она ни была**. Достаточно один раз увести дерево на другую ветку — и деплой продолжает рапортовать «зелёно», обновляя не то, что собрано в рантайме. Так и вышло: контейнеры собраны из `master`, дерево стояло на `main`, и увидеть это было негде (маркер версии в образе появился только в `v1.0.0`).

- Работа через Hermes на VPS **не коммитит в `/opt/aimash`**. Правка кода — только `master` → PR → CI → авто-деплой. Ветка на сервере всегда `master`.
- Расхождение теперь **видно снаружи**: readiness-пинг админам печатает `AIMASH_GIT_SHA` (проставляет CI из `git rev-parse HEAD` **после** `reset --hard`). Не совпал с `git ls-remote origin master` — дерево или образ разъехались, разбираться до, а не после следующего деплоя.
- ⚠️ `docker exec … git rev-parse` внутри контейнера не работает и работать не будет: `.dockerignore` исключает `.git`. Единственный канал версии — build-arg `GIT_SHA`.

---

## Verification (end-to-end)

1. Ruleset Active на `master` → прямой push отклонён.
2. PR билдера в путь из `CODEOWNERS` → требует approve владельца, без него merge заблокирован.
3. `lint-test` красный → merge заблокирован (`ci.yml` статусы).
4. Bypass-list пуст → `gh api repos/:owner/:repo/rulesets` не показывает bypass_actors.
