# Кампании из медиа: GDN, Demand Gen, Video (ТЗ §11)

> **[legacy-референс]** Форматы медиа, валидация, `PAUSED`-создание, отдельный запус и `apply_*`-исполнители — сохраняемая
> логика. Кнопки, парсинг брифа в aiogram-хендлере и callback-флоу не переносятся. Цель — медиа-вход Hermes + типизированный
> PLAN/WRITE MCP (`SPEC.md` §3.6).

Создание кампаний из присланных в Telegram **медиа**: фото → медийная (GDN); видео →
**Demand Gen** или **Video** (YouTube). Как и любая мутация — **за confirm-гейтом**: бот показывает
черновик и ждёт «да». Все кампании создаются в статусе **PAUSED** (не тратят деньги до явного
запуска оператором); после успешного создания бот предлагает «🚀 Запустить» — это ОТДЕЛЬНЫЙ
proposal `resume_campaign` (тот же гейт). Реализация — [`ads/assets.py`](../ads/assets.py)
(изображения + YouTube-ассеты) + `apply_create_gdn_campaign` / `apply_create_demand_gen_campaign` /
`apply_create_video_campaign` в [`ads/mutations.py`](../ads/mutations.py); тесты —
[`tests/test_gdn_campaign.py`](../tests/test_gdn_campaign.py),
[`tests/test_video_campaigns.py`](../tests/test_video_campaigns.py).

## GDN из фото
```
Фото в Telegram → КОД режет 2 кадра (1.91:1 + 1:1) → во временное хранилище по media_id
  → бриф «название | ссылка | бюджет [| гео]» → в proposal.params идёт ТОЛЬКО media_id (не байты)
  → бот показывает черновик + ✅/❌
  → на «да»: upload_image_asset (ensure_allowed) → создание PAUSED-кампании [+ ГЕО] → audit-row
```
- **ГЕО (§11):** опциональное 4-е поле брифа (локации через запятую) — резолв названий →
  `geoTargetConstant` и привязка к кампании делает КОД (reuse `_set_geo_location_via_sdk`,
  live-сверенный на Search/§19). Пусто ⇒ без гео (Google по умолчанию покажет по всем локациям —
  сводка честно предупреждает).

### Подготовка изображений (Pillow)
`prepare_display_images(photo_bytes)` → два JPEG-кадра для адаптивного медийного объявления (RDA):
- **landscape 1.91:1** — 1200×628 (≥ маркетингового минимума Google 600×314, взято ×2 с запасом);
- **square 1:1** — 600×600 (≥ 300×300).

Кроп — **центральный** под целевую пропорцию, затем ресайз (LANCZOS), сохранение в JPEG q=88.
Не-изображение → понятный `ValueError` (тип ошибки без секретов), пользователю — дружелюбный текст.

## Demand Gen / Video из видео (§11)
Загрузить видеофайл в Google Ads напрямую нельзя — видео живёт на **YouTube** (примечание ТЗ §11).
```
Видео в Telegram (или /newvideo) → бот просит ссылку на YouTube → parse_youtube_video_id (КОД)
  → выбор типа: 🎯 Demand Gen | ▶️ Video → бриф «название | сайт | бюджет [| гео]»
  → генерация текстов (LLM) → [DG: опц. логотип-фото или ⏭ Пропустить]
  → черновик + ✅/❌ → на «да»: yt-ассет (AssetService) → PAUSED-кампания → audit-row → «🚀 Запустить»
```
- **Demand Gen** (`create_demand_gen_campaign`): канал DEMAND_GEN,
  `demand_gen_video_responsive_ad`; стратегия по умолчанию **Maximize Clicks** (`target_spend`) —
  работает на аккаунтах без conversion tracking (тот же фикс, что §19.3); `goal=conversions` →
  Maximize Conversions. Логотип (опц.) — квадратный image-ассет из присланного фото.
- **Video** (`create_video_campaign`): канал VIDEO, группа `VIDEO_RESPONSIVE`,
  `video_responsive_ad`, стратегия **target CPM** (охват). Описания валидируются консервативно
  **≤70** символов (`VIDEO_DESCRIPTION_MAX`, кириллица=1 считает КОД).
- YouTube-ссылки: `watch?v=` / `youtu.be/` / `shorts/` / `embed/` / `live/` или голый
  11-символьный id (`parse_youtube_video_id` — чистая функция, покрыта тестами).

> ⚠️ **Live-сверка перед сдачей:** SDK-цепочки Demand Gen/Video собраны по официальному примеру
> google-ads-python (API v25) и проверены офлайн-тестами (мок SDK); перед боевым использованием прогнать
> на тест-аккаунте (`scripts/live_smoke_test.py` / ручной прогон на Draft). GDN-цепочка сверена live.

## Где живут байты (безопасность)
Бинарь фото/логотипа **не попадает** в `proposal.params`, логи или БД. Между приёмом и
подтверждением кадры лежат во **временных файлах** по `media_id` (`save_pending_media`;
переживают рестарт), а в `params` идёт только `media_id`/`logo_media_id` (метаданные). `media_id`
валидируется как `isalnum()` — защита от path-traversal. После исполнения/отмены/TTL —
`clear_pending_media` (executor `finally`, `_do_cancel`, `cleanup_stale_proposals`).

## Загрузка ассетов и замок аккаунта
`upload_image_asset` / `upload_youtube_video_asset` грузят ассеты через `AssetService` и возвращают
`resource_name`. **`ensure_allowed`** стоит и здесь (golden rule #9): ассеты грузятся **только** в
`Aimash Draft` (`7753643025`), нигде больше.

## Ограничения / заметки
- Все кампании создаются **PAUSED** — запуск только осознанной командой оператора («🚀 Запустить»
  минтит `resume_campaign` proposal, гейт не обходится).
- Двойной гейт: `ensure_allowed` (замок аккаунта) → `_require_confirmation` (одноразовый claim) →
  `user_initiated` (создание кампании = деньги; реестр `_EXPECTED_MONEY_OPS` в
  `tests/test_invariants_core.py` ловит дрейф гарда).
- Политические объявления (EU) и прочие флаги SDK — best-effort, см. комментарии в `apply_create_*`.

Гарантии безопасности целиком — [SECURITY.md](SECURITY.md). Шаблон новой мутации — скил `new-mutation`.
