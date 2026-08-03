"""Конфигурация из окружения (.env). Секреты только отсюда, никогда из кода.

Секреты обёрнуты в pydantic.SecretStr — маскируются в логах/трейсбеках/repr;
реальное значение доступно ТОЛЬКО через .get_secret_value() в точке использования.
SecretStr — это защита от утечки в логи, НЕ шифрование (за шифрование at-rest — core.secrets).
"""

from __future__ import annotations

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_customer_id(customer_id: str) -> str:
    """Customer ID без разделителей: '775-364-3025' -> '7753643025'. Только цифры."""
    return "".join(ch for ch in str(customer_id) if ch.isdigit())


# OpenRouter provider.sort принимает ТОЛЬКО эти значения (или пусто = роутинг по умолчанию).
# Любое иное значение в .env → BadRequestError 400 «provider.sort: Invalid input».
_VALID_PROVIDER_SORTS: frozenset[str] = frozenset({"", "price", "throughput", "latency"})

# D3: пароли БД, которые лежали в git (docker-compose.yml, db/init/*.sql) до ротации. В prod
# такой пароль = публичный ⇒ старт с ним запрещён (_reject_leaked_db_password_in_prod).
_LEAKED_DB_PASSWORDS: frozenset[str] = frozenset({"aimash", "aimash_ro"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Окружение
    env: str = "dev"  # dev => только TEST MCC

    # Модель через OpenRouter (сменяемая)
    openrouter_api_key: SecretStr = SecretStr("")
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # #10 Наблюдаемость: management-ключ (provisioning) для GET /api/v1/activity — per-день/per-модель
    # разбивка трат за 30 UTC-дней. ОБЫЧНЫЙ inference-ключ на /activity даёт 403 (по доке OpenRouter),
    # потому ключ отдельный. Пусто ⇒ /activity не зовём (fail-soft, ридер деградирует на /key), фича
    # opt-in — зажигается, когда владелец заведёт ключ на VPS (процедура RB-3). Секрет: только из env.
    openrouter_provisioning_key: SecretStr = SecretStr("")
    # Фильтр /activity по КОНКРЕТНОМУ ключу (SHA-256 hex, как в keys API): изолировать траты Hermes-
    # ключа от ботовского, если ключи разные. Пусто ⇒ без фильтра (весь аккаунт). Не секрет (хэш).
    openrouter_key_hash: str = ""
    # имена llm_* (не model_*) — иначе шадоуят метод BaseModel.model_copy()
    # Разделение «что за что» (дефолты; ручной выбор оператора через /model бьёт их — см.
    # agent.router.effective_model: override > роль-дефолт):
    #   parsing — разбор команд (function calling, денежный путь): дёшево и точно, ошибку
    #             ловит confirm-гейт + код-валидация → дорогая модель тут не нужна.
    #   copy    — генерация RSA-текстов: качество РУССКОГО важнее цены → сильная модель.
    llm_parsing: str = "deepseek/deepseek-chat"  # A/B: дёшево, ≈Claude на парсинге команд
    llm_copy: str = "anthropic/claude-opus-4.8"  # копирайт RU — максимум качества RSA (решение владельца 2026-07)
    llm_fallback: str = "anthropic/claude-sonnet-4.6"  # Hermes выбыл (нет tool use на OpenRouter)
    # P2: отдельная роль КЛАСТЕРИЗАЦИИ/интента keyword research (вопрос заказчика 2026-07-06
    # «какая модель?»). Пусто ⇒ = llm_parsing (поведение прежнее байт-в-байт); LLM_CLUSTERING
    # в .env позволяет прогнать A/B сильной модели ТОЛЬКО на интент-классификации.
    llm_clustering: str = ""
    # Отдельная роль СМЫСЛОВЫХ keyword-задач (решение владельца 2026-07): seed-ключи, оценка
    # релевантности, генерация минус-слов, кластеризация, извлечение профиля клиента §20 —
    # редкие и качество-критичные, но РАЗ отделены от латентно-чувствительного командного парсинга
    # (agent/loop.py остаётся на дешёвой llm_parsing). Дефолт — сильная модель ради RU-семантики;
    # пусто ⇒ = llm_parsing (обратная совместимость). Сменяемо в .env / рантайм /model (override
    # бьёт все роли).
    llm_keywords: str = "anthropic/claude-opus-4.8"
    # P3: роль АНАЛИТИКА (агентный нарратив /audit — multi-turn read-only рассуждение поверх уже
    # посчитанного КОДОМ аудита). Пусто ⇒ = llm_parsing (дешёвый дефолт; галлюцинации безвредны —
    # на выходе fact-guard + детерминированный fallback). LLM_ANALYST в .env = A/B сильной модели.
    llm_analyst: str = ""
    # §20 досье: РАЗНЫЕ роли на две половины map-reduce (решение владельца 2026-07-14).
    #   extract — извлечение фактов из чанка краула (десятки вызовов на сайт) → дешёвая модель;
    #             наивно на opus это ~$21 за сайт в 1000 страниц, поэтому роль отделена.
    #   dossier — ОДИН синтез связной прозы в конце → сильная модель (качество RU-текста).
    # Пусто ⇒ extract = llm_parsing, dossier = llm_keywords (та же opus-4.8, что и у ключей).
    llm_extract: str = ""
    llm_dossier: str = ""
    # Пресеты для рантайм-переключателя /model (CSV slug'ов OpenRouter). Пусто => дефолт в
    # agent.router._DEFAULT_CHOICES (tool-use-capable модели). Своя модель — через /model в боте.
    model_choices: str = ""
    # Потолок генерации по ролям (явный max_tokens — экономия бюджета БЕЗ потери качества:
    # без него OpenRouter резервирует полный max-output против дневного бюджета; см.
    # agent.router.ROLE_MAX_TOKENS). Парсинг → крошечный tool-call; копирайт → короткий JSON.
    llm_max_tokens_parsing: int = 1024
    llm_max_tokens_copy: int = 2048
    # Потолок смысловых keyword-задач: фильтр релевантности на 120 ключей отдаёт JSON крупнее
    # parsing-1024 (объект {ключ: bool}), поэтому берём copy-уровень.
    llm_max_tokens_keywords: int = 2048
    # P3: потолок нарратива аналитика (короткий человеческий разбор аудита; без него OpenRouter
    # резервирует полный max-output против дневного бюджета — см. agent.router.ROLE_MAX_TOKENS).
    llm_max_tokens_analyst: int = 1536
    # §20 досье. R13 (мина, найденная при планировании): clients/profile_extract.py звал chat()
    # БЕЗ max_tokens → упирался в потолок роли (2048) и JSON обрезался МОЛЧА (_extract_json_object
    # возвращал None → пустой профиль). Обогащённая схема досье тем более не влезает — обе роли
    # зовутся с ЯВНЫМ max_tokens, и он должен быть больше, чем у keywords.
    llm_max_tokens_extract: int = 4096  # JSON фактов с одного чанка (услуги+люди+факты)
    llm_max_tokens_dossier: int = 8192  # финальный синтез: проза + УТП по всему сайту
    # §20 нормализация досье к одному языку (reduce-шаг перед синтезом): выход крупнее прозы — все
    # списки (услуги/УТП/факты) + индексы группировки, поэтому явный потолок ≥ dossier (R13: иначе
    # JSON группировки обрежется молча).
    llm_max_tokens_dossier_normalize: int = 8192
    # P3: включать ли агентный НАРРАТИВ в /audit (multi-turn LLM поверх детерминированного ядра).
    # True (дефолт) — карточка предваряется человеческим разбором; сбой/timeout/fact-guard молча
    # откатывают на детерминированную карточку (числа — всегда КОД). False — только детерминир. карточка.
    audit_agentic_narrative: bool = True
    # После /audit — режим доп-вопросов (Q&A): свободный текст = вопрос к READ-ONLY аналитику
    # (тот же fact-guard, БЕЗ мутаций). True (дефолт) — включаем; False — отдельный kill-switch
    # (независим от narrative выше), если чтения/бюджет LLM надо срезать. Выход — команда/кнопка.
    audit_qa_enabled: bool = True
    # :floor — роутинг к самому дешёвому провайдеру (тот же вес = текст-нейтрально, но фиксирует
    # на одном эндпоинте → операционно рискованнее). По умолчанию ВЫКЛ (fail-safe к надёжности).
    openrouter_price_floor: bool = False
    # Роутинг ТОЛЬКО parsing-роли (самый чувствительный к задержке путь — пользователь ждёт в
    # «печатает…») к быстрейшему эндпоинту модели через OpenRouter provider:{sort}. Значения:
    # "throughput" (выше токенов/с) или "latency" (ниже TTFT); пусто => ВЫКЛ (текущее поведение).
    # Копирайт НЕ трогаем (там важнее качество). Как и :floor, фиксирует на конкретном эндпоинте →
    # операционно рискованнее, поэтому по умолчанию ВЫКЛ и включается осознанно в .env.
    openrouter_parsing_provider_sort: str = ""

    # Telegram
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_whitelist_chat_ids: str = ""  # "123,456"
    # Host-level watcher (`scripts/ops_alert.py`): отдельный адрес назначения, потому что
    # ADMIN_CHAT_IDS — пользователи/лички, а инфраструктурные события нужны в общем топике.
    # Строки намеренно: пустой OPS_ALERT_THREAD_ID валиден и означает General topic по умолчанию;
    # числовой формат fail-closed проверяет сам sender перед сетью.
    ops_alert_chat_id: str = ""
    ops_alert_thread_id: str = ""
    # Пер-пользовательская изоляция аккаунтов ЧТЕНИЯ (core.access, таблица account_access):
    #   auto          — пустая таблица грантов ⇒ legacy-проход (все whitelisted видят весь
    #                   read-list; поведение одно-операторного режима), ПЕРВЫЙ грант включает
    #                   enforcement для всех;
    #   enforced      — строгий режим даже с пустой таблицей (не-Draft только по гранту);
    #   legacy (дефолт private profile) — все trusted operators видят весь глобальный read-ceiling.
    # Draft доступен всем whitelisted в любом режиме. Невалидное значение → warning + legacy.
    account_access_mode: str = "legacy"
    # Админы бота (CSV chat_id): им доступны /grant /revoke (управление грантами account_access).
    # Пусто ⇒ админ-команды недоступны НИКОМУ (fail-closed; фича опциональна — не роняем prod).
    admin_chat_ids: str = ""

    # Google Ads
    google_ads_developer_token: SecretStr = SecretStr("")
    google_ads_client_id: str = ""  # OAuth client id — не секрет
    google_ads_client_secret: SecretStr = SecretStr("")
    google_ads_refresh_token: SecretStr = SecretStr("")
    google_ads_login_customer_id: str = ""  # менеджерский аккаунт (MCC), контекст авторизации
    # §8/мультиаккаунт: ДОПОЛНИТЕЛЬНЫЕ MCC (под разными менеджерами), под которыми бот обходит/
    # логинится (Фаза 3 — аккаунты под разными MCC). CSV. Легаси-скаляр выше вложен в множество
    # (login_customer_id_set). Пусто => только основной login_customer_id (поведение не меняется).
    google_ads_login_customer_ids: str = ""
    # Белый список аккаунтов для МУТАЦИЙ (см. ads.client.ensure_allowed). Сентинел «all»/«*» =
    # мутации на ПОЛНОМ видимом наборе (allowed_ceiling(): Draft ∪ read-list ∪ обнаруженные
    # дочерние MCC) — см. allow_all_visible; в prod это ЯВНОЕ значение env (решение владельца),
    # а не дефолт. Пусто => fail-closed в ЛЮБОМ окружении, включая prod (BZ-1, 2026-07-30):
    # мутаций нет, чтение работает — «очистить env» ЗАКРЫВАЕТ мутации, а не открывает.
    # Замок видимости и confirm-гейт остаются: чужой аккаунт вне MCC немутируем, «да» обязателен.
    google_ads_allowed_customer_ids: str = ""
    # §8: аккаунты, доступные ТОЛЬКО на чтение (сводка по дочерним MCC) ПОМИМО мутационного списка.
    # fail-closed; мутации этим НЕ затрагиваются (свой узкий замок). Пусто => чтение, как и мутации,
    # только на разрешённый аккаунт (поведение не меняется). См. ads.client.ensure_read_allowed.
    google_ads_read_customer_ids: str = ""
    # §11 Video: создание VIDEO-кампаний через API разрешено Google ТОЛЬКО по allowlist аккаунта
    # (иначе mutate → MUTATE_NOT_ALLOWED, trigger «VIDEO»). Дефолт False ⇒ визард не ведёт менеджера
    # в гарантированный тупик (предлагает Demand Gen — рабочий путь из видео). Владелец включает
    # True, ТОЛЬКО когда его аккаунт добавлен в allowlist Google. Код Video-цепочки готов и покрыт.
    google_ads_video_enabled: bool = False
    google_ads_api_version: str = "v25"  # API-версия (мажор). SDK-пин google-ads — в pyproject.toml
    # ЕДИНСТВЕННЫЙ исполняемый пин API-версии. Второй литерал живёт в
    # `scripts/verify_readonly_ceiling.py` (он намеренно не импортирует core.config — работает на
    # голой ВМ); их равенство держит `tests/test_gads_version_pin.py`, иначе расхождение молчит.
    # Гео-страна ОПЦИОНАЛЬНОГО «домашнего» дефолта (2026-07, решение владельца): по умолчанию ПУСТО ⇒
    # страна берётся ИЗ ЗАПРОСА (описание/бриф), а при её отсутствии создание ТРЕБУЕТ гео (не молча
    # шлёт по всему миру и не подставляет чужую страну). Задать ISO (напр. UG) стоит ТОЛЬКО если
    # агентство ВСЕГДА льёт на одну страну — тогда это последняя-инстанция при неуказанном гео.
    default_geo_country_code: str = ""  # ISO-3166 alpha-2 дом-дефолта; пусто = резолв из запроса
    # Язык, на котором заданы НАЗВАНИЯ локаций (для их резолва в geoTargetConstant, не страна-таргет).
    # Дефолт «ru». Пустая строка ⇒ выводим из дом-страны (если задана), иначе «ru».
    default_geo_locale: str = "ru"
    # Дневной лимит операций Google Ads API (§3): Basic dev-token = 15 000 операций/сутки; Standard —
    # фактически без лимита (ставь высоким). core.quota предупреждает на 80% и БЛОКИРУЕТ новые
    # МУТАЦИИ на 95% (чтение не блокируем). 0 ⇒ трекинг выключен (без гарда).
    google_ads_daily_op_limit: int = 15000

    # BZ-1: аварийный рубильник мутаций (core.killswitch). Существование файла по этому пути
    # В Compose переопределён общим `/run/aimash-safety/KILL_SWITCH`, смонтированным read-only в
    # bot/scheduler. Пусто = файл-канал выключен (остаётся живой env-канал).
    kill_switch_file: str = "KILL_SWITCH"
    # B1-4: суточный blast-radius ПОВЫШЕНИЙ бюджета на аккаунт (окно 24 ч по исполненным строкам
    # proposals; понижения не ограничиваются никогда). Проверка — ДО claim, в
    # ads.mutations._require_budget_blast_radius (отказ не сжигает подтверждение). 0 = выключено;
    # в prod max_ops автоподнимается до 10 (_default_budget_blast_radius_in_prod) — молча
    # выключенным счётный кап в prod не остаётся. Кап по сумме — в ЕДИНИЦАХ валюты аккаунта
    # (не micros); универсального дефолта нет (зависит от валюты), задаёт владелец.
    daily_budget_increase_max_ops: int = 0
    daily_budget_increase_cap_units: float = 0.0

    # §12 2FA (опц. в ТЗ): PIN-подтверждение ОПАСНЫХ операций ПЕРЕД исполнением. Opt-in, дефолт
    # OFF, fail-closed: включён без PIN ⇒ опасные операции БЛОКИРУЮТСЯ (не fail-open). PIN сверяет
    # КОД (core.twofa, hmac.compare_digest — constant-time), сырьё не логируется (SecretStr).
    two_factor_enabled: bool = False
    two_factor_pin: SecretStr = SecretStr(
        ""
    )  # маскируется в логах/repr/трейсбеках (golden rule #5)
    # Какие операции требуют 2FA-кода (CSV). Дефолт — необратимые/денежные: удаление кампании/группы
    # (уже за двойным confirm) + бюджет/ставка/стратегия. Пусто ⇒ при включённом 2FA НИЧЕГО не
    # гейтится (осознанный выбор владельца); операции нормализуются в two_factor_ops (property).
    two_factor_ops_csv: str = (
        "remove_campaign,remove_ad_group,update_budget,update_bid,update_keyword_bid,"
        "set_bidding_strategy"
    )
    # A14: базовый кулдаун (мин) после _TWOFA_MAX_ATTEMPTS неверных PIN подряд — вход в 2FA-режим
    # блокируется (fail-closed: опасная op недоступна), при повторных локаутах растёт экспоненциально
    # (bot.main._twofa_register_fail). Анти-перебор PIN: раньше счётчик обнулялся каждым новым ✅.
    two_factor_lockout_minutes: int = 15

    # Additive to the author's existing reply-confirmation.  Selected risk tiers also need an
    # independent approver/admin vote before the authoritative claim CAS can execute them.
    four_eyes_required: bool = False
    four_eyes_risk_tiers_csv: str = "L3"

    # Безопасность / БД
    secrets_encryption_key: SecretStr = SecretStr("")
    # Stable keyed pseudonymization for CRM and identity ids. Separate from Fernet intentionally:
    # rotating the encryption key must not invalidate historical dedup/identity hashes.
    pseudonymization_hmac_key: SecretStr = SecretStr("")
    # Hermes PLAN/WRITE stays physically absent unless this flag is explicit. The HMAC key is
    # shared only with the trusted gateway plugin; Hermes still has no Google OAuth credentials.
    hermes_write_enabled: bool = False
    aimash_trust_hmac_key: SecretStr = SecretStr("")
    # SecretStr: DSN несёт пароль БД — маскируем в repr/логах/трейсбеках (golden rule #5).
    # Реальное значение — только через .get_secret_value() (db.session, migrations.env).
    database_url: SecretStr = SecretStr("postgresql+asyncpg://aimash:aimash@localhost:5432/aimash")

    # B3: публичность экспортных Google Sheets. True (дефолт — прежнее поведение: заказчик мог
    # открыть ссылку без запроса доступа) => таблица шарится anyone-with-link. False => таблица
    # остаётся приватной для OAuth-аккаунта бота (финансовые данные клиента НЕ за периметром;
    # получатель запрашивает доступ). При True бот предупреждает «доступно всем по ссылке».
    sheets_public_link: bool = True

    # Лист/вкладка «Находки» в /export и /sheets: полный аудит (≈23 доп. чтения) собирается на ЯВНУЮ
    # команду человека — квоту это не жжёт (никакого веера по MCC, никакого планировщика). True —
    # дефолт: экспорт без диагноза бесполезен менеджеру. False — kill-switch, если Google начнёт
    # резать квоту чтений: книга уходит как раньше, без листа. Сбой аудита и так не роняет экспорт.
    export_findings: bool = True

    # Чей Google Drive хранит созданные ботом таблицы. По умолчанию — тот же OAuth, что у Google Ads
    # (файлы падают в «Мой диск» аккаунта GOOGLE_ADS_REFRESH_TOKEN и едят его квоту). Задав
    # SHEETS_REFRESH_TOKEN (выдан ОТДЕЛЬНЫМ прогоном scripts/get_refresh_token.py --sheets под нужным
    # gmail, scopes drive.file + spreadsheets.readonly, БЕЗ adwords), Sheets/Drive уходят на этот
    # аккаунт, а Ads-токен остаётся нетронутым. Так и надо: аккаунт-владелец таблиц ≠ аккаунт с
    # доступом к MCC. client_id/secret пустые ⇒ берём Ads-овские (тот же OAuth-клиент Google Cloud).
    sheets_refresh_token: SecretStr = SecretStr("")
    sheets_client_id: str = ""  # OAuth client id — не секрет
    sheets_client_secret: SecretStr = SecretStr("")
    # Ожидаемый владелец таблиц (например myhalads@gmail.com). Не гейт (файл уже создан), а СВЕРКА:
    # scripts/check_sheets_share.py читает owners у созданного файла и падает при расхождении —
    # иначе «не тот Drive» тихо обнаруживается месяцами позже, когда в чужой квоте кончится место.
    sheets_owner_email: str = ""

    # Наблюдаемость / мониторинг ошибок (Sentry, опционально). Пусто => ВЫКЛ (core.observability):
    # ноль накладных расходов и сети. SecretStr — DSN считается чувствительным. Перф-трейсинг по
    # умолчанию 0.0 (без оверхеда на запросах); ошибки сэмплируются 100%.
    sentry_dsn: SecretStr = SecretStr("")
    sentry_traces_sample_rate: float = 0.0

    # Планировщик / расписание (§14). Глобальная кадэнс read-only задач (отчёт/аномалии/очистка).
    # REPORT_SCHEDULE — стандартная crontab-строка «мин час день месяц день_недели»: одним полем
    # покрывает и ежедневно, и еженедельно (ТЗ §14 «ежедн./еженед.»). По умолчанию ежедневно 09:00.
    # Невалидная строка НЕ роняет старт (это не security-гейт): scheduler откатывается на дефолт с
    # громким логом (fail-safe). UserSettings.report_schedule (per-user) — задел под мультиюзер;
    # пока единый источник глобального расписания — env.
    report_schedule: str = "0 9 * * *"  # crontab: ежедневно 09:00 (локальное время)
    anomaly_interval_hours: int = 6  # проверка аномалий каждые N часов
    # Анти-спам: тот же алерт (аккаунт + вид) не повторяем чаще, чем раз в N часов. Без кулдауна
    # одна аномалия внутри окна anomaly_window_days рассылалась на КАЖДОМ прогоне (≈4 раза в сутки
    # всю неделю) → alert fatigue, оператор перестаёт читать. 0 = слать каждый прогон (как раньше).
    anomaly_cooldown_hours: float = 24.0
    # §advisor «утренний экран действий»: сколько топ-рекомендаций (по доле расхода под риском,
    # кросс-аккаунтно, БЕЗ FX) слать в проактивном дайджесте и пауза между сообщениями (flood-
    # limits Telegram: ≤1 msg/s на чат; 0.7c — тот же темп, что message-throttle).
    advise_digest_top_n: int = 5
    advise_digest_send_pause: float = 0.7
    # 1.6: недельный БИЗНЕС-дайджест менеджерам (WoW + топ-3 совета + аномалии), opt-in /bizdigest.
    business_digest_schedule: str = "0 9 * * 1"  # crontab: пн 09:00
    # 2.4: суточная фоновая re-discovery детей MCC (0 = выкл, набор аккаунтов — снимок на старте).
    mcc_rediscovery_hours: int = 24
    # 2.11 (§14): авто-подстройка порогов аномалий — READ-ONLY джоба СЧИТАЕТ волатильность и
    # ПРЕДЛАГАЕТ per-account пороги кнопкой «Принять» (пишет настройку бота только тап человека).
    # Opt-in (дефолт ВЫКЛ — анти-спам); расписание — crontab (вт 10:00).
    threshold_tune_enabled: bool = False
    threshold_tune_schedule: str = "0 10 * * 2"
    # 2.6: окна планового отчёта/сравнения аномалий (дни) — раньше зашиты в scheduler/jobs.
    report_window_days: int = 7
    anomaly_window_days: int = 7
    # Closed-loop optimization: important applied mutations are measured after an equal before/after
    # window and reported once to the originating Telegram chat. The scheduler stays read-only.
    outcome_check_days: int = 7
    outcome_check_schedule: str = "0 10 * * *"
    outcome_check_max_attempts: int = 3
    # Р6: алерт о правках, сделанных в аккаунте МИМО бота (change_event). Opt-in: 0 = выкл, потому
    # что на чужом аккаунте с активным агентством это шумный поток, а не сигнал. Окно шире интервала
    # НАМЕРЕННО (после простоя событие не должно выпасть из выборки) — дубли отсекает per-chat
    # курсор, а не узость окна. Потолок окна — 29 дней (ресурс живёт 30, сутки — запас, см.
    # reports.queries.CHANGE_EVENT_MAX_DAYS).
    external_changes_interval_hours: int = 0
    external_changes_window_days: int = 7
    # 2.6: таймауты денежного пути (сек) — крутятся без пересборки образа (env ADS_TIMEOUT_S/
    # LLM_TIMEOUT_S); влияют на деградацию OpenRouter/Google Ads.
    ads_timeout_s: float = 60.0
    llm_timeout_s: float = 45.0
    cleanup_interval_minutes: int = 60  # очистка просроченных черновиков каждые N минут
    # Durable incident notifications: enqueue, lease, and retry without storing route secrets.
    notification_outbox_interval_seconds: int = 30
    notification_outbox_lease_seconds: int = 120
    notification_outbox_max_attempts: int = 5
    notification_outbox_base_retry_seconds: int = 30
    incident_critical_escalation_minutes: int = 15
    incident_warning_escalation_minutes: int = 240
    incident_escalation_cooldown_minutes: int = 60
    # C4 (§5.3): чей процесс владеет джобами. `True` — исторический режим: APScheduler крутится в
    # event loop бота. `False` — джобы у отдельного процесса `python -m scheduler` (топология: три
    # процесса). Дефолт `True` намеренно: при архивации `bot/` планировщик обязан НЕ исчезнуть
    # молча, поэтому «ничего не настроили» = «работает как вчера», а не «джоб нет». Это не гейт
    # безопасности, а маршрутизация, поэтому fail-safe (правило 10 про отказ в доступе, не про
    # расписание). Реальный энфорсмент от двойного запуска — advisory-lock роли `scheduler`.
    scheduler_in_bot: bool = True
    # §19: TTL активного черновика визарда «Создание кампании» (campaign_drafts). Щедрый по
    # умолчанию — Этап-2 round-trip с Google Sheets может занять день. Старше → status='abandoned'
    # (та же очистка, что и просроченные proposals; cleanup_interval_minutes задаёт кадэнс).
    campaign_draft_ttl_hours: int = 72
    # 2.6: TTL неподтверждённого черновика мутации (часы) — раньше был зашит в scheduler/jobs
    # (и продублирован литералом «24 ч» в текстах карточки; теперь тексты берут {ttl_h} отсюда).
    proposal_ttl_hours: int = 24
    # Волна 5: TTL согласия на черновик тира L3 (`confirm/risk.py` — необратимое удаление, общий
    # бюджет, сдвиг денег ≥50%, снимок «было» не прочитан). Короче общего НАМЕРЕННО: сутки — это
    # срок, за который состояние аккаунта успевает измениться, и «да» вчерашней карточке про
    # удвоение бюджета относится уже не к тому аккаунту. Условие ДОПОЛНИТЕЛЬНОЕ (конъюнкт к общему
    # TTL в том же CAS), поэтому значение больше `proposal_ttl_hours` срок не удлиняет — общий
    # потолок всё равно действует.
    proposal_ttl_hours_l3: int = 2

    # §20: краулинг сайта клиента (clients.crawler). Статический краулер (без headless) с жёсткими
    # лимитами — не перегружать чужой сайт и не голодить общий event loop (краул в фоне, bounded).
    # Потолок 1000 страниц — прямое требование владельца (ОТКЛОНЕНИЕ от ТЗ §20.4 «до 50–100»,
    # зафиксировано в плане): «У наших клиентов есть 90% информации» — терять её нельзя. Обход при
    # этом остаётся вежливым: конкурентность ограничена, пауза общая, бюджет времени жёсткий.
    crawl_max_pages: int = 1000  # потолок числа страниц за обход
    crawl_max_depth: int = 3  # глубина BFS от главной
    crawl_concurrency: int = 6  # одновременных запросов к сайту (потолок вежливости — crawl_fetch)
    crawl_time_budget_s: float = 240.0  # общий бюджет времени на весь обход (внутренний дедлайн)
    crawl_delay_s: float = 0.5  # вежливая пауза между запросами к одному домену
    crawl_max_text_chars: int = 5000  # сколько текста берём с одной страницы (токены/поверхность)
    # Сколько страниц СОХРАНЯЕМ (client_site_pages). Раньше здесь стояли два независимых потолка
    # (site_pages_payload(limit=60) и срез [:200] в apply_upsert) — они резали карту сайта независимо
    # от crawl_max_pages, и поднимать один лимит было бессмысленно.
    crawl_store_max_pages: int = 1000
    # Сколько собранного текста уезжает в LLM-сведе́ние профиля и как он делится между страницами.
    # Общий потолок был 8000 сплошным срезом → в промпт попадали главная и половина второй страницы;
    # /price и /catalog не доходили никогда (§20.4 «цены со страницы каталога»).
    crawl_llm_text_chars: int = 24000  # общий бюджет текста краула на промпт
    crawl_llm_per_page_chars: int = 1500  # квота на одну страницу (чтобы хватило всем типам)
    # §20 ДОСЬЕ (map-reduce поверх сохранённого текста страниц). Квота выше — не «сколько влезет в
    # один промпт», а размер ЧАНКА: страниц много, вызовов много, но каждый — дешёвой моделью.
    dossier_chunk_chars: int = 7000  # размер чанка map-фазы (после вычитания шаблона)
    dossier_map_concurrency: int = 4  # одновременных вызовов LLM на map-фазе
    # Потолок числа map-вызовов на один сайт. ЖЁСТКИЙ литерал живёт в clients/dossier_map.py
    # (HARD_MAP_CALLS_CAP) — env может только ОПУСТИТЬ этот потолок, не поднять: иначе опечатка в
    # .env превращается в счёт от OpenRouter.
    dossier_max_map_calls: int = 40
    # Сколько низкоценных страниц одного типа (catalog/blog/other) вообще участвуют в досье:
    # 300 карточек авто не добавляют к досье ничего, кроме счёта за токены.
    dossier_max_pages_per_type: int = 12
    # §20: свести досье к языку владельца и схлопнуть кросс-язычные дубли (reduce-шаг normalize_ru
    # перед синтезом). Двуязычный сайт (напр. darial.co.jp EN+RU) иначе даёт задвоенные услуги/факты/
    # преимущества. True (дефолт) — включаем; False — прежнее поведение (рынки всё равно канонизирует
    # КОД). Сбой/бюджет-стоп нормализации не роняет досье (fail-open на код-сведённые списки).
    dossier_normalize_ru: bool = True
    # Сколько символов профиля/досье уезжает КОНТЕКСТОМ в генераторы (RSA, ключи, кластеры).
    # Одна ручка вместо хардкод-срезов по файлам: контекст берётся из PII-free llm_context досье.
    profile_ctx_chars: int = 3000
    # §20: зависшая (running) crawl_jobs старше N минут → failed на реконсиляции (in-process задача
    # умерла с процессом на рестарте). Кадэнс — cleanup_interval_minutes (та же очистка).
    crawl_stale_minutes: int = 30
    # §12: черновик в 'executing' старше N минут (процесс упал ПОСЛЕ claim, посреди мутации —
    # исход в Google Ads неизвестен) → needs_review на реконсиляции + уведомление владельца
    # (scheduler.jobs.reconcile_stale_executing). Порог ≫ худшего run_ads_call (4 попытки × 60с
    # + backoff ≈ 5 мин) — живой процесс не зацепим. Кадэнс — cleanup_interval_minutes.
    executing_stale_minutes: int = 30
    # §20.3: сколько ждём молча после последнего сообщения профиля до авто-сохранения (менеджер
    # может слать инфу несколькими сообщениями подряд — накапливаем в буфер, потом извлекаем).
    client_text_idle_s: int = 60

    # §15 (A1): проактивный алерт админам (ADMIN_CHAT_IDS) о НОВЫХ error_events. error_events
    # наполняется всегда, но был пассивным (узнать об ошибке — только вызвав /diag). Джоба
    # scheduler.jobs.run_error_alerts шлёт админам дайджест новых инцидентов раз в N минут. 0 ⇒
    # ВЫКЛ (алерты не шлём — фича opt-in, не спамим по умолчанию). Нет админов ⇒ тоже no-op.
    error_alert_interval_minutes: int = 0
    # §6/§15 (1.3): еженедельный дайджест админам (ADMIN_CHAT_IDS) — ошибки за 7 дней + баг-репорты +
    # сводка активности (операции/кампании/квота). Текст + прикреплённый файл. Джоба
    # scheduler.jobs.run_weekly_digest по крону weekly_digest_schedule. False ⇒ ВЫКЛ (opt-in, как
    # error_alert; нет админов ⇒ тоже no-op). Крон-строка невалидна ⇒ откат на пн 09:00 (fail-safe).
    weekly_digest_enabled: bool = False
    weekly_digest_schedule: str = "0 9 * * 1"  # crontab: понедельник 09:00 (локальное время)
    # §15 (C2): ретеншн растущих таблиц. error_events/crawl_jobs копятся монотонно (cleanup-джобы
    # лишь меняют статус, не удаляют). Джоба scheduler.jobs.purge_stale_rows удаляет строки старше
    # порога. audit_log НЕ трогаем (денежный реестр — ручной колд-архив, docs/BACKUP.md). 0 ⇒ ВЫКЛ.
    error_events_retain_days: int = 90
    crawl_jobs_retain_days: int = 30
    # N1.1: снапшоты health-score /audit (account_health_snapshot) — по строке на (аккаунт, день);
    # субстрат трендов, год истории достаточно. 0 ⇒ ВЫКЛ (не удаляем).
    account_health_retain_days: int = 365
    # §20: тексты страниц краула (client_site_pages.text) — сырьё для пересборки досье без нового
    # обхода. Крупные и чужие: держим 90 дней, дальше purge_stale_rows обнуляет ТЕКСТ (строка с
    # url/title остаётся — карта sitelinks не должна усохнуть). 0 ⇒ хранить вечно.
    site_page_text_retain_days: int = 90
    # §3 (C2): строки распределённого счётчика квоты (ads_quota_ops). Счёт нужен только за скользящее
    # 24ч-окно — всё старше мертво для подсчёта; держим 2 суток (окно + буфер против клок-скью и как
    # короткий форензик-след), дальше purge_stale_rows удаляет. 0 ⇒ ВЫКЛ (таблица растёт, но счёт
    # остаётся верным — фильтр по окну не зависит от prune).
    ads_quota_ops_retain_days: int = 2
    # Волна 3 (event sourcing): журнал прогонов (agent_runs + agent_run_events). Уборка идёт ЦЕЛЫМИ
    # ПРОГОНАМИ, а не отдельными событиями: вырезать звено из хэш-цепочки значит своими руками создать
    # тот самый разрыв, который verify_chain обязан считать подделкой. Прогон, где есть ХОТЬ ОДНО
    # событие денежного пути (MONEY_KINDS), не убирается вовсе — его пол хранения держит триггер СУБД,
    # а решение об архивации денежного следа принимает человек (docs/BACKUP.md), не джоба. 0 ⇒ ВЫКЛ.
    agent_runs_retain_days: int = 90
    # Волна 4 (автооткат): журнал наблюдений за применёнными мутациями. Денежного следа тут нет —
    # он в audit_log и agent_run_events, — поэтому строку можно убирать обычным ретеншном. 0 ⇒ ВЫКЛ.
    rollback_watch_retain_days: int = 180
    operations_retain_days: int = 365
    revenue_events_retain_days: int = 730
    channel_metrics_retain_days: int = 730
    # Окно наблюдения после применения мутации, часы. За него измеримы РАСХОД и КЛИКИ, но не CPA:
    # конверсия атрибутируется ко времени клика и досчитывается сутками (см. scheduler/rollback.py).
    # Меньше 2 ч ставить бессмысленно — час применения неполный, и база сравнения его не покрывает.
    rollback_watch_window_hours: int = 4
    # Порог вердикта: во сколько робастных отклонений (MAD) расход должен превысить медиану того же
    # часа того же дня недели, чтобы час считался деградировавшим. 3.0 — консервативно намеренно:
    # цена ложного «degraded» в режиме auto — чужие деньги, цена пропуска — один невыполненный откат.
    rollback_watch_mad_k: float = 3.0
    # Режим контура: shadow (пишет вердикт, наружу ничего) | alert (сигналит человеку) | auto
    # (исполняет компенсацию — НЕ РЕАЛИЗОВАН, Волна 6a; значение отвергается на старте детектора).
    rollback_watch_mode: str = "shadow"
    # §12 (C3): пер-юзер дневной потолок LLM-вызовов (анти-абуз/защита OpenRouter-бюджета). Единственный
    # тормоз до этого — message-throttle 0.7/с + баланс OpenRouter (не enforced). core.llm_budget
    # считает вызовы per chat_id за сутки; warn на 80%, отказ (fail-closed) на 100%. 0 ⇒ ВЫКЛ (без
    # гарда). Дефолт 0 — opt-in: не удивить владельца (главный оператор) лимитом в разгар работы.
    llm_daily_calls_per_user: int = 0
    # #10 Наблюдаемость / spend-cap НИЖЕ агента (предусловие delegation, config.yaml:75-78): дневной
    # потолок СТОИМОСТИ (USD), не числа вызовов. llm_daily_calls_per_user ограничивает наш NL-путь
    # пер-chat, но НЕ покрывает автономный Hermes-цикл (идёт мимо процесса). Этот потолок сверяется с
    # реальными тратами из core.or_activity (OpenRouter /key usage_daily или /activity); энфорсится
    # в agent/router.chat (BZ-4, единая точка всех наших LLM-вызовов) + ранний человекочитаемый отказ
    # в bot/main._llm_budget_or_reply. 0.0 ⇒ ВЫКЛ (в prod автодефолт 10 USD —
    # _default_llm_cost_cap_in_prod). Это МЯГКИЙ рубеж в нашем коде; ЖЁСТКИЙ — limit+limit_reset:daily
    # на самом ключе OpenRouter (серверный enforcement, RB-3, руки владельца). Оба нужны: наш — раньше
    # и с контекстом.
    llm_daily_cost_cap_usd: float = 0.0

    @property
    def whitelist(self) -> set[int]:
        return {int(x) for x in self.telegram_whitelist_chat_ids.split(",") if x.strip()}

    @property
    def admin_ids(self) -> set[int]:
        """Админы бота (/grant /revoke). Пусто ⇒ никому (fail-closed). Нечисловой мусор отбрасываем
        (как whitelist, но без падения — фича опциональна)."""
        out: set[int] = set()
        for x in self.admin_chat_ids.split(","):
            x = x.strip()
            if x.lstrip("-").isdigit():
                out.add(int(x))
        return out

    @property
    def model_choice_list(self) -> list[str]:
        """Пресеты моделей для /model (из env MODEL_CHOICES). Пусто => дефолт в agent.router."""
        return [m.strip() for m in self.model_choices.split(",") if m.strip()]

    @property
    def allowed_customer_ids(self) -> set[str]:
        """Аккаунты, которые боту РАЗРЕШЕНО трогать (нормализованные). Замок — в ads.client.
        Фильтруем по НОРМАЛИЗОВАННОМУ результату (не по сырому `x.strip()`): мусорный токен без
        цифр (inline-комментарий/плейсхолдер из .env) нормализуется в '' и НЕ должен попасть в
        множество — иначе '' протекает в замки (см. login_customer_id_set)."""
        return {
            n
            for x in self.google_ads_allowed_customer_ids.split(",")
            if (n := normalize_customer_id(x))
        }

    @property
    def allow_all_visible(self) -> bool:
        """Сентинел «all»/«*» в GOOGLE_ADS_ALLOWED_CUSTOMER_IDS — мутации разрешены на ПОЛНОМ
        видимом наборе (ads.client.allowed_ceiling(): Draft ∪ read-list ∪ дочерние, обнаруженные
        обходом MCC). С 2026-07-30 (BZ-1) это ЯВНОЕ значение env, не prod-дефолт
        (коэрция пустого значения снята — см. _warn_mutations_closed_in_prod). НЕ снимает две
        страховки: (1) confirm-гейт («да» + confirmation_id, ensure_allowed на исполнении),
        (2) потолок видимости — аккаунт вне MCC мутировать нельзя (бот его не видит). Динамически
        ограничен фактически обнаруженным набором: сбой discovery ⇒ мутабелен только пол потолка
        {Draft} (безопасная деградация, не эскалация)."""
        return self.google_ads_allowed_customer_ids.strip().lower() in {"all", "*"}

    @property
    def read_customer_ids(self) -> set[str]:
        """§8: аккаунты, доступные на ЧТЕНИЕ помимо мутационного allow-list (сводка по дочерним
        MCC), нормализованные. Замок чтения — ads.client.ensure_read_allowed (fail-closed).
        Фильтр по нормализованному результату (мусор без цифр → '' → отбрасывается)."""
        return {
            n
            for x in self.google_ads_read_customer_ids.split(",")
            if (n := normalize_customer_id(x))
        }

    @property
    def two_factor_ops(self) -> set[str]:
        """§12: множество операций, требующих 2FA-кода (нормализованные имена мутаций). Гейт —
        core.twofa.required_for × bot.main._do_confirm. Пусто ⇒ ничего не гейтится."""
        return {op for x in self.two_factor_ops_csv.split(",") if (op := x.strip())}

    @property
    def four_eyes_risk_tiers(self) -> set[str]:
        """Normalized proposal risk tiers requiring an independent approval vote."""
        return {
            tier
            for value in self.four_eyes_risk_tiers_csv.split(",")
            if (tier := value.strip().upper()) in {"L1", "L2", "L3"}
        }

    @model_validator(mode="after")
    def _validate_four_eyes_tiers(self) -> "Settings":
        raw = {
            value.strip().upper()
            for value in self.four_eyes_risk_tiers_csv.split(",")
            if value.strip()
        }
        invalid = raw - {"L1", "L2", "L3"}
        if invalid:
            raise ValueError(f"FOUR_EYES_RISK_TIERS_CSV contains unknown tiers: {sorted(invalid)}")
        if self.four_eyes_required and not raw:
            raise ValueError("FOUR_EYES_REQUIRED needs at least one configured risk tier")
        return self

    @model_validator(mode="after")
    def _validate_notification_outbox(self) -> "Settings":
        if not 5 <= self.notification_outbox_interval_seconds <= 3600:
            raise ValueError("NOTIFICATION_OUTBOX_INTERVAL_SECONDS must be between 5 and 3600")
        if not 10 <= self.notification_outbox_lease_seconds <= 3600:
            raise ValueError("NOTIFICATION_OUTBOX_LEASE_SECONDS must be between 10 and 3600")
        if not 1 <= self.notification_outbox_max_attempts <= 20:
            raise ValueError("NOTIFICATION_OUTBOX_MAX_ATTEMPTS must be between 1 and 20")
        if not 1 <= self.notification_outbox_base_retry_seconds <= 3600:
            raise ValueError("NOTIFICATION_OUTBOX_BASE_RETRY_SECONDS must be between 1 and 3600")
        if not 0 <= self.incident_critical_escalation_minutes <= 10080:
            raise ValueError("INCIDENT_CRITICAL_ESCALATION_MINUTES must be between 0 and 10080")
        if not 0 <= self.incident_warning_escalation_minutes <= 10080:
            raise ValueError("INCIDENT_WARNING_ESCALATION_MINUTES must be between 0 and 10080")
        if not 1 <= self.incident_escalation_cooldown_minutes <= 10080:
            raise ValueError("INCIDENT_ESCALATION_COOLDOWN_MINUTES must be between 1 and 10080")
        return self

    @property
    def geo_default_country(self) -> str:
        """ОПЦИОНАЛЬНАЯ страна-последней-инстанции (ISO alpha-2), когда запрос гео НЕ задал. Из env
        DEFAULT_GEO_COUNTRY_CODE; ПУСТО по умолчанию (без хардкода «UA») ⇒ страна берётся из запроса,
        а при её отсутствии создание требует гео (см. bot.main._effective_geo_locations/гейт)."""
        return self.default_geo_country_code.strip().upper()

    @property
    def geo_default_locale(self) -> str:
        """D7: язык названий локаций по умолчанию. Явный DEFAULT_GEO_LOCALE побеждает; иначе
        выводим из страны (ads.geo: UG→en, UA→uk…), финальный фолбэк «ru»."""
        if self.default_geo_locale.strip():
            return self.default_geo_locale.strip()
        from ads.geo import language_for_country

        return language_for_country(self.geo_default_country) or "ru"

    @property
    def login_customer_id_set(self) -> set[str]:
        """Все MCC (нормализованные), под которыми разрешён обход/логин (§8). Основной
        login_customer_id ∪ доп. список google_ads_login_customer_ids. Замок обхода —
        ads.client.ensure_manager_allowed (fail-closed на пустом множестве).

        КРИТИЧНО: фильтруем по НОРМАЛИЗОВАННОМУ результату, а не по сырому `x.strip()`. Раньше
        непустой мусор без цифр (напр. inline-комментарий из .env.defaults, «просочившийся» как
        значение) проходил `x.strip()`, но `normalize_customer_id(x) == ''` — и '' попадал в
        множество. Тогда стартовый discover_read_children делал ga.search(customer_id='') →
        GoogleAdsException «Invalid customer ID ''», а ensure_manager_allowed fail-open на ''.
        Фильтр по нормализованному значению убирает класс целиком (происхождение мусора неважно)."""
        base_n = normalize_customer_id(self.google_ads_login_customer_id)
        base = {base_n} if base_n else set()
        extra = {
            n
            for x in self.google_ads_login_customer_ids.split(",")
            if (n := normalize_customer_id(x))
        }
        return base | extra

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    @model_validator(mode="after")
    def _validate_env_name(self) -> "Settings":
        """B5: ENV должен быть dev|test|prod. Опечатка (напр. «production», «Prod») раньше молча
        трактовалась как НЕ-prod → ВСЕ prod-гейты (whitelist/ключ шифрования/мутации fail-fast)
        тихо отключались, а бот поднимался в небезопасной конфигурации. Нормализуем регистр/пробелы;
        неизвестное значение — падаем на старте (fail-fast, а не тихий небезопасный dev-режим)."""
        raw = (self.env or "").strip().lower()
        if raw not in {"dev", "test", "prod"}:
            raise ValueError(
                f"ENV={self.env!r} недопустимо — ожидается dev|test|prod. Опечатка (напр. "
                "'production') молча отключила бы все prod-гейты безопасности (fail-fast)."
            )
        # Присваиваем ТОЛЬКО если нормализация что-то изменила: любое `self.env = ...` заносит поле
        # в model_fields_set, а по нему require_dev_env() отличает «ENV задан явно» от «сработал
        # дефолт "dev"» (D3, fail-closed). Безусловное присваивание делало дефолт неотличимым.
        if raw != self.env:
            self.env = raw
        return self

    @model_validator(mode="after")
    def _require_encryption_key_in_prod(self) -> "Settings":
        """Fail-fast: в prod пустой/невалидный SECRETS_ENCRYPTION_KEY недопустим (токены
        шифруются at-rest). В dev/тестах (SQLite, без шифрования) — не требуем, чтобы суйта
        оставалась зелёной. Срабатывает единообразно для бота, scheduler, скриптов и Alembic."""
        if self.env == "prod":
            key = self.secrets_encryption_key.get_secret_value()
            if not key:
                raise ValueError(
                    "SECRETS_ENCRYPTION_KEY обязателен в prod — сгенерируй: Fernet.generate_key()"
                )
            try:
                from cryptography.fernet import Fernet

                # Round-trip (а не только конструктор Fernet): шифруем И расшифровываем пробу —
                # ловит порчу ключа / сломанный crypto-бэкенд ДО первой реальной мутации токенов,
                # а не на первом обращении к oauth_tokens. Без импорта core.secrets (тот тянет config).
                f = Fernet(key.encode())
                probe = b"aimash-keycheck"
                if f.decrypt(f.encrypt(probe)) != probe:
                    raise ValueError("round-trip mismatch")
            except Exception as e:
                raise ValueError(
                    "SECRETS_ENCRYPTION_KEY невалиден (нужен ключ Fernet.generate_key())"
                ) from e
        return self

    @model_validator(mode="after")
    def _require_hermes_trusted_transport(self) -> "Settings":
        """PLAN/WRITE without a signing key would let a fail-open hook erase actor provenance."""
        if self.hermes_write_enabled:
            key = self.aimash_trust_hmac_key.get_secret_value().encode("utf-8")
            if len(key) < 32:
                raise ValueError(
                    "HERMES_WRITE_ENABLED=true requires AIMASH_TRUST_HMAC_KEY of at least 32 bytes"
                )
        return self

    @model_validator(mode="after")
    def _default_llm_cap_in_prod(self) -> "Settings":
        """2.12: в prod пер-юзер LLM-лимит НЕ должен быть выключен молча — один оператор (или
        скомпрометированный Telegram) выжег бы общий баланс OpenRouter для всех. 0 в prod →
        разумный дефолт 500 вызовов/сутки (широкая ручная работа не упирается; env переопределяет
        явно). В dev дефолт остаётся 0 (не мешаем отладке)."""
        if self.env == "prod" and int(self.llm_daily_calls_per_user or 0) <= 0:
            self.llm_daily_calls_per_user = 500
        return self

    @model_validator(mode="after")
    def _default_llm_cost_cap_in_prod(self) -> "Settings":
        """BZ-4: в prod долларовый потолок дня НЕ должен быть выключен молча — до 2026-07-30
        check_daily_cost_cap не звался нигде, а решение D1 («$10/сутки кредитным лимитом на ключе
        OpenRouter») замером 29.07 опровергнуто: limit=null на живом ключе. 0 в prod → 10 USD/сутки
        (значение из D1; env переопределяет явно — тот же паттерн, что _default_llm_cap_in_prod).
        В dev дефолт остаётся 0 (не мешаем отладке; тестам не нужен httpx-мок в каждом вызове)."""
        if self.env == "prod" and float(self.llm_daily_cost_cap_usd or 0.0) <= 0:
            self.llm_daily_cost_cap_usd = 10.0
        return self

    @model_validator(mode="after")
    def _warn_mutations_closed_in_prod(self) -> "Settings":
        """BZ-1 (2026-07-30): пустой GOOGLE_ADS_ALLOWED_CUSTOMER_IDS больше НЕ коэрцится в «all».
        Прежний дефолт («prod готов из коробки», решение владельца 2026-07) делал «выключить
        мутации очисткой env» операцией, которая их ОТКРЫВАЕТ на все видимые аккаунты, — инверсия
        правила 10 в самой опасной точке конфигурации. Теперь пусто = fail-closed в любом
        окружении: prod поднимается read-only (ensure_allowed отказывает каждой мутации), о чём
        предупреждаем на старте — молчаливым это состояние быть не должно. Сентинел «all» остаётся
        валидным ЯВНЫМ значением env: прежнее поведение возвращается одной строкой, но как решение
        владельца, а не тихий дефолт."""
        if self.env == "prod" and not self.google_ads_allowed_customer_ids.strip():
            import logging  # stdlib напрямую: core.logging импортирует этот модуль (цикл)

            logging.getLogger("aimash.config").warning(
                "GOOGLE_ADS_ALLOWED_CUSTOMER_IDS пуст — мутации ВЫКЛЮЧЕНЫ (fail-closed, "
                "read-only режим). Включить: =all (все видимые) или явный список id."
            )
        return self

    @model_validator(mode="after")
    def _default_budget_blast_radius_in_prod(self) -> "Settings":
        """B1-4: в prod счётный кап повышений бюджета НЕ должен быть выключен молча — серию
        подтверждённых «+20%» за день больше ничто не ограничивает (тиры L1/L3 не заменяют
        отдельный накопительный cap,
        quota считает операции, MONEY_MAX — потолок ОДНОЙ операции). 0 в prod → 10 повышений на
        аккаунт в сутки (ручная работа не упирается; env переопределяет явно — тот же паттерн,
        что _default_llm_cap_in_prod). Денежный кап (units) дефолтом не трогаем: сумма зависит
        от валюты аккаунта, универсального числа нет."""
        if self.env == "prod" and int(self.daily_budget_increase_max_ops or 0) <= 0:
            self.daily_budget_increase_max_ops = 10
        return self

    @model_validator(mode="after")
    def _clamp_external_changes_window(self) -> "Settings":
        """Р6: окно журнала правок вне 1..29 дней → откат на 7 с ГРОМКИМ логом.

        Ресурс `change_event` хранит 30 дней, и ридер (`check_change_event_days`) на выходе за
        границу отказывает — правильно для вызова инструмента, но для фоновой джобы это значило бы
        отказ на каждом аккаунте каждый цикл: опечатка в env превратилась бы в «алертов нет» вместо
        «алерты сломаны». Расписание — не гейт безопасности, поэтому здесь тот же fail-safe, что у
        невалидного REPORT_SCHEDULE: работаем на дефолте и говорим об этом вслух. Потолок 29, а не
        30, — сутки запаса на host-date фолбэк чтения таймзоны (см. CHANGE_EVENT_MAX_DAYS); число
        продублировано литералом (импорт `reports.queries` отсюда = цикл), совпадение держит тест."""
        days = int(self.external_changes_window_days or 0)
        if not 1 <= days <= 29:
            import logging  # stdlib напрямую: core.logging импортирует этот модуль (цикл)

            logging.getLogger("aimash.config").warning(
                "EXTERNAL_CHANGES_WINDOW_DAYS=%s вне 1..29 (ретенция change_event минус сутки "
                "запаса) — беру 7",
                self.external_changes_window_days,
            )
            self.external_changes_window_days = 7
        return self

    @model_validator(mode="after")
    def _require_whitelist_in_prod(self) -> "Settings":
        """Fail-fast: в prod пустой whitelist недопустим — иначе бот отвечал бы ВСЕМ (fail-open).
        В dev/тестах не требуем (удобство), но WhitelistMiddleware всё равно fail-closed (пустой
        whitelist => бот никому не отвечает), как и замок аккаунта (ads.client.ensure_allowed)."""
        if self.env == "prod" and not self.whitelist:
            raise ValueError(
                "TELEGRAM_WHITELIST_CHAT_IDS обязателен в prod — пустой whitelist означал бы "
                "ответы всем (fail-open). Укажи хотя бы один chat_id."
            )
        return self

    @model_validator(mode="after")
    def _require_google_ads_in_prod(self) -> "Settings":
        """Fail-fast: в prod без developer token бот не сможет работать с Google Ads ВООБЩЕ (даже
        читать). Падаем на СТАРТЕ (тут), а не на первом вызове API. Пустой allowed_customer_ids
        старт больше НЕ роняет (BZ-1, 2026-07-30): это валидная read-only конфигурация — мутации
        выключены fail-closed (ensure_allowed), чтение/отчёты/алерты работают; предупреждение
        пишет _warn_mutations_closed_in_prod. В dev/тестах не требуем (работа на фейках)."""
        if self.env == "prod" and not self.google_ads_developer_token.get_secret_value():
            raise ValueError(
                "В prod обязателен GOOGLE_ADS_DEVELOPER_TOKEN — иначе бот не сможет работать с "
                "Google Ads (fail-fast на старте, а не на первом вызове API)."
            )
        return self

    @model_validator(mode="after")
    def _require_strong_2fa_pin_in_prod(self) -> "Settings":
        """A14: в prod включённый 2FA с ЗАДАННЫМ, но слишком коротким PIN (<6 символов) — fail-fast
        на старте. Короткий PIN тривиально перебирается; вместе с кулдауном (bot.main) это база
        анти-перебора. Пустой PIN при включённом 2FA НЕ роняет старт (is_ready() fail-closed
        блокирует опасные ops в рантайме); в dev/тестах длину не требуем (короткие PIN в фикстурах)."""
        if self.env == "prod" and self.two_factor_enabled:
            pin = (self.two_factor_pin.get_secret_value() or "").strip()
            if pin and len(pin) < 6:
                raise ValueError(
                    "TWO_FACTOR_PIN должен быть ≥6 символов в prod (короткий PIN легко перебрать)"
                )
        return self

    @model_validator(mode="after")
    def _reject_leaked_db_password_in_prod(self) -> "Settings":
        """D3: пароли `aimash` / `aimash_ro` лежали ЛИТЕРАЛАМИ в tracked docker-compose.yml и
        db/init/*.sql — то есть утекли в git (и в каждый клон/форк). Параметризация compose
        (`${POSTGRES_PASSWORD:?}`) сама по себе не мешает подставить туда же «aimash» и оставить
        всё как было. Поэтому в prod известный утёкший пароль = fail-fast на старте: ротируй
        (ALTER ROLE на VPS — том уже проинициализирован, см. docs/DEPLOYMENT.md).
        В dev/тестах не требуем (локальная БД в контуре разработчика)."""
        if self.env != "prod":
            return self
        from urllib.parse import urlsplit

        try:  # URL может быть sqlite/кривым — тогда пароля просто нет, проверять нечего
            pw = urlsplit(self.database_url.get_secret_value()).password
        except ValueError:
            pw = None
        if pw in _LEAKED_DB_PASSWORDS:
            raise ValueError(
                "DATABASE_URL в prod использует пароль, утёкший в git (дефолт репозитория). "
                "Задай POSTGRES_PASSWORD в .env и смени пароль роли на сервере: "
                "ALTER ROLE aimash WITH PASSWORD '…'; — иначе доступ к БД (audit_log, "
                "шифрованные refresh-токены) есть у любого, кто видел репозиторий."
            )
        return self

    @model_validator(mode="after")
    def _normalize_provider_sort(self) -> "Settings":
        """OpenRouter `provider.sort` принимает только price|throughput|latency (или пусто = ВЫКЛ).
        Кривое значение из .env раньше улетало в API как есть → BadRequestError 400
        «provider.sort: Invalid input» на КАЖДОМ парсинге команды (денежный путь). Нормализуем
        (strip/lower) и коэрсим невалидное → "" (ВЫКЛ) с warning — денежный путь становится
        fail-safe к опечатке/лишним кавычкам в .env, а не роняет весь парсинг."""
        raw = (self.openrouter_parsing_provider_sort or "").strip().lower()
        if raw not in _VALID_PROVIDER_SORTS:
            import logging

            logging.getLogger("aimash.config").warning(
                "OPENROUTER_PARSING_PROVIDER_SORT=%r невалидно (допустимо: price|throughput|latency "
                "или пусто) — отключаю provider.sort, чтобы не ловить 400 от OpenRouter.",
                self.openrouter_parsing_provider_sort,
            )
            raw = ""
        self.openrouter_parsing_provider_sort = raw
        return self


settings = Settings()


def require_dev_env() -> None:
    """Гард dev-скриптов прямой записи (минуют confirm-гейт): разрешено ТОЛЬКО при ЯВНОМ ENV=dev
    (golden rule #10). Иначе SystemExit — скрипт не стартует вне dev. Единый источник, чтобы гард
    нельзя было забыть в новом demo-скрипте (вызывать первой строкой main()).

    D3: раньше проверялось только `settings.env != "dev"`, а дефолт поля — "dev". На проде, где
    ENV в .env не задан (легко: ни один валидатор его не требует), дефолт молча делал хост
    «dev» ⇒ demo-скрипт с прямой записью в Google Ads стартовал бы на боевых кредах. Теперь
    отсутствие ENV = ОТКАЗ (fail-closed, правило #10): «не знаю окружение» ≠ «это dev».
    `model_fields_set` содержит поле, только если оно пришло из окружения/.env/явного присвоения.
    """
    if "env" not in settings.model_fields_set:
        raise SystemExit(
            "ENV не задан явно — прямая запись мимо confirm-гейта запрещена (fail-closed: "
            "неизвестное окружение НЕ считается dev). Поставь ENV=dev в .env — golden rule #10."
        )
    if settings.env != "dev":
        raise SystemExit(
            f"Прямая запись мимо confirm-гейта запрещена вне ENV=dev (сейчас ENV={settings.env!r}) "
            "— golden rule #10."
        )
