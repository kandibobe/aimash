# Google Ads API — закреплённые ссылки и факты версии

> Сверено по живой доке **2026-06-25**. Перепроверять ежемесячно (скил `gads-version`).

## Текущие версии
- **API: v24.2** (релиз 2026-06-24). Пин на уровне мажора — `GOOGLE_ADS_API_VERSION=v24`.
- **SDK `google-ads`: 31.1.0** (PyPI, 2026-06-24). Пин — `google-ads>=31.1,<32` в `pyproject.toml`.
- **Lib-версия ≠ API-версия.** Пакет бандлит несколько API-версий; выбирается на запрос:
  `client.get_service("GoogleAdsService", version="v24")`.
- Кадэнс с янв 2026 — **ежемесячный** (~3–4 мажора/год). Мажор живёт ~12 мес, поддерживаются ~3 последних.
- **Сансет v24 — ~май 2027.** v22 (~окт 2026) и v23 (~фев 2027) ещё работают в lib 31.1 как фолбэк.
- В core-путях (SearchStream, CampaignBudgetService, KeywordPlanIdeaService, customer_client/MCC) **breaking changes в v23→v24 нет**.

## Соответствие lib ↔ API (из GitHub releases)
| `google-ads` | Дата | API |
|---|---|---|
| 31.1.0 | 2026-06-24 | +v24.2 |
| 31.0.0 | 2026-05-13 | +v24.1; −v20 |
| 30.1.0 | 2026-04-22 | +v24 (первая lib с v24) |
| 30.0.0 | 2026-03-25 | +v23.2 |

## Ссылки (pin)
- Release notes: https://developers.google.com/google-ads/api/docs/release-notes
- Versioning policy: https://developers.google.com/google-ads/api/docs/concepts/versioning
- Sunset dates: https://developers.google.com/google-ads/api/docs/sunset-dates
- Deprecations (unversioned): https://developers.google.com/google-ads/api/docs/deprecations
- Stay updated: https://developers.google.com/google-ads/api/docs/productionize/stay-updated
- Python client lib: https://developers.google.com/google-ads/api/docs/client-libs/python
- PyPI: https://pypi.org/project/google-ads/ · GitHub releases: https://github.com/googleads/google-ads-python/releases
- GAQL grammar: https://developers.google.com/google-ads/api/docs/query/grammar
- Test accounts: https://developers.google.com/google-ads/api/docs/best-practices/test-accounts
- OAuth (cloud project / desktop): https://developers.google.com/google-ads/api/docs/oauth/cloud-project
- Rate limits: https://developers.google.com/google-ads/api/docs/productionize/rate-limits
- Limits & quotas: https://developers.google.com/google-ads/api/docs/best-practices/quotas

## Заметки на будущее (вне текущего объёма)
- **v24:** видео-RSA (`VideoResponsiveAdInfo`) — поля `videos`/`business_name`/`logo_images` стали обязательны; удалён `Campaign.video_brand_safety_suitability`.
- **2026-06-01:** retention гранулярных отчётов сокращён до **37 месяцев**.
- **2026-04/06:** Customer Match и offline-конверсии мигрируют в **Data Manager API** (если когда-нибудь добавим загрузку конверсий).
