---
name: gads-version
description: Проверка текущей версии Google Ads API и графика сансета перед работой с SDK. Использовать при настройке google-ads SDK, обновлении версии или подозрении на депрекейт.
---

# Версия Google Ads API и сансет

Google Ads API обновляется **ежемесячно** (с янв 2026): ~3–4 мажора в год + помесячные обратносовместимые минорки. Мажор живёт **~12 месяцев**, одновременно поддерживаются ~3 последних мажора. Бот без обслуживания сломается в течение года.

## ⚠️ Lib-версия ≠ API-версия (главная ловушка)
Версия пакета **`google-ads`** (PyPI) и версия **API** (`vNN`) — РАЗНЫЕ нумерации. Пакет бандлит несколько API-версий, версия выбирается на запрос (`client.get_service("GoogleAdsService", version="v24")`).

| Пакет `google-ads` | Дата | Добавляет API |
|---|---|---|
| **31.1.0** | 2026-06-24 | **v24.2** |
| 31.0.0 | 2026-05-13 | v24.1; убрал v20 |
| 30.1.0 | 2026-04-22 | **v24** (первая lib с v24) |
| 30.0.0 | 2026-03-25 | v23.2 |

→ Для API **v24** нужна lib **≥ 30.1**; для v24.2 — **31.1.0**. `27.x` НЕ умеет v24. Пин в `pyproject.toml`: **`google-ads>=31.1,<32`**.

## Что делать
1. Проверить актуальные версии: WebFetch/WebSearch на
   `https://developers.google.com/google-ads/api/docs/release-notes`,
   `.../docs/sunset-dates`, `https://pypi.org/project/google-ads/`,
   `https://github.com/googleads/google-ads-python/releases`.
2. На 2026-06-25 актуально: **API v24.2**, lib **google-ads 31.1.0**; **v24 сансет ~май 2027**.
3. **Пин API-версии** — в одном месте (`GOOGLE_ADS_API_VERSION=v24`); **пин SDK** — в `pyproject.toml` (`>=31.1,<32`).
4. При апгрейде — прочитать release notes на breaking changes (core-пути SearchStream / CampaignBudgetService / KeywordPlanIdeaService / customer_client в v23→v24 НЕ менялись).

## Правила
- Не хардкодить номер версии по коду — только конфиг (API) + pyproject (SDK).
- Перепроверять версию ежемесячно (заложить в обслуживание/ретейнер).
- При `RESOURCE_EXHAUSTED` / депрекейте — сверяться с докой, а не гадать.
- Полный список doc-ссылок — `docs/gads-api-refs.md`.

## Чеклист
- [ ] API-версия сверена с release-notes
- [ ] SDK `google-ads` пин соответствует нужной API-версии (lib≥30.1 для v24)
- [ ] версия API запинена в одном месте конфига
- [ ] проверены даты сансета (v24 → ~май 2027)
- [ ] обновление заложено в обслуживание
