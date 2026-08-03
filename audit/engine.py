"""Детерминированный движок аудита аккаунта — БЕЗ сети/SDK, полностью тестируемо.

Принимает УЖЕ прочитанные данные (reports.service.ReportData + опц. impression-share / действия-
конверсии / стратегии ставок / нативный optimization_score — всё duck-typed, БЕЗ импорта reports/ads)
и возвращает AuditResult: health-score (КОД), буква, деньги-под-риском (дедуп ≤ расхода), находки по
семьям. Это ТОЛЬКО диагностика — движок НЕ создаёт proposal и не меняет аккаунт (golden rule #1/#3);
исполнение любого совета идёт отдельной командой через confirm-гейт.

Google-optimization_score показываем как «второе мнение» РЯДОМ (не в нашем score) — числа Google
считает Google. Наш score — только КОД.

⚠️ Пакет audit/ НЕ импортирует ads.mutations / ads.service (инвариант test_audit_engine).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from adcopy.validate import MIN_KEYWORD_COVERAGE, keyword_coverage
from audit.bidscape import (
    FIRST_PAGE,
    MANUAL_BID_STRATEGIES,
    TOP_OF_PAGE,
    opportunities,
    sim_upside,
)
from audit.terms import harvest, waste_ngrams
from audit.terms import norm as t_norm
from audit.terms import tokens as t_tokens
from audit.thresholds import (
    DEFAULT_AUDIT_THRESHOLDS,
    FAMILY_WEIGHT,
    GRADE_BANDS,
    NONMONEY_INTENSITY,
    SCORE_MODEL_EPOCH,
    SEVERITY_MULT,
    grade_for,
)

# Операции, применимые в ОДИН тап (совпадает с bot ADVISE_APPLY_OPS — не денежные). Всё остальное
# (бюджет/ставка) — только прозаический совет без кнопки: бюджет меняется лишь прямой командой (rule #3).
# G12/G11 (2026-07-14) добавлены сюда осознанно: обе НЕ денежные (не в MONEY_OPS), обратимы одним
# кликом и чинятся строго в БЕЗОПАСНУЮ сторону — КМС выключаем, гео сужаем до PRESENCE. Обратное
# (включить КМС / расширить гео) бот одним тапом не предлагает никогда: параметры зашиты константами
# в bot.main._advise_apply_params, а не берутся из находки.
# 3.2в (2026-07-17): remove_negative_keywords — снять КАМПАНИЙНЫЙ минус, блокирующий свой же ключ
# (negative_keyword_conflicts). Неденежная, точечная (текст+тип известны из находки) и обратимая
# (минус возвращается той же командой). Группа/shared-список остаются прозой: схема мутации работает
# на уровне кампании, «сняли хоть где-то» выглядело бы как «готово», не будучи им.
ONE_TAP_OPS = frozenset(
    {
        "pause_campaign",
        "add_negative_keywords",
        "remove_negative_keywords",
        "set_campaign_display_network",
        "set_campaign_geo_target_type",
    }
)


@dataclass
class Finding:
    """Одна находка аудита. at_risk — деньги-под-риском в валюте аккаунта (0 → неденежная).
    spend_segment — ключ дедупа денег (кампания/ключ/«__account__»), чтобы находки на одном сегменте
    не суммировались (крит.C2). suggested_operation ∈ ONE_TAP_OPS → кнопка «применить»; иначе — прозой."""

    check_id: str
    family: str
    severity: str  # warning | info
    at_risk: float = 0.0
    spend_segment: str | None = None
    target_campaign: str | None = None
    suggested_operation: str | None = None
    facts: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)
    # МЕТКА операции, к которой находка ведёт содержательно, но которую бот НЕ исполняет одним тапом
    # (update_bid / update_budget / create_rsa). Нужна, чтобы advisor.outcome смог связать «совет →
    # применённая руками мутация → замер эффекта»: раньше эту роль играл suggested_operation у
    # advisor-детекторов, а у аудита он обязан быть None (деньги = только прямая команда, rule #3).
    # ⚠️ Это НЕ путь исполнения: advice_operation НИКОГДА не попадает в ONE_TAP_OPS и не рисует кнопку
    # (инвариант tests/test_audit_engine.test_advice_operation_never_one_tap).
    advice_operation: str | None = None
    # Экспертное расширение (2026-07-09): вклад в score для находок, где деньги — это УПУЩЕННАЯ выгода
    # (не потраченное), которую нельзя класть в at_risk (сломает инвариант at_risk ≤ total_spend и
    # дедуп). Пример: is_lost_revenue ставит score_intensity = clamp(lost_revenue/total_spend), at_risk=0.
    # None → интенсивность как раньше (at_risk/spend для денежных, иначе NONMONEY_INTENSITY).
    score_intensity: float | None = None

    @property
    def one_tap(self) -> bool:
        return self.suggested_operation in ONE_TAP_OPS


@dataclass
class AuditResult:
    customer_id: str
    currency: str
    score: int | None  # None → нет активности (grade «—»)
    grade: str
    total_spend: float
    at_risk: float  # дедуплицированные деньги-под-риском, ≤ total_spend
    findings: list[Finding]  # ранжированы worst-first
    families: dict  # family → {"count", "at_risk", "penalty"}
    has_activity: bool
    # Нативный Google optimization_score (0..100) + uplift — «второе мнение», НЕ наш score. В конце
    # dataclass (позиционные конструкторы/тесты не ломаются). None → недоступно.
    optimization_score: int | None = None
    optimization_uplift: int | None = None
    # Нативные Google-рекомендации (тип → число активных) — показ, НЕ применяем (см. render).
    google_recommendations: dict = field(default_factory=dict)
    # N1.0a: версия score-модели (хэш весов/порогов/набора проверок) — тренд-дельты сравнимы ТОЛЬКО
    # в пределах одной версии, иначе деплой нового чека читается клиентом как ложный «−10 за неделю».
    score_model_version: str = ""
    # N1.3: best-effort сигналы, которые collect НЕ получил (сбой фетчера → None). None → collect
    # не запускался вовсе (engine-only вызов, напр. /report health) — рендер тогда молчит про семьи.
    data_gaps: list | None = None
    # N1.5: расход есть, а активной PRIMARY-конверсии нет → баннер «сначала почини измерение»
    # НАД score (сам score не подавляем — честность). False и при отсутствии живых conversion_actions.
    measurement_gap: bool = False
    # Heartbeat: customer.status ∈ {SUSPENDED, CANCELED, CLOSED} → баннер-катастрофа НАД score
    # («показы остановлены»). None — аккаунт активен ИЛИ статус не прочитан (fail-soft). НЕ подавляет
    # score (карточка кампаний остаётся полезной, как measurement_gap).
    account_status: str | None = None
    # Экспертное расширение (2026-07-09): ОЦЕНКА упущенной выгоды (недополученная выручка/конверсии из-за
    # потери impression share по бюджету/рангу) в валюте аккаунта. Это НЕ at_risk (не потраченные деньги,
    # а не заработанные) — показывается ОТДЕЛЬНОЙ строкой «💡 Упущено ~X», помеченной как оценка. 0.0 →
    # нет оценки. В конце dataclass (позиционные конструкторы/тесты не ломаются).
    lost_opportunity: float = 0.0
    # Провенанс сбора: какие ctx-сигналы движку ФАКТИЧЕСКИ подали (kwarg is not None). Имена — тот же
    # словарь, что у data_gaps в collect. Это НЕ data_gaps: тот — заявление слоя СБОРА («пробовал
    # прочитать, вернулось None»), а это факт слоя ДВИЖКА («мне подали аргумент»). Пусто → engine-only
    # вызов (/report, дайджест): чеки семей, чей вердикт без ctx недостоверен, гадают по метрикам —
    # штраф честен, а ЯРЛЫК нет. Потребители советов обязаны такие семьи молчать
    # (advisor.from_findings.untrusted_families); флаг report_only, который надо было ПОМНИТЬ, — снят.
    ctx_signals: frozenset[str] = frozenset()
    # Обогащённая ВЫГРУЗКА /audit (bot._run_audit_export → reports.xlsx/sheets): сырьё, которое
    # gather_audit УЖЕ собрал и обычно отбрасывает после build_audit, прицеплено к результату, чтобы
    # книга/Sheets несли ДАННЫЕ (итоги + кампании/ключи/запросы/QS/гео), а не только прозу поста
    # (жалоба владельца «в щитс те же данные, что и в посте»). Это ТОЛЬКО бумага (GR3) — на
    # score/находки/семьи НЕ влияет (движок их не читает). None → engine-only вызов (нет слоя сбора)
    # ИЛИ старый кэш ⇒ выгрузка деградирует к прежним 3 вкладкам. Заполняет их ТОЛЬКО collect.gather_audit.
    report: object = None
    audit_tables: list | None = (
        None  # list[reports.queries.Breakdown]: запросы/QS/гео (не в report.breakdowns)
    )


@dataclass
class _Ctx:
    """Контекст проверок: цель CPA (глоб./пер-кампания) + опц. живые данные (IS/конверсии). duck-typed."""

    target_cpa: float | None = None
    is_rows: list | None = None
    conversion_actions: list | None = None
    # Вариант B: ридер действий-конверсий УПАЛ в collect (сигнал попал в data_gaps), а НЕ «ctx не
    # подавали вовсе» (engine-only /advise,/report,дайджест). При упавшей верификации
    # check_no_conversion_tracking МОЛЧИТ — честность несёт data_gaps («балл может быть завышен»),
    # а не выдуманный уверенный critical. False (engine-only) → метрическая эвристика РОЖДАЕТ находку.
    conversion_read_failed: bool = False
    search_terms: list | None = None
    bidding_by_name: dict = field(default_factory=dict)
    ad_policy: list | None = (
        None  # Heartbeat: строки модерации объявлений (None → сигнал не прочитан)
    )
    adgroup_structure: list | None = (
        None  # D: строки структуры групп (ключи/RSA per group), None → нет данных
    )
    negative_lists: object | None = (
        None  # D: NegativeListsInfo (кол-во/привязка минус-списков), None → нет данных
    )
    keyword_quality: list | None = (
        None  # B: строки Quality Score ключей (+3 компонента), None → нет данных
    )
    geo_waste: list | None = (
        None  # A: строки geographic_view (кампания/регион/метрики), None → нет данных
    )
    schedule: list | None = (
        None  # A: ячейки час×день (кампания/день/час/метрики), None → нет данных
    )
    bid_landscape: list | None = (
        None  # Ф1: ставка ключа + оценки позиций Google + доли верха, None → нет данных
    )
    # Ф1: симуляторы Google (ключ CPC_BID / кампания BUDGET). Пусто — это НОРМА (Google строит их
    # только при достаточных данных), поэтому в data_gaps они не идут: «нет симулятора» ≠ «не прочитано».
    bid_simulations: list | None = None
    budget_simulations: list | None = None
    # Ф2: активные Google-рекомендации с ИХ оценкой эффекта (recommendation.impact). None → не читано.
    recommendations: list | None = None
    # Ф3: работающие RSA целиком (тексты/пины/ad_strength/возраст) + ключи их групп. None → не читано.
    rsa_ads: list | None = None
    # Ф4: ПОЛНЫЙ список активных ключей — вместе с нулевыми (топ-по-расходу выборка их теряет).
    # Он же «что уже покрыто» для майнинга запросов. None → не читано (майнинг молчит, а не врёт).
    keyword_inventory: list | None = None
    # Ревизия волны: список УПЁРСЯ в LIMIT фетчера ⇒ он неполный. Для чеков-«счётчиков» (нули,
    # каннибализация) это недосчёт, а для майнинга — ЛОЖНЫЕ СОВЕТЫ: инвентарь там работает
    # ОТРИЦАТЕЛЬНЫМ фильтром («чего нет — предложи», «что своё — не минусуй»), и обрезанный ключ
    # выглядит как несуществующий. Флаг ставит collect-слой (он знает лимит фетчера).
    keyword_inventory_truncated: bool = False
    # Ф6: токены бренда из профиля клиента (§20). Пусто → brand_nonbrand_mixed молчит: угадывать
    # бренд нечем, а ложно назвать брендовым чужой ключ хуже, чем промолчать.
    brand_terms: set | None = None
    # Ф6: счётчики расширений по кампаниям (ситилинки/уточнения/структ. описания). None → не читано.
    campaign_assets: list | None = None
    # Ф6.2: настройки сетей + тип гео-таргетинга ENABLED-кампаний (+ расход по сети КМС внутри
    # кампании). None → не читано ⇒ G12/G11 молчат: галочку не видно — значит не видно, а не «выкл».
    campaign_settings: list | None = None
    # Ф7: PMax — кампании (деньги, минусы, бренд-исключения, фид, топ поисковых запросов) и группы
    # активов (оценка Google + состав ассетов + сигналы + деньги группы). None → не читано ⇒ вся
    # семья молчит. [] → PMax в аккаунте НЕТ: это не пробел данных, а факт (в data_gaps не идёт).
    pmax_campaigns: list | None = None
    pmax_asset_groups: list | None = None
    # Ф9: расход campaign × устройство (DevicePerformanceRow). None → не читано ⇒ чек молчит.
    device_performance: list | None = None
    # Ф9: минус-слова с текстами (NegativeKeywordsInfo: 3 уровня + привязка shared-списков).
    # None → не читано; конфликт-чек работает в паре с keyword_inventory.
    negative_keywords: object | None = None
    # Ф9: подписки авто-применения рекомендаций Google (RecommendationSubscriptionRow).
    recommendation_subscriptions: list | None = None
    # Ф9: режим ротации объявлений ENABLED-групп (AdRotationRow). None → не читано.
    ad_rotation: list | None = None

    def target_for(self, campaign_name: str) -> float | None:
        """Цель CPA для кампании: пер-кампанийная стратегия (tCPA) побеждает глобальный /target."""
        b = self.bidding_by_name.get(campaign_name)
        if b is not None and getattr(b, "target_cpa", None):
            return float(b.target_cpa)
        return self.target_cpa


# ── Хелперы чтения разбивок (как advisor/rules.py) ───────────────────────────────────
def _breakdown_rows(report, key: str) -> list:
    b = next((b for b in getattr(report, "breakdowns", []) if b.key == key), None)
    return list(b.rows) if b and b.rows else []


def _campaign_rows(report) -> list:
    return _breakdown_rows(report, "campaign")


# ── Проверки (принимают уже прочитанные данные + ctx; ничего не читают) ───────────────
def check_spend_no_conv(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """ENABLED-кампания: расход ≥ порога, клики есть, 0 конверсий — деньги впустую (семья waste)."""
    cur = getattr(report, "currency", "")
    out: list[Finding] = []
    for (name, status), m in _campaign_rows(report):
        if status != "ENABLED":
            continue
        if m.cost >= thr["pause_min_spend"] and m.clicks > 0 and m.conversions == 0:
            out.append(
                Finding(
                    check_id="spend_no_conv",
                    family="waste",
                    severity="warning",
                    at_risk=round(m.cost, 2),
                    spend_segment=name,
                    target_campaign=name,
                    suggested_operation="pause_campaign",
                    facts={
                        "campaign": name,
                        "cost": round(m.cost, 2),
                        "clicks": m.clicks,
                        "currency": cur,
                    },
                    evidence={
                        "cost": round(m.cost, 2),
                        "clicks": m.clicks,
                        "conversions": m.conversions,
                    },
                )
            )
    return out


def check_high_cpa(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """ENABLED-кампания с конверсиями, чей CPA ≥ factor × средний по аккаунту (семья waste, прозой).
    at_risk = перерасход над средней эффективностью аккаунта. totals.cpa == 0 → правило молчит."""
    acct_cpa = float(getattr(report.totals, "cpa", 0.0) or 0.0)
    if acct_cpa <= 0:
        return []
    factor = thr["high_cpa_factor"]
    cur = getattr(report, "currency", "")
    out: list[Finding] = []
    for (name, status), m in _campaign_rows(report):
        if status != "ENABLED" or m.conversions <= 0 or m.cost < thr["min_spend"]:
            continue
        if m.cpa >= factor * acct_cpa:
            excess = max(0.0, m.cost - m.conversions * acct_cpa)
            out.append(
                Finding(
                    check_id="high_cpa",
                    family="waste",
                    severity="warning",
                    at_risk=round(excess, 2),
                    spend_segment=name,
                    target_campaign=name,
                    suggested_operation=None,  # ставка/бюджет — прозой (rule #3), без кнопки
                    advice_operation="update_bid",  # метка для outcome-линковки, НЕ кнопка
                    facts={
                        "campaign": name,
                        "cpa": round(m.cpa, 2),
                        "acct_cpa": round(acct_cpa, 2),
                        "factor": round(m.cpa / acct_cpa, 1),
                        "currency": cur,
                    },
                    # conversions — не косметика: advisor.outcome._baseline_from_evidence снимает по
                    # ним «до» и сравнивает с «после» (без них вердикт всегда neutral).
                    evidence={
                        "cpa": round(m.cpa, 2),
                        "acct_cpa": round(acct_cpa, 2),
                        "cost": round(m.cost, 2),
                        "conversions": m.conversions,
                    },
                )
            )
    return out


def check_kill_rule(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """«3× Kill Rule»: CPA кампании ≥ factor × ЦЕЛЬ. Цель — пер-кампанийная tCPA (ctx.bidding) ИЛИ
    глобальный /target. Нет цели → no-op (не фабрикуем «×цель» на goalless/manual-CPC). pause — one-tap."""
    factor = thr["kill_cpa_factor"]
    cur = getattr(report, "currency", "")
    out: list[Finding] = []
    for (name, status), m in _campaign_rows(report):
        if status != "ENABLED" or m.conversions <= 0 or m.cost < thr["min_spend"]:
            continue
        target = ctx.target_for(name)
        if not target or target <= 0:
            continue
        if m.cpa >= factor * target:
            excess = max(0.0, m.cost - m.conversions * target)
            out.append(
                Finding(
                    check_id="kill_rule",
                    family="waste",
                    severity="critical",  # Ф8: втрое дороже СВОЕЙ цели — не «дороговато», а стоп-кран
                    at_risk=round(excess, 2),
                    spend_segment=name,
                    target_campaign=name,
                    suggested_operation="pause_campaign",
                    facts={
                        "campaign": name,
                        "cpa": round(m.cpa, 2),
                        "target_cpa": round(float(target), 2),
                        "factor": round(m.cpa / target, 1),
                        "currency": cur,
                    },
                    evidence={
                        "cpa": round(m.cpa, 2),
                        "target_cpa": round(float(target), 2),
                        "cost": round(m.cost, 2),
                    },
                )
            )
    return out


# Типы соответствия, которые принимает мутация add_negative_keywords (llm.schemas.MatchType —
# НИЖНИЙ регистр). GAQL отдаёт ENUM-имя ('BROAD'), поэтому нормализуем ЗДЕСЬ, а не в bot-слое: сырой
# 'BROAD' в evidence уронил бы валидацию схемы на one-tap «➖ В минус-слова» — кнопка молча стала бы
# «совет устарел». Незнакомое значение → None (bot подставит свой дефолт), а не мусор в мутацию.
# Дрейф со схемой ловит tests/test_audit_engine.test_evidence_match_type_matches_mutation_schema.
_MATCH_TYPES = frozenset({"broad", "phrase", "exact"})


def _norm_match_type(v: object) -> str | None:
    mt = str(v or "").strip().casefold()
    return mt if mt in _MATCH_TYPES else None


def keyword_spend_segment(campaign: str, keyword: str) -> str:
    """Сегмент денег ключа. ОДИН источник для wasteful_keyword и wasteful_search_term: расход
    поискового запроса ⊂ расход его ключа, поэтому обе находки обязаны попасть в ОДИН сегмент
    (иначе _dedup_at_risk сложит их и задвоит «Под риском» — D5). Нормализуем регистр/пробелы:
    keyword_view и segments.keyword.info.text приходят из разных ресурсов GAQL."""
    return f"kw::{campaign}::{(keyword or '').strip().casefold()}"


def check_wasteful_keyword(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ключ с расходом ≥ порога и кликами, но 0 конверсий → кандидат в минус-слова (семья keywords).
    Топ-N по расходу (анти-спам). one-tap add_negative_keywords (по умолчанию exact — решает bot-слой)."""
    cur = getattr(report, "currency", "")
    out: list[Finding] = []
    for dims, m in _breakdown_rows(report, "keyword"):
        campaign, ad_group, kw_text, match_type = (list(dims) + ["", "", "", ""])[:4]
        if m.cost >= thr["kw_min_spend"] and m.clicks > 0 and m.conversions == 0:
            # Минус ЗЕРКАЛИТ тип соответствия самого ключа (exact-ключ → exact-минус), а не режет
            # шире дефолтным broad: находка про ЭТОТ ключ, а broad-минус выбил бы и его точных
            # соседей в других группах. Тип неизвестен/не из схемы → ключа в evidence нет,
            # bot._advise_apply_params подставит прежний дефолт.
            ev = {"cost": round(m.cost, 2), "clicks": m.clicks, "keyword": kw_text}
            if mt := _norm_match_type(match_type):
                ev["match_type"] = mt
            out.append(
                Finding(
                    check_id="wasteful_keyword",
                    family="keywords",
                    severity="warning",
                    at_risk=round(m.cost, 2),
                    spend_segment=keyword_spend_segment(campaign, kw_text),
                    target_campaign=campaign,
                    suggested_operation="add_negative_keywords",
                    facts={
                        "campaign": campaign,
                        "ad_group": ad_group,
                        "keyword": kw_text,
                        "match_type": match_type,
                        "cost": round(m.cost, 2),
                        "clicks": m.clicks,
                        "currency": cur,
                    },
                    evidence=ev,
                )
            )
    out.sort(key=lambda f: -float(f.evidence.get("cost", 0) or 0))
    return out[: int(thr.get("kw_top_n", 5))]


def check_wasteful_search_term(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Поисковый запрос (search_term_view) с расходом ≥ порога и кликами, но 0 конверсий → кандидат
    в МИНУС-СЛОВА (семья keywords). ОТДЕЛЬНЫЙ check_id от wasteful_keyword (запрос vs существующий ключ),
    но сегмент денег — РОДИТЕЛЬСКОГО ключа. Минус по умолчанию EXACT (режет только этот запрос).

    D5: раньше сегмент был «st::{кампания}::{запрос}» — свой, отдельный от «kw::{кампания}::{ключ}».
    Но расход запроса ⊂ расход его ключа (запрос показался ПО этому ключу), поэтому находки
    «дорогой ключ» и «дорогой запрос того же ключа» СУММИРОВАЛИСЬ в headline «Под риском» — деньги
    считались дважды (80 из 1000 вместо 40; outward-facing денежное утверждение). Наследуя сегмент
    ключа (`segments.keyword.info.text` уже есть в SearchTermRow.keyword), _dedup_at_risk берёт по
    сегменту МАКСИМУМ ⇒ расход ключа поглощает расход своего запроса. Ключ неизвестен (пусто —
    напр. DSA/Performance Max) → сегмент прежний, «st::…»: там родителя нет, двойного счёта тоже.
    """
    rows = ctx.search_terms or []
    cur = getattr(report, "currency", "")
    out: list[Finding] = []
    for st in rows:
        m = getattr(st, "metrics", None)
        if m is None:
            continue
        if m.cost >= thr["kw_min_spend"] and m.clicks > 0 and m.conversions == 0:
            term = getattr(st, "search_term", "")
            campaign = getattr(st, "campaign", "")
            parent_kw = (getattr(st, "keyword", "") or "").strip()
            segment = (
                keyword_spend_segment(campaign, parent_kw)
                if parent_kw
                else f"st::{campaign}::{term}"
            )
            out.append(
                Finding(
                    check_id="wasteful_search_term",
                    family="keywords",
                    severity="warning",
                    at_risk=round(m.cost, 2),
                    spend_segment=segment,
                    target_campaign=campaign,
                    suggested_operation="add_negative_keywords",
                    facts={
                        "campaign": campaign,
                        "search_term": term,
                        "cost": round(m.cost, 2),
                        "clicks": m.clicks,
                        "currency": cur,
                    },
                    # match_type=exact → _advise_apply_params добавит ТОЧНЫЙ минус (не broad).
                    evidence={
                        "keyword": term,
                        "match_type": "exact",
                        "cost": round(m.cost, 2),
                        "clicks": m.clicks,
                    },
                )
            )
    out.sort(key=lambda f: -float(f.evidence.get("cost", 0) or 0))
    return out[: int(thr.get("kw_top_n", 5))]


def check_low_ctr_ad(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Объявление с показами ≥ порога и CTR < factor × средний по аккаунту → освежить RSA (семья rsa,
    неденежная info). Опора — totals.ctr (0 → правило молчит)."""
    acct_ctr = float(getattr(report.totals, "ctr", 0.0) or 0.0)
    if acct_ctr <= 0:
        return []
    cur = getattr(report, "currency", "")
    out: list[Finding] = []
    for dims, m in _breakdown_rows(report, "ad"):
        campaign, ad_group = (list(dims) + ["", ""])[:2]
        if m.impressions >= thr["min_impressions"] and m.ctr < acct_ctr * thr["low_ctr_factor"]:
            out.append(
                Finding(
                    check_id="low_ctr_ad",
                    family="rsa",
                    severity="info",
                    at_risk=0.0,
                    spend_segment=None,
                    target_campaign=campaign,
                    suggested_operation=None,  # create_rsa — не one-tap (нужна курация)
                    advice_operation="create_rsa",  # метка для outcome-линковки, НЕ кнопка
                    facts={
                        "campaign": campaign,
                        "ad_group": ad_group,
                        "ctr": round(m.ctr * 100, 2),
                        "acct_ctr": round(acct_ctr * 100, 2),
                        "currency": cur,
                    },
                    # cost при at_risk=0 — не «деньги под риском» (штраф остаётся неденежным), а
                    # СИЛА СИГНАЛА для ранжирования: advisor._magnitude падает на evidence['cost'],
                    # иначе слабый RSA в кампании на 900 и на 9 стоял бы в очереди одинаково.
                    evidence={
                        "ctr": round(m.ctr, 4),
                        "acct_ctr": round(acct_ctr, 4),
                        "impressions": m.impressions,
                        "cost": round(m.cost, 2),
                    },
                )
            )
    return out


def check_budget_imbalance(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Одна ENABLED-кампания съедает ≥ budget_share_pct расхода, а её эффективность НЕ лучше средней
    (семья budget, неденежная info, прозой — бюджет только прямой командой, rule #3)."""
    total_cost = float(getattr(report.totals, "cost", 0.0) or 0.0)
    if total_cost < thr["pause_min_spend"]:
        return []
    acct_cpa = float(getattr(report.totals, "cpa", 0.0) or 0.0)
    cur = getattr(report, "currency", "")
    out: list[Finding] = []
    for (name, status), m in _campaign_rows(report):
        if status != "ENABLED":
            continue
        share = m.cost / total_cost * 100.0 if total_cost else 0.0
        worse = (m.conversions <= 0) or (acct_cpa > 0 and m.cpa > acct_cpa)
        if share >= thr["budget_share_pct"] and worse:
            out.append(
                Finding(
                    check_id="budget_imbalance",
                    family="budget",
                    severity="info",
                    at_risk=0.0,
                    spend_segment=name,
                    target_campaign=name,
                    suggested_operation=None,
                    advice_operation="update_budget",  # метка для outcome-линковки, НЕ кнопка
                    facts={
                        "campaign": name,
                        "share": round(share),
                        "cpa": round(m.cpa, 2),
                        "currency": cur,
                    },
                    evidence={
                        "share": round(share, 1),
                        "cpa": round(m.cpa, 2),
                        "acct_cpa": round(acct_cpa, 2),
                        "cost": round(m.cost, 2),
                        "conversions": m.conversions,
                    },
                )
            )
    return out


def check_single_campaign(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Единственная ENABLED-кампания с расходом ≥ порога → рассмотреть разделение (семья structure,
    неденежная info, без кнопки — ведёт к созданию новой кампании через отдельный флоу)."""
    enabled = [((n, s), m) for (n, s), m in _campaign_rows(report) if s == "ENABLED"]
    if len(enabled) != 1:
        return []
    (name, _s), m = enabled[0]
    if m.cost < thr["single_campaign_min_spend"]:
        return []
    return [
        Finding(
            check_id="single_campaign",
            family="structure",
            severity="info",
            at_risk=0.0,
            spend_segment=None,
            target_campaign=name,
            suggested_operation=None,
            facts={
                "campaign": name,
                "cost": round(m.cost, 2),
                "currency": getattr(report, "currency", ""),
            },
            evidence={"cost": round(m.cost, 2)},
        )
    ]


def check_no_conversion_tracking(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Отслеживание конверсий. С живыми conversion_action (ctx): НЕТ активных действий при расходе →
    красный «no_conversion_tracking» (весь расход под риском); есть действия, но 0 конверсий при
    кликах → «zero_conversions». Без данных о действиях — метрическая эвристика (расход & 0 конв).

    Ф8: обе находки — critical. Это не «одна проблема из многих»: без измерения конверсий ВСЕ прочие
    числа аудита (CPA, «слив», Smart Bidding) опираются на пустоту — чинить надо это, и первым."""
    t = report.totals
    total_cost = float(getattr(t, "cost", 0.0) or 0.0)
    clicks = float(getattr(t, "clicks", 0) or 0)
    conv = float(getattr(t, "conversions", 0.0) or 0.0)
    cur = getattr(report, "currency", "")
    spends = total_cost >= thr["no_conv_min_spend"]

    cas = ctx.conversion_actions
    if cas is not None:
        enabled = [c for c in cas if getattr(c, "status", "") == "ENABLED"]
        if spends and not enabled:
            return [
                Finding(
                    check_id="no_conversion_tracking",
                    family="conversion_tracking",
                    severity="critical",
                    at_risk=round(total_cost, 2),
                    spend_segment="__account__",
                    facts={
                        "cost": round(total_cost, 2),
                        "clicks": int(clicks),
                        "currency": cur,
                        "reason": "no_action",
                    },
                    evidence={"cost": round(total_cost, 2), "enabled_actions": 0},
                )
            ]
        if enabled and spends and clicks > 0 and conv == 0:
            return [
                Finding(
                    check_id="zero_conversions",
                    family="conversion_tracking",
                    severity="critical",
                    at_risk=round(total_cost, 2),
                    spend_segment="__account__",
                    facts={
                        "cost": round(total_cost, 2),
                        "clicks": int(clicks),
                        "currency": cur,
                        "reason": "zero_conv",
                    },
                    evidence={"cost": round(total_cost, 2), "conversions": conv},
                )
            ]
        return []

    # Fallback без живых действий-конверсий (ctx.conversion_actions is None).
    if ctx.conversion_read_failed:
        # /audit, но ридер действий-конверсий УПАЛ (collect пометил "conversion_actions" в data_gaps).
        # Верификации НЕ было — эмитить уверенный critical «отслеживания нет» с at_risk = весь расход
        # значит соврать: ТА ЖЕ карточка честно пишет «⚠️ Неполные данные: действия-конверсии — балл
        # может быть завышен» (score_affecting_gaps). Уверенная находка + «завышен» = «занижен И
        # завышен» одновременно. Честность несёт data_gaps, а не выдуманный вердикт (Вариант B,
        # решение владельца 2026-07-15). measurement_gap здесь тоже False (только при ЖИВЫХ действиях).
        return []
    # Engine-only (/advise,/report,дайджест): ctx НЕ подавали вовсе (data_gaps=None). Метрическая
    # эвристика — расход+клики+0 конв → находка РОЖДАЕТСЯ (штраф семьи честен: деньги в риске), но
    # ЯРЛЫК недостоверен ⇒ advisor.untrusted_families снимает его на советующих поверхностях
    # (инвариант test_advise_findings_are_subset_of_audit).
    if spends and clicks > 0 and conv == 0:
        return [
            Finding(
                check_id="no_conversion_tracking",
                family="conversion_tracking",
                severity="critical",
                at_risk=round(total_cost, 2),
                spend_segment="__account__",
                facts={"cost": round(total_cost, 2), "clicks": int(clicks), "currency": cur},
                evidence={"cost": round(total_cost, 2), "clicks": int(clicks), "conversions": conv},
            )
        ]
    return []


def check_impression_share(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Impression-share: потеря показов по БЮДЖЕТУ (семья budget) vs по РАНГУ (rank_constrained, семья
    rsa). Классифицируем ТОЛЬКО когда данные полны (Σ долей ≈ 1.0) — иначе proto3-zero «нет данных», молчим.

    Экспертное расширение (C, 2026-07-09): если кампания С доказанными конверсиями теряет показы по
    БЮДЖЕТУ — это УПУЩЕННАЯ выручка (не потраченное!). Оцениваем консервативно:
      lost_conv = conversions × budget_lost_is / search_is  (search_is ≥ пол — иначе экстраполяция рвётся)
      lost_revenue = lost_conv × ценность конверсии (conv_value/conv, иначе target_cpa, иначе acct_cpa)
    Эмитим is_lost_revenue (warning, at_risk=0 — упущенное НЕ кладём в headline, чтобы не сломать
    инвариант at_risk ≤ расход и дедуп; вклад в score через score_intensity = clamp(lost_rev/расход)).
    Без конверсий / ненадёжный search_is → is_budget_constrained (info, config-сигнал, как раньше).
    Rank-lost — is_rank_constrained (info, rsa): ранг чинится качеством/ставкой, не бюджетом (порог 20%)."""
    rows = ctx.is_rows or []
    tol = thr.get("is_data_tolerance", 0.02)
    lost_min = thr.get("is_lost_min", 0.10)
    rank_min = thr.get("is_rank_lost_min", 0.20)
    search_floor = thr.get("is_search_floor", 0.05)
    cur = getattr(report, "currency", "")
    total_spend = float(getattr(report.totals, "cost", 0.0) or 0.0)
    acct_cpa = float(getattr(report.totals, "cpa", 0.0) or 0.0)
    m_by_name = {name: m for (name, _st), m in _campaign_rows(report)}
    out: list[Finding] = []
    for r in rows:
        if getattr(r, "channel_type", "SEARCH") not in ("SEARCH", "SHOPPING"):
            continue
        b = float(getattr(r, "budget_lost_is", 0.0) or 0.0)
        rk = float(getattr(r, "rank_lost_is", 0.0) or 0.0)
        s = float(getattr(r, "search_is", 0.0) or 0.0)
        if not (1.0 - tol <= s + b + rk <= 1.0 + tol):
            continue  # нет данных — не выдумываем
        name = getattr(r, "campaign_name", "")
        if b >= lost_min and b >= rk:
            m = m_by_name.get(name)
            conv = float(getattr(m, "conversions", 0.0) or 0.0) if m else 0.0
            if conv > 0 and s >= search_floor:  # доказанная способность + надёжный search_is
                lost_conv = conv * b / s
                conv_value = float(getattr(m, "conv_value", 0.0) or 0.0)
                value_per_conv = (
                    conv_value / conv
                    if conv_value > 0
                    else (ctx.target_for(name) or acct_cpa or 0.0)
                )
                lost_rev = round(lost_conv * value_per_conv, 2)
                si = min(1.0, lost_rev / total_spend) if total_spend > 0 else 0.0
                out.append(
                    Finding(
                        check_id="is_lost_revenue",
                        family="budget",
                        severity="warning",
                        at_risk=0.0,  # упущенное ≠ потраченное → в score через score_intensity
                        spend_segment=name,
                        target_campaign=name,
                        suggested_operation=None,  # бюджет — прозой (rule #3)
                        facts={
                            "campaign": name,
                            "budget_lost": round(b * 100),
                            "lost_conv": round(lost_conv, 1),
                            "lost_revenue": lost_rev,
                            "currency": cur,
                        },
                        evidence={
                            "budget_lost_is": round(b, 4),
                            "search_is": round(s, 4),
                            "conversions": round(conv, 2),
                        },
                        score_intensity=si,
                    )
                )
            else:
                out.append(
                    Finding(
                        check_id="is_budget_constrained",
                        family="budget",
                        severity="info",
                        at_risk=0.0,
                        spend_segment=name,
                        target_campaign=name,
                        suggested_operation=None,  # бюджет — прозой (rule #3)
                        facts={"campaign": name, "budget_lost": round(b * 100), "currency": cur},
                        evidence={"budget_lost_is": round(b, 4), "rank_lost_is": round(rk, 4)},
                    )
                )
        elif rk >= rank_min:
            out.append(
                Finding(
                    check_id="is_rank_constrained",
                    family="rsa",
                    severity="info",
                    at_risk=0.0,
                    spend_segment=name,
                    target_campaign=name,
                    suggested_operation=None,
                    facts={"campaign": name, "rank_lost": round(rk * 100), "currency": cur},
                    evidence={"rank_lost_is": round(rk, 4)},
                )
            )
    return out


# Стратегии БЕЗ конверсионного автобиддинга (N1.2). Whitelist-детект: неизвестная/новая стратегия →
# НЕ флагуем (fail-safe, не гадаем). Знание-детектор из README FGRibreau/mcp-google-ads (MIT);
# сам сервер (31 write-тул) отвергнут — берём только правило «BROAD без Smart Bidding = риск».
_NON_SMART_BIDDING = frozenset(
    {"MANUAL_CPC", "MANUAL_CPM", "MANUAL_CPV", "TARGET_SPEND", "PERCENT_CPC"}
)


def _is_legacy_bmm(kw_text: str) -> bool:
    """Legacy Broad Match Modified: тип соответствия свёрнут Google в 2021, но САМИ КЛЮЧИ остались
    и опознаются по «+» перед словами («+купить +обувь»). Ведут себя как ФРАЗОВЫЕ, а API отдаёт им
    match_type=BROAD — то есть это не «неуправляемое широкое», флажить их нельзя (ложный позитив).

    ⚠️ Приписка claude-ads к G17 утверждает обратное: будто Google срезал «+» при миграции, BMM стал
    неотличим от BROAD, и потому не надо флажить ВЕСЬ «BROAD + Manual CPC». Посылка неверна — «+» в
    тексте ключа сохраняется. Поэтому BMM отсекаем ТОЧНО, по тексту, а настоящий BROAD без Smart
    Bidding продолжаем флажить: слепо следуя приписке, мы бы молча погасили реальный слив."""
    return any(t.startswith("+") for t in kw_text.split())


def check_broad_unmanaged(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Широкое соответствие БЕЗ управления (семья bidding, N1.2 / claude-ads G17). Два случая:
      • BROAD в кампании со стратегией из `_NON_SMART_BIDDING` — конверсионного сигнала нет, ставку
        ведёт человек ⇒ широкий трафик ничем не отсекается;
      • BROAD в кампании СО Smart Bidding, но у кампании НЕТ НИ ОДНОГО минус-слова (ни списка, ни
        прямых) — алгоритму нечем ограничить выборку. Вторая половина G17: раньше не ловилась вовсе.
    Legacy BMM (текст с «+», см. `_is_legacy_bmm`) НЕ считаем — это фразовое поведение, не широкое.

    ТОЛЬКО прозой — сужение соответствия/минус-слова требуют курации, не one-tap (rule #3/#6).
    Fail-safe: нет данных о стратегии (ctx.bidding_by_name пуст / кампания не найдена) → молчим
    совсем; нет карты минусов (ctx.negative_lists=None или стаб без поля) → молчит ТОЛЬКО smart-ветка.
    НЕДЕНЕЖНАЯ (ревью 2026-07-08): это риск КОНФИГУРАЦИИ, а не измеренный слив — BROAD-расход
    может конвертить; деньги-под-риском считают проверки слива (wasteful_keyword/spend_no_conv),
    иначе headline задваивал бы тот же ключ и инфлировался конвертящим расходом. Полный
    BROAD-расход остаётся в facts/evidence для прозы."""
    if not ctx.bidding_by_name:
        return []
    status_by_name = {name: status for (name, status), _m in _campaign_rows(report)}
    cur = getattr(report, "currency", "")
    # None → карты покрытия нет (сигнал не прочитан / duck-стаб без поля) ⇒ smart-ветка молчит.
    covered = (
        getattr(ctx.negative_lists, "campaigns_with_negatives", None)
        if ctx.negative_lists is not None
        else None
    )
    agg: dict[str, dict] = {}
    for dims, m in _breakdown_rows(report, "keyword"):
        campaign, _ad_group, kw_text, match_type = (list(dims) + ["", "", "", ""])[:4]
        if match_type != "BROAD" or status_by_name.get(campaign) != "ENABLED":
            continue
        if _is_legacy_bmm(kw_text):  # BMM ≠ широкое соответствие (см. _is_legacy_bmm)
            continue
        b = ctx.bidding_by_name.get(campaign)
        if b is None:
            continue
        strategy = getattr(b, "strategy_type", "")
        if strategy in _NON_SMART_BIDDING:
            reason = "no_smart_bidding"
        elif covered is not None and campaign not in covered:
            reason = "no_negatives"  # Smart Bidding есть, а отсекать мусор нечем
        else:
            continue
        a = agg.setdefault(campaign, {"cost": 0.0, "n": 0, "strategy": strategy, "reason": reason})
        a["cost"] += float(m.cost)
        a["n"] += 1
    out: list[Finding] = []
    for campaign, a in agg.items():
        if a["cost"] < thr["broad_min_spend"]:
            continue
        out.append(
            Finding(
                check_id="broad_unmanaged",
                family="bidding",
                severity="warning",
                at_risk=0.0,  # риск конфигурации, не измеренный слив (см. docstring)
                spend_segment=None,
                target_campaign=campaign,
                suggested_operation=None,  # курация соответствий/минусов — НЕ one-tap
                facts={
                    "campaign": campaign,
                    "reason": a["reason"],
                    "strategy_type": a["strategy"],
                    "cost": round(a["cost"], 2),
                    "kw_count": a["n"],
                    "currency": cur,
                },
                evidence={
                    "cost": round(a["cost"], 2),
                    "kw_count": a["n"],
                    "strategy_type": a["strategy"],
                    "reason": a["reason"],
                },
            )
        )
    return out


def check_duplicate_conversions(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Консервативный детект двойного счёта конверсий (N1.5): ≥2 ENABLED PRIMARY-действий ОДНОЙ
    содержательной категории пишут в столбец «Конверсии» → Smart Bidding учится на задвоенных
    данных. Только прозой, без кнопки. Без живых conversion_actions/категорий → молчим."""
    cas = ctx.conversion_actions
    if not cas:
        return []
    by_cat: dict[str, int] = {}
    for c in cas:
        if getattr(c, "status", "") != "ENABLED" or not getattr(c, "primary_for_goal", False):
            continue
        cat = str(getattr(c, "category", "") or "")
        if cat in ("", "DEFAULT", "UNSPECIFIED", "UNKNOWN"):
            continue  # категория-заглушка — не повод для флага (консервативно)
        by_cat[cat] = by_cat.get(cat, 0) + 1
    out: list[Finding] = []
    for cat, n in sorted(by_cat.items()):
        if n >= 2:
            out.append(
                Finding(
                    check_id="duplicate_conversions",
                    family="conversion_tracking",
                    severity="info",
                    at_risk=0.0,
                    spend_segment=None,
                    suggested_operation=None,
                    facts={"category": cat, "count": n},
                    evidence={"category": cat, "count": n},
                )
            )
    return out


# Статусы модерации объявления, при которых оно НЕ показывается / показывается ограниченно
# (Heartbeat). APPROVED и служебные UNKNOWN/UNSPECIFIED не флагуем.
_IMPAIRED_APPROVAL = frozenset({"DISAPPROVED", "APPROVED_LIMITED", "AREA_OF_INTEREST_ONLY"})


def check_ads_disapproved(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Heartbeat: ENABLED-объявления с impaired approval_status (дизапрув/ограничение) — молча не
    показываются. Агрегат ПО КАМПАНИИ (per-ad затопил бы _AUDIT_MAX_FINDINGS). НЕденежная (at_risk=0):
    это тихая потеря БЕЗ расхода; чинится правкой объявления/посадочной, НЕ мутацией бота (не one-tap).
    Нет данных о модерации (ctx.ad_policy is None) → молчим (fail-safe, как check_broad_unmanaged)."""
    if ctx.ad_policy is None:
        return []
    agg: dict[str, dict] = {}
    for r in ctx.ad_policy:
        status = str(getattr(r, "approval_status", "") or "")
        if status not in _IMPAIRED_APPROVAL:
            continue
        campaign = str(getattr(r, "campaign", "") or "")
        a = agg.setdefault(campaign, {"n": 0, "disapproved": 0, "sample": ""})
        a["n"] += 1
        if status == "DISAPPROVED":
            a["disapproved"] += 1
        if not a["sample"]:
            a["sample"] = str(getattr(r, "ad_group", "") or "")
    out: list[Finding] = []
    for campaign, a in sorted(agg.items()):
        out.append(
            Finding(
                check_id="ads_disapproved",
                family="delivery",
                severity="warning",
                at_risk=0.0,
                spend_segment=None,
                target_campaign=campaign,
                suggested_operation=None,  # правка объявления/посадочной — НЕ мутация бота
                facts={
                    "campaign": campaign,
                    "count": a["n"],
                    "disapproved": a["disapproved"],
                    "ad_group": a["sample"],
                },
                evidence={"count": a["n"], "disapproved": a["disapproved"]},
            )
        )
    return out


def check_zero_impressions(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Heartbeat: ENABLED-кампания с 0 показов за окно — включена, но молчит (сломанные объявления/
    узкий таргет/пустые группы). Тихая потеря БЕЗ расхода. НЕденежная (at_risk=0), прозой, не one-tap.
    Гард активностью аккаунта (totals.impressions>0) — не флагать целиком мёртвый/Draft-аккаунт.
    Новый GAQL не нужен — из campaign-разбивки отчёта."""
    acct_impr = float(getattr(report.totals, "impressions", 0) or 0)
    if acct_impr <= 0:
        return []
    out: list[Finding] = []
    for (name, status), m in _campaign_rows(report):
        if status == "ENABLED" and float(getattr(m, "impressions", 0) or 0) == 0:
            out.append(
                Finding(
                    check_id="zero_impressions",
                    family="delivery",
                    severity="warning",
                    at_risk=0.0,
                    spend_segment=None,
                    target_campaign=name,
                    suggested_operation=None,
                    facts={"campaign": name},
                    evidence={"impressions": 0},
                )
            )
    return out


def check_adgroup_bloat(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """D (claude-ads G03): активные группы с >N ключей — «свалка» тем размывает релевантность и Quality
    Score. Один агрегат (сколько групп + худшая), не спам per-группа. Неденежная (структура), прозой,
    не one-tap. Нет данных структуры (ctx.adgroup_structure=None) → молчим (fail-safe).

    G03 accuracy note (Ф0, 2026-07-13): что именно считать ключом — решает фетчер
    (`reports.queries.fetch_adgroup_structure`): только ENABLED-группы, только ключи С ПОКАЗАМИ за
    период, дедуп по тексту (BROAD+PHRASE одного ключа = один). Спящий хвост и три типа соответствия
    одного ключа раздували счётчик → ложные «свалки»."""
    rows = ctx.adgroup_structure
    if not rows:
        return []
    cap = int(thr.get("kw_per_group_max", 20))
    bloated = [r for r in rows if int(getattr(r, "kw_count", 0) or 0) > cap]
    if not bloated:
        return []
    worst = max(bloated, key=lambda r: int(getattr(r, "kw_count", 0) or 0))
    return [
        Finding(
            check_id="adgroup_bloat",
            family="structure",
            severity="info",
            at_risk=0.0,
            spend_segment=None,
            target_campaign=getattr(worst, "campaign", None),
            suggested_operation=None,
            facts={
                "count": len(bloated),
                "cap": cap,
                "worst_group": getattr(worst, "ad_group", ""),
                "worst_kw": int(getattr(worst, "kw_count", 0) or 0),
            },
            evidence={"bloated_groups": len(bloated)},
        )
    ]


def check_rsa_thin(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """D (copilot/North Country): активные группы с <N включённых RSA — мало вариантов для показа/
    оптимизации. Один агрегат (сколько групп + худшая). Неденежная (rsa), прозой. Нет данных → молчим."""
    rows = ctx.adgroup_structure
    if not rows:
        return []
    need = int(thr.get("rsa_min_per_group", 2))
    thin = [r for r in rows if int(getattr(r, "rsa_count", 0) or 0) < need]
    if not thin:
        return []
    worst = min(thin, key=lambda r: int(getattr(r, "rsa_count", 0) or 0))
    return [
        Finding(
            check_id="rsa_thin",
            family="rsa",
            severity="info",
            at_risk=0.0,
            spend_segment=None,
            target_campaign=getattr(worst, "campaign", None),
            suggested_operation=None,
            facts={
                "count": len(thin),
                "need": need,
                "worst_group": getattr(worst, "ad_group", ""),
                "worst_rsa": int(getattr(worst, "rsa_count", 0) or 0),
            },
            evidence={"thin_groups": len(thin)},
        )
    ]


# ── Ф3: тексты объявлений (RSA) ──────────────────────────────────────────────────────
def _serving_rsa(ctx: _Ctx) -> list:
    """RSA, которые РЕАЛЬНО показывались за период. Объявление без показов судить не за что —
    флажить его значило бы шуметь на черновиках и свежих группах (правило анти-ложных, Ф0)."""
    return [
        a
        for a in (ctx.rsa_ads or [])
        if int(getattr(getattr(a, "metrics", None), "impressions", 0) or 0) > 0
    ]


def _rsa_cost(ad) -> float:
    return float(getattr(getattr(ad, "metrics", None), "cost", 0.0) or 0.0)


def _rsa_where(ad) -> tuple[str, str]:
    return getattr(ad, "campaign", "") or "", getattr(ad, "ad_group", "") or ""


def check_rsa_keyword_coverage(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф3 (claude-ads G35/G-KW2 — ПРЯМОЙ запрос заказчика «используются ли ключи в объявлениях»):
    слова, за которые платим, не встречаются в заголовках объявления. Бьёт по релевантности → QS →
    цене клика. Покрытие считает КОД (`adcopy.validate.keyword_coverage`) — ТОТ ЖЕ, что гейт
    генерации: аудит не может требовать одного, а генератор выдавать другое.

    Судим только показывавшиеся объявления с известными ключами группы. Один агрегат + худшее
    (по расходу) объявление; деньги в evidence (сила сигнала для ранжирования). Нет данных → молчим."""
    ads = [
        a
        for a in _serving_rsa(ctx)
        if getattr(a, "keywords", None) and getattr(a, "headlines", None)
    ]
    if not ads:
        return []
    floor = float(thr.get("rsa_kw_coverage_min", MIN_KEYWORD_COVERAGE))
    low = [
        (a, keyword_coverage(list(a.headlines), list(a.keywords)))
        for a in ads
        if keyword_coverage(list(a.headlines), list(a.keywords)) < floor
    ]
    if not low:
        return []
    worst_ad, worst_cov = max(low, key=lambda p: (_rsa_cost(p[0]), -p[1]))
    campaign, ad_group = _rsa_where(worst_ad)
    # Ключи, которых нет НИ В ОДНОМ заголовке, — их и подставлять в новые тексты (3 примера).
    missing = [
        kw
        for kw in list(worst_ad.keywords)[:20]
        if keyword_coverage(list(worst_ad.headlines), [kw]) <= 0.0
    ][:3]
    return [
        Finding(
            check_id="rsa_keyword_coverage_low",
            family="rsa",
            severity="warning",
            at_risk=0.0,
            spend_segment=None,
            target_campaign=campaign,
            suggested_operation=None,  # тексты пишет генератор → курация, не одна кнопка
            advice_operation="create_rsa",
            facts={
                "count": len(low),
                "min_pct": int(floor * 100),
                "campaign": campaign,
                "ad_group": ad_group,
                "worst_pct": int(worst_cov * 100),
                "missing": missing,
                "currency": getattr(report, "currency", ""),
            },
            evidence={
                "cost": round(sum(_rsa_cost(a) for a, _ in low), 2),
                "worst_cost": round(_rsa_cost(worst_ad), 2),
                "ads": len(ads),
            },
        )
    ]


def check_rsa_copy_thin(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф3 (claude-ads G27/G28): в объявлении мало заголовков (<8 при максимуме 15) или описаний
    (<3 при 4) — Google нечего комбинировать, ротация и ad_strength страдают. Два агрегата."""
    ads = _serving_rsa(ctx)
    if not ads:
        return []
    out: list[Finding] = []
    need_h = int(thr.get("rsa_min_headlines", 8))
    need_d = int(thr.get("rsa_min_descriptions", 3))
    thin_h = [a for a in ads if len(getattr(a, "headlines", []) or []) < need_h]
    thin_d = [a for a in ads if len(getattr(a, "descriptions", []) or []) < need_d]
    if thin_h:
        worst = max(thin_h, key=_rsa_cost)
        campaign, ad_group = _rsa_where(worst)
        out.append(
            Finding(
                check_id="rsa_headlines_thin",
                family="rsa",
                severity="info",
                at_risk=0.0,
                spend_segment=None,
                target_campaign=campaign,
                suggested_operation=None,
                advice_operation="create_rsa",
                facts={
                    "count": len(thin_h),
                    "need": need_h,
                    "campaign": campaign,
                    "ad_group": ad_group,
                    "worst_n": len(getattr(worst, "headlines", []) or []),
                },
                evidence={"cost": round(sum(_rsa_cost(a) for a in thin_h), 2)},
            )
        )
    if thin_d:
        worst = max(thin_d, key=_rsa_cost)
        campaign, ad_group = _rsa_where(worst)
        out.append(
            Finding(
                check_id="rsa_descriptions_thin",
                family="rsa",
                severity="info",
                at_risk=0.0,
                spend_segment=None,
                target_campaign=campaign,
                suggested_operation=None,
                advice_operation="create_rsa",
                facts={
                    "count": len(thin_d),
                    "need": need_d,
                    "campaign": campaign,
                    "ad_group": ad_group,
                    "worst_n": len(getattr(worst, "descriptions", []) or []),
                },
                evidence={"cost": round(sum(_rsa_cost(a) for a in thin_d), 2)},
            )
        )
    return out


def check_rsa_ad_strength(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф3 (claude-ads G29): собственная оценка Google — `ad_strength = POOR`. AVERAGE НЕ флажим:
    это половина живых объявлений, шум. Пустая строка = поле не прочитано ⇒ молчим (gr8)."""
    poor = [a for a in _serving_rsa(ctx) if getattr(a, "ad_strength", "") == "POOR"]
    if not poor:
        return []
    worst = max(poor, key=_rsa_cost)
    campaign, ad_group = _rsa_where(worst)
    return [
        Finding(
            check_id="rsa_ad_strength_poor",
            family="rsa",
            severity="info",
            at_risk=0.0,
            spend_segment=None,
            target_campaign=campaign,
            suggested_operation=None,
            advice_operation="create_rsa",
            facts={
                "count": len(poor),
                "campaign": campaign,
                "ad_group": ad_group,
                "currency": getattr(report, "currency", ""),
            },
            evidence={"cost": round(sum(_rsa_cost(a) for a in poor), 2)},
        )
    ]


def check_rsa_overpinned(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф3 (claude-ads G30): закреплены ВСЕ ТРИ позиции заголовка — ротация мертва, Google не может
    подбирать комбинации под запрос. Флажим только полный пин 1+2+3: пин одной позиции (бренд/
    юридическая обязаловка) — нормальная практика, флажить её значило бы врать."""
    over = [
        a
        for a in _serving_rsa(ctx)
        if {"HEADLINE_1", "HEADLINE_2", "HEADLINE_3"}
        <= set(getattr(a, "pinned_headline_positions", ()) or ())
    ]
    if not over:
        return []
    worst = max(over, key=_rsa_cost)
    campaign, ad_group = _rsa_where(worst)
    return [
        Finding(
            check_id="rsa_overpinned",
            family="rsa",
            severity="info",
            at_risk=0.0,
            spend_segment=None,
            target_campaign=campaign,
            suggested_operation=None,
            advice_operation="create_rsa",
            facts={
                "count": len(over),
                "campaign": campaign,
                "ad_group": ad_group,
                "pinned": int(getattr(worst, "pinned_headlines", 0) or 0),
            },
            evidence={"cost": round(sum(_rsa_cost(a) for a in over), 2)},
        )
    ]


def check_rsa_stale(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф3 (claude-ads G-AD1): в группе НЕТ ни одного объявления моложе N дней — тексты не обновляли,
    выгорание/усталость аудитории. Считаем по САМОМУ СВЕЖЕМУ объявлению группы. age_days=-1 (дата не
    прочитана — сервер отверг поле) → группа не судится вовсе: нет данных ≠ «старая» (gr8)."""
    days = int(thr.get("rsa_stale_days", 90))
    fresh_by_group: dict[str, int] = {}
    meta: dict[str, tuple[str, str, float]] = {}
    for a in _serving_rsa(ctx):
        age = int(getattr(a, "age_days", -1) or -1)
        if age < 0:
            continue
        gid = str(getattr(a, "ad_group_id", "") or getattr(a, "ad_group", ""))
        fresh_by_group[gid] = min(fresh_by_group.get(gid, age), age)
        campaign, ad_group = _rsa_where(a)
        prev_cost = meta.get(gid, ("", "", 0.0))[2]
        meta[gid] = (campaign, ad_group, prev_cost + _rsa_cost(a))
    stale = {gid: age for gid, age in fresh_by_group.items() if age >= days}
    if not stale:
        return []
    worst_gid = max(stale, key=lambda g: meta.get(g, ("", "", 0.0))[2])
    campaign, ad_group, _ = meta.get(worst_gid, ("", "", 0.0))
    return [
        Finding(
            check_id="rsa_stale",
            family="rsa",
            severity="info",
            at_risk=0.0,
            spend_segment=None,
            target_campaign=campaign,
            suggested_operation=None,
            advice_operation="create_rsa",
            facts={
                "count": len(stale),
                "days": days,
                "campaign": campaign,
                "ad_group": ad_group,
                "worst_days": max(stale.values()),
            },
            evidence={"cost": round(sum(meta.get(g, ("", "", 0.0))[2] for g in stale), 2)},
        )
    ]


def check_no_negative_list(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """D (claude-ads G14+G15): в аккаунте с реальным трафиком НЕТ минус-слов ВООБЩЕ — ни shared-списка
    (NEGATIVE_KEYWORDS), ни минусов прямо на кампаниях. Базовая гигиена не настроена, нецелевые
    запросы не отсекаются. Неденежная (keywords), прозой. Нет данных (ctx.negative_lists=None) →
    молчим (fail-safe).

    G14/G15 accuracy note (Ф0, 2026-07-13): раньше смотрели ТОЛЬКО shared_set — аккаунт с минусами
    на уровне кампаний (нормальная практика, когда кампания одна) получал ложное «гигиены нет»."""
    nl = ctx.negative_lists
    if nl is None:
        return []
    lists = int(getattr(nl, "count", 0) or 0)
    direct = int(getattr(nl, "campaign_level_count", 0) or 0)
    if lists > 0 or direct > 0:
        return []
    if float(getattr(report.totals, "clicks", 0) or 0) <= 0:  # нет трафика → минусы и не нужны
        return []
    return [
        Finding(
            check_id="no_negative_list",
            family="keywords",
            severity="info",
            at_risk=0.0,
            spend_segment=None,
            target_campaign=None,
            suggested_operation=None,
            facts={"lists": 0, "campaign_level": 0},
            evidence={"negative_lists": 0, "campaign_level_negatives": 0},
        )
    ]


def _kw_texts(ctx: _Ctx) -> list[str]:
    return [getattr(k, "keyword", "") or "" for k in (ctx.keyword_inventory or [])]


def check_keyword_harvest(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф4 (claude-ads G-KW3, «сбор урожая»): запросы, которые ПРИНЕСЛИ КОНВЕРСИИ, но ключа под них
    нет — деньги уже заработаны вслепую, через широкое соответствие. Добавить их точным ключом =
    управляемая ставка и свой текст объявления. Обратный ход к /searchterms («в минус»), которого
    в боте не было вовсе.

    🔒 На балл НЕ влияет (score_intensity=0.0): это невыбранная возможность, а не дефект аккаунта —
    штрафовать за неё значило бы ругать клиента за то, чего у него нет. Нужны ОБА сигнала (запросы
    и список ключей): без ключей мы не знаем, что уже покрыто, и предложили бы дубли.

    Инвентарь УСЕЧЁН лимитом фетчера (огромный аккаунт) → тоже молчим: обрезанный ключ неотличим от
    отсутствующего, и мы предложили бы добавить то, что у клиента уже есть."""
    if not ctx.search_terms or ctx.keyword_inventory is None or ctx.keyword_inventory_truncated:
        return []
    items = harvest(
        ctx.search_terms,
        _kw_texts(ctx),
        min_conv=float(thr.get("harvest_min_conv", 1.0)),
        top_n=int(thr.get("harvest_top_n", 5)),
    )
    if not items:
        return []
    best = items[0]
    return [
        Finding(
            check_id="keyword_harvest",
            family="keywords",
            severity="info",
            at_risk=0.0,
            spend_segment=None,
            target_campaign=best.campaign,
            suggested_operation=None,  # ключ ставят в конкретную группу со ставкой → не один тап
            advice_operation="add_keywords",
            score_intensity=0.0,  # возможность, не дефект — балл не трогаем
            facts={
                "count": len(items),
                "campaign": best.campaign,
                "ad_group": best.ad_group,
                "terms": [
                    {"term": i.term, "conversions": i.conversions, "cost": i.cost} for i in items
                ],
                "conversions": round(sum(i.conversions for i in items), 2),
                "currency": getattr(report, "currency", ""),
            },
            evidence={
                "cost": round(sum(i.cost for i in items), 2),
                "keywords": [i.term for i in items],
                "match_type": "exact",
            },
        )
    ]


def check_ngram_waste(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф4 (claude-ads G16, системный слой): не один дорогой запрос, а СЛОВО, которое стабильно жжёт
    бюджет во многих запросах и не конвертит нигде («бесплатно», «вакансии», «своими руками»).
    Считает КОД (audit.terms.waste_ngrams), не модель; свои ключи неприкосновенны (гард в terms).

    at_risk=0 НАМЕРЕННО: этот расход уже посчитан в wasteful_search_term/wasteful_keyword — положив
    его сюда, мы бы задвоили «Под риском» (та же ошибка, что чинила эпоха 4). Деньги показываем в
    фактах, а в балл идём как неденежный warning.

    Не one-tap (в отличие от точного минуса на один запрос): n-грамма пересекает кампании, а
    add_negative_keywords бьёт по ОДНОЙ — кнопка применила бы половину, а выглядело бы как «готово».

    Усечённый инвентарь → молчим: гард «свои ключи неприкосновенны» держится на ПОЛНОМ списке, а на
    обрезанном мы посоветовали бы заминусовать слово, входящее в собственный ключ клиента (применение
    такого совета — прямой убыток, а не недосчёт)."""
    if not ctx.search_terms or ctx.keyword_inventory is None or ctx.keyword_inventory_truncated:
        return []
    grams = waste_ngrams(
        ctx.search_terms,
        _kw_texts(ctx),
        min_cost=float(thr.get("ngram_min_cost", 10.0)),
        min_terms=int(thr.get("ngram_min_terms", 2)),
        top_n=int(thr.get("ngram_top_n", 5)),
    )
    if not grams:
        return []
    return [
        Finding(
            check_id="ngram_waste",
            family="keywords",
            severity="warning",
            at_risk=0.0,  # расход уже учтён денежными чеками запросов/ключей — не задваиваем
            spend_segment=None,
            target_campaign=None,
            suggested_operation=None,  # минус-слово по n-грамме — прозой, см. докстринг
            advice_operation="add_negative_keywords",
            facts={
                "count": len(grams),
                "cost": round(sum(g.cost for g in grams), 2),
                "words": [{"text": g.text, "cost": g.cost, "terms": g.terms} for g in grams],
                "currency": getattr(report, "currency", ""),
            },
            evidence={
                "cost": round(sum(g.cost for g in grams), 2),
                "clicks": sum(g.clicks for g in grams),
                "ngrams": [g.text for g in grams],
            },
        )
    ]


def check_keyword_cannibalization(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф4: ОДИН И ТОТ ЖЕ ключ с ОДНИМ типом соответствия живёт в разных группах — они конкурируют
    друг с другом на одном аукционе: Google выберет одну, вторая копит историю впустую, а ставки
    и тексты у них разные.

    Анти-ложные: судим только по РЕАЛЬНО показывавшимся дублям (impressions > 0 минимум в двух
    группах) — дубль-«спящий» никому не мешает; разные типы соответствия (broad + exact одного
    текста) — НОРМАЛЬНАЯ структура, не каннибализация."""
    inv = ctx.keyword_inventory
    # G/GR8: обрезанный инвентарь (LIMIT без ORDER BY) — произвольное подмножество; дубль-ключ мог
    # оказаться по разные стороны отсечки → молчим, иначе врём про полноту (как harvest/ngram).
    if not inv or ctx.keyword_inventory_truncated:
        return []
    groups: dict[tuple[str, str], list] = {}
    for k in inv:
        if int(getattr(getattr(k, "metrics", None), "impressions", 0) or 0) <= 0:
            continue
        key = (t_norm(getattr(k, "keyword", "") or ""), getattr(k, "match_type", "") or "")
        if not key[0]:
            continue
        groups.setdefault(key, []).append(k)
    dupes = {
        key: rows
        for key, rows in groups.items()
        if len({(getattr(r, "campaign", ""), getattr(r, "ad_group", "")) for r in rows}) > 1
    }
    if not dupes:
        return []

    def _cost_of(rows) -> float:
        return sum(float(getattr(getattr(r, "metrics", None), "cost", 0.0) or 0.0) for r in rows)

    worst_key = max(dupes, key=lambda k: _cost_of(dupes[k]))
    worst = dupes[worst_key]
    places = sorted({f"{getattr(r, 'campaign', '')} / {getattr(r, 'ad_group', '')}" for r in worst})
    return [
        Finding(
            check_id="keyword_cannibalization",
            family="keywords",
            severity="info",
            at_risk=0.0,
            spend_segment=None,
            target_campaign=getattr(worst[0], "campaign", "") or None,
            suggested_operation=None,
            advice_operation="remove_keywords",  # лишний дубль убирают руками — какой именно, решает человек
            facts={
                "count": len(dupes),
                "keyword": worst_key[0],
                "match_type": worst_key[1],
                "places": places[:4],
                "cost": round(_cost_of(worst), 2),
                "currency": getattr(report, "currency", ""),
            },
            evidence={
                "cost": round(sum(_cost_of(r) for r in dupes.values()), 2),
                "groups": len(worst),
            },
        )
    ]


def check_zero_impression_keywords(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф4 (claude-ads G-KW1): большая часть активных ключей за период не показалась НИ РАЗУ — они
    слишком узкие, задавлены рангом или дублируют друг друга. Такой ключ не стоит денег, но врёт
    про структуру: аккаунт выглядит проработанным, а работают полтора ключа.

    Порог двойной (доля И количество): на маленьком аккаунте 5 из 8 нулевых — не сигнал, а норма
    молодой кампании. Фетчер тут ОТДЕЛЬНЫЙ (fetch_keyword_inventory) именно потому, что топ-по-
    расходу выборка нулевые ключи выбрасывает первыми."""
    inv = ctx.keyword_inventory
    # G/GR8 (критично здесь): чек считает ДОЛЮ zero/total. Обрезанный инвентарь (LIMIT без ORDER BY)
    # = произвольное подмножество → доля бессмысленна и даст ложную находку. Обрезано ⇒ молчим.
    if not inv or ctx.keyword_inventory_truncated:
        return []
    total = len(inv)
    zero = [k for k in inv if int(getattr(getattr(k, "metrics", None), "impressions", 0) or 0) <= 0]
    share = len(zero) / total if total else 0.0
    if len(zero) < int(thr.get("zero_impr_min_count", 20)) or share < float(
        thr.get("zero_impr_share", 0.5)
    ):
        return []
    return [
        Finding(
            check_id="zero_impression_keywords",
            family="keywords",
            severity="info",
            at_risk=0.0,
            spend_segment=None,
            target_campaign=None,
            suggested_operation=None,
            advice_operation="remove_keywords",
            facts={
                "count": len(zero),
                "total": total,
                "pct": int(share * 100),
                "examples": [getattr(k, "keyword", "") for k in zero[:3]],
            },
            evidence={"zero": len(zero), "total": total},
        )
    ]


def check_quality_score(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """B (claude-ads G20-G24 + Hassanelsisi): Quality Score дорогих ключей + разложение на 3 компонента
    (Expected CTR / Ad Relevance / Landing Page). qs_low — есть ключи с QS ≤ порога; qs_ctr_below /
    qs_relevance_below / qs_landing_below — доля ключей с компонентом «ниже среднего» ≥ порога (диагноз,
    ЧТО чинить). Гейт proto3-zero: quality_score==0 → «нет данных», ключ не учитываем. Только ключи с
    расходом ≥ kw_min_spend (шум мелких отсекаем). Всё info/неденежное (rsa), прозой. Нет данных → молчим."""
    rows = ctx.keyword_quality
    if not rows:
        return []
    floor = thr.get("kw_min_spend", 3.0)
    qs_fail = int(thr.get("qs_fail", 4))
    below_pct = thr.get("qs_component_below_pct", 0.35)
    considered = [
        r
        for r in rows
        if int(getattr(r, "quality_score", 0) or 0) > 0
        and float(getattr(getattr(r, "metrics", None), "cost", 0.0) or 0.0) >= floor
    ]
    if not considered:
        return []
    n = len(considered)
    out: list[Finding] = []
    low = [r for r in considered if int(r.quality_score) <= qs_fail]
    if low:
        worst = min(low, key=lambda r: (int(r.quality_score), -float(r.metrics.cost)))
        out.append(
            Finding(
                check_id="qs_low",
                family="rsa",
                severity="info",
                at_risk=0.0,
                spend_segment=None,
                target_campaign=getattr(worst, "campaign", None),
                suggested_operation=None,
                facts={
                    "count": len(low),
                    "qs_fail": qs_fail,
                    "worst_kw": getattr(worst, "keyword", ""),
                    "worst_qs": int(worst.quality_score),
                },
                evidence={"low_qs_keywords": len(low)},
            )
        )
    ctr_below = [r for r in considered if getattr(r, "expected_ctr", "") == "BELOW_AVERAGE"]
    if ctr_below and len(ctr_below) / n >= below_pct:
        out.append(
            Finding(
                check_id="qs_ctr_below",
                family="rsa",
                severity="info",
                at_risk=0.0,
                spend_segment=None,
                target_campaign=None,
                suggested_operation=None,
                facts={"share": round(len(ctr_below) / n * 100), "count": len(ctr_below)},
                evidence={"ctr_below": len(ctr_below)},
            )
        )
    rel_below = [r for r in considered if getattr(r, "ad_relevance", "") == "BELOW_AVERAGE"]
    if rel_below and len(rel_below) / n >= below_pct:
        out.append(
            Finding(
                check_id="qs_relevance_below",
                family="rsa",
                severity="info",
                at_risk=0.0,
                spend_segment=None,
                target_campaign=None,
                suggested_operation=None,
                facts={"share": round(len(rel_below) / n * 100), "count": len(rel_below)},
                evidence={"relevance_below": len(rel_below)},
            )
        )
    lp_below = [r for r in considered if getattr(r, "landing_page", "") == "BELOW_AVERAGE"]
    if lp_below and len(lp_below) / n >= below_pct:
        out.append(
            Finding(
                check_id="qs_landing_below",
                family="rsa",
                severity="info",
                at_risk=0.0,
                spend_segment=None,
                target_campaign=None,
                suggested_operation=None,
                facts={"share": round(len(lp_below) / n * 100), "count": len(lp_below)},
                evidence={"landing_below": len(lp_below)},
            )
        )
    return out


def check_geo_no_conv(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """A (North Country): регион (LOCATION_OF_PRESENCE) с расходом ≥ порога и 0 конверсий при кликах —
    реальный слив гео. ДЕНЕЖНАЯ (at_risk=cost, семья geo). spend_segment=`geo::{кампания}::{регион}`,
    target_campaign=кампания → дедуп кэпит cost региона расходом кампании (гео ⊂ кампания, не задваивает
    spend_no_conv/wasteful_keyword). Топ-N по расходу (анти-спам). Не one-tap (гео-исключение курируется).
    Нет данных гео (ctx.geo_waste=None) → молчим (fail-safe)."""
    rows = ctx.geo_waste
    if not rows:
        return []
    floor = thr.get("geo_min_spend", 20.0)
    cur = getattr(report, "currency", "")
    out: list[Finding] = []
    for r in rows:
        m = getattr(r, "metrics", None)
        cost = float(getattr(m, "cost", 0.0) or 0.0)
        if (
            cost >= floor
            and float(getattr(m, "clicks", 0) or 0) > 0
            and float(getattr(m, "conversions", 0) or 0) == 0
        ):
            camp = getattr(r, "campaign", "")
            region = getattr(r, "region", "")
            out.append(
                Finding(
                    check_id="geo_no_conv",
                    family="geo",
                    severity="warning",
                    at_risk=round(cost, 2),
                    spend_segment=f"geo::{camp}::{region}",
                    target_campaign=camp,
                    suggested_operation=None,  # гео-исключение — не one-tap (курация)
                    facts={
                        "campaign": camp,
                        "region": region,
                        "cost": round(cost, 2),
                        "clicks": int(getattr(m, "clicks", 0) or 0),
                        "currency": cur,
                    },
                    evidence={"cost": round(cost, 2), "clicks": int(getattr(m, "clicks", 0) or 0)},
                )
            )
    out.sort(key=lambda f: -f.at_risk)
    return out[: int(thr.get("kw_top_n", 5))]


def check_schedule_waste(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """A (Hassanelsisi heatmap): ячейки час×день с расходом ≥ порога и 0 конверсий при кликах — слив по
    расписанию (dayparting). НЕденежная (at_risk=0, семья geo): расход ячейки ⊂ расход кампании, деньги
    уже меряют слив-проверки; здесь — сигнал «сдвинь ставки по времени». Один агрегат (сколько ячеек +
    худшая). Не one-tap. Нет данных (ctx.schedule=None) → молчим."""
    cells = ctx.schedule
    if not cells:
        return []
    floor = thr.get("schedule_min_spend", 20.0)
    cur = getattr(report, "currency", "")
    waste = [
        c
        for c in cells
        if float(getattr(getattr(c, "metrics", None), "cost", 0.0) or 0.0) >= floor
        and float(getattr(getattr(c, "metrics", None), "clicks", 0) or 0) > 0
        and float(getattr(getattr(c, "metrics", None), "conversions", 0) or 0) == 0
    ]
    if not waste:
        return []
    worst = max(waste, key=lambda c: float(getattr(c.metrics, "cost", 0.0) or 0.0))
    total = round(sum(float(getattr(c.metrics, "cost", 0.0) or 0.0) for c in waste), 2)
    return [
        Finding(
            check_id="schedule_waste",
            family="geo",
            severity="info",
            at_risk=0.0,
            spend_segment=None,
            target_campaign=getattr(worst, "campaign", None),
            suggested_operation=None,
            facts={
                "count": len(waste),
                "cost": total,
                "worst_day": getattr(worst, "day_of_week", ""),
                "worst_hour": int(getattr(worst, "hour", 0) or 0),
                "currency": cur,
            },
            evidence={"waste_cells": len(waste)},
        )
    ]


def check_manual_bid_high_vol(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Bidding (claude-ads G36/G40): ENABLED-кампания на РУЧНОЙ стратегии (_NON_SMART_BIDDING) с высоким
    объёмом конверсий (> порога) — ручные ставки оставляют деньги на столе, Smart Bidding оптимизировал
    бы лучше. Неденежная (bidding), прозой — смена стратегии требует решения владельца, не one-tap.
    Нет данных о стратегии (ctx.bidding_by_name пуст) → молчим (fail-safe)."""
    if not ctx.bidding_by_name:
        return []
    need = float(thr.get("smart_bid_min_conv", 30))
    out: list[Finding] = []
    for (name, status), m in _campaign_rows(report):
        if status != "ENABLED":
            continue
        conv = float(getattr(m, "conversions", 0.0) or 0.0)
        if conv <= need:
            continue
        b = ctx.bidding_by_name.get(name)
        strat = getattr(b, "strategy_type", "") if b is not None else ""
        if strat not in _NON_SMART_BIDDING:
            continue
        out.append(
            Finding(
                check_id="manual_bid_high_vol",
                family="bidding",
                severity="info",
                at_risk=0.0,
                spend_segment=None,
                target_campaign=name,
                suggested_operation=None,
                facts={"campaign": name, "conversions": round(conv, 1), "strategy_type": strat},
                evidence={"conversions": round(conv, 2), "strategy_type": strat},
            )
        )
    return out


def bid_opportunities(report, thr: dict, ctx: _Ctx) -> list:
    """Ф1: возможности по ставкам (audit.bidscape) из прочитанного слоя ставок/позиций. ОДИН источник
    и для чеков ниже, и для команды /bids — пороги/формулы не разъедутся. Нет данных → []."""
    rows = ctx.bid_landscape
    if not rows:
        return []
    return opportunities(
        rows,
        acct_cpa=float(getattr(report.totals, "cpa", 0.0) or 0.0),
        target_for=ctx.target_for,
        gap_min=float(thr.get("bid_gap_min", 0.10)),
        min_cost=float(thr.get("min_spend", 1.0)),
    )


def _bid_facts(o, cur: str) -> dict:
    return {
        "campaign": o.campaign,
        "ad_group": o.ad_group,
        "keyword": o.keyword,
        "match_type": o.match_type,
        "bid": o.bid,
        "target_bid": o.target_bid,
        "uplift_pct": o.uplift_pct,
        "conversions": o.conversions,
        "cpa": o.cpa,
        "currency": cur,
    }


def _bid_evidence(o) -> dict:
    """criterion_id/ad_group_id — адрес ключа для будущей мутации ставки (её исполняет КОД после «да»,
    не модель): без них совет «подними до X» некуда применить."""
    return {
        "bid": o.bid,
        "target_bid": o.target_bid,
        "cost": o.cost,
        "conversions": o.conversions,
        "criterion_id": o.criterion_id,
        "ad_group_id": o.ad_group_id,
    }


def check_bid_below_first_page(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф1 (ядро запроса «какие слова поднять»): ставка ключа НИЖЕ ОЦЕНКИ GOOGLE для первой страницы
    (position_estimates.first_page_cpc) при живых показах — ключ почти не выходит на первую страницу.

    at_risk = 0.0: это УПУЩЕННАЯ выгода, а не потраченное — класть такое в «Под риском» нельзя (сломает
    инвариант at_risk ≤ расход и дедуп). Ставку считает КОД по цифрам Google; поднять её можно ТОЛЬКО
    прямой командой через confirm-гейт (rule #3) ⇒ suggested_operation=None (кнопки нет), метка для
    замера эффекта — update_keyword_bid. Smart Bidding отсекает audit.bidscape (там ставка ключа ничего
    не решает). Нет данных → молчим."""
    cur = getattr(report, "currency", "")
    ops = [o for o in bid_opportunities(report, thr, ctx) if o.kind == FIRST_PAGE]
    return [
        Finding(
            check_id="bid_below_first_page",
            family="bidding",
            severity="warning",
            at_risk=0.0,  # апсайд, не потраченное
            spend_segment=None,
            target_campaign=o.campaign,
            suggested_operation=None,  # ставка — только прямой командой (rule #3)
            advice_operation="update_keyword_bid",  # метка для outcome-линковки, НЕ кнопка
            facts=_bid_facts(o, cur),
            evidence=_bid_evidence(o),
        )
        for o in ops[: int(thr.get("kw_top_n", 5))]
    ]


def check_bid_below_top_of_page(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф1: ключ КОНВЕРТИТ по приемлемому CPA (≤ цель кампании, иначе ≤ средний по аккаунту), но ставка
    ниже оценки верха страницы (top_of_page_cpc) — доказанная ценность без верхних позиций. info: это
    апсайд, а не слив. at_risk=0.0 и никаких кнопок — как у bid_below_first_page (rule #3)."""
    cur = getattr(report, "currency", "")
    ops = [o for o in bid_opportunities(report, thr, ctx) if o.kind == TOP_OF_PAGE]
    return [
        Finding(
            check_id="bid_below_top_of_page",
            family="bidding",
            severity="info",
            at_risk=0.0,
            spend_segment=None,
            target_campaign=o.campaign,
            suggested_operation=None,
            advice_operation="update_keyword_bid",  # метка для outcome-линковки, НЕ кнопка
            facts=_bid_facts(o, cur),
            evidence=_bid_evidence(o),
        )
        for o in ops[: int(thr.get("kw_top_n", 5))]
    ]


def check_top_is_rank_lost(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф1: платящие ключи теряют ВЕРХНИЕ позиции из-за РАНГА (не бюджета) — search_rank_lost_top_
    impression_share ≥ порога. Ранг = ставка × качество, поэтому диагноз общий: поднять ставку ИЛИ
    починить Quality Score (какой именно компонент — скажут qs_*). Один агрегат (сколько ключей +
    худший), неденежная info (семья bidding), прозой. Доля 0.0 = «не прочитано» (proto3-zero или
    деградация фетчера) — такие строки не считаем: нет данных ≠ «в норме»."""
    rows = ctx.bid_landscape
    if not rows:
        return []
    floor = float(thr.get("kw_min_spend", 3.0))
    rank_min = float(thr.get("kw_rank_lost_top_min", 0.30))
    hit = [
        r
        for r in rows
        if float(getattr(r, "rank_lost_top_is", 0.0) or 0.0) >= rank_min
        and float(getattr(getattr(r, "metrics", None), "cost", 0.0) or 0.0) >= floor
    ]
    if not hit:
        return []
    worst = max(hit, key=lambda r: float(getattr(r, "rank_lost_top_is", 0.0) or 0.0))
    cost = round(sum(float(getattr(r.metrics, "cost", 0.0) or 0.0) for r in hit), 2)
    return [
        Finding(
            check_id="top_is_rank_lost",
            family="bidding",
            severity="info",
            at_risk=0.0,
            spend_segment=None,
            target_campaign=getattr(worst, "campaign", None),
            suggested_operation=None,
            facts={
                "count": len(hit),
                "cost": cost,
                "worst_kw": getattr(worst, "keyword", ""),
                "worst_share": round(float(getattr(worst, "rank_lost_top_is", 0.0)) * 100),
                "currency": getattr(report, "currency", ""),
            },
            evidence={"rank_lost_keywords": len(hit), "cost": cost},
        )
    ]


def check_competitive_pressure(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф5а: КОНКУРЕНЦИЯ — суррогат по данным аукциона. Имён конкурентов Google через API не отдаёт
    (ресурса auction_insight нет, программа доступа закрыта), поэтому имена — только импортом CSV
    (/competitors). Зато САМО давление аукциона видно и без имён, и это цифра, а не мнение:

      «ты берёшь 18% показов, 47% отдаёшь из-за РАНГА и 12% из-за БЮДЖЕТА; наверху страницы — 22%;
       по 9 из 14 платящих ключей твоя ставка ниже оценки верха от Google.»

    Считаем ПО АККАУНТУ, взвешивая доли по показам кампании (кампания на 100 показов не должна тянуть
    картину так же, как кампания на 100 000). Берём только кампании с ПОЛНЫМИ данными (Σ долей ≈ 1.0):
    proto3-zero неотличим от истинного нуля, и «не прочитано» тут стало бы «конкурентов нет» (GR8).

    🔒 Балл не трогаем (score_intensity=0, семья competition ВНЕ FAMILY_WEIGHT): конкретные дефекты —
    потерю по рангу и по бюджету — уже штрафуют is_rank_constrained / is_budget_constrained / is_lost_
    revenue. Штрафовать ещё и агрегатом = посчитать одну и ту же болезнь дважды. Это карточка-диагноз.

    Молчим, пока показов меньше порога (аукцион на сотне показов — не рынок) и пока давление не
    доказано: доля показов ниже потолка И потеря по рангу выше порога. Иначе здоровый аккаунт получал
    бы находку просто за то, что он в аукционе не один."""
    rows = ctx.is_rows or []
    if not rows:
        return []
    tol = float(thr.get("is_data_tolerance", 0.02))
    m_by_name = {name: m for (name, _st), m in _campaign_rows(report)}
    pts: list[tuple[float, float, float, float]] = []  # (search_is, budget_lost, rank_lost, показы)
    for r in rows:
        if getattr(r, "channel_type", "SEARCH") not in ("SEARCH", "SHOPPING"):
            continue
        s = float(getattr(r, "search_is", 0.0) or 0.0)
        b = float(getattr(r, "budget_lost_is", 0.0) or 0.0)
        rk = float(getattr(r, "rank_lost_is", 0.0) or 0.0)
        if not (1.0 - tol <= s + b + rk <= 1.0 + tol):
            continue  # доли не сходятся → данных нет, не выдумываем
        m = m_by_name.get(getattr(r, "campaign_name", ""))
        imp = float(getattr(m, "impressions", 0) or 0) if m else 0.0
        if imp > 0:
            pts.append((s, b, rk, imp))
    total_imp = sum(p[3] for p in pts)
    if total_imp < float(thr.get("comp_min_impressions", 500.0)):
        return []
    share = sum(p[0] * p[3] for p in pts) / total_imp
    budget_lost = sum(p[1] * p[3] for p in pts) / total_imp
    rank_lost = sum(p[2] * p[3] for p in pts) / total_imp
    if not (
        share < float(thr.get("comp_is_max", 0.60))
        and rank_lost >= float(thr.get("comp_rank_min", 0.20))
    ):
        return []  # давление не доказано: либо забираешь своё, либо теряешь не в аукционе, а по бюджету

    facts: dict = {
        "share": round(share * 100),
        "rank_lost": round(rank_lost * 100),
        "budget_lost": round(budget_lost * 100),
        "campaigns": len(pts),
        "currency": getattr(report, "currency", ""),
        # Кто виноват в потере: ранг (ставка × качество) или бюджет. Совет из этого разный.
        "verdict": "rank" if rank_lost >= budget_lost else "budget",
    }
    # Верх страницы + «рынок просит больше, чем ты ставишь» — по НАШИМ же ключам (bid_landscape).
    # Оценка top_of_page_cpc от Google уже в правильном гео/языке аккаунта — в отличие от коридора
    # Keyword Planner, где гео пришлось бы угадывать (угаданный «рынок» = уверенно неверное число).
    kws = [
        r
        for r in (ctx.bid_landscape or [])
        if float(getattr(getattr(r, "metrics", None), "impressions", 0) or 0) > 0
    ]
    tops = [
        (float(getattr(r, "top_is", 0.0) or 0.0), float(getattr(r.metrics, "impressions", 0) or 0))
        for r in kws
    ]
    if any(
        v > 0 for v, _ in tops
    ):  # все нули = доли верха не прочитаны (деградация фетчера), молчим
        w = sum(imp for _, imp in tops)
        facts["top_is"] = round(sum(v * imp for v, imp in tops) / w * 100) if w > 0 else 0
    floor = float(thr.get("kw_min_spend", 3.0))
    paying = [r for r in kws if float(getattr(r.metrics, "cost", 0.0) or 0.0) >= floor]
    under = [
        r
        for r in paying
        if 0.0
        < float(getattr(r, "bid", 0.0) or 0.0)
        < float(getattr(r, "top_of_page_cpc", 0.0) or 0.0)
    ]
    if paying:
        facts["underbid"] = len(under)
        facts["paying"] = len(paying)
    return [
        Finding(
            check_id="competitive_pressure",
            family="competition",
            severity="info",
            at_risk=0.0,
            spend_segment=None,
            target_campaign=None,
            suggested_operation=None,
            facts=facts,
            evidence={
                "search_is": round(share, 4),
                "rank_lost_is": round(rank_lost, 4),
                "budget_lost_is": round(budget_lost, 4),
                "impressions": int(total_imp),
            },
            score_intensity=0.0,  # диагноз, не дефект: рангом/бюджетом уже штрафуют другие чеки
        )
    ]


def check_sim_bid_upside(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф1: симулятор Google по КЛЮЧУ — «подними ставку до X → +N конверсий за +$C». Цифры даёт сам
    Google (ad_group_criterion_simulation, CPC_BID/UNIFORM), мы лишь идём по его точкам маржинальным
    шагом, пока Δрасход/Δконверсии ≤ цель CPA (иначе средний по аккаунту): см. bidscape.sim_upside.

    Симулятора нет (мало трафика / Smart Bidding) → тишина, это НОРМА, а не пробел в данных. Ставку
    берём из bid_landscape: там же и стратегия — на Smart Bidding cpc_bid ключа аукционом не правит.
    Неденежная info: это упущенная выгода, а не потраченное (at_risk=0). Кнопки нет (rule #3)."""
    sims, rows = ctx.bid_simulations, ctx.bid_landscape
    if not sims or not rows:
        return []
    by_key = {(str(s.ad_group_id), str(s.criterion_id)): s for s in sims}
    acct_cpa = float(getattr(report.totals, "cpa", 0.0) or 0.0)
    cur = getattr(report, "currency", "")
    gain = float(thr.get("sim_min_conv_gain", 0.5))
    hits: list[tuple] = []
    for r in rows:
        if str(getattr(r, "strategy_type", "")) not in MANUAL_BID_STRATEGIES:
            continue
        s = by_key.get((str(getattr(r, "ad_group_id", "")), str(getattr(r, "criterion_id", ""))))
        if s is None:
            continue
        target = ctx.target_for(getattr(r, "campaign", "")) or acct_cpa
        up = sim_upside(
            s.points,
            float(getattr(r, "bid", 0.0) or 0.0),
            cap_cpa=float(target),
            min_conv_gain=gain,
        )
        if up is not None:
            hits.append((up, r, s))
    if not hits:
        return []
    hits.sort(key=lambda t: -t[0].add_conversions)
    return [
        Finding(
            check_id="sim_bid_upside",
            family="bidding",
            severity="info",
            at_risk=0.0,
            spend_segment=None,
            target_campaign=getattr(r, "campaign", None),
            suggested_operation=None,
            advice_operation="update_keyword_bid",
            facts={
                "campaign": getattr(r, "campaign", ""),
                "ad_group": getattr(r, "ad_group", ""),
                "keyword": getattr(r, "keyword", ""),
                "bid": up.current,
                "target_bid": up.target,
                "uplift_pct": up.uplift_pct,
                "add_conversions": up.add_conversions,
                "add_cost": up.add_cost,
                "marginal_cpa": up.marginal_cpa,
                "currency": cur,
            },
            evidence={
                "criterion_id": str(getattr(r, "criterion_id", "") or ""),
                "ad_group_id": str(getattr(r, "ad_group_id", "") or ""),
                "sim_points": len(s.points),
                "add_clicks": up.add_clicks,
            },
        )
        for up, r, s in hits[: int(thr.get("kw_top_n", 5))]
    ]


def check_sim_budget_upside(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф1: симулятор Google по КАМПАНИИ (BUDGET) — «подними дневной бюджет до X → +N конверсий».
    Тот же маржинальный шаг с потолком окупаемости. Дополняет is_budget_constrained (та говорит
    «теряешь показы по бюджету», эта — сколько это стоит и сколько даст, цифрами Google).

    Бюджет — деньги ⇒ кнопки нет, только прозой и по прямой команде (golden rule #3)."""
    sims = ctx.budget_simulations
    if not sims:
        return []
    acct_cpa = float(getattr(report.totals, "cpa", 0.0) or 0.0)
    cur = getattr(report, "currency", "")
    gain = float(thr.get("sim_min_conv_gain", 0.5))
    hits: list[tuple] = []
    for s in sims:
        target = ctx.target_for(getattr(s, "campaign", "")) or acct_cpa
        up = sim_upside(
            s.points,
            float(getattr(s, "current_budget", 0.0) or 0.0),
            cap_cpa=float(target),
            min_conv_gain=gain,
        )
        if up is not None:
            hits.append((up, s))
    if not hits:
        return []
    hits.sort(key=lambda t: -t[0].add_conversions)
    return [
        Finding(
            check_id="sim_budget_upside",
            family="budget",
            severity="info",
            at_risk=0.0,
            spend_segment=None,
            target_campaign=getattr(s, "campaign", None),
            suggested_operation=None,
            advice_operation="update_budget",
            facts={
                "campaign": getattr(s, "campaign", ""),
                "budget": up.current,
                "target_budget": up.target,
                "uplift_pct": up.uplift_pct,
                "add_conversions": up.add_conversions,
                "add_cost": up.add_cost,
                "marginal_cpa": up.marginal_cpa,
                "currency": cur,
            },
            evidence={
                "campaign_id": str(getattr(s, "campaign_id", "") or ""),
                "sim_points": len(s.points),
            },
        )
        for up, s in hits[:3]
    ]


def check_google_recommendations(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф2: активные рекомендации Google — «второе мнение» с ЕГО ЖЕ цифрами эффекта
    (recommendation.impact.potential_metrics − base_metrics). Одна сводная находка: сколько
    рекомендаций ждёт, сколько конверсий Google обещает и во сколько это ему видится по расходу.

    🔒 На НАШ score не влияет by design: балл считает наш код по нашим правилам, а Google-балл и его
    рекомендации живут рядом как второе мнение (score_intensity=0.0 + семья вне FAMILY_WEIGHT).
    Иначе мы бы штрафовали клиента за чужую модель, которую не можем ни объяснить, ни проверить.

    Прирост показываем только там, где potential > base: нули impact — «Google эффект не оценил»
    (заполнен не для всех типов), а не «эффекта нет» (gr8). Применение — отдельной мутацией через
    confirm-гейт, не отсюда (часть рекомендаций меняет бюджет/ставки ⇒ только прямая команда)."""
    recs = [
        r
        for r in (ctx.recommendations or [])
        if not getattr(r, "dismissed", False)
        and str(getattr(r, "type", "") or "") not in ("", "UNSPECIFIED", "UNKNOWN")
    ]
    if not recs:
        return []
    cur = getattr(report, "currency", "")

    def _add_conv(r) -> float:
        return max(0.0, float(getattr(r, "add_conversions", 0.0) or 0.0))

    scored = sorted(recs, key=lambda r: -_add_conv(r))
    add_conv = round(sum(_add_conv(r) for r in recs), 1)
    # Расход считаем только по тем, где Google обещает конверсии: иначе «+$0» рекомендаций без
    # оценённого impact размывало бы цену прироста.
    add_cost = round(
        sum(float(getattr(r, "add_cost", 0.0) or 0.0) for r in recs if _add_conv(r) > 0), 2
    )
    top = [
        {
            "type": str(getattr(r, "type", "")),
            "campaign": str(getattr(r, "campaign", "") or ""),
            "add_conversions": round(_add_conv(r), 1),
        }
        for r in scored[:3]
    ]
    # Полный набор типов считаем ОДИН раз: он идёт и в facts (агент), и в evidence (advisor).
    rec_types = sorted({str(getattr(r, "type", "")) for r in recs})
    return [
        Finding(
            check_id="google_recommendations_pending",
            family="recommendations",
            severity="info",
            at_risk=0.0,
            spend_segment=None,
            suggested_operation=None,  # применяет ЧЕЛОВЕК через confirm-гейт, не кнопка аудита
            score_intensity=0.0,  # второе мнение Google не двигает наш балл
            facts={
                "count": len(recs),
                "add_conversions": add_conv,
                "add_cost": add_cost,
                "currency": cur,
                "top": top,
                # Разбивка по типам — В FACTS: в MCP-конверт уезжает только facts
                # (mcp_server/serialize.py:finding_dict), поэтому агент видел count + суммы + топ-3
                # и НЕ видел, что именно советует Google. Размер ограничен ридером
                # (reports.queries.fetch_recommendations limit=50) и самим перечислением типов.
                "types": rec_types,
            },
            # ДУБЛЬ (отдельный список, не алиас): evidence — внутренний контракт advisor
            # (from_findings → store → apply/rules/outcome), его читателей снимать нельзя, а
            # общий объект дал бы «усекли facts — молча урезали то, что персистится в БД».
            evidence={"types": list(rec_types)},
        )
    ]


def check_target_cpa_too_low(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф6 (claude-ads G37): цель CPA задана в РАЗЫ ниже фактической — Google не находит конверсий по
    такой цене и режет показы: кампания формально живёт, а трафика почти не получает. Лечится не
    бюджетом, а честной целью (поднять до факта и снижать шагами).

    Анти-ложные (GR8): нужны ≥ `tcpa_min_conv` конверсий — на одной случайной «фактический CPA» это
    шум, а не факт; кампания без цели (нет target_cpa) не проверяется вовсе. Деньгами не меряем:
    это НЕ потраченное впустую, а НЕ потраченное вообще (at_risk=0)."""
    if not ctx.bidding_by_name:
        return []
    factor = float(thr.get("tcpa_gap_factor", 2.0))
    need_conv = float(thr.get("tcpa_min_conv", 3.0))
    cur = getattr(report, "currency", "")
    out: list[Finding] = []
    for (name, status), m in _campaign_rows(report):
        if status != "ENABLED":
            continue
        b = ctx.bidding_by_name.get(name)
        target = float(getattr(b, "target_cpa", 0.0) or 0.0) if b is not None else 0.0
        conv = float(getattr(m, "conversions", 0.0) or 0.0)
        cost = float(getattr(m, "cost", 0.0) or 0.0)
        if target <= 0 or conv < need_conv or cost < thr["min_spend"]:
            continue
        actual = cost / conv
        if actual < target * factor:
            continue
        out.append(
            Finding(
                check_id="target_cpa_too_low",
                family="bidding",
                severity="warning",
                at_risk=0.0,
                spend_segment=None,
                target_campaign=name,
                suggested_operation=None,  # смена цели — решение владельца денег (rule #3), не кнопка
                facts={
                    "campaign": name,
                    "target_cpa": round(target, 2),
                    "actual_cpa": round(actual, 2),
                    "conversions": round(conv, 1),
                    "currency": cur,
                },
                evidence={"cost": round(cost, 2), "conversions": round(conv, 2)},
            )
        )
    return out


def check_brand_nonbrand_mixed(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф6 (claude-ads G05): брендовые и небрендовые ключи в ОДНОЙ кампании. Бренд — самый дешёвый и
    самый конвертящий трафик; в общей кампании он задирает средние показатели (небрендовая часть
    прячется за его CPA) и делит с ней один бюджет и одну стратегию ставок. Разделять.

    Бренд-токены берём из профиля клиента (§20) — только поле `brand`. Профиля нет → чек МОЛЧИТ:
    угадывать бренд по домену/названию кампании значит рано или поздно назвать брендовым чужой ключ.
    Считаем только ключи с показами: спящий брендовый ключ ни с кем бюджет не делит."""
    inv = ctx.keyword_inventory
    brands = ctx.brand_terms
    # G/GR8: обрезанный инвентарь → небрендовые ключи кампании могли не попасть в выборку; «чисто
    # брендовая» станет ложной. Обрезано ⇒ молчим (как harvest/ngram/cannibalization).
    if not inv or not brands or ctx.keyword_inventory_truncated:
        return []
    need_other = int(thr.get("brand_mix_min_nonbrand", 3))
    min_spend = float(thr.get("brand_mix_min_spend", 20.0))
    cur = getattr(report, "currency", "")
    per: dict[str, dict] = {}
    for k in inv:
        m = getattr(k, "metrics", None)
        if int(getattr(m, "impressions", 0) or 0) <= 0:
            continue
        camp = getattr(k, "campaign", "") or ""
        text = getattr(k, "keyword", "") or ""
        if not camp or not text:
            continue
        side = "brand" if (set(t_tokens(text)) & brands) else "other"
        d = per.setdefault(camp, {"brand": [], "other": [], "brand_cost": 0.0, "other_cost": 0.0})
        d[side].append(text)
        d[f"{side}_cost"] += float(getattr(m, "cost", 0.0) or 0.0)

    out: list[Finding] = []
    for camp, d in sorted(per.items(), key=lambda kv: -(kv[1]["brand_cost"] + kv[1]["other_cost"])):
        cost = d["brand_cost"] + d["other_cost"]
        if not d["brand"] or len(d["other"]) < need_other or cost < min_spend:
            continue
        out.append(
            Finding(
                check_id="brand_nonbrand_mixed",
                family="structure",
                severity="warning",
                at_risk=0.0,  # структура: деньги не сгорают, но их эффективность не видна
                spend_segment=None,
                target_campaign=camp,
                suggested_operation=None,
                facts={
                    "campaign": camp,
                    "brand_kw": len(d["brand"]),
                    "other_kw": len(d["other"]),
                    "brand_cost": round(d["brand_cost"], 2),
                    "brand_share": round(100.0 * d["brand_cost"] / cost) if cost else 0,
                    "examples": sorted(set(d["brand"]))[:3],
                    "currency": cur,
                },
                evidence={"cost": round(cost, 2)},
            )
        )
    return out


def _asset_gaps(report, thr: dict, ctx: _Ctx, field_name: str, minimum: int) -> list[tuple]:
    """Ф6 (G50/G51/G52), общий отбор: SEARCH-кампании с расходом ≥ assets_min_spend, у которых
    расширений типа field_name меньше minimum. Топ-3 по расходу (анти-спам) → [(строка, расход)].

    Считаем ссылки ТРЁХ уровней (кампания+аккаунт+группа, см. fetch_campaign_assets): ситилинк,
    привязанный на аккаунт, работает во всех кампаниях. Не SEARCH (Display/Video/PMax) не трогаем —
    там расширения работают иначе. Данных нет (фетчер упал) → пусто, чеки молчат."""
    rows = ctx.campaign_assets
    if not rows:
        return []
    spend = {
        name: float(getattr(m, "cost", 0.0) or 0.0) for (name, _st), m in _campaign_rows(report)
    }
    min_spend = float(thr.get("assets_min_spend", 20.0))
    hits = [
        (r, spend.get(getattr(r, "campaign", ""), 0.0))
        for r in rows
        if getattr(r, "channel_type", "") == "SEARCH"
        and spend.get(getattr(r, "campaign", ""), 0.0) >= min_spend
        and int(getattr(r, field_name, 0) or 0) < minimum
    ]
    hits.sort(key=lambda rs: -rs[1])
    return hits[:3]


def _asset_facts(report, r, field_name: str, need: int, cost: float) -> dict:
    return {
        "campaign": r.campaign,
        "count": int(getattr(r, field_name, 0) or 0),
        "need": need,
        "spend": round(cost, 2),
        "currency": getattr(report, "currency", ""),
    }


def check_assets_sitelinks_thin(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф6 (claude-ads G50): <4 ситилинков на SEARCH-кампании — объявление занимает меньше места в
    выдаче и собирает меньше кликов при той же ставке. Петля замкнута: читаем → /campaigns генерирует
    → применяет confirm-гейт (кнопки в аудите нет: тексты сперва должен увидеть человек)."""
    need = int(thr.get("assets_min_sitelinks", 4))
    return [
        Finding(
            check_id="assets_sitelinks_thin",
            family="assets",
            severity="info",
            at_risk=0.0,
            target_campaign=r.campaign,
            advice_operation="add_sitelinks",
            facts=_asset_facts(report, r, "sitelinks", need, cost),
            evidence={"sitelinks": r.sitelinks},
        )
        for r, cost in _asset_gaps(report, thr, ctx, "sitelinks", need)
    ]


def check_assets_callouts_thin(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф6 (claude-ads G51): <4 уточнений на SEARCH-кампании (Google показывает до 6)."""
    need = int(thr.get("assets_min_callouts", 4))
    return [
        Finding(
            check_id="assets_callouts_thin",
            family="assets",
            severity="info",
            at_risk=0.0,
            target_campaign=r.campaign,
            advice_operation="add_callouts",
            facts=_asset_facts(report, r, "callouts", need, cost),
            evidence={"callouts": r.callouts},
        )
        for r, cost in _asset_gaps(report, thr, ctx, "callouts", need)
    ]


def check_assets_no_snippets(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф6 (claude-ads G52): у SEARCH-кампании НЕТ структурированных описаний (порога нет: 0 → флаг)."""
    return [
        Finding(
            check_id="assets_no_snippets",
            family="assets",
            severity="info",
            at_risk=0.0,
            target_campaign=r.campaign,
            advice_operation="add_structured_snippets",
            facts=_asset_facts(report, r, "snippets", 1, cost),
            evidence={"snippets": r.snippets},
        )
        for r, cost in _asset_gaps(report, thr, ctx, "snippets", 1)
    ]


def _settings_rows(ctx: _Ctx, channel: str | None = "SEARCH"):
    """Ф6.2: строки настроек кампаний (channel=None → все каналы). Не читано → пусто ⇒ чеки молчат."""
    rows = ctx.campaign_settings or []
    return [r for r in rows if channel is None or getattr(r, "channel_type", "") == channel]


def check_display_on_search_campaign(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф6.2 (claude-ads G12): у SEARCH-кампании включена сеть КМС («Display expansion»). Баннерный
    трафик подмешивается к поисковому: он дешевле по клику, поэтому съедает заметную долю бюджета,
    а конверсий не даёт — и портит средние показатели, по которым Smart Bidding учится.

    Флажим ТОЛЬКО по деньгам: расход именно по сети CONTENT ≥ порога И ноль конверсий с неё
    (GR8 — включённая галочка без расхода это не потеря, а конвертящий КМС не «слив»). at_risk =
    расход по КМС; он ⊂ расхода кампании, поэтому spend_segment — имя кампании (дедуп возьмёт max)."""
    min_spend = float(thr.get("content_on_search_min_spend", 5.0))
    cur = getattr(report, "currency", "")
    out: list[Finding] = []
    for r in _settings_rows(ctx):
        if not getattr(r, "content_network", False):
            continue
        cost = float(getattr(r, "content_cost", 0.0) or 0.0)
        conv = float(getattr(r, "content_conversions", 0.0) or 0.0)
        if cost < min_spend or conv > 0:
            continue
        out.append(
            Finding(
                check_id="display_on_search_campaign",
                family="waste",
                severity="warning",
                at_risk=round(cost, 2),
                spend_segment=r.campaign,
                target_campaign=r.campaign,
                # One-tap (2026-07-14): выключить КМС — не деньги, обратимо, чинится в одну сторону.
                # Кнопка минтит ЧЕРНОВИК (confirm-гейт), а не исполняет: «да» по-прежнему за человеком.
                suggested_operation="set_campaign_display_network",
                facts={
                    "campaign": r.campaign,
                    "content_cost": round(cost, 2),
                    "currency": cur,
                },
                evidence={"content_conversions": round(conv, 2)},
            )
        )
    return out


def check_geo_interest_waste(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф6.2 (claude-ads G11): гео-таргетинг «присутствие ИЛИ интерес» — объявления видят люди,
    физически находящиеся ВНЕ ваших регионов (им просто «интересен» регион). Для локального бизнеса
    это чистый слив: приехать и купить они не могут.

    Меряем деньгами, а не галочкой: user_location_view.targeting_location = FALSE — это клики людей
    вне таргета. Расход по ним ≥ порога И ноль конверсий → находка. Если «интересующиеся» конвертят —
    молчим: у национального/онлайн-бизнеса настройка законна (GR8).

    ⚠️ Эпоха 5 (ревизия волны): деньги в at_risk НЕ кладём (0.0 + score_intensity), хотя условие
    срабатывания — по-прежнему деньги. Клик человека вне таргета уже посчитан `geo_no_conv`: тот же
    расход приходит из geographic_view с LOCATION_OF_PRESENCE (регион вне таргета) — это ОДНИ И ТЕ ЖЕ
    деньги с двух сторон. Сегменты у чеков разные (`geo::камп::регион` vs кампания), поэтому дедуп
    брал не MAX, а СУММУ, и headline «Под риском» задваивался (600 при 300 сожжённых). Схлопнуть
    сегменты нельзя: у `geo_no_conv` регионы ВНУТРИ таргета — разные деньги, их сумма законна.
    Прецедент — `schedule_waste`/`is_lost_revenue`: расход показывается в тексте находки и штрафует
    балл через intensity, но в «Под риском» его кладёт РОВНО ОДИН чек."""
    min_spend = float(thr.get("geo_interest_min_spend", 20.0))
    cur = getattr(report, "currency", "")
    total_spend = float(getattr(report.totals, "cost", 0.0) or 0.0)
    out: list[Finding] = []
    for r in _settings_rows(ctx, channel=None):
        if getattr(r, "geo_target_type", "") not in ("PRESENCE_OR_INTEREST", "SEARCH_INTEREST"):
            continue
        cost = float(getattr(r, "outside_cost", 0.0) or 0.0)
        conv = float(getattr(r, "outside_conversions", 0.0) or 0.0)
        if cost < min_spend or conv > 0:
            continue
        out.append(
            Finding(
                check_id="geo_interest_waste",
                family="geo",
                severity="warning",
                at_risk=0.0,  # деньги вне таргета кладёт в headline geo_no_conv — иначе двойной счёт
                spend_segment=None,
                target_campaign=r.campaign,
                # …но балл штрафуем ПРОПОРЦИОНАЛЬНО расходу вне таргета (не фиксом NONMONEY_INTENSITY):
                # настройка на 3% бюджета и на 60% — разные болезни.
                score_intensity=(min(1.0, cost / total_spend) if total_spend > 0 else 0.0),
                # One-tap (2026-07-14): сузить гео до PRESENCE. Расширение (PRESENCE_OR_INTEREST)
                # кнопкой недоступно — параметр зашит константой в bot.main._advise_apply_params.
                suggested_operation="set_campaign_geo_target_type",
                facts={
                    "campaign": r.campaign,
                    "outside_cost": round(cost, 2),
                    "geo_target_type": r.geo_target_type,
                    "currency": cur,
                },
                evidence={"outside_conversions": round(conv, 2)},
            )
        )
    return out


# ── Ф7: Performance Max ──────────────────────────────────────────────────────────────
# ⚠️ ДЕНЬГИ PMax НЕ СУММИРУЮТСЯ. Расход поисковых запросов ⊂ расход групп активов ⊂ расход кампании:
# это ОДНИ И ТЕ ЖЕ деньги, посчитанные с разных сторон. Поэтому у обоих денежных чеков
# spend_segment = ИМЯ КАМПАНИИ — _dedup_at_risk берёт по сегменту МАКСИМУМ, а не сумму. Дать им
# разные сегменты значит вернуть ровно тот дефект двойного счёта, из-за которого чинили эпоху 4.
# Поисковые запросы PMax живут только в campaign_search_term_view: fetch_search_terms их НЕ ВИДИТ
# (segments.keyword.* отфильтровывает PMax), поэтому wasteful_search_term тут молчит и не дублирует.


def check_pmax_search_term_waste(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф7: поисковый запрос PMax с расходом ≥ порога и кликами, но 0 конверсий → в минус-слова.
    PMax принимает минус-слова НА УРОВНЕ КАМПАНИИ (campaign_criterion) — операция одна и та же,
    поэтому находка one-tap: match_type=exact режет только этот запрос.

    Сегмент денег — кампания (не запрос): расход запроса ⊂ расход группы активов той же кампании,
    иначе pmax_asset_group_no_conv и этот чек сложили бы одни деньги дважды."""
    rows = ctx.pmax_campaigns or []
    min_spend = float(thr["kw_min_spend"])
    cur = getattr(report, "currency", "")
    out: list[Finding] = []
    for c in rows:
        for st in getattr(c, "search_terms", []) or []:
            cost = float(getattr(st, "cost", 0.0) or 0.0)
            if cost < min_spend or int(getattr(st, "clicks", 0) or 0) <= 0:
                continue
            if float(getattr(st, "conversions", 0.0) or 0.0) > 0:
                continue
            term = getattr(st, "search_term", "") or ""
            if not term:
                continue
            out.append(
                Finding(
                    check_id="pmax_search_term_waste",
                    family="keywords",
                    severity="warning",
                    at_risk=round(cost, 2),
                    spend_segment=c.campaign,
                    target_campaign=c.campaign,
                    suggested_operation="add_negative_keywords",
                    facts={
                        "campaign": c.campaign,
                        "search_term": term,
                        "cost": round(cost, 2),
                        "clicks": int(getattr(st, "clicks", 0) or 0),
                        "currency": cur,
                    },
                    evidence={
                        "keyword": term,
                        "match_type": "exact",
                        "cost": round(cost, 2),
                        "clicks": int(getattr(st, "clicks", 0) or 0),
                    },
                )
            )
    out.sort(key=lambda f: -float(f.evidence.get("cost", 0) or 0))
    return out[: int(thr.get("kw_top_n", 5))]


def check_pmax_asset_group_no_conv(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф7: группа активов PMax тратит и кликает, но не конвертит — ВНУТРИ кампании, которая в целом
    конвертит. Группы делят один бюджет кампании: пустая группа забирает показы у работающей.

    Флажим только при живых конверсиях кампании (GR8): если не конвертит ВСЯ кампания, дело не в
    группе — про это говорит spend_no_conv/high_cpa, и дублировать их находку нельзя. Клики > 0:
    группа без кликов деньги не жгла. at_risk = расход неконвертящих групп (⊂ расход кампании)."""
    groups = ctx.pmax_asset_groups
    camps = ctx.pmax_campaigns
    if not groups or not camps:
        return []
    conv_by_camp = {c.campaign_id: float(getattr(c, "conversions", 0.0) or 0.0) for c in camps}
    min_spend = float(thr.get("pmax_min_spend", 20.0))
    cur = getattr(report, "currency", "")
    per: dict[str, dict] = {}
    for g in groups:
        if conv_by_camp.get(g.campaign_id, 0.0) <= 0:
            continue
        cost = float(getattr(g, "cost", 0.0) or 0.0)
        if cost < min_spend or int(getattr(g, "clicks", 0) or 0) <= 0:
            continue
        if float(getattr(g, "conversions", 0.0) or 0.0) > 0:
            continue
        d = per.setdefault(g.campaign, {"cost": 0.0, "names": []})
        d["cost"] += cost
        d["names"].append(g.asset_group)
    out: list[Finding] = []
    for camp, d in sorted(per.items(), key=lambda kv: -kv[1]["cost"]):
        out.append(
            Finding(
                check_id="pmax_asset_group_no_conv",
                family="waste",
                severity="warning",
                at_risk=round(d["cost"], 2),
                spend_segment=camp,
                target_campaign=camp,
                suggested_operation=None,  # группы активов PMax через API не паузим — правка вручную
                facts={
                    "campaign": camp,
                    "groups": len(d["names"]),
                    "examples": sorted(d["names"])[:3],
                    "cost": round(d["cost"], 2),
                    "currency": cur,
                },
                evidence={"cost": round(d["cost"], 2)},
            )
        )
    return out


def check_pmax_brand_cannibalization(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф7 (G-PM3 + G07): PMax собирает конверсии с БРЕНДОВЫХ запросов. Бренд — самый дешёвый трафик:
    человек уже искал вас по имени и пришёл бы всё равно. PMax присваивает эту конверсию себе, её
    ROAS выглядит блестящим, и бюджет перетекает туда с настоящего поискового спроса.

    Бренд-токены — только из профиля клиента (§20); профиля нет → чек МОЛЧИТ (угадывать бренд нечем).
    at_risk=0: деньги не сгорают, искажена АТРИБУЦИЯ. Оценка доли КОНСЕРВАТИВНА — брендовые конверсии
    считаем по топ-N запросов (фетчер режет по расходу), а долю берём от ВСЕХ конверсий кампании.
    brand_excluded (есть ли исключение бренда, SharedSet BRANDS) меняет совет: без него — «включите
    исключение бренда», с ним — «оно есть, но конверсии всё равно брендовые, проверьте список»."""
    rows = ctx.pmax_campaigns
    brands = ctx.brand_terms
    if not rows or not brands:
        return []
    min_conv = float(thr.get("pmax_brand_min_conv", 3.0))
    min_share = float(thr.get("pmax_brand_conv_share", 0.30))
    cur = getattr(report, "currency", "")
    out: list[Finding] = []
    for c in rows:
        total_conv = float(getattr(c, "conversions", 0.0) or 0.0)
        if total_conv <= 0:
            continue
        brand_conv = 0.0
        brand_cost = 0.0
        examples: list[str] = []
        for st in getattr(c, "search_terms", []) or []:
            term = getattr(st, "search_term", "") or ""
            if not (set(t_tokens(term)) & brands):
                continue
            brand_conv += float(getattr(st, "conversions", 0.0) or 0.0)
            brand_cost += float(getattr(st, "cost", 0.0) or 0.0)
            examples.append(term)
        share = brand_conv / total_conv if total_conv else 0.0
        if brand_conv < min_conv or share < min_share:
            continue
        out.append(
            Finding(
                check_id="pmax_brand_cannibalization",
                family="structure",
                severity="warning",
                at_risk=0.0,  # атрибуция, а не сожжённые деньги
                spend_segment=None,
                target_campaign=c.campaign,
                suggested_operation=None,
                facts={
                    "campaign": c.campaign,
                    "brand_share": round(100.0 * share),
                    "brand_conv": round(brand_conv, 1),
                    "brand_cost": round(brand_cost, 2),
                    "brand_excluded": bool(getattr(c, "brand_exclusions", 0)),
                    "examples": sorted(set(examples))[:3],
                    "currency": cur,
                },
                evidence={"cost": round(brand_cost, 2)},
            )
        )
    return out


def _pmax_spending_groups(ctx: _Ctx, thr: dict) -> list:
    """Группы активов PMax с расходом ≥ порога: спящая группа — не дефект, а ещё не запущенная."""
    min_spend = float(thr.get("pmax_min_spend", 20.0))
    return [
        g
        for g in (ctx.pmax_asset_groups or [])
        if float(getattr(g, "cost", 0.0) or 0.0) >= min_spend
    ]


def check_pmax_ad_strength_poor(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф7 (G-PM2): сам Google оценил группу активов как POOR/NO_ADS. AVERAGE не флажим — это половина
    живых групп, находка была бы шумом (тот же принцип, что в rsa_ad_strength_poor).

    Чего именно не хватает — тоже говорит Google: asset_coverage.ad_strength_action_items («добавьте
    N ассетов типа X»). Своей «плотности ассетов» (20 картинок / 5 лого) НЕ считаем: это лимиты
    слотов Google, а не порог качества — порог из головы дал бы ложные находки."""
    rows = [
        g
        for g in _pmax_spending_groups(ctx, thr)
        if getattr(g, "ad_strength", "") in ("POOR", "NO_ADS")
    ]
    rows.sort(key=lambda g: -float(getattr(g, "cost", 0.0) or 0.0))
    cur = getattr(report, "currency", "")
    out: list[Finding] = []
    for g in rows[:3]:
        items = [f"{t}×{n}" for t, n in (getattr(g, "action_items", []) or [])][:3]
        out.append(
            Finding(
                check_id="pmax_ad_strength_poor",
                family="pmax",
                severity="info",
                at_risk=0.0,
                spend_segment=None,
                target_campaign=g.campaign,
                suggested_operation=None,
                facts={
                    "campaign": g.campaign,
                    "asset_group": g.asset_group,
                    "ad_strength": g.ad_strength,
                    "action_items": items,
                    "cost": round(float(getattr(g, "cost", 0.0) or 0.0), 2),
                    "currency": cur,
                },
                evidence={"cost": round(float(getattr(g, "cost", 0.0) or 0.0), 2)},
            )
        )
    return out


def check_pmax_no_video(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф7 (G32): в группе активов нет СВОЕГО видео. Google всё равно покажет видео на YouTube — он
    соберёт его сам из картинок (source = AUTOMATICALLY_CREATED), и это худшее из возможных видео.
    Считать авто-видео за своё нельзя: чек промолчал бы ровно в самом плохом случае."""
    out: list[Finding] = []
    for g in _pmax_spending_groups(ctx, thr):
        if int(getattr(g, "videos", 0) or 0) > 0:
            continue
        out.append(
            Finding(
                check_id="pmax_no_video",
                family="pmax",
                severity="info",
                at_risk=0.0,
                spend_segment=None,
                target_campaign=g.campaign,
                suggested_operation=None,
                facts={
                    "campaign": g.campaign,
                    "asset_group": g.asset_group,
                    "videos_auto": int(getattr(g, "videos_auto", 0) or 0),
                },
                evidence={"cost": round(float(getattr(g, "cost", 0.0) or 0.0), 2)},
            )
        )
    return out


def check_pmax_no_signals(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф7 (G-PM4): группа активов без ЕДИНОГО сигнала — ни поисковых тем, ни аудиторий — и кампания
    без товарного фида. Google учится с нуля, тратя бюджет на разведку.

    Все три условия обязательны: аудитория — такой же сигнал, что и тема, а у retail-PMax сигналом
    служит сам фид (merchant_id). Проверять одни search_themes значило бы врать двум типам кампаний.

    GR8: фид живёт в ДРУГОМ чтении (ctx.pmax_campaigns). Упало оно одно — `or []` превратил бы
    «фид не прочитан» в «фида нет», и retail-PMax (законно работающий на одном фиде) получил бы
    обвинение «ни одного сигнала». Нет фид-сигнала → молчим."""
    if ctx.pmax_campaigns is None:
        return []
    feed = {c.campaign_id: bool(getattr(c, "merchant_id", "")) for c in ctx.pmax_campaigns}
    out: list[Finding] = []
    for g in _pmax_spending_groups(ctx, thr):
        if int(getattr(g, "search_themes", 0) or 0) > 0 or int(getattr(g, "audiences", 0) or 0) > 0:
            continue
        if feed.get(g.campaign_id, False):
            continue
        out.append(
            Finding(
                check_id="pmax_no_signals",
                family="pmax",
                severity="info",
                at_risk=0.0,
                spend_segment=None,
                target_campaign=g.campaign,
                suggested_operation=None,
                facts={"campaign": g.campaign, "asset_group": g.asset_group},
                evidence={"cost": round(float(getattr(g, "cost", 0.0) or 0.0), 2)},
            )
        )
    return out


def check_pmax_no_negatives(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф7 (G-PM6): у PMax-кампании с расходом нет НИ ОДНОГО минус-слова — ни своего, ни через список,
    ни аккаунтного. PMax тянет трафик из всех сетей; без минусов он неизбежно платит за «бесплатно»,
    «вакансии», «своими руками». info, а не warning: сожжённые деньги доказывает
    pmax_search_term_waste — этот чек про отсутствующую защиту, а не про факт потери."""
    min_spend = float(thr.get("pmax_min_spend", 20.0))
    out: list[Finding] = []
    for c in ctx.pmax_campaigns or []:
        cost = float(getattr(c, "cost", 0.0) or 0.0)
        if cost < min_spend or int(getattr(c, "negatives", 0) or 0) > 0:
            continue
        out.append(
            Finding(
                check_id="pmax_no_negatives",
                family="pmax",
                severity="info",
                at_risk=0.0,
                spend_segment=None,
                target_campaign=c.campaign,
                suggested_operation=None,
                facts={
                    "campaign": c.campaign,
                    "cost": round(cost, 2),
                    "currency": getattr(report, "currency", ""),
                },
                evidence={"cost": round(cost, 2)},
            )
        )
    return out


def check_pmax_insufficient_conversions(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф7 (G-PM7): НЕСКОЛЬКО PMax-кампаний, и каждая набирает меньше 30 конверсий за 30 дней — сигнал
    делится на всех, и не учится ни одна. Лечится слиянием.

    Одна кампания ниже порога — МОЛЧИМ: это «мало спроса», а не ошибка настройки, и лекарства у нас
    нет (совет «получите больше конверсий» — вода). Период < 14 дней — тоже молчим: экстраполяция
    двух недель на месяц слишком шумная, чтобы называть это находкой."""
    rows = [c for c in (ctx.pmax_campaigns or []) if float(getattr(c, "cost", 0.0) or 0.0) > 0]
    if len(rows) < 2:
        return []
    min_days = int(thr.get("pmax_min_days", 14))
    need = float(thr.get("pmax_learn_min_conv", 30.0))
    if any(int(getattr(c, "days", 0) or 0) < min_days for c in rows):
        return []
    per_month = {
        c.campaign: float(getattr(c, "conversions", 0.0) or 0.0)
        * 30.0
        / max(int(getattr(c, "days", 30) or 30), 1)
        for c in rows
    }
    if any(v >= need for v in per_month.values()):
        return []
    return [
        Finding(
            check_id="pmax_insufficient_conversions",
            family="pmax",
            severity="info",
            at_risk=0.0,
            spend_segment=None,
            target_campaign=None,
            suggested_operation=None,
            facts={
                "campaigns": len(rows),
                "need": int(need),
                "worst": round(min(per_month.values()), 1),
                "best": round(max(per_month.values()), 1),
            },
            evidence={"cost": round(sum(float(getattr(c, "cost", 0.0) or 0.0) for c in rows), 2)},
        )
    ]


# ── Ф9: волна индустриальных чек-листов (2026-07-17) ──────────────────────────────────
def check_device_waste(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф9: разрыв эффективности по устройствам внутри кампании. Две ветки: (а) устройство с расходом
    ≥ порога, кликами и 0 конверсий, пока ОСТАЛЬНЫЕ устройства кампании конвертят — деньги в риске
    (at_risk = расход устройства, сегмент ⊂ кампания, дедуп не задваивает spend_no_conv);
    (б) устройство конвертит, но его CPA ≥ factor × CPA остальных — прозой (at_risk=0: деньги
    работают, но переплата; сумма переплаты в facts). Лечится bid adjustment по устройству → семья
    bidding. Не one-tap (корректировка ставок — денежная, только по прямой команде).
    Нет данных (ctx.device_performance=None) → молчим (fail-safe)."""
    rows = ctx.device_performance
    if not rows:
        return []
    floor = float(thr.get("device_min_spend", 20.0))
    factor = float(thr.get("device_cpa_factor", 2.0))
    cur = getattr(report, "currency", "")
    by_camp: dict[str, list] = {}
    for r in rows:
        by_camp.setdefault(getattr(r, "campaign", ""), []).append(r)
    out: list[Finding] = []
    for camp, devs in by_camp.items():
        if len(devs) < 2:
            continue  # одно устройство — сравнивать не с чем
        for d in devs:
            m = getattr(d, "metrics", None)
            cost = float(getattr(m, "cost", 0.0) or 0.0)
            if cost < floor:
                continue
            clicks = float(getattr(m, "clicks", 0) or 0)
            conv = float(getattr(m, "conversions", 0.0) or 0.0)
            rest = [o for o in devs if o is not d]
            rest_conv = sum(
                float(getattr(getattr(o, "metrics", None), "conversions", 0.0) or 0.0) for o in rest
            )
            rest_cost = sum(
                float(getattr(getattr(o, "metrics", None), "cost", 0.0) or 0.0) for o in rest
            )
            if rest_conv <= 0:
                continue  # остальные не конвертят — это не разрыв устройства, а проблема кампании
            device = getattr(d, "device", "")
            rest_cpa = rest_cost / rest_conv
            if clicks > 0 and conv == 0:
                out.append(
                    Finding(
                        check_id="device_performance_gap",
                        family="bidding",
                        severity="warning",
                        at_risk=round(cost, 2),
                        spend_segment=f"device::{camp}::{device}",
                        target_campaign=camp,
                        suggested_operation=None,
                        facts={
                            "campaign": camp,
                            "device": device,
                            "cost": round(cost, 2),
                            "clicks": int(clicks),
                            "currency": cur,
                            "reason": "zero_conv",
                        },
                        evidence={"cost": round(cost, 2), "rest_conv": round(rest_conv, 1)},
                    )
                )
            elif conv > 0 and cost / conv >= factor * rest_cpa:
                out.append(
                    Finding(
                        check_id="device_performance_gap",
                        family="bidding",
                        severity="warning",
                        at_risk=0.0,  # конверсии есть — деньги работают; переплата в facts
                        spend_segment=None,
                        target_campaign=camp,
                        suggested_operation=None,
                        facts={
                            "campaign": camp,
                            "device": device,
                            "cost": round(cost, 2),
                            "cpa": round(cost / conv, 2),
                            "rest_cpa": round(rest_cpa, 2),
                            "overpay": round(cost - conv * rest_cpa, 2),
                            "currency": cur,
                            "reason": "cpa_gap",
                        },
                        evidence={"cpa": round(cost / conv, 2), "rest_cpa": round(rest_cpa, 2)},
                    )
                )
    out.sort(key=lambda f: -(f.at_risk or float(f.facts.get("overpay", 0.0))))
    return out[: int(thr.get("kw_top_n", 5))]


def _tokens_contain(hay: list[str], needle: list[str]) -> bool:
    """Входит ли последовательность needle в hay ПОДРЯД (семантика PHRASE-минуса)."""
    n = len(needle)
    if not n or n > len(hay):
        return False
    return any(hay[i : i + n] == needle for i in range(len(hay) - n + 1))


def check_negative_conflicts(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф9: минус-слово блокирует СВОЙ ЖЕ активный ключ. Семантика минусов (у негативов НЕТ близких
    вариантов, только буквальный текст): EXACT — тексты равны; PHRASE — минус входит в ключ подряд;
    BROAD — все токены минуса есть в ключе (порядок неважен). Область действия: минус кампании бьёт
    по её ключам, минус группы — по группе, shared-список — по всем ПРИВЯЗАННЫМ кампаниям.

    Усечённый инвентарь тут НЕ глушит чек (в отличие от harvest/ngram): найденный конфликт —
    позитивный факт, полнота списка на его истинность не влияет (неполнота = недосчёт находок,
    не ложь). Нет данных (минусы или инвентарь не читаны) → молчим."""
    info = ctx.negative_keywords
    inv = ctx.keyword_inventory
    if info is None or inv is None:
        return []
    negs = list(getattr(info, "rows", None) or [])
    attach = dict(getattr(info, "shared_attachments", None) or {})
    if not negs or not inv:
        return []
    kws = []  # (campaign, ad_group, text, norm, tokens)
    for k in inv:
        text = getattr(k, "keyword", "") or ""
        nrm = t_norm(text)
        if nrm:
            kws.append(
                (getattr(k, "campaign", ""), getattr(k, "ad_group", ""), text, nrm, nrm.split())
            )
    out: list[Finding] = []
    for ng in negs:
        n_norm = t_norm(getattr(ng, "text", "") or "")
        if not n_norm:
            continue
        n_tokens = n_norm.split()
        n_tokset = set(n_tokens)
        mt = str(getattr(ng, "match_type", "") or "").upper()
        scope = getattr(ng, "scope", "")
        camp = getattr(ng, "campaign", "")
        group = getattr(ng, "ad_group", "")
        list_name = getattr(ng, "list_name", "")
        if scope == "campaign":
            in_scope = [k for k in kws if k[0] == camp]
        elif scope == "ad_group":
            in_scope = [k for k in kws if k[0] == camp and k[1] == group]
        elif scope == "shared":
            camps = attach.get(list_name, frozenset())
            in_scope = [k for k in kws if k[0] in camps]
        else:
            continue
        if mt == "EXACT":
            blocked = [k for k in in_scope if k[3] == n_norm]
        elif mt == "PHRASE":
            blocked = [k for k in in_scope if _tokens_contain(k[4], n_tokens)]
        else:  # BROAD (и неизвестный тип судим по самой широкой семантике)
            blocked = [k for k in in_scope if n_tokset <= set(k[4])]
        if not blocked:
            continue
        where = f"{camp} / {group}" if scope == "ad_group" else (camp or list_name)
        # 3.2в: кампанийный минус бот умеет снять сам (remove_negative_keywords — точный текст+тип из
        # находки, неденежно, обратимо) → one-tap. Группа/shared — прозой: схема мутации кампанийная.
        # Скоринг/детекция НЕ тронуты (кнопка не входит в score) — эпоха модели та же.
        mt_norm = _norm_match_type(mt)
        one_tap = scope == "campaign" and bool(camp) and mt_norm is not None
        out.append(
            Finding(
                check_id="negative_keyword_conflicts",
                family="keywords",
                severity="warning",
                at_risk=0.0,  # заблокированный ключ не тратит — он ТЕРЯЕТ трафик
                spend_segment=None,
                target_campaign=camp or (blocked[0][0] if blocked else None),
                suggested_operation="remove_negative_keywords" if one_tap else None,
                facts={
                    "negative": getattr(ng, "text", ""),
                    "match_type": mt.lower(),
                    "scope": scope,
                    "where": where,
                    "blocked_count": len(blocked),
                    "examples": [k[2] for k in blocked[:3]],
                },
                evidence={
                    "blocked": len(blocked),
                    # для one-tap применения (bot._advise_apply_params) и outcome-линковки
                    "negative": getattr(ng, "text", ""),
                    "match_type": mt_norm,
                },
            )
        )
    out.sort(key=lambda f: -int(f.facts.get("blocked_count", 0)))
    return out[: int(thr.get("kw_top_n", 5))]


def check_no_conversion_value(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф9: конверсии считаются, а их ЦЕННОСТЬ — нет (conversions ≥ порога, conversions_value = 0).
    Без value невозможны tROAS и сравнение кампаний по выручке — оптимизация слепа на один глаз.
    Только при ЖИВЫХ действиях-конверсиях (ctx.conversion_actions подан и ENABLED есть): без
    верификации это гадание, а «трекинга нет вовсе» кричит critical-чек. Порог отсекает шум:
    на 2-3 конверсиях нулевое value — рано судить."""
    cas = ctx.conversion_actions
    if cas is None:
        return []
    if not any(getattr(c, "status", "") == "ENABLED" for c in cas):
        return []
    t = report.totals
    conv = float(getattr(t, "conversions", 0.0) or 0.0)
    value = float(getattr(t, "conv_value", 0.0) or 0.0)
    if conv < float(thr.get("no_value_min_conv", 10.0)) or value > 0:
        return []
    return [
        Finding(
            check_id="no_conversion_value",
            family="conversion_tracking",
            severity="info",
            at_risk=0.0,
            spend_segment=None,
            facts={"conversions": round(conv, 1)},
            evidence={"conversions": round(conv, 1), "conv_value": 0},
        )
    ]


def check_auto_apply_recommendations(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф9: включённые подписки авто-применения рекомендаций — Google меняет аккаунт САМ (ставки/
    ключи/тексты), без подтверждения владельца. Прямое противоречие философии бота (правило 1:
    никаких изменений без «да»). Семья recommendations — вне score (показываем, не штрафуем:
    это выбор владельца, а не дефект кампаний). Нет данных → молчим."""
    subs = ctx.recommendation_subscriptions
    if not subs:
        return []
    enabled = sorted(
        {str(getattr(s, "type", "") or "") for s in subs if getattr(s, "status", "") == "ENABLED"}
        - {""}
    )
    if not enabled:
        return []
    return [
        Finding(
            check_id="auto_apply_recommendations_on",
            family="recommendations",
            severity="info",
            at_risk=0.0,
            spend_segment=None,
            facts={"count": len(enabled), "types": enabled[:5]},
            evidence={"enabled_subscriptions": len(enabled)},
        )
    ]


def check_attribution_last_click(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф9: primary-действия на атрибуции «последний клик» — вся ценность пути присваивается
    последнему клику, верхние ключи выглядят «неработающими», и Smart Bidding недоплачивает за них.
    Google сам мигрирует аккаунты на data-driven; last click сегодня — осознанный шаг назад.
    Судим только primary ENABLED (вспомогательные действия на последнем клике — норма).
    getattr-гейт: у стабов/старых строк поля нет → молчим, не гадаем."""
    cas = ctx.conversion_actions
    if cas is None:
        return []
    last = [
        c
        for c in cas
        if getattr(c, "status", "") == "ENABLED"
        and getattr(c, "primary_for_goal", False)
        and getattr(c, "attribution_model", "") == "GOOGLE_ADS_LAST_CLICK"
    ]
    if not last:
        return []
    return [
        Finding(
            check_id="attribution_model_last_click",
            family="conversion_tracking",
            severity="info",
            at_risk=0.0,
            spend_segment=None,
            facts={
                "count": len(last),
                "names": [getattr(c, "name", "") for c in last[:3]],
            },
            evidence={"last_click_actions": len(last)},
        )
    ]


def check_ad_rotation(report, thr: dict, ctx: _Ctx) -> list[Finding]:
    """Ф9: группы с ротацией ROTATE_FOREVER — «показывать по кругу, не оптимизировать»: лучшее
    объявление не выигрывает чаще, CTR группы ниже возможного. Почти всегда наследие A/B-теста,
    который забыли закрыть. Один агрегат (сколько групп + пример). Нет данных → молчим."""
    rows = ctx.ad_rotation
    if not rows:
        return []
    bad = [r for r in rows if getattr(r, "rotation_mode", "") == "ROTATE_FOREVER"]
    if not bad:
        return []
    worst = bad[0]
    return [
        Finding(
            check_id="ad_rotation_rotate_forever",
            family="rsa",
            severity="info",
            at_risk=0.0,
            spend_segment=None,
            target_campaign=getattr(worst, "campaign", "") or None,
            facts={
                "count": len(bad),
                "campaign": getattr(worst, "campaign", ""),
                "ad_group": getattr(worst, "ad_group", ""),
            },
            evidence={"rotate_forever_groups": len(bad)},
        )
    ]


# Порядок проверок = порядок запуска (на рендер не влияет — там сортировка по важности).
_CHECKS = (
    check_no_conversion_tracking,
    check_duplicate_conversions,
    check_ads_disapproved,
    check_zero_impressions,
    check_spend_no_conv,
    check_kill_rule,
    check_high_cpa,
    check_wasteful_keyword,
    check_wasteful_search_term,
    check_broad_unmanaged,
    check_impression_share,
    check_budget_imbalance,
    check_low_ctr_ad,
    check_single_campaign,
    check_adgroup_bloat,
    check_rsa_thin,
    check_rsa_keyword_coverage,
    check_rsa_copy_thin,
    check_rsa_ad_strength,
    check_rsa_overpinned,
    check_rsa_stale,
    check_no_negative_list,
    check_keyword_harvest,
    check_ngram_waste,
    check_keyword_cannibalization,
    check_zero_impression_keywords,
    check_quality_score,
    check_manual_bid_high_vol,
    check_target_cpa_too_low,
    check_brand_nonbrand_mixed,
    check_assets_sitelinks_thin,
    check_assets_callouts_thin,
    check_assets_no_snippets,
    check_display_on_search_campaign,
    check_geo_interest_waste,
    check_bid_below_first_page,
    check_bid_below_top_of_page,
    check_top_is_rank_lost,
    check_competitive_pressure,
    check_sim_bid_upside,
    check_sim_budget_upside,
    check_google_recommendations,
    check_geo_no_conv,
    check_schedule_waste,
    check_pmax_search_term_waste,
    check_pmax_asset_group_no_conv,
    check_pmax_brand_cannibalization,
    check_pmax_ad_strength_poor,
    check_pmax_no_video,
    check_pmax_no_signals,
    check_pmax_no_negatives,
    check_pmax_insufficient_conversions,
    check_device_waste,
    check_negative_conflicts,
    check_no_conversion_value,
    check_auto_apply_recommendations,
    check_attribution_last_click,
    check_ad_rotation,
)

# N1.0a: полный реестр эмитируемых проверок check_id → (family, severity) — входит в версию
# score-модели (правка семьи/severity чека тоже сдвигает измерение!). Дрейф с телами проверок
# ловит tests/test_audit_engine.py (regex по исходнику): новый чек ОБЯЗАН попасть и сюда.
CHECK_REGISTRY: dict[str, tuple[str, str]] = {
    "spend_no_conv": ("waste", "warning"),
    "high_cpa": ("waste", "warning"),
    # Ф8: ТРИ critical на весь реестр — фундамент, а не отдельная кампания. kill_rule: жжём втрое
    # дороже собственной цели. no_conversion_tracking/zero_conversions: без измерения конверсий все
    # прочие числа аудита — гадание. Уровень редкий ОСОЗНАННО: critical у половины чеков = нет уровня.
    "kill_rule": ("waste", "critical"),
    "wasteful_keyword": ("keywords", "warning"),
    "wasteful_search_term": ("keywords", "warning"),
    "low_ctr_ad": ("rsa", "info"),
    "budget_imbalance": ("budget", "info"),
    "single_campaign": ("structure", "info"),
    "no_conversion_tracking": ("conversion_tracking", "critical"),
    "zero_conversions": ("conversion_tracking", "critical"),
    "is_budget_constrained": ("budget", "info"),
    "is_lost_revenue": ("budget", "warning"),
    "is_rank_constrained": ("rsa", "info"),
    "broad_unmanaged": ("bidding", "warning"),
    "duplicate_conversions": ("conversion_tracking", "info"),
    "ads_disapproved": ("delivery", "warning"),
    "zero_impressions": ("delivery", "warning"),
    "adgroup_bloat": ("structure", "info"),
    "rsa_thin": ("rsa", "info"),
    # Ф3 (G35/G27/G28/G29/G30/G-AD1). Покрытие ключей — единственный warning из RSA-семьи: это не
    # косметика, а релевантность → Quality Score → цена клика. Остальное — info (гигиена текстов).
    "rsa_keyword_coverage_low": ("rsa", "warning"),
    "rsa_headlines_thin": ("rsa", "info"),
    "rsa_descriptions_thin": ("rsa", "info"),
    "rsa_ad_strength_poor": ("rsa", "info"),
    "rsa_overpinned": ("rsa", "info"),
    "rsa_stale": ("rsa", "info"),
    "no_negative_list": ("keywords", "info"),
    # Ф4 (майнинг запросов). ngram_waste — warning: слово, жгущее бюджет во МНОГИХ запросах, это
    # система, а не случай. keyword_harvest — info и БЕЗ штрафа (score_intensity=0): возможность,
    # не дефект. Каннибализация/нули — info: структура, деньгами напрямую не измеряется.
    "keyword_harvest": ("keywords", "info"),
    "ngram_waste": ("keywords", "warning"),
    "keyword_cannibalization": ("keywords", "info"),
    "zero_impression_keywords": ("keywords", "info"),
    "qs_low": ("rsa", "info"),
    "qs_ctr_below": ("rsa", "info"),
    "qs_relevance_below": ("rsa", "info"),
    "qs_landing_below": ("rsa", "info"),
    "manual_bid_high_vol": ("bidding", "info"),
    # Ф6 (G37/G05/G50-52). Задушенная цель CPA и смесь бренд/не-бренд — warning: обе тихо ломают
    # экономику кампании. Семья «assets» с Ф8 весит 3 (была 0): расширения — не деньги, а упущенный
    # CTR, поэтому вес мал; аккаунт без расширений находок не получает вовсе (GR8) и не штрафуется.
    "target_cpa_too_low": ("bidding", "warning"),
    "brand_nonbrand_mixed": ("structure", "warning"),
    "assets_sitelinks_thin": ("assets", "info"),
    "assets_callouts_thin": ("assets", "info"),
    "assets_no_snippets": ("assets", "info"),
    # Ф6.2 (G12/G11). Оба — ДЕНЕЖНЫЕ (at_risk = реально сожжённый расход сегмента), поэтому warning
    # и живые семьи: КМС на поиске — «waste», «интерес» вместо «присутствия» — «geo».
    "display_on_search_campaign": ("waste", "warning"),
    "geo_interest_waste": ("geo", "warning"),
    "bid_below_first_page": ("bidding", "warning"),
    "bid_below_top_of_page": ("bidding", "info"),
    "top_is_rank_lost": ("bidding", "info"),
    # Ф5а: семья «competition» НАМЕРЕННО вне FAMILY_WEIGHT (вес 0) — потерю по рангу/бюджету уже
    # штрафуют is_rank_constrained/is_budget_constrained/is_lost_revenue. Агрегат — карточка-диагноз.
    "competitive_pressure": ("competition", "info"),
    "sim_bid_upside": ("bidding", "info"),
    "sim_budget_upside": ("budget", "info"),
    # Ф2: семья «recommendations» НАМЕРЕННО отсутствует в FAMILY_WEIGHT (вес 0) — мнение Google
    # показываем, но нашим баллом не штрафуем (см. check_google_recommendations).
    "google_recommendations_pending": ("recommendations", "info"),
    "geo_no_conv": ("geo", "warning"),
    "schedule_waste": ("geo", "info"),
    # Ф7 (PMax). ДЕНЕЖНЫЕ два — и они живут в СВОИХ семьях (waste/keywords), потому что деньги горят
    # одинаково в любом канале. Остальные пять — конфигурация PMax: семья «pmax», с Ф8 вес 3 (была 0).
    # Вес мал ОСОЗНАННО: это info-находки о настройке, а не сожжённые деньги; аккаунт без PMax находок
    # не получает (GR8) ⇒ и не штрафуется — «pmax» появляется в карточке только там, где PMax есть.
    # Каннибализация бренда — «structure» и warning: это ровно тот же дефект, что brand_nonbrand_mixed.
    "pmax_search_term_waste": ("keywords", "warning"),
    "pmax_asset_group_no_conv": ("waste", "warning"),
    "pmax_brand_cannibalization": ("structure", "warning"),
    "pmax_ad_strength_poor": ("pmax", "info"),
    "pmax_no_video": ("pmax", "info"),
    "pmax_no_signals": ("pmax", "info"),
    "pmax_no_negatives": ("pmax", "info"),
    "pmax_insufficient_conversions": ("pmax", "info"),
    # Ф9 (2026-07-17, индустриальные чек-листы). Двое — warning: разрыв по устройствам жжёт
    # реальные деньги (ветка zero_conv кладёт расход устройства в at_risk), конфликт минусов
    # молча душит собственный трафик. Остальные — info-конфигурация. Авто-применение — семья
    # recommendations (вне score: выбор владельца, не дефект кампаний). SCORE_MODEL_EPOCH не
    # бампаем: новые check_id в этом реестре + новые пороги хэш видит сам (версия ротируется,
    # тренд честно «н/д» один раз — ровно как Ф8).
    "device_performance_gap": ("bidding", "warning"),
    "negative_keyword_conflicts": ("keywords", "warning"),
    "no_conversion_value": ("conversion_tracking", "info"),
    "auto_apply_recommendations_on": ("recommendations", "info"),
    "attribution_model_last_click": ("conversion_tracking", "info"),
    "ad_rotation_rotate_forever": ("rsa", "info"),
}
CHECK_IDS = frozenset(CHECK_REGISTRY)


def compute_score_model_version(
    *,
    family_weight: dict | None = None,
    severity_mult: dict | None = None,
    nonmoney_intensity: float | None = None,
    grade_bands=None,
    thresholds: dict | None = None,
    check_registry: dict | None = None,
    epoch: int | None = None,
) -> str:
    """N1.0a: стабильная версия score-модели — sha256 (12 hex) от ВСЕХ констант измерения: весов
    семей, множителей severity, порогов, бэндов, реестра проверок (check_id → family/severity) и
    ручной эпохи (SCORE_MODEL_EPOCH — бампается при правке СЕМАНТИКИ тел проверок, которую хэш не
    видит). Любая правка модели → новая версия → тренды между версиями честно «н/д», а не ложная
    дельта (outward-facing деньги). Kwargs — только для тестов чувствительности."""
    reg = check_registry if check_registry is not None else CHECK_REGISTRY
    payload = {
        "family_weight": family_weight if family_weight is not None else FAMILY_WEIGHT,
        "severity_mult": severity_mult if severity_mult is not None else SEVERITY_MULT,
        "nonmoney_intensity": (
            nonmoney_intensity if nonmoney_intensity is not None else NONMONEY_INTENSITY
        ),
        "grade_bands": [list(b) for b in (grade_bands if grade_bands is not None else GRADE_BANDS)],
        "thresholds": thresholds if thresholds is not None else DEFAULT_AUDIT_THRESHOLDS,
        "check_registry": {k: list(v) for k, v in sorted(reg.items())},
        "epoch": epoch if epoch is not None else SCORE_MODEL_EPOCH,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


# Константы модели не меняются в рантайме → версия вычисляется один раз при импорте.
SCORE_MODEL_VERSION = compute_score_model_version()


def _intensity(f: Finding, total_spend: float) -> float:
    """Интенсивность находки для штрафа: денежная → доля at_risk/расход (0..1); явный score_intensity
    (упущенная выгода, at_risk=0) → он; иначе — фикс NONMONEY_INTENSITY."""
    if f.at_risk > 0 and total_spend > 0:
        return max(0.0, min(1.0, f.at_risk / total_spend))
    if f.score_intensity is not None:
        return max(0.0, min(1.0, f.score_intensity))
    return NONMONEY_INTENSITY


def _dedup_at_risk(
    findings: list[Finding], total_spend: float, campaign_cost: dict | None = None
) -> float:
    """Деньги-под-риском без двойного счёта (крит.C2), в три шага:
    1) по (кампания, сегмент) → MAX (одинаковые сегменты не суммируются);
    2) по КАМПАНИИ → сумма её сегментов, но CAP по СОБСТВЕННОМУ расходу кампании — расход ключа ⊂
       расход кампании, поэтому «слив кампании» + «дорогой ключ той же кампании» не плюсуются поверх
       (иначе headline завышался, ревью 2026-07-08); независимые ключи внутри капа по-прежнему суммируются;
    3) сумма кампаний → CAP по общему расходу. campaign_cost={имя: расход}; None → шаг-2 CAP пропускаем."""
    seg: dict[tuple, float] = {}
    for f in findings:
        if f.at_risk <= 0:
            continue
        key = (f.target_campaign or "", f.spend_segment or f.check_id)
        seg[key] = max(seg.get(key, 0.0), f.at_risk)
    per_camp: dict[str, float] = {}
    for (camp, _sg), v in seg.items():
        per_camp[camp] = per_camp.get(camp, 0.0) + v
    if campaign_cost:
        for camp, v in list(per_camp.items()):
            cc = campaign_cost.get(camp)
            if cc is not None:
                per_camp[camp] = min(v, round(float(cc), 2))
    total = sum(per_camp.values())
    if total_spend > 0:
        total = min(total, total_spend)
    return round(total, 2)


def build_audit(
    report,
    thresholds: dict | None = None,
    target_cpa: float | None = None,
    *,
    is_rows: list | None = None,
    conversion_actions: list | None = None,
    bidding: list | None = None,
    optimization_score=None,
    recommendations: list | None = None,
    search_terms: list | None = None,
    ad_policy: list | None = None,
    account_status: str | None = None,
    data_gaps: list | None = None,
    adgroup_structure: list | None = None,
    negative_lists: object | None = None,
    keyword_quality: list | None = None,
    geo_waste: list | None = None,
    schedule: list | None = None,
    bid_landscape: list | None = None,
    bid_simulations: list | None = None,
    budget_simulations: list | None = None,
    rsa_ads: list | None = None,
    keyword_inventory: list | None = None,
    keyword_inventory_truncated: bool = False,
    brand_terms: set | None = None,
    campaign_assets: list | None = None,
    campaign_settings: list | None = None,
    pmax_campaigns: list | None = None,
    pmax_asset_groups: list | None = None,
    device_performance: list | None = None,
    negative_keywords: object | None = None,
    recommendation_subscriptions: list | None = None,
    ad_rotation: list | None = None,
) -> AuditResult:
    """Собрать аудит по УЖЕ прочитанным данным. Чистая функция (без сети/SDK). Опц. живые данные
    (is_rows / conversion_actions / bidding / optimization_score / recommendations) — duck-typed,
    None → проверка деградирует мягко. Пустой аккаунт → score None / grade «—» (не фейковые 100).
    score — только КОД. Google-рекомендации/optimization_score — «второе мнение», НЕ в нашем score."""
    thr = {**DEFAULT_AUDIT_THRESHOLDS, **(thresholds or {})}
    totals = report.totals
    total_spend = float(getattr(totals, "cost", 0.0) or 0.0)
    currency = getattr(report, "currency", "") or ""
    cid = str(getattr(report, "customer_id", "") or "")

    # Нативный Google-балл (0..1 → 0..100) — «второе мнение», НЕ в нашем score.
    opt_score = opt_uplift = None
    if optimization_score is not None:
        opt_score = round(float(getattr(optimization_score, "score", 0.0)) * 100)
        opt_uplift = round(float(getattr(optimization_score, "uplift", 0.0)) * 100)
    # Активные Google-рекомендации (тип → число) — показ, не применяем.
    grec: dict[str, int] = {}
    for r in recommendations or []:
        if getattr(r, "dismissed", False):
            continue
        t = str(getattr(r, "type", "") or "")
        if t and t not in ("UNSPECIFIED", "UNKNOWN"):
            grec[t] = grec.get(t, 0) + 1

    # Heartbeat: аккаунт приостановлен/отменён/закрыт → баннер-катастрофа (нужен и на мёртвом
    # аккаунте: suspension объясняет отсутствие активности). Иначе None (активен / статус не прочитан).
    acct_status = account_status if account_status in ("SUSPENDED", "CANCELED", "CLOSED") else None

    # Провенанс ctx (см. AuditResult.ctx_signals): что нам подали, а не что collect пытался прочитать.
    # Словарь имён — тот же, что у data_gaps (audit/collect.py); `[]` считается ПОДАННЫМ («прочитано,
    # строк нет» ≠ «не прочитано»), как и там. Симуляторы/brand_terms/target_cpa — не сигналы семей.
    ctx_signals = frozenset(
        name
        for name, val in (
            ("impression_share", is_rows),
            ("optimization_score", optimization_score),
            ("conversion_actions", conversion_actions),
            ("bidding", bidding),
            ("recommendations", recommendations),
            ("search_terms", search_terms),
            ("ad_policy", ad_policy),
            ("adgroup_structure", adgroup_structure),
            ("negative_lists", negative_lists),
            ("keyword_quality", keyword_quality),
            ("geo_waste", geo_waste),
            ("schedule", schedule),
            ("bid_landscape", bid_landscape),
            ("rsa_ads", rsa_ads),
            ("keyword_inventory", keyword_inventory),
            ("campaign_assets", campaign_assets),
            ("campaign_settings", campaign_settings),
            ("device_performance", device_performance),
            ("negative_keywords", negative_keywords),
            ("recommendation_subscriptions", recommendation_subscriptions),
            ("ad_rotation", ad_rotation),
        )
        if val is not None
    )
    # Ф7: два чтения — один сигнал (как в data_gaps: семья pmax слепа, если не прочитано ЛЮБОЕ из них).
    if pmax_campaigns is not None and pmax_asset_groups is not None:
        ctx_signals |= {"pmax"}

    impressions = float(getattr(totals, "impressions", 0) or 0)
    has_activity = impressions > 0 or total_spend > 0
    if not has_activity:
        # `grec` заполнен, а findings пуст — не рассинхрон: чеки на мёртвом аккаунте не гоняются
        # вовсе, но рекомендации Google прочитаны и отдаются как есть (в конверте — extra).
        return AuditResult(
            cid,
            currency,
            None,
            "—",
            0.0,
            0.0,
            [],
            {},
            False,
            opt_score,
            opt_uplift,
            grec,
            score_model_version=SCORE_MODEL_VERSION,
            data_gaps=data_gaps,
            account_status=acct_status,
            ctx_signals=ctx_signals,
        )

    campaign_cost = {name: m.cost for (name, _status), m in _campaign_rows(report)}
    bidding_by_name = {getattr(b, "name", ""): b for b in (bidding or [])}
    ctx = _Ctx(
        target_cpa=target_cpa,
        is_rows=is_rows,
        conversion_actions=conversion_actions,
        # "conversion_actions" ∈ data_gaps ⇒ collect ПЫТАЛСЯ прочитать действия-конверсии и упал (/audit,
        # случай пользователя). data_gaps=None (engine-only) или сигнала там нет ⇒ False.
        conversion_read_failed=("conversion_actions" in (data_gaps or ())),
        search_terms=search_terms,
        bidding_by_name=bidding_by_name,
        ad_policy=ad_policy,
        adgroup_structure=adgroup_structure,
        negative_lists=negative_lists,
        keyword_quality=keyword_quality,
        geo_waste=geo_waste,
        schedule=schedule,
        bid_landscape=bid_landscape,
        bid_simulations=bid_simulations,
        budget_simulations=budget_simulations,
        recommendations=recommendations,
        rsa_ads=rsa_ads,
        keyword_inventory=keyword_inventory,
        keyword_inventory_truncated=keyword_inventory_truncated,
        brand_terms=brand_terms,
        campaign_assets=campaign_assets,
        campaign_settings=campaign_settings,
        pmax_campaigns=pmax_campaigns,
        pmax_asset_groups=pmax_asset_groups,
        device_performance=device_performance,
        negative_keywords=negative_keywords,
        recommendation_subscriptions=recommendation_subscriptions,
        ad_rotation=ad_rotation,
    )

    # N1.3 (ревью): IS-строки прочитаны, но НИ ОДНА не прошла tolerance-гейт (proto3-нули) —
    # check_impression_share промолчит «нет данных», и без этой пометки рендер показал бы
    # budget/rsa «в норме». Пробел честнее: сигнал есть формально, данных в нём нет.
    if data_gaps is not None and is_rows and "impression_share" not in data_gaps:
        tol = thr.get("is_data_tolerance", 0.02)
        usable = any(
            getattr(r, "channel_type", "SEARCH") in ("SEARCH", "SHOPPING")
            and 1.0 - tol
            <= (
                float(getattr(r, "search_is", 0.0) or 0.0)
                + float(getattr(r, "budget_lost_is", 0.0) or 0.0)
                + float(getattr(r, "rank_lost_is", 0.0) or 0.0)
            )
            <= 1.0 + tol
            for r in is_rows
        )
        if not usable:
            data_gaps = [*data_gaps, "impression_share"]

    # N1.5: разрыв измерения — расход есть, а активной PRIMARY-конверсии нет: Smart Bidding и все
    # числа ниже опираются на неполные данные. Только при ЖИВЫХ conversion_actions (None → data gap).
    measurement_gap = False
    if conversion_actions is not None and total_spend >= thr["no_conv_min_spend"]:
        measurement_gap = not any(
            getattr(c, "status", "") == "ENABLED" and getattr(c, "primary_for_goal", False)
            for c in conversion_actions
        )

    findings: list[Finding] = []
    for check in _CHECKS:
        findings.extend(check(report, thr, ctx))

    by_family: dict[str, list[Finding]] = {}
    for f in findings:
        by_family.setdefault(f.family, []).append(f)

    total_penalty = 0.0
    families: dict[str, dict] = {}
    for fam, fs in by_family.items():
        raw_sum = sum(SEVERITY_MULT.get(f.severity, 0.5) * _intensity(f, total_spend) for f in fs)
        penalty = FAMILY_WEIGHT.get(fam, 0.0) * min(1.0, raw_sum)
        total_penalty += penalty
        families[fam] = {
            "count": len(fs),
            "at_risk": _dedup_at_risk(fs, total_spend, campaign_cost),
            "penalty": round(penalty, 2),
        }

    score = max(0, min(100, round(100.0 - total_penalty)))
    at_risk = _dedup_at_risk(findings, total_spend, campaign_cost)
    # C: суммарная ОЦЕНКА упущенной выгоды (не at_risk!) — из facts находок is_lost_revenue. Отдельная
    # строка карточки «💡 Упущено ~X»; в headline «под риском» НЕ входит (потраченное ≠ недополученное).
    lost_opportunity = round(
        sum(float((f.facts or {}).get("lost_revenue", 0.0) or 0.0) for f in findings), 2
    )
    findings.sort(key=lambda f: (-SEVERITY_MULT.get(f.severity, 0.0), -f.at_risk, not f.one_tap))

    return AuditResult(
        customer_id=cid,
        currency=currency,
        score=score,
        grade=grade_for(score),
        total_spend=round(total_spend, 2),
        at_risk=at_risk,
        findings=findings,
        families=families,
        has_activity=True,
        optimization_score=opt_score,
        optimization_uplift=opt_uplift,
        google_recommendations=grec,
        score_model_version=SCORE_MODEL_VERSION,
        data_gaps=data_gaps,
        measurement_gap=measurement_gap,
        account_status=acct_status,
        lost_opportunity=lost_opportunity,
        ctx_signals=ctx_signals,
    )
