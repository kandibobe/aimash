# Google Ads: доступы и OAuth (онбординг)

Как с нуля получить доступ к Google Ads API для Aimash. Это про **получение ключей**; их хранение
и деплой — в [DEPLOYMENT.md](DEPLOYMENT.md). Напоминание: бот работает **только** на TEST MCC при
разработке и **только** на аккаунте `Aimash (Draft)` = `7753643025` (замок в коде, см.
[SECURITY.md](SECURITY.md)).

## Что нужно получить
| Ключ | Где | env-переменная |
|---|---|---|
| Test MCC + тест-аккаунты | ads.google.com → создать менеджерский **тест**-аккаунт | — |
| developer token (**Basic**) | Google Ads → Tools → API Center (у Антона уже есть) | `GOOGLE_ADS_DEVELOPER_TOKEN` |
| OAuth client (Desktop) | Google Cloud Console → APIs & Services → Credentials | `GOOGLE_ADS_CLIENT_ID`, `GOOGLE_ADS_CLIENT_SECRET` |
| refresh token | скрипт ниже | `GOOGLE_ADS_REFRESH_TOKEN` |
| MCC id (контекст авторизации) | id менеджерского аккаунта | `GOOGLE_ADS_LOGIN_CUSTOMER_ID` |

## Шаги

### 1. Test MCC и тест-аккаунт
Создай **менеджерский тест-аккаунт** на ads.google.com (бесплатно, без биллинга) и под ним
тест-клиента. На тест-аккаунтах Keyword Planner отдаёт идеи, но метрики показов/кликов пустые —
это нормально (см. память проекта про Basic-токен).

### 2. Developer token
В Google Ads API Center. Уровень **Basic** достаточен (работает и на тестовых, и на боевых
аккаунтах с лимитами). Не печатать, не коммитить — только в `.env` / секрет-менеджер.

### 3. OAuth-клиент (Desktop app)
В Google Cloud Console (тот же проект, что и developer token):
1. APIs & Services → **Enable** «Google Ads API».
2. Credentials → Create credentials → **OAuth client ID** → тип **Desktop app**.
3. Скопируй `client_id` / `client_secret` в `.env`.
4. OAuth consent screen → добавь свой gmail в **Test users** (иначе на шаге 4 будет «Access blocked»).

### 4. Refresh token
```bash
cp .env.example .env          # заполни CLIENT_ID/SECRET и DEVELOPER_TOKEN
make refresh-token            # = python scripts/get_refresh_token.py
```
Откроется браузер: войди gmail-ом с доступом к аккаунту Aimash, разреши доступ. Скрипт впишет
`GOOGLE_ADS_REFRESH_TOKEN` в `.env` **сам** (токен не печатается в консоль). Если refresh_token
пуст — перезапусти (скрипт запрашивает согласие с `prompt=consent`).

> **Scope.** Скрипт запрашивает **все три** scope в одном flow: `adwords` (чтение/запись Google
> Ads), `drive.file` и `spreadsheets.readonly` (для `/sheets` — выгрузки отчёта в Google Sheets) —
> см. `SCOPES` в `scripts/get_refresh_token.py`. Важно на **экране согласия отметить все три
> доступа**: набор фиксируется в момент consent, добор scope на refresh невозможен (см. раздел
> «Google Sheets экспорт — scope и re-auth» ниже). Без scope Sheets `.xlsx` через `/export`
> работает всегда, а `/sheets` отвечает понятной ошибкой.
> Токен аккаунта-**хранилища** таблиц (`--sheets`) — это отдельный, более узкий consent (только
> `drive.file`): см. «Два токена — и sensitive-scope только на одном из них» ниже.

### 5. Проверка доступа (read-only)
```bash
make check-access             # = python scripts/check_access.py
```
Делает только чтение на разрешённом аккаунте. Если видишь статистику/кампании — доступ настроен.

## Google Sheets экспорт — scope и re-auth

Экспорт отчётов существует в двух формах, и **только одна** из них требует Google-OAuth:

| Форма | Команда | Требует OAuth-scope? | Реализация |
|---|---|---|---|
| `.xlsx` вложением (файл) | `/export` | **Нет** — локальный файл через openpyxl | `reports/xlsx.py`, `keywords/export.py` |
| Google Sheets (ссылка на таблицу) | `/sheets` | **Да** — `drive.file` (+ `spreadsheets.readonly` для §19.4.1) | `reports/sheets.py` |

### `.xlsx` работает без Google-доступа
`/export` строит книгу целиком локально: `reports.xlsx.write_report_xlsx` собирает `Workbook`
через openpyxl (`reports/xlsx.py:115-118`, импорт `from openpyxl import Workbook` —
`reports/xlsx.py:10`), файл кладётся во временный путь и отправляется вложением
(`bot/main.py:989-993`). Ни `Credentials`, ни Sheets API тут не задействованы — экспорт
ключей в `.xlsx`/CSV аналогично строится только на openpyxl (`keywords/export.py:1,10`).
Поэтому `.xlsx` доступен всегда, даже если refresh-токен выдан только со scope `adwords`.

### Google Sheets требует отдельный scope на deploy-токене
`/sheets` **создаёт** новую Google-таблицу (`spreadsheets.create` + `values.batchUpdate`,
`reports/sheets.py:130-149`), поэтому нужен OAuth-scope, которого нет у Google Ads-токена
(`adwords`):
- **`https://www.googleapis.com/auth/drive.file`** — минимально достаточный для СОЗДАНИЯ
  таблиц (доступ только к файлам, созданным приложением): `SHEETS_SCOPE` в
  `reports/sheets.py:25-27`. Это единственный scope, нужный для `/sheets` и §19.4.2 (выгрузка
  ключей). У Google он **non-sensitive** — верификация приложения для него не нужна.
- **`https://www.googleapis.com/auth/spreadsheets.readonly`** — дополнительно, чтобы ЧИТАТЬ
  произвольную таблицу менеджера (§19.4.1 «Ссылка на Google Sheets»): `SHEETS_READONLY_SCOPE`
  в `reports/sheets.py:28-33`. `drive.file` видит только созданное ботом, `readonly` — любую
  доступную пользователю таблицу. У Google это **sensitive**-scope.

### Два токена — и sensitive-scope только на одном из них
`SHEETS_REFRESH_TOKEN` (аккаунт-**хранилище** таблиц, см. ниже) просит **ровно один** scope —
`drive.file` (`SHEETS_SCOPES` в `reports/sheets.py:34-37`, дубль в
`scripts/get_refresh_token.py:62-66`; расхождение ловит `tests/test_keyword_sheets.py`).
Добавить туда `spreadsheets.readonly` **нельзя**: это sensitive-scope, и неверифицированному
приложению Google на нём отвечает **«This app is blocked. This app tried to access sensitive
info in your Google Account»** — владелец аккаунта не сможет выдать согласие вовсе (напоролись
2026-07 на аккаунте заказчика). Верификация приложения в Google — недели (домен, homepage,
privacy policy, демо-видео).

Sensitive-scope несёт **Ads-токен** (`GOOGLE_ADS_REFRESH_TOKEN`, наш аккаунт, consent давно
выдан): `SCOPES = [adwords, drive.file, spreadsheets.readonly]`. Отсюда разведение кредов в
`reports/sheets._oauth_credentials(external_read=…)`:

| Операция | Чьи креды | Scope |
|---|---|---|
| создание таблицы, шаринг, чтение СВОЕЙ таблицы (§19.4.2 round-trip, `own_file=True`) | аккаунт-хранилище (`SHEETS_REFRESH_TOKEN`) | `drive.file` |
| чтение ЧУЖОЙ таблицы (§19.4.1, `/kw add`) | Ads-токен | `spreadsheets.readonly` |

Следствие: **чужая** таблица с ключами должна быть доступна именно **Ads**-аккаунту — «всем, у
кого есть ссылка» либо расшарена на него. Иначе Sheets отдаст 403, бот его ловит и просит
прислать ключи текстом.

### Re-auth обязателен, чтобы выдать эти scope на deploy-токене
Токен, полученный только с `adwords`, для `/sheets` не годится — нужна **перевыдача**
refresh-токена с полным набором scope. `scripts/get_refresh_token.py` запрашивает все три
scope сразу (`SCOPES`, `scripts/get_refresh_token.py:67-73`), поэтому re-auth = повторный
прогон `make refresh-token`, на экране согласия отметить ВСЕ три доступа. Токен
аккаунта-хранилища выпускается отдельно: `python scripts/get_refresh_token.py --sheets` — там
на экране согласия будет **один** доступ (`drive.file`). Скрипт проверяет, что `drive.file`
действительно выдан, и предупреждает, если нет.

Важно: на **refresh** scope НЕ передаётся (`scopes=None` в `_oauth_credentials`,
`reports/sheets.py`) — иначе Google вернёт `invalid_scope`, если токен был выдан без
`readonly`. Набор scope фиксируется в момент consent, а не на обновлении токена. Поэтому
именно re-auth (новый consent), а не правка кода, включает Sheets-экспорт.

### Поведение без scope (fallback)
- `/sheets`: при отсутствии `drive.file` вызов падает, бот отвечает понятной ошибкой через
  `err_sheets` (`bot/main.py:1029-1031`) — без утечки сырого текста исключения.
- §19.4.2 (выгрузка сгенерированных ключей): при сбое Sheets визард откатывается на прямое
  сохранение списка без round-trip через таблицу (`bot/main.py:3364-3368`).
- Для скачивания отчёта менеджер всегда может использовать `/export` (`.xlsx`) — он не зависит
  от Sheets-настройки.

Полные шаги включения (в т.ч. активация Google Sheets API в Cloud-проекте) —
[DEPLOYMENT.md §Google Sheets-экспорт](DEPLOYMENT.md).

## Частые ошибки
- **«Google hasn't verified this app»** на шаге 4 → Advanced → Go to … (нормально для своего dev-приложения).
- **«Access blocked»** → gmail не в Test users (шаг 3.4) либо consent screen не настроен.
- **refresh_token пуст** → Google вернул только access-токен; перезапусти `make refresh-token`
  (он форсит `prompt=consent`, чтобы выдать refresh).
- **`PermissionError` про allow-list/потолок** при запуске → проверь
  `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS=7753643025` (пусто = fail-closed) и что не указываешь чужой id.

Дальше: запуск и прод — [DEPLOYMENT.md](DEPLOYMENT.md); гарантии безопасности — [SECURITY.md](SECURITY.md).
