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

> **Scope.** Скрипт запрашивает только `adwords` (чтение/запись Google Ads). Для команды `/sheets`
> (выгрузка отчёта в Google Sheets) нужен **дополнительный** scope `drive.file` — это отдельная
> перевыдача токена с обоими scope; шаги — в [DEPLOYMENT.md §Google Sheets-экспорт](DEPLOYMENT.md).
> Без него `.xlsx` через `/export` работает всегда, а `/sheets` отвечает понятной ошибкой.

### 5. Проверка доступа (read-only)
```bash
make check-access             # = python scripts/check_access.py
```
Делает только чтение на разрешённом аккаунте. Если видишь статистику/кампании — доступ настроен.

## Частые ошибки
- **«Google hasn't verified this app»** на шаге 4 → Advanced → Go to … (нормально для своего dev-приложения).
- **«Access blocked»** → gmail не в Test users (шаг 3.4) либо consent screen не настроен.
- **refresh_token пуст** → Google вернул только access-токен; перезапусти `make refresh-token`
  (он форсит `prompt=consent`, чтобы выдать refresh).
- **`PermissionError` про allow-list/потолок** при запуске → проверь
  `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS=7753643025` (пусто = fail-closed) и что не указываешь чужой id.

Дальше: запуск и прод — [DEPLOYMENT.md](DEPLOYMENT.md); гарантии безопасности — [SECURITY.md](SECURITY.md).
