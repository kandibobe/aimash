---
name: gads-version
description: Проверка текущей версии Google Ads API и графика сансета перед работой с SDK. Использовать при настройке google-ads SDK, обновлении версии или подозрении на депрекейт.
---

# Версия Google Ads API и сансет

Google Ads API обновляется **ежемесячно** (с янв 2026): ~3–4 мажора в год + помесячные обратносовместимые минорки. Мажор живёт **~12 месяцев**, одновременно поддерживаются ~3 последних мажора. Бот без обслуживания сломается в течение года.

## ⚠️ Lib-версия ≠ API-версия (главная ловушка)
Версия пакета **`google-ads`** (PyPI) и версия **API** (`vNN`) — РАЗНЫЕ нумерации. Пакет бандлит несколько API-версий, версия выбирается на запрос (`client.get_service("GoogleAdsService", version="v25")`).

| Пакет `google-ads` | Дата | Добавляет API |
|---|---|---|
| **31.2.0** | 2026-07 | **v25** (первая lib с v25) |
| 31.1.0 | 2026-06-24 | v24.2 |
| 31.0.0 | 2026-05-13 | v24.1; убрал v20 |
| 30.1.0 | 2026-04-22 | v24 (первая lib с v24) |

→ Для API **v25** нужна lib **≥ 31.2**. Пин в `pyproject.toml`: **`google-ads>=31.2,<32`**, хард-пин в `constraints.txt`: **`google-ads==31.2.0`** (его применяют Dockerfile и CI). Не верить таблице — проверять интроспекцией установленного пакета:
`python -c "import pkgutil, google.ads.googleads as p; print(sorted(m.name for m in pkgutil.iter_modules(p.__path__) if m.name.startswith('v')))"`

## ⚠️ Пин НЕ в одном месте — их шесть
Раньше этот скил утверждал «пин API-версии — в одном месте». Это неверно и стоило бы молчаливого дрейфа. Фактические точки:

| Точка | Что там |
|---|---|
| `core/config.py` | `google_ads_api_version` — дефолт, ЕДИНСТВЕННЫЙ исполняемый пин приложения |
| `scripts/verify_readonly_ceiling.py` | свой литерал `API_VERSION` (модуль намеренно не импортирует `core.config` — работает на голой ВМ) |
| `.env` / `.env.example` / `.env.server` | `GOOGLE_ADS_API_VERSION=` — перекрывает дефолт конфига |
| `pyproject.toml` | диапазон SDK |
| `constraints.txt` | хард-пин SDK (реально применяется в Docker/CI) |
| исходники | хардкод прото-пути `google.ads.googleads.vNN.…` — запрещён (был в тестах) |

**Гард:** `tests/test_gads_version_pin.py` сверяет все точки между собой и проверяет, что установленный SDK бандлит настроенную версию. Бамп в одном месте теперь красит тест, а не проходит молча.

## Что делать
1. Проверить актуальные версии: WebFetch/WebSearch на
   `https://developers.google.com/google-ads/api/docs/release-notes`,
   `.../docs/sunset-dates`, `https://pypi.org/project/google-ads/`,
   `https://github.com/googleads/google-ads-python/releases`.
2. На 2026-07-25 актуально: **API v25** (релиз 2026-07-22), lib **google-ads 31.2.0**; сансет **v23 ~фев 2027, v24 ~май 2027, v25 ~июль 2027**.
3. Прочитать release notes на breaking changes. В v24→v25 core-пути (SearchStream, CampaignBudgetService, CampaignService.mutate, KeywordPlanIdeaService, customer_client) НЕ менялись; сломалось только то, чего у нас нет (`CustomerLifecycleGoalService`/`CampaignLifecycleGoalService` удалены, Incentive-энумы, Reach `plannable_location_id`→`plannable_location_ids`, CreatorInsights `search_brand`).
4. Бампить ВСЕ точки из таблицы выше одним заходом, затем `pytest tests/test_gads_version_pin.py tests/test_write_layer.py`. `.env` на VPS правится отдельно — гард его не видит.

## Правила
- Не хардкодить номер версии по коду — только конфиг (API) + pyproject/constraints (SDK).
- Перепроверять версию ежемесячно (заложить в обслуживание/ретейнер).
- При `RESOURCE_EXHAUSTED` / депрекейте — сверяться с докой, а не гадать.
- Полный список doc-ссылок — `docs/gads-api-refs.md`.

## Чеклист
- [ ] API-версия сверена с release-notes
- [ ] SDK `google-ads` пин соответствует нужной API-версии (lib≥31.2 для v25) — проверено интроспекцией, не таблицей
- [ ] все шесть точек пина обновлены; `tests/test_gads_version_pin.py` зелёный
- [ ] `.env` на VPS обновлён (гард его не проверяет)
- [ ] проверены даты сансета (v25 → ~июль 2027)
- [ ] обновление заложено в обслуживание
