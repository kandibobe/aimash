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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Окружение
    env: str = "dev"  # dev => только TEST MCC

    # Модель через OpenRouter (сменяемая)
    openrouter_api_key: SecretStr = SecretStr("")
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # имена llm_* (не model_*) — иначе шадоуят метод BaseModel.model_copy()
    # Разделение «что за что» (дефолты; ручной выбор оператора через /model бьёт их — см.
    # agent.router.effective_model: override > роль-дефолт):
    #   parsing — разбор команд (function calling, денежный путь): дёшево и точно, ошибку
    #             ловит confirm-гейт + код-валидация → дорогая модель тут не нужна.
    #   copy    — генерация RSA-текстов: качество РУССКОГО важнее цены → сильная модель.
    llm_parsing: str = "deepseek/deepseek-chat"  # A/B: дёшево, ≈Claude на парсинге
    llm_copy: str = "anthropic/claude-sonnet-4.6"  # копирайт RU — лучшее качество (RSA)
    llm_fallback: str = "anthropic/claude-sonnet-4.6"  # Hermes выбыл (нет tool use на OpenRouter)
    # P2: отдельная роль КЛАСТЕРИЗАЦИИ/интента keyword research (вопрос заказчика 2026-07-06
    # «какая модель?»). Пусто ⇒ = llm_parsing (поведение прежнее байт-в-байт); LLM_CLUSTERING
    # в .env позволяет прогнать A/B сильной модели ТОЛЬКО на интент-классификации.
    llm_clustering: str = ""
    # P3: роль АНАЛИТИКА (агентный нарратив /audit — multi-turn read-only рассуждение поверх уже
    # посчитанного КОДОМ аудита). Пусто ⇒ = llm_parsing (дешёвый дефолт; галлюцинации безвредны —
    # на выходе fact-guard + детерминированный fallback). LLM_ANALYST в .env = A/B сильной модели.
    llm_analyst: str = ""
    # Пресеты для рантайм-переключателя /model (CSV slug'ов OpenRouter). Пусто => дефолт в
    # agent.router._DEFAULT_CHOICES (tool-use-capable модели). Своя модель — через /model в боте.
    model_choices: str = ""
    # Потолок генерации по ролям (явный max_tokens — экономия бюджета БЕЗ потери качества:
    # без него OpenRouter резервирует полный max-output против дневного бюджета; см.
    # agent.router.ROLE_MAX_TOKENS). Парсинг → крошечный tool-call; копирайт → короткий JSON.
    llm_max_tokens_parsing: int = 1024
    llm_max_tokens_copy: int = 2048
    # P3: потолок нарратива аналитика (короткий человеческий разбор аудита; без него OpenRouter
    # резервирует полный max-output против дневного бюджета — см. agent.router.ROLE_MAX_TOKENS).
    llm_max_tokens_analyst: int = 1536
    # P3: включать ли агентный НАРРАТИВ в /audit (multi-turn LLM поверх детерминированного ядра).
    # True (дефолт) — карточка предваряется человеческим разбором; сбой/timeout/fact-guard молча
    # откатывают на детерминированную карточку (числа — всегда КОД). False — только детерминир. карточка.
    audit_agentic_narrative: bool = True
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
    # Пер-пользовательская изоляция аккаунтов ЧТЕНИЯ (core.access, таблица account_access):
    #   auto (дефолт) — пустая таблица грантов ⇒ legacy-проход (все whitelisted видят весь
    #                   read-list; поведение одно-операторного режима), ПЕРВЫЙ грант включает
    #                   enforcement для всех;
    #   enforced      — строгий режим даже с пустой таблицей (не-Draft только по гранту);
    #   legacy        — осознанное отключение пер-юзер изоляции (только глобальный read-замок).
    # Draft доступен всем whitelisted в любом режиме. Невалидное значение → warning + auto.
    account_access_mode: str = "auto"
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
    # дочерние MCC) — см. allow_all_visible. В ПРОД пусто => дефолт «all» (prod готов из коробки,
    # _default_mutations_all_in_prod); в dev/test пусто => fail-closed (мутаций нет). Замок
    # видимости и confirm-гейт остаются: чужой аккаунт вне MCC немутируем, «да» обязателен.
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
    google_ads_api_version: str = "v24"  # API-версия (мажор). SDK-пин google-ads — в pyproject.toml
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
        "remove_campaign,remove_ad_group,update_budget,update_bid,set_bidding_strategy"
    )
    # A14: базовый кулдаун (мин) после _TWOFA_MAX_ATTEMPTS неверных PIN подряд — вход в 2FA-режим
    # блокируется (fail-closed: опасная op недоступна), при повторных локаутах растёт экспоненциально
    # (bot.main._twofa_register_fail). Анти-перебор PIN: раньше счётчик обнулялся каждым новым ✅.
    two_factor_lockout_minutes: int = 15

    # Безопасность / БД
    secrets_encryption_key: SecretStr = SecretStr("")
    # SecretStr: DSN несёт пароль БД — маскируем в repr/логах/трейсбеках (golden rule #5).
    # Реальное значение — только через .get_secret_value() (db.session, migrations.env).
    database_url: SecretStr = SecretStr("postgresql+asyncpg://aimash:aimash@localhost:5432/aimash")

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
    # 2.6: таймауты денежного пути (сек) — крутятся без пересборки образа (env ADS_TIMEOUT_S/
    # LLM_TIMEOUT_S); влияют на деградацию OpenRouter/Google Ads.
    ads_timeout_s: float = 60.0
    llm_timeout_s: float = 45.0
    cleanup_interval_minutes: int = 60  # очистка просроченных черновиков каждые N минут
    # §19: TTL активного черновика визарда «Создание кампании» (campaign_drafts). Щедрый по
    # умолчанию — Этап-2 round-trip с Google Sheets может занять день. Старше → status='abandoned'
    # (та же очистка, что и просроченные proposals; cleanup_interval_minutes задаёт кадэнс).
    campaign_draft_ttl_hours: int = 72
    # 2.6: TTL неподтверждённого черновика мутации (часы) — раньше был зашит в scheduler/jobs
    # (и продублирован литералом «24 ч» в текстах карточки; теперь тексты берут {ttl_h} отсюда).
    proposal_ttl_hours: int = 24

    # §20: краулинг сайта клиента (clients.crawler). Статический краулер (без headless) с жёсткими
    # лимитами — не перегружать чужой сайт и не голодить общий event loop (краул в фоне, bounded).
    crawl_max_pages: int = 50  # потолок числа страниц за обход (ТЗ §20.4: «до 50–100»)
    crawl_max_depth: int = 3  # глубина BFS от главной
    crawl_time_budget_s: float = 90.0  # общий бюджет времени на весь обход (asyncio.wait_for)
    crawl_delay_s: float = 0.5  # вежливая пауза между запросами к одному домену
    crawl_max_text_chars: int = 5000  # сколько текста берём с одной страницы (токены/поверхность)
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
    # §12 (C3): пер-юзер дневной потолок LLM-вызовов (анти-абуз/защита OpenRouter-бюджета). Единственный
    # тормоз до этого — message-throttle 0.7/с + баланс OpenRouter (не enforced). core.llm_budget
    # считает вызовы per chat_id за сутки; warn на 80%, отказ (fail-closed) на 100%. 0 ⇒ ВЫКЛ (без
    # гарда). Дефолт 0 — opt-in: не удивить владельца (главный оператор) лимитом в разгар работы.
    llm_daily_calls_per_user: int = 0

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
        обходом MCC). Это prod-дефолт (см. _default_mutations_all_in_prod). НЕ снимает две
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
    def _default_llm_cap_in_prod(self) -> "Settings":
        """2.12: в prod пер-юзер LLM-лимит НЕ должен быть выключен молча — один оператор (или
        скомпрометированный Telegram) выжег бы общий баланс OpenRouter для всех. 0 в prod →
        разумный дефолт 500 вызовов/сутки (широкая ручная работа не упирается; env переопределяет
        явно). В dev дефолт остаётся 0 (не мешаем отладке)."""
        if self.env == "prod" and int(self.llm_daily_calls_per_user or 0) <= 0:
            self.llm_daily_calls_per_user = 500
        return self

    @model_validator(mode="after")
    def _default_mutations_all_in_prod(self) -> "Settings":
        """Решение владельца 2026-07: Draft-only доктрина СНЯТА — в prod бот готов к работе на
        ВСЕХ видимых аккаунтах по умолчанию. Если ENV=prod и GOOGLE_ADS_ALLOWED_CUSTOMER_IDS
        пуст ⇒ выставляем сентинел «all» (мутации на allowed_ceiling(): Draft ∪ read-list ∪
        обнаруженные дочерние; всё так же ⊆ видимости + confirm-гейт). Владелец может СУЗИТЬ
        явным списком id. В dev/test пусто остаётся fail-closed (не открываем локаль/CI). Должен
        идти ДО _require_google_ads_in_prod, чтобы тот увидел уже заполненное значение."""
        if self.env == "prod" and not self.google_ads_allowed_customer_ids.strip():
            self.google_ads_allowed_customer_ids = "all"
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
        """Fail-fast: в prod без developer token / allowed_customer_ids бот всё равно не сможет
        работать с Google Ads. Падаем на СТАРТЕ (тут), а не на первом вызове API: ensure_allowed
        и так fail-closed на пустой allow-list, но это срабатывает позже — лучше не подняться с
        неполной конфигурацией. В dev/тестах не требуем (работа на фейках/без живых кредов)."""
        if self.env == "prod":
            missing = []
            if not self.google_ads_developer_token.get_secret_value():
                missing.append("GOOGLE_ADS_DEVELOPER_TOKEN")
            # Сентинел «all» (allow_all_visible) — валидная конфигурация мутаций (полный видимый
            # набор). В prod он проставляется дефолтом (_default_mutations_all_in_prod), так что
            # эта ветка практически недостижима пустой; проверка остаётся как явный инвариант.
            if not (self.allow_all_visible or self.allowed_customer_ids):
                missing.append("GOOGLE_ADS_ALLOWED_CUSTOMER_IDS")
            if missing:
                raise ValueError(
                    f"В prod обязательны: {', '.join(missing)} — иначе бот не сможет работать с "
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
    """Гард dev-скриптов прямой записи (минуют confirm-гейт): разрешено ТОЛЬКО при ENV=dev
    (golden rule #10). Иначе SystemExit — скрипт не стартует вне dev. Единый источник, чтобы гард
    нельзя было забыть в новом demo-скрипте (вызывать первой строкой main())."""
    if settings.env != "dev":
        raise SystemExit(
            f"Прямая запись мимо confirm-гейта запрещена вне ENV=dev (сейчас ENV={settings.env!r}) "
            "— golden rule #10."
        )
