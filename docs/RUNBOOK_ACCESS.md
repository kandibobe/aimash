# Ранбук: доступы (аккаунты Google Ads + админка бота)

Практические шаги для двух типовых задач владельца: «открыть человеку чтение аккаунта / всего
дерева» и «выдать админку». Термины: **read-замок** = `ads.client.ensure_read_allowed`
(что бот вообще может читать), **грант** = пер-пользовательская запись `account_access`
(кто из операторов какой аккаунт видит), **админ** = env `ADMIN_CHAT_IDS` ∪ таблица `admins`
(рантайм `/addadmin`). Мутации ВСЕГДА отдельный замок (`ensure_allowed`, Draft-потолок) —
ничто из этого ранбука их не открывает.

## 1. Открыть чтение аккаунта (пример: 1674890775, «читаться должно всё, что есть»)

### Шаг 1 — классифицировать аккаунт (Google Ads UI → Настройки → Доступ и безопасность)
- **MCC (менеджерский)?** → в `.env` на VPS добавить его в `GOOGLE_ADS_LOGIN_CUSTOMER_IDS`
  (CSV) и **перезапустить бота** (env читается на старте). Обход MCC на старте обнаружит все
  ENABLED-дочерние — «всё, что есть на аккаунте» станет читаемым автоматически.
  Если OAuth-пользователь бота не имеет доступа к этому MCC — сперва пригласить его в MCC
  (или `python -m scripts.register_account --account <id> --login 1674890775` для
  per-account токена; нужен `SECRETS_ENCRYPTION_KEY`).
- **Обычный (leaf) под УЖЕ настроенным MCC?** → ничего в конфиге не нужно: `/refresh` в боте
  (пере-обход без рестарта) → проверить `/accounts`. Если не появился — глянуть лог
  «mcc discover» (сколько ENABLED/inactive найдено): не-ENABLED дети читаются только явным
  `/account <id>` (осознанно скрыты из пикеров).
- **Leaf ВНЕ наших MCC?** → `.env`: добавить в `GOOGLE_ADS_READ_CUSTOMER_IDS` +
  `scripts/register_account.py` (OAuth того, кто имеет доступ) + рестарт.

### Шаг 2 — открыть аккаунт оператору (если он не админ)
- Узнать chat_id оператора: он пишет боту `/whoami` (или бот echo-ит chat_id в отказе доступа).
- Админ: `/adduser <chat_id>` (если оператор ещё не в whitelist) → в inline-пикере выбрать
  «Все аккаунты» или конкретные; либо точечно `/grant <chat_id> <customer_id>`.
- ⚠️ В режиме `auto` ПЕРВЫЙ `/grant` включает пер-юзер изоляцию для всех не-админов.
- Админам гранты не нужны: админ читает всё read-allowed (bypass в `core.access`).

### Шаг 3 — проверить
- От имени оператора: `/accounts` (аккаунт в списке), `/report` → пикер → аккаунт виден,
  «📊 Все аккаунты (MCC)» даёт кросс-аккаунтную сводку; `/export` → «Все аккаунты (MCC)» →
  deep-xlsx с листом на каждый дочерний.

## 2. Выдать админку (себе и заказчику)

1. **Первый (bootstrap) админ — только env:** в `.env` на VPS `ADMIN_CHAT_IDS=<chat_id1>,<chat_id2>`
   + рестарт. Chat_id каждый узнаёт у бота: `/whoami` (бот должен отвечать — т.е. быть в whitelist;
   если нет — сперва `TELEGRAM_WHITELIST_CHAT_IDS` в env или `/adduser` от действующего админа).
2. **Дальше — рантайм, без рестарта:** действующий админ выдаёт `/addadmin <chat_id> [заметка]`
   (цель автоматически whitelist-ится). Снять: `/removeadmin <chat_id>`. Список: `/admins`.
3. Гарды от самоблокировки: env-админы в рантайме неснимаемы; нельзя снять себя; нельзя снять
   последнего админа. Сбой БД ⇒ действуют только env-админы (fail-closed).
4. Что даёт админка: `/grant` `/revoke` `/adduser` `/removeuser` `/users` `/addadmin`
   `/removeadmin` `/admins` `/mutready`, полный `/diag`, алерты об ошибках и weekly-digest,
   чтение ВСЕХ read-allowed аккаунтов без грантов. **Мутации НЕ открывает** — они по-прежнему
   только на Draft (или на аккаунт из `GOOGLE_ADS_ALLOWED_CUSTOMER_IDS`, решение владельца).

## 3. Быстрая диагностика
- `/whoami` — мой chat_id, активный аккаунт, режим доступа, админ ли я.
- `/accounts` — что мне открыто на чтение. `/refresh` — пере-обход MCC без рестарта.
- `/mutready [id]` — чек-лист готовности аккаунта к мутациям (админ).
- Таблицы ключей/отчёты Google Sheets создаются с доступом «всем по ссылке»
  (ключи — редактор, отчёты — читатель); если шаринг не удался, бот присылает пометку —
  тогда «Request access» в самой таблице.

## 4. Операционные роли и four-eyes

Роли `viewer|operator|approver|admin` не расширяют мутационный account allow-list. Выдать роль
может только действующий env/runtime admin (или обладатель `manage_roles`):

```bash
python -m scripts.manage_roles assign --actor <admin_user_id> --user <user_id> \
  --role approver --customer <customer_id>
python -m scripts.manage_roles revoke --actor <admin_user_id> --user <user_id> \
  --role approver --customer <customer_id>
```

Сначала выдать минимум двух разных identity и проверить `tests/test_operations_layer.py`, затем
включить `FOUR_EYES_REQUIRED=true` и `FOUR_EYES_RISK_TIERS_CSV=L3`. Если роли/vote нет, автор
неизвестен, approve поставил автор или существует reject, L3 остаётся `confirmed` и не claim-ится.
Существующий Telegram UI ещё не публикует экран голосования; до trusted UI/cutover настройку на
live включать нельзя. SQL-вставки вручную не являются штатной процедурой.
