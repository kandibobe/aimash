# Aimash — единое техническое задание

> **Статус:** принято заказчиком 31.07.2026; единый нормативный источник
> **Дата:** 31.07.2026
> **Контракт:** после переноса в `SPEC.md` заменяет прежние продуктовые спеки. Операционные runbooks,
> security/API references и исходные `.docx` остаются только подчинёнными техническими материалами.

## 1. Цель продукта

Aimash — приватный AI-агент агентства для управления Google Ads через Telegram. Он работает
на уровне MCC, сам понимает свободный запрос, выбирает инструменты, собирает данные, готовит
диагноз, рекомендацию и черновик изменения.

Ключевой принцип:

> **Hermes автономно читает, анализирует, исследует, планирует и выполняет неденежные задачи.
> Одно явное подтверждение человека требуется только перед spend-affecting операцией Google Ads.**

## 2. Пользователи и модель доверия

- Бот приватный. Им пользуются владелец и не более 1–2 доверенных сотрудников агентства.
- Клиенты агентства и внешние пользователи доступа к боту не имеют.
- Все допущенные люди считаются trusted operators. RBAC, SSO и four-eyes approval не являются
  блокерами текущего прода.
- Доступ ограничивается Telegram allowlist. Пустой allowlist блокирует всех.
- Топик Telegram-супергруппы задаёт текущий клиентский контекст; явно выбранный account всегда важнее
  предположения из истории.

## 3. Целевой UX в Telegram

### 3.1. Естественный диалог

- Основной вход — свободный текст на русском или английском. Слэш-команды — необязательные shortcuts.
- Агент сохраняет контекст диалога, понимает «эту кампанию», «там же», «сделай +20%» и не заставляет
  повторять уже переданные имена или ID.
- Если совпадение одно, агент продолжает без уточнения.
- Если осталось 2–4 конечных варианта, агент показывает inline-кнопки. Нельзя просить скопировать ID,
  точное имя или весь запрос.
- Свободный уточняющий вопрос задаётся только когда конечные варианты нельзя построить.

### 3.2. Кнопки и финансовое подтверждение

Для spend-affecting команды Google Ads нормальный путь такой:

```text
команда менеджера
  → автономное чтение и подготовка
  → при необходимости один выбор inline-кнопкой
  → один черновик с diff «было → станет»
  → один тап ✅ Да
  → исполнение и повторная проверка
```

Для чтения, аудита, отчётов, исследований, профиля клиента, файлов, решений, инцидентов, расписаний,
создания скилов в staging и других неденежных задач карточка подтверждения не показывается.

Требования к финансовой карточке:

- Команда на spend-affecting изменение не является подтверждением. После неё агент сам готовит черновик.
- Карточка показывает account, объект, действие, «было → станет», важные последствия и кнопки `✅ Да` /
  `❌ Нет`.
- Одно нажатие `✅ Да` достаточно. Второго «да», точного повтора команды или `confirmation_id` нет.
- Reply на всю карточку со смыслом «да / поехали / согласен» равноценен кнопке. Неоднозначный ответ не
  исполняется.
- Несколько связанных изменений из одной команды собираются в один batch, одну сводку и одно подтверждение.
- Большие списки показываются кратко в чате и полностью в файле/артефакте.

### 3.3. Формат ответов

- Сначала вывод или результат, затем короткие детали.
- Эмодзи используются как смысловые метки, а не в каждом предложении.
- Текст делится на короткие смысловые блоки; стена текста недопустима.
- Статусы: `🔄 РАБОТАЮ`, `❓ НУЖЕН ОТВЕТ`, `⛔ БЛОКЕР`, `✅ ГОТОВО`.
- Служебные model/context/token данные в обычные ответы не попадают.

## 4. Граница автономии

### 4.1. Hermes делает сам

- понимает задачу и контекст;
- выбирает инструменты, скилы, модель и последовательность шагов;
- решает, что и сколько читать;
- делегирует подзадачи;
- использует web, browser, terminal, files, code execution, vision, video, image generation,
  context engine, session search, memory, todo и cron, если это помогает задаче;
- проводит аудит аккаунта и ищет причины изменений метрик;
- формулирует диагноз, рекомендацию, confidence и черновик действия;
- создаёт отчёты, дайджесты, рекламные тексты и артефакты;
- ведёт общую память команды и account-scoped память клиента;
- находит и применяет готовые скилы;
- предлагает новый постоянный скил после повторившегося и проверенного workflow;
- ставит и исполняет расписания отчётов, аудитов и алертов.

### 4.2. Код обязан делать детерминированно

- валидировать типизированные аргументы;
- вычислять деньги, проценты, производные метрики, лимиты символов и billing units;
- проверять account ceiling, allowlist, provenance, freshness, kill-switch и квоты;
- атомарно привязывать одноразовое подтверждение к точному черновику;
- исполнять Google Ads API mutation;
- писать audit-row и повторно читать состояние из API;
- сообщать «выполнено» только из проверенного результата.

### 4.3. Самообучение

- Обобщённый скил можно создать после двух успешно повторившихся workflow или по прямой просьбе.
- В скил не попадают секреты, customer IDs, имена клиентов, сырые отчёты и непроверенные выводы.
- Новый/изменённый постоянный скил проходит Skills Guard и одно утверждение перед публикацией.
- `skills.inline_shell` всегда `false`.

## 5. Google Ads: чтение и анализ

Агент читает на уровне MCC и клиентских аккаунтов:

- аккаунты и иерархию;
- кампании, группы, объявления, ключи, минус-слова, shared lists и ассеты;
- статусы, бюджеты, ставки, bidding strategies, networks, geo, languages и audiences;
- impressions, clicks, CTR, CPC, cost, conversions, conversion value, CPA, ROAS и доступные
  разбивки по device, geo, time, network, search terms и auction insights;
- Google change history и внутренний Aimash audit trail;
- квоты, свежесть данных и текущие pending decisions/proposals/incidents.

Любое число в ответе берётся из инструмента или вычисляется кодом. Агент не выдумывает метрики.

## 6. Google Ads: изменяющие операции

Поддерживаются:

- создание, изменение, пауза, возобновление и удаление кампаний, групп и объявлений;
- изменение бюджета, ставок и bidding strategy;
- добавление, удаление и изменение ставок ключей;
- добавление/удаление минус-слов и shared negative lists;
- изменение geo, proximity, networks, display-network flag и campaign geo target type;
- подключение/отключение audiences и shared sets;
- создание RSA и добавление/удаление assets: sitelinks, callouts, calls, structured snippets,
  price, promotion и других типов, которые поддерживает актуальная Google Ads API;
- создание Search, GDN/Display, App campaigns (UAC), Video, Demand Gen и других поддержанных типов кампаний;
- создание медийных кампаний из загруженных в Telegram изображений и видео после проверки формата,
  размера, aspect ratio и доступности asset type для выбранного account;
- запуск созданной кампании отдельной командой.

Особые правила:

- Одно подтверждение обязательно только для `update_budget`, изменения CPC/keyword bid, bidding strategy,
  `launch/enable/resume` и другой операции, которая непосредственно начинает или меняет расход.
- Неденежная операция выполняется автономно через typed tool, account ceiling, freshness и audit, без proposal-card.
- Если код не может доказать, что операция неденежная, она считается spend-affecting и требует один confirm.
- Бюджет и ставка меняются только по прямой команде человека. Алерт, cron или рекомендация могут создать
  decision, но не денежную mutation.
- Команды с процентом или абсолютной суммой вычисляются кодом; валюта должна совпадать с валютой account,
  если не задан явный доверенный FX-источник.
- Все новые кампании создаются `PAUSED`. Перевод в `ENABLED` — отдельная команда и одно новое подтверждение.
- Протухший или изменившийся черновик не исполняется и пересобирается из свежих данных.

## 7. Создание Search-кампании

Старый восьмиэтапный aiogram-визард **не является целевым UX**. Его поля и возможности сохраняются,
но Hermes сам определяет порядок и спрашивает только недостающее.

Агент должен уметь:

1. выбрать account по топику, имени или inline-кнопке;
2. разобрать свободное описание в campaign name, goal, budget, language, geo, networks,
   bidding strategy, dates, audience, URL и другие поля, которые нужны актуальной API;
3. подтянуть из account и профиля клиента безопасные defaults, отметив их как «по аналогии»;
4. принять ключевые слова текстом/файлом или самому провести keyword research;
5. показать объём, competition и bid ranges, кластеризовать по intent, отметить relevance и предложить negatives;
6. задать match type по группе или по ключу;
7. проанализировать final URL, профиль клиента и ключи;
8. сгенерировать RSA: до 15 headlines, до 4 descriptions и display path;
9. принять курацию естественным языком: «оставь 1, 3, 5; второй перепиши про рассрочку»;
10. добавить image assets и выбрать из текущих;
11. создать/подключить sitelinks, callouts, structured snippets, call, price, promotion, lead form,
    location, app, business name/logo и другие доступные assets;
12. задать tracking template, final URL suffix и custom parameters;
13. показать единую финальную сводку и автономно создать всё в `PAUSED`, если в batch нет spend-affecting действия;
14. запустить кампанию только по отдельной команде и новому подтверждению.

Лимиты RSA валидирует код по Unicode code points: headline ≤ 30, description ≤ 90, path ≤ 15;
кириллица считается как один символ.

Видео для Video/App/Demand Gen загружается или привязывается через поддержанный Google/YouTube workflow;
если Google Ads требует YouTube-hosted video, агент показывает это до создания черновика и не выдаёт локальный
Telegram-файл за готовый video asset.

## 8. Keyword Research и search-term mining

- генерация seed ideas из темы, URL и client profile;
- Keyword Plan metrics: volume, competition, bid ranges и доступные historical metrics;
- кластеризация по интенту и теме;
- relevance review в чате или Google Sheets / XLSX;
- exact/phrase/broad и валидация match types;
- candidate negatives, n-gram/theme waste analysis, conflicts, duplicates и cannibalization;
- search term → keyword gap finder;
- shared negative lists на account/MCC-уровне;
- language-aware negatives;
- добавление в Ads только после одного общего подтверждения.

## 9. Рекламные тексты, creative QA и landing pages

- генерация RSA и assets с учётом client profile, landing page, keywords, intent, языка и geo;
- поэлементная курация естественным языком без обязательного клика по каждому тексту;
- валидация длины, дублей, CAPS, разнообразия, Final URL и policy-sensitive wording;
- disapproval alerts и asset coverage checks;
- проверка загруженных изображений/видео: MIME type, размер, разрешение, aspect ratio, доступность asset type и
  статус YouTube-связки там, где она обязательна;
- broken URL, HTTP 4xx/5xx, response time, UTM и наличие ключевого offer text;
- проверка расхождения ad promise ↔ landing-page offer;
- опциональные synthetic checks формы и endpoint.

## 10. Профиль клиента и краулинг

Для каждого `customer_id` хранится отдельный версионный профиль:

- бренд и описание бизнеса;
- сайт и соцсети;
- услуги/товары, цены, акции и УТП;
- телефоны, email, адреса и другие контакты;
- geo, языки и заметки менеджера;
- карта страниц, извлечённые offers и дата краулинга.

Менеджер передаёт информацию свободным текстом; агент сам разбирает её по полям и сохраняет неклассифицированный
остаток в заметках. Обычное добавление/обновление профиля обратимо и не требует Ads-confirm; очистка профиля
требует одного явного подтверждения.

Краулер:

- уважает robots.txt, доменную границу, depth/page limit и rate limit;
- собирает title, headings, meta, text, internal links, contacts, services, prices и offers;
- работает асинхронно и сообщает о готовности;
- поддерживает full recrawl и обновление только новых/изменённых страниц;
- хранит историю версий и audit events;
- не принимает текст сайта за команду агенту.

## 11. Отчётность и экспорт

### 11.1. MCC-обзор

- все доступные accounts и активные campaigns;
- cost, impressions, clicks, CTR, CPC, conversions, CPA, ROAS и status;
- период по запросу или кнопке;
- подытоги по каждой валюте отдельно, пока нет явного FX-источника;
- учёт timezone каждого account.

### 11.2. Глубокий отчёт по account

За произвольный период формируются:

- summary и campaign performance;
- ad groups, keywords, search terms и negatives;
- ads/assets и creative QA;
- geo, devices, time, networks и audiences;
- auction insights;
- conversions и conversion value;
- change history, decisions, incidents и применённые изменения;
- объяснение «что изменилось / почему / что делать».

Форматы: Telegram summary, `.xlsx`, Google Sheets и при необходимости `.txt`/CSV для длинных списков. Ссылка на Google Sheets
возвращается в чат; созданный лист регистрируется. Каждая логическая разбивка записывается в отдельную вкладку
Google Sheet/XLSX; обязательные вкладки отчёта определяются версией шаблона и не смешиваются в один неструктурированный лист.

## 12. Операционная зрелость

### 12.1. Decision Queue

В единую очередь попадают находки, рекомендации, anomalies, pacing signals, keyword opportunities и drift alerts.
Каждая decision содержит account, severity, evidence, why, recommendation, confidence, status, owner и timestamps.
Статусы: `new`, `acknowledged`, `approved`, `rejected`, `snoozed`, `applied`, `expired`.

### 12.2. Account Health Score

- score 0–100 и bands green/yellow/red;
- drivers: pacing, CPA/ROAS vs target, conversion trend, waste, drift risk, tracking health и alert pressure;
- portfolio view: какие accounts требуют внимания и почему.

### 12.3. Budget pacing

- месячный план из Google Sheets / CSV или ручного ввода;
- spend-to-date и forecast до конца меся;
- account/campaign/portfolio scope;
- overspend/underspend и рекомендации «оставить / увеличить / снизить / перераспределить»;
- monthly/daily ceilings и critical alerts;
- бюджет меняется только по новой прямой команде человека и одному confirm.

### 12.4. Incidents и anomaly detection

- severity, ACK, snooze, resolve, deduplication и cooldown;
- несколько events склеиваются в один incident;
- escalation неподтверждённых critical incidents;
- Telegram обязателен; email/Slack/webhook могут добавляться как channels.

### 12.5. Conversion integrity

- active/primary conversion actions;
- conversion volume/lag anomalies;
- «расход идёт, conversions = 0»;
- изменения attribution/setup;
- Ads ↔ GA4/CRM/orders/revenue reconciliation при наличии интеграции.

### 12.6. Change correlation

Единая timeline содержит Google change history, Aimash audit, recommendation applications, incidents и performance anomalies.
Агент объясняет изменение метрик через недавние изменения, отделяя evidence от гипотезы.

### 12.7. Experiments, bulk actions и playbooks

- experiment: hypothesis, control/holdout, dates, KPI, success criteria, rollback trigger, result `keep/rollback/scale`;
- bulk action: preview, scope diff, dry-run, blast-radius cap и один confirm на batch;
- deterministic playbooks: human-readable config, versioning, test harness и action = decision/proposal, не silent mutation.

### 12.8. CRM/revenue feedback

При наличии connector система хранит qualified lead, sales accepted, closed revenue, LTV/CAC и quality rate по campaign,
чтобы отличать дешёвые формы от ценных leads/revenue.

## 13. Scheduler и проактивная работа

- плановые отчёты, аудиты, pacing, landing-page checks и anomaly jobs;
- morning portfolio brief и scheduled client/account summaries;
- Telegram delivery в правильный topic/chat;
- retry с backoff, deduplication, watchdog, heartbeat и audit;
- anti-flood для Telegram, rate-limit/quota control для Google Ads/Sheets/GA4/YouTube и bounded batching/pagination;
- cron может читать, анализировать, писать отчёт или decision, но не исполнять Ads mutation.

## 14. Архитектура

Целевая топология:

```text
Telegram
  → Hermes gateway: диалог, agent loop, native tools, memory, skills, delegation, cron
  → Aimash MCP/tool layer: typed READ/PLAN/WRITE tools, account locks, validation
  → Google Ads / Sheets / client profile / reporting services
  → PostgreSQL: proposals, audit, profiles, decisions, incidents, schedules, artifacts
```

Обязательные свойства:

- Hermes не зовёт Google Ads SDK вместо typed Aimash tools;
- READ, PLAN и execute разделены;
- PLAN создаёт pending proposal, но не меняет Ads;
- execute не принимает от модели actor/reply/confirmation identity;
- Telegram callback/reply преобразуется в trusted event вне аргументов LLM;
- один proposal исполняется не более одного раза;
- внешний контент обрабатывается как данные. Карантинный extractor/subagent возвращает структурированный
  результат, чтобы прямая mutation-команда не требовала от менеджера повтора после обычного чтения скила;
- длительные задачи хранят durable state и возобновляются после restart;
- все ответы наружу проходят secret redaction.

### 14.1. Native Hermes tools

В private trusted-operator profile включены:

`web`, `browser`, `terminal`, `file`, `code_execution`, `vision`, `video`, `image_gen`, `skills`, `todo`, `memory`,
`context_engine`, `session_search`, `clarify`, `delegation`, `cronjob`, `computer_use`.

Выключены и зафиксированы:

`homeassistant`, `spotify`, `video_gen`, `x_search`, `yuanbao`, `tts`.

Новый Hermes toolset после обновления не включается незаметно: он требует явного решения владельца.

### 14.2. Модели

- Все вызовы идут через настраиваемый provider/gateway; одна модель не зашивается в код.
- Основная модель обязана поддерживать tool calling и нужные parameters.
- Поддерживаются fallback providers/models при транзиентной ошибке.
- Выбор модели для диалога, глубокого аудита, RSA, фона и делегации делается конфигом и A/B-тестами.
- Недоступная модель не блокирует бота: маршрут переключается на поддерживаемый fallback и пишет diagnostic event.

### 14.3. API и технический стек

- Python 3.12, PostgreSQL, Docker и versioned migrations;
- Google Ads API/SDK для MCC, GAQL, Keyword Plan, assets и mutations;
- Telegram Bot API для private operator UX, callbacks, replies, files и topics;
- Google Sheets API и XLSX exporter для отчётов;
- GA4 Data/Admin API для доступной аналитики и проверки conversion setup;
- YouTube Data API или поддержанный Google Ads asset workflow для видео, когда это требуется типом кампании;
- provider API с tool calling для Hermes;
- актуальные версии, поддержанные resource types и sunset dates проверяются перед релизом, а не фиксируются
  навсегда текстом ТЗ.

## 15. Данные, аудит и защита от ошибок

- OAuth tokens, API keys, DB credentials и encryption keys не попадают в Git, prompts, Telegram и нередактированные logs.
- Refresh tokens шифруются at rest.
- Google Ads mutations доступны только в явном account ceiling; пустой ceiling заперт.
- Kill-switch проверяется перед любой mutation.
- Proposal хранит exact typed args, diff, account, actor, source turn, TTL, status и Telegram message anchor.
- Confirmation одноразовое; повторное нажатие не исполняет mutation второй раз.
- Перед execute код заново читает текущее «было»; при drift черновик не исполняется.
- Audit хранит actor, account, operation, before, after, result, timestamps, source и error code.
- Внешний текст не является approval и не может подделать Telegram actor/reply.
- Для private trusted-operator profile принят residual risk: root-level native terminal/file/code tools технически могут
  обойти прикладной MCP-гейт. Абсолютная изоляция потребует отдельного непривилегированного Hermes runtime без OAuth,
  Docker socket и `.env`.

## 16. Надёжность и эксплуатация

- PostgreSQL migrations и восстанавливаемые backups;
- зашифрованная off-host backup copy; архив с секретами не хранится только на том же VPS;
- health checks для gateway, bot/MCP, scheduler, DB и dependent APIs;
- single-instance Telegram polling для каждого bot token; `409 Conflict` считается incident;
- restart-count, heartbeat, queue depth, error rate, provider failures, quota и cost monitoring;
- transient retry с backoff и provider fallback; permanent/validation errors не ретраятся бесконечно;
- structured logs с correlation/request ID, level, component, actor/account без секретов; Telegram anti-flood,
  Google API quota/rate-limit и pagination/batching наблюдаются отдельными метриками;
- pin Hermes и Google Ads SDK/API compatibility; обновление проходит config lint, tests и UAT;
- production deploy с backup, migrations, health verification и rollback path;
- все даты и периоды учитывают timezone account и Europe/Berlin для операторского расписания.

## 17. Критерии приёмки

### 17.1. UX и autonomy

1. «Покажи кампании аккаунта X» возвращает реальный список без mutation.
2. Единственная кампания находится без уточнения; при 2–4 вариантах Telegram показывает кнопки.
3. После выбора кнопкой не нужно повторять account, campaign или запрос.
4. После restart диалог, память и durable tasks продолжаются.
5. Агент находит и применяет подходящий скил без указания его имени.
6. Повторившийся workflow превращается в pending skill и после одного approval доступен в новой сессии.

### 17.2. Подтверждение только для финансового риска

1. «Увеличь бюджет кампании X на 20%» создаёт одну карточку; Ads не изменён.
2. Один тап `✅ Да` исполняет операцию. Нет второго «да», копирования ID или повтора команды.
3. Повторный тап не исполняет mutation второй раз.
4. Кнопка другого черновика, другого chat/account или после TTL не исполняется.
5. После success ответ «готово» совпадает с audit-row и повторным API read.
6. Прямая budget-команда не блокируется обычным `skill_view`; external research выполняется через карантин без требования
   повторить весь запрос.
7. READ/audit/report/research/client-profile/decision/incident инструменты не блокируются фазой внешнего контента.
8. Создание `PAUSED`-кампании и другие доказанно неденежные действия исполняются автономно и пишут audit-row.
9. Неизвестная или spend-affecting операция никогда не попадает в автономный allowlist и требует один confirm.

### 17.3. Функциональные сценарии

1. MCC-сводка сходится с Google Ads UI по accounts и метрикам.
2. Глубокий отчёт за период создаёт валидный XLSX и Google Sheet.
3. Keyword research возвращает metrics, clusters, relevance, match types и negatives; длинный список не теряется.
4. RSA содержит до 15 headlines и до 4 descriptions, проходит лимиты и поэлементную курацию.
5. Search campaign заполняется из свободного текста/client profile; создаётся в `PAUSED`; запуск — отдельно.
6. Client profile разбирается из текста, краулинг собирает pages/services/prices/contacts, recrawl не теряет историю.
7. Decision queue, incidents, health score и pacing видны в account/portfolio context и имеют lifecycle.
8. Conversion integrity ловит сломанное измерение, а change timeline показывает предшествующие changes.
9. Creative/landing-page QA ловит disapproval, broken URL и missing/changed offer.
10. Scheduled report/alert приходит один раз в нужный Telegram context.

### 17.4. Надёжность

1. При пустом allowlist/account ceiling доступ закрыт.
2. Kill-switch блокирует все mutations без рестарта.
3. Секрет не попадает в Telegram, logs, reports, skills и Git.
4. Provider failure даёт редактированную ошибку и fallback/diagnostic event, а не бесконечную петлю.
5. На один Telegram token работает ровно один poller; `409 Conflict` отсутствует.
6. Backup восстанавливает DB/config/artifacts; зашифрованная копия хранится вне VPS.
7. После deploy gateway, MCP/bot, scheduler и DB healthy; restart count не растёт.

## 18. Осознанно отложено

Не блокирует текущий прод:

- доступ клиентов к боту;
- enterprise RBAC/four-eyes/SSO/SAML/compliance suite;
- multi-tenant isolation внутри одного Hermes profile;
- cross-channel mutations для Meta/Microsoft/TikTok;
- абсолютная изоляция root-level native tools от Google Ads credentials;
- единый MCC total через валюты без явно утверждённого FX-источника.

## 19. Трассируемость к трём исходникам

| Исходный раздел | Новый раздел | Статус покрытия |
|---|---|---|
| ТЗ-1 §1–4 | 1–6, 14 | Цель, Google Ads, agent core и function calling сохранены; Hermes уточнён как framework |
| ТЗ-1 §5–6 | 3, 6, 15, 17 | Show-and-confirm сохранён и упрощён до одного тапа; aiogram menus не нормативны |
| ТЗ-1 §7 | 8 | Keyword Research и добавление ключей сохранены и расширены |
| ТЗ-1 §8–9 | 11 | MCC, deep reports, Sheets/XLSX сохранены |
| ТЗ-1 §10 | 7, 9 | RSA и курация сохранены; обязательные 19 кликов заменены language curation |
| ТЗ-1 §11 | 6–7, 9 | Медиа и типы кампаний сохранены; всё создаётся `PAUSED` |
| ТЗ-1 §12–17 | 13–16 | Security, DB, scheduler, logging, API и stack перенесены в целевую архитектуру |
| ТЗ-1 §18 | 17 | Критерии приёмки переписаны в проверяемые сценарии |
| ТЗ-2 §19.1–19.9 | 7–9, 17 | Весь Search-flow и поля сохранены; жёсткий wizard заменён agent-first диалогом |
| ТЗ-3 §20.1–20.9 | 10, 17 | Account-scoped profile, text ingest, crawl, recrawl, generation context, history и audit сохранены |
| Операционные решения 30–31.07 | 2–4, 12–18 | Private team, autonomy, one-tap confirm, native tools, self-learning и production maturity добавлены |

## 20. Единый канон

Переход выполнен 31.07.2026:

1. этот файл — единственный нормативный продуктовый источник истины;
2. прежние продуктовые спеки находятся в `docs/archive/pre-single-spec-2026-07/` и помечены `NON-NORMATIVE`;
3. три оригинальных `.docx` и их SHA-256 сохранены как contract evidence, а не как инструкция для разработки;
4. `AGENTS.md`, `CLAUDE.md`, `README.md` и `docs/README.md` ссылаются только на этот продуктовый канон;
5. операционные runbooks, security/API references и migration docs остаются подчинённой технической документацией;
6. CI не допускает появления второго файла, объявленного продуктовым источником истины.
