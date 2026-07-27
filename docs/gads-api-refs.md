# Google Ads API — закреплённые ссылки и факты версии

> Сверено по живой доке **2026-07-25**. Перепроверять ежемесячно (скил `gads-version`).

## Текущие версии
- **API: v25** (релиз 2026-07-22). Пин на уровне мажора — `GOOGLE_ADS_API_VERSION=v25`.
- **SDK `google-ads`: 31.2.0** (PyPI). Пин — `google-ads>=31.2,<32` в `pyproject.toml`;
  хард-пин `google-ads==31.2.0` в `constraints.txt` (его применяют Dockerfile и CI).
- **Lib-версия ≠ API-версия.** Пакет бандлит несколько API-версий; выбирается на запрос:
  `client.get_service("GoogleAdsService", version="v25")`. Проверено интроспекцией установленного
  пакета: 31.2.0 бандлит `v21, v22, v23, v24, v25`.
- Кадэнс с янв 2026 — **ежемесячный** (~3–4 мажора/год). Мажор живёт ~12 мес, поддерживаются ~3 последних.
- **Сансет: v23 ~фев 2027, v24 ~май 2027, v25 ~июль 2027.**
- В core-путях (SearchStream, CampaignBudgetService, CampaignService.mutate, KeywordPlanIdeaService,
  customer_client/MCC) **breaking changes в v24→v25 нет** — денежный путь не затронут.
- Согласованность всех точек пина (конфиг, зонд, `.env`-шаблоны, `pyproject`, `constraints`,
  отсутствие хардкода прото-пути) держит `tests/test_gads_version_pin.py`, а не чеклист.

## Соответствие lib ↔ API (из GitHub releases)
| `google-ads` | Дата | API |
|---|---|---|
| 31.2.0 | 2026-07 | +v25 (первая lib с v25) |
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
- **v25 breaking changes — ни один не задевает наши пути** (проверено грепом по репо, 2026-07-25):
  удалены `CustomerLifecycleGoalService`/`CampaignLifecycleGoalService`, перестроены Incentive-энумы,
  в Reach forecasting `plannable_location_id` → `plannable_location_ids`, у CreatorInsights убран
  `search_brand`. Ни один из этих сервисов/полей в коде не используется.
- **v24:** видео-RSA (`VideoResponsiveAdInfo`) — поля `videos`/`business_name`/`logo_images` стали обязательны; удалён `Campaign.video_brand_safety_suitability`.
- **2026-06-01:** retention гранулярных отчётов сокращён до **37 месяцев**.
- **2026-04/06:** Customer Match и offline-конверсии мигрируют в **Data Manager API** (если когда-нибудь добавим загрузку конверсий).
