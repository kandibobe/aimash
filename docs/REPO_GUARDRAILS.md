# Репо-рубежи: защита денежного пути от агента-билдера

**Зачем.** Сегодня `merge в master` == авто-деплой в прод: `.github/workflows/ci.yml:84` на push в `master` делает `git reset --hard origin/master` + `docker compose up -d --build` на VPS. Значит право мержить == право снять confirm-гейт (`confirm/**`, `core/provenance.py`, `ads/client.py`) и потом «подтвердить себе» мутацию. Контур B (агент-билдер, SPEC §5 / HERMES_SPEC §19) приносит **Pull Request**, а не merge; принимает — человек.

**Статус:** `.github/CODEOWNERS` — в репозитории. Но **сам по себе он ничего не энфорсит** — вступает в силу только вместе с ruleset ниже. Конфигурация — не «галочки в UI», а два версионируемых payload'а в [`.github/rulesets/`](../.github/rulesets/): рубеж, который живёт только в чужом UI, невозможно ни отревьюить, ни воспроизвести — именно поэтому он и не был включён до 30.07.2026.

---

## 1. Ruleset на `master` — их ДВА, и это не дробление ради красоты

Прежняя редакция §1 описывала один ruleset и была **неисполнима**: «Require a pull request» + «Required approvals ≥ 1» + «Bypass list ПУСТО» при единственном человеке, который же и единственный владелец во всех правилах `CODEOWNERS`, означает, что свой PR одобрить нечем — merge в `master` не проходит НИКОГДА, то есть авто-деплой умирает насмерть. Это не придирка к формулировке: старая «Проверка» прямо требовала, чтобы push отклонялся «даже владельцем». §1 был написан под мир §2, где автор дифа — билдер, а ревьюер — человек; в сегодняшнем мире автор и ревьюер — одно лицо.

Разделение на два ruleset'а вынужденное: **`bypass_actors` действует на ruleset целиком, а не на правило.** Один ruleset с владельцем в bypass снял бы с него и запрет force-push. Правила разных ruleset'ов агрегируются (применяется самое строгое), поэтому нужный результат достигается только парой.

### 1.1 `master-history` — связывает ВСЕХ, включая владельца

[`.github/rulesets/master-history.json`](../.github/rulesets/master-history.json)

| Правило | API-имя | Почему именно оно, и почему bypass пуст |
|---|---|---|
| Block force pushes | `non_fast_forward` | Нельзя переписать историю `master`. Обычный fast-forward push **не задевает** — поэтому владельца в bypass вносить не нужно, и дыры нет |
| Restrict deletions | `deletion` | Нельзя снести ветку. Тоже не лежит на пути обычного push |
| **Bypass list** | `[]` | ⚠️ Строка 24 прежней редакции выполняется здесь **буквально** — и возможно это ровно потому, что обходить нечего |

Цель — не билдер, а авария: агент под кредами владельца, `commit --amend` уже опубликованного, ошибочный `reset --hard` + force. Границей от Контура B это **не является** (см. 1.2).

⚠️ Цена: `git push --force`/`--force-with-lease` в `master` перестаёт работать и у владельца. Аварийный выход — `gh api -X PATCH repos/kandibobe/aimash/rulesets/{id} -f enforcement=disabled`, force push, вернуть `active`.

### 1.2 `master-review-gate` — связывает всех, КРОМЕ владельца

[`.github/rulesets/master-review-gate.json`](../.github/rulesets/master-review-gate.json)

| Правило | API-имя | Параметры |
|---|---|---|
| Require a pull request before merging | `pull_request` | `required_approving_review_count: 1`, `require_code_owner_review: true`, `dismiss_stale_reviews_on_push: true` |
| Require status checks to pass | `required_status_checks` | контексты **ровно** `lint-test` и `secret-scan` |
| **Bypass list** | `kandibobe` (`actor_type: "User"`, `actor_id: 215415996`, `bypass_mode: "always"`) | единственное исключение, см. ниже |

**Почему владелец в bypass — необходимость, а не компромисс.** Смысл правила `pull_request` не в том, чтобы связать владельца, а в том, чтобы **write-токен билдера физически не мог обновить `master`** — он в bypass не входит. Без владельца в bypass правило не «строже», а просто выключает деплой (см. преамбулу §1). Именно `actor_type: "User"` (персональный аккаунт), а **не `RepositoryRole` admin**: роль накрыла бы любого будущего админа и любую машину с этой ролью. `bypass_mode: "always"`, не `"exempt"`: `exempt` глушит правила целиком и не оставляет bypass-записи в audit log — для чужих денег худший вариант.

**Контексты — только `lint-test` и `secret-scan`.** `deploy` в этот список не вносить **никогда**: он существует только на push в `master` (`ci.yml:86`), на PR его нет вовсе, и требование обязательного отсутствующего чека блокирует merge вечно.

Отдельно, чтобы не переоценивать этот рубеж: «не катим сломанное» энфорсит не он, а `needs: [lint-test, secret-scan]` у джобы `deploy` (`ci.yml:85`) — красный CI не пускает деплой и без всякого ruleset. `required_status_checks` нужен для **merge PR-ов**, то есть для мира §2.

**Триггер снять владельца из bypass:** появление machine-аккаунта Контура B и переход владельца на PR-поток. Тогда §1 становится исполнимым буквально, потому что автор дифа и ревьюер — разные лица.

### 1.3 Применение — порядок обязателен

`master-history` сперва (риска нет). `master-review-gate` создаётся **`enforcement: "disabled"`**, потому что `actor_type: "User"` для repo-level bypass в REST-доках не подтверждён (есть в enum схемы, в UI-списке eligible — только роли/команды/приложения): сначала читаем, что GitHub фактически сохранил в `bypass_actors`, и только потом включаем. Активный PR-гейт с непроверенным bypass — это lockout, отличающийся от рабочей конфигурации одним полем.

```
gh api -X POST repos/kandibobe/aimash/rulesets --input .github/rulesets/master-history.json
gh api -X POST repos/kandibobe/aimash/rulesets --input .github/rulesets/master-review-gate.json
gh api repos/kandibobe/aimash/rulesets --jq '.[]|{id,name,enforcement}'
gh api repos/kandibobe/aimash/rulesets/{ID-review-gate} --jq '.bypass_actors'
gh api -X PATCH repos/kandibobe/aimash/rulesets/{ID-review-gate} -f enforcement=active
```

Если POST второго вернёт 422 на `bypass_actors` — выставить bypass в UI и прочитать фактический actor-объект тем же `GET`, а payload в репозитории привести к прочитанному. `RepositoryRole` с угаданным `actor_id` **не подставлять**: номера ролей не документированы, а ошибка в номере = bypass выдан роли `write`, то есть ровно билдеру.

---

## 2. Machine-аккаунт билдера — без права merge

Контур B пушит **ветки**, открывает PR, и на этом его права кончаются.

- Отдельный GitHub-аккаунт (или fine-grained PAT) с `Contents: write` на репозиторий — этого хватает, чтобы пушить ветку и открыть PR.
- **НЕ добавлять** его в bypass-list ни одного ruleset'а и **НЕ давать** ему admin/maintain. Под активным `master-review-gate` (§1.2) write-токен физически не может обновить `master` — только PR. ⚠️ Это свойство правила `pull_request`, а не «ruleset'а вообще»: пока `master-review-gate` стоит `disabled` (или содержит только §1.1), write-токен пушит в `master` напрямую → авто-деплой → `confirm/**` в проде без ревью. Выдавать билдеру `Contents: write` **до** включения §1.2 нельзя.
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

Проверяемо **сегодня** (руками владельца, оба пункта — живые действия, без них «ruleset работает» непроверенное утверждение):

1. `git push --force-with-lease origin master` на неизменённой истории → **отклонён** (`master-history`).
2. Обычный `git push origin master` с новым коммитом → **проходит**, и поднимаются `lint-test`/`secret-scan`/`deploy`. Рабочий путь цел.
3. `gh api repos/kandibobe/aimash/rulesets/{ID-history} --jq '.bypass_actors'` → `[]`.

Станет проверяемо **после появления билдера** (§2) — то, ради чего писался §1.2:

4. push билдерского токена прямо в `master` → отклонён; ветка + PR — проходят.
5. PR билдера в путь из `CODEOWNERS` → требует approve владельца, без него merge заблокирован.
6. `lint-test` красный → merge PR заблокирован (у прямого push это и так энфорсит `needs:` джобы `deploy`).

⚠️ Пункт «bypass-list пуст» из прежней редакции относится только к `master-history`. У `master-review-gate` он **непуст по проекту** (§1.2) — и это единственное узаконенное исключение: владелец как `User`; machine-аккаунт и `RepositoryRole` — никогда.
