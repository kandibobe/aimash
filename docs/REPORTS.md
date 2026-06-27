# Отчёты и экспорт (`/report`, `/export`, `/sheets`, ТЗ §9)

Глубокий отчёт по аккаунту: итоги + сравнение период-к-периоду + разбивки. **READ-ONLY**; метрики
из micros (cost/CPC/CPA/ROAS) считает **КОД**, не модель. Реализация:
[`reports/period.py`](../reports/period.py) (периоды), [`reports/queries.py`](../reports/queries.py)
(GAQL-разбивки + метрики), [`reports/service.py`](../reports/service.py) (сборка), `reports/xlsx.py`
/ `reports/sheets.py` (экспорт). Тесты — [`tests/test_reports.py`](../tests/test_reports.py),
[`tests/test_sheets.py`](../tests/test_sheets.py).

## Команды
| Команда | Что делает |
|---|---|
| `/report [7\|30\|90\|MTD]` | короткая сводка в чат: итоги + сравнение + топ-3 кампании |
| `/export [...]` | глубокий отчёт `.xlsx` вложением (лист «Сводка» + лист на каждую разбивку) |
| `/sheets [...]` | тот же отчёт в Google Sheets, присылает ссылку (нужен scope `drive.file`) |

## Периоды (`reports/period.py`)
Пресеты: `7` / `30` / `90` дней, `MTD` (с начала месяца); плюс произвольный диапазон (`custom`).
Как и Google `LAST_N_DAYS`, пресеты **не включают сегодняшний** (неполный) день — верхняя граница
вчера. `previous()` даёт предыдущий **равный по длине** период для сравнения. GAQL-фильтр —
`segments.date BETWEEN '...' AND '...'`.

## Метрики (`reports/queries.py`)
Единый порядок колонок (xlsx и текст): **Показы, Клики, CTR, Сред. CPC, Расход, Конверсии,
Ценность, CPA, ROAS**. Производные (CTR/CPC/CPA/ROAS) — это `@property` на `Metrics`, считает код:
- `ctr = clicks/impressions`, `avg_cpc = cost/clicks`, `cpa = cost/conversions`,
  `roas = conv_value/cost`, `cost = cost_micros/1e6`.

## Разбивки (порядок ТЗ §9)
`build_account_report` собирает итоги + (опц.) предыдущий период + все разбивки из
`BREAKDOWN_FETCHERS`:
1. **Кампании** (имя, статус)
2. **Группы объявлений** (кампания, группа, статус)
3. **Ключевые слова** (кампания, группа, ключ, тип соответствия) — топ-`TOP_N=1000` по расходу
4. **Объявления** (кампания, группа, ID, тип) — топ-`TOP_N=1000` по расходу
5. **Устройства**
6. **Сети**
7. **По дням**

Большие разбивки (ключи/объявления) усекаются до топ-N **с явной пометкой** (`Breakdown.note`) —
без «тихого» обрезания. Каждый `fetch_*` повторно проходит `ensure_allowed` (замок аккаунта).

## Экспорт
- **`.xlsx`** (`/export`) — работает всегда, вложением.
- **Google Sheets** (`/sheets`) — создаёт таблицу (лист «Сводка» + лист на разбивку) и шлёт ссылку.
  Нужен **дополнительный OAuth-scope** `https://www.googleapis.com/auth/drive.file` (у Google Ads
  токена его нет). Включение — перевыпуск refresh-токена с обоими scope; шаги в
  [DEPLOYMENT.md §Google Sheets-экспорт](DEPLOYMENT.md). Без scope `/sheets` отвечает понятной
  ошибкой, а `.xlsx` доступен всегда.

SDK-вызовы синхронные → бот зовёт `build_account_report` через `asyncio.to_thread`. Плановая
рассылка отчётов — [SCHEDULER.md](SCHEDULER.md).
