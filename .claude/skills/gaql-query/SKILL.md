---
name: gaql-query
description: Помогает писать и валидировать GAQL-запросы к Google Ads API (чтение статистики, кампаний, ключей по MCC и дочерним аккаунтам). Использовать при любом чтении данных Google Ads.
---

# GAQL — запросы к Google Ads

GAQL = язык запросов Google Ads (как SQL). Для отчётности и чтения.

## Правила
- Для чтения предпочитай **`SearchStream`** (`GoogleAdsService.SearchStream`): одна страница до 10 000 строк = **1 операция** против дневной квоты. `Search` (paged) тратит больше операций.
- Мульти-аккаунт: сначала перечисли дочерние через `customer_client` на менеджерском аккаунте, затем по каждому `customer_id` свой GAQL.
- Фильтр по датам — поле `segments.date` с оператором `BETWEEN 'YYYY-MM-DD' AND 'YYYY-MM-DD'`.
- `login_customer_id` = ID менеджерского (MCC). При `ENV=dev` — только TEST MCC.
- Кэшируй чтение: дневной cap операций на токен — узкое место MCC (Basic = 15 000/сут).

## Каркас
```sql
SELECT
  campaign.id, campaign.name, campaign.status,
  metrics.impressions, metrics.clicks, metrics.ctr,
  metrics.average_cpc, metrics.cost_micros, metrics.conversions,
  metrics.conversions_value
FROM campaign
WHERE segments.date BETWEEN '2026-05-01' AND '2026-05-31'
ORDER BY metrics.cost_micros DESC
```

## Частые ресурсы
- `customer_client` — обход иерархии MCC (поля: `customer_client.id`, `.descriptive_name`, `.manager`, `.level`).
- `campaign`, `ad_group`, `ad_group_criterion` (ключи), `ad_group_ad` (объявления).
- Деньги — в **micros** (1 USD = 1 000 000 micros). Делить на 1e6 при выводе.

## Чеклист
- [ ] `SearchStream`, а не paged `Search`
- [ ] даты через `segments.date BETWEEN`
- [ ] деньги из micros переведены
- [ ] для MCC — обход через `customer_client`
- [ ] запрос проверен на TEST MCC
