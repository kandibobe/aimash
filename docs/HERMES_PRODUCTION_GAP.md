# Hermes: production gap и приёмка

Актуально на 2026-07-31. Это чек-лист целевого Hermes-контура, а не legacy aiogram.

## Что уже доказано живьём

- READ: список кампаний, статусы и бюджеты аккаунта `7753643025` прочитаны через Hermes.
- WRITE: переименование кампании прошло `proposal → trusted reply → execute_confirmed → audit` на
  Draft-аккаунте; без подтверждения Google Ads не менялся.
- Gateway v0.19.0 активен; MCP WRITE включён через HMAC trusted transport.

Один успешный rename доказывает денежный шов, но не весь функциональный объём трёх ТЗ.

## P0 — до объявления Hermes основным production-интерфейсом

| Гейт | Текущее состояние | Приёмка |
|---|---|---|
| Кнопка подтверждения | Реализовано: `✅ Да / ❌ Нет`; callback и reply сходятся в одном CAS | H1–H7 ниже |
| Отмена черновика | `cancel_proposal` включён в trusted live surface | H3 |
| Строгий Hermes config | Deploy удаляет только доказанно inert v0.19-ключи, сохраняя host-local модель/dashboard | lint: 0 ошибок |
| Backup Hermes state | Версионированный systemd timer + контрольный архив `.env`/`state.db` на каждом deploy | timer active; archive test |
| Получатель аварийных алертов | `ADMIN_CHAT_IDS` настроен на двух владельцев; host-level события отдельно доставляются Hermes-ботом в `OPS_ALERT_CHAT_ID` | Live alert-test пройден; kill-switch/quota alert проверить отдельным UAT без изменения Ads |
| Полный UAT денежного пути | Проверен rename; остальные классы мутаций не приняты живьём через Hermes | бюджет, ставка, pause/resume, create PAUSED, launch отдельно, replay/TTL/чужой actor |
| Два Telegram-контура | Legacy и Hermes работают параллельно | Явно назначить основной токен; scheduler/alerts должны приходить в выбранный контур до архивации legacy |
| Off-host backup + restore | AES-256-GCM ciphertext вынесен с VPS, полная расшифровка сверена; recovery-key хранится отдельно с user-only ACL | ✅ Hermes и PostgreSQL восстановлены 2026-07-31 в чистых network-isolated Linux-контейнерах; production state не менялся |

## P1 — код перенесён, остаётся live-приёмка новых workflow

На 2026-07-31 в Hermes surface реализованы и покрыты офлайн-тестами:

1. `.xlsx`/Google Sheets report delivery в исходный Telegram topic через подписанный artifact descriptor.
2. Keyword research: seed → Google ideas → relevance → clustering → negatives → xlsx/Sheets →
   проверка владения и round-trip.
3. RSA 15/4: durable поэлементная курация `approve/reject/replace`, лимиты считает код; один финальный
   `create_rsa` proposal.
4. Search wizard: восемь durable этапов; финал создаёт одну PAUSED Search-кампанию, запуск отдельным
   `launch_campaign` proposal.
5. Фото копируется доверенным gateway-транспортом и готовится в Ads media; видео принимается и
   проверяется, но для Google Ads по-прежнему нужен публичный `youtube_video_id` — локальный mp4 нельзя
   честно объявить Ads-ready.
6. Профили клиентов: confirm-gated create/update/clear и bounded full/incremental crawl сохранённого URL.
7. Composite proposal: 2–10 детерминированно обратимых изменений, одна карточка/одно подтверждение,
   последовательное исполнение и компенсация при частичном сбое. Необратимые операции отклоняются.

Наличие кода не равно live-приёмке: перед сменой основного контура каждый пункт выше нужно прогнать
через реальный Telegram gateway и проверить артефакт/audit/повторное чтение.

UAC/App и единый MCC-total через FX остаются решениями заказчика, а не скрытыми TODO: без приложения
UAC исключён; без утверждённого источника курсов суммы показываются отдельно по валютам.

## Live-сценарии H1–H10

Все изменяющие сценарии — только аккаунт `7753643025`.

| ID | Действие | Ожидается |
|---|---|---|
| H1 | Попросить переименовать PAUSED-кампанию, не нажимать кнопки 2 минуты | Карточка с `✅ Да / ❌ Нет`; в Ads имя прежнее |
| H2 | На новом черновике нажать `❌ Нет` | `status=rejected`; Ads не изменён; повторный клик не действует |
| H3 | Создать новый черновик и ответить `нет` реплаем на всю карточку | Тот же отказ через reply-CAS; Ads не изменён |
| H4 | На новом черновике нажать `✅ Да` | Ровно одна мутация; ответ основан на audit; повторный клик не исполняет второй раз |
| H5 | Создать ещё один черновик и ответить reply `да` на всю карточку | Fallback выполняет тот же CAS |
| H6 | Написать отдельным сообщением `да`, не reply | Ничего не выполняется |
| H7 | Другой Telegram-user нажимает кнопку чужой карточки | Отказ; Ads не изменён; попытка видна в audit |
| H8 | Попросить изменить бюджет без единицы/валюты или в чужой валюте | Уточнение/отказ; никакой молчаливой конвертации |
| H9 | Включить kill-switch и подтвердить Draft-изменение | Отказ до SDK; после снятия нужен новый безопасный прогон |
| H10 | После deploy проверить gateway, 93 MCP tools, bot/scheduler контейнеры, timer и свежий Hermes archive | Всё active/healthy, restart count не растёт, Telegram 409 отсутствует |

После H4/H5 обязательно повторно прочитать объект через Google Ads API, а не доверять только тексту
агента.
