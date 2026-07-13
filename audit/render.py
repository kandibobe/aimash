"""Детерминированный рендер карточки аудита (fallback, когда агентный нарратив недоступен/отклонён).

Чистый текст, без зависимостей от bot/SDK — кормится ТОЛЬКО AuditResult (числа из КОДА). Bot-слой
позже даёт HTML-версию + i18n-ключи + клавиатуры; здесь — минимальная честная карточка на RU/EN,
которая всегда доступна offline. Score/grade — вне любого парафраза (крит.S6)."""

from __future__ import annotations

from audit.engine import AuditResult, Finding

_FAMILY_LABEL = {
    "ru": {
        "waste": "Слив бюджета",
        "conversion_tracking": "Отслеживание конверсий",
        "budget": "Бюджеты и охват",
        "bidding": "Ставки",
        "keywords": "Ключевые слова",
        "rsa": "Тексты и качество",
        "structure": "Структура",
        "delivery": "Показ и модерация",
        "geo": "Гео и таргетинг",
        "assets": "Расширения",
    },
    "en": {
        "waste": "Wasted spend",
        "conversion_tracking": "Conversion tracking",
        "budget": "Budgets & reach",
        "bidding": "Bidding",
        "keywords": "Keywords",
        "rsa": "Ads & quality",
        "structure": "Structure",
        "delivery": "Delivery & policy",
        "geo": "Geo & targeting",
        "assets": "Assets",
    },
}

_SEV_EMOJI = {"warning": "❗", "info": "🟡"}

# N1.3: человекочитаемые метки best-effort сигналов collect-слоя (для строки «недостаточно данных»).
_SIGNAL_LABEL = {
    "ru": {
        "impression_share": "доля показов (IS)",
        "conversion_actions": "действия-конверсии",
        "bidding": "стратегии ставок",
        "search_terms": "поисковые запросы",
        "optimization_score": "оценка Google",
        "recommendations": "рекомендации Google",
        "ad_policy": "модерация объявлений",
    },
    "en": {
        "impression_share": "impression share",
        "conversion_actions": "conversion actions",
        "bidding": "bidding strategies",
        "search_terms": "search terms",
        "optimization_score": "Google optimization score",
        "recommendations": "Google recommendations",
        "ad_policy": "ad policy status",
    },
}

# N1.3: семья → доп-сигналы, без которых «✅ в норме» утверждать нечестно (сигнал упал → семья
# не попадает в «в норме», а сигнал показывается строкой «недостаточно данных»).
_FAMILY_SIGNALS = {
    "keywords": ("search_terms", "negative_lists"),
    "budget": ("impression_share",),
    "rsa": ("impression_share", "adgroup_structure", "keyword_quality"),
    "conversion_tracking": ("conversion_actions",),
    "bidding": ("bidding",),
    "delivery": (
        "ad_policy",
    ),  # упал ad_policy → delivery не «в норме» (zero_impressions — из отчёта)
    "structure": ("adgroup_structure",),  # D: без структуры групп не утверждаем «структура в норме»
    "geo": ("geo_waste",),  # A: без geographic_view не утверждаем «гео в норме»
}

# Семьи с реализованными проверками (assets — задел: про неё не утверждаем ни «в норме», ни «нет
# данных» — проверок ещё нет). geo стала реализованной (geo_no_conv/schedule_waste, A).
_IMPLEMENTED_FAMILIES = (
    "waste",
    "conversion_tracking",
    "budget",
    "bidding",
    "keywords",
    "rsa",
    "structure",
    "delivery",
    "geo",
)


def _money(v: float, cur: str) -> str:
    s = f"{v:,.2f}".replace(",", " ")
    return f"{s} {cur}" if cur else s


def _finding_line(f: Finding, lang: str, cur: str) -> str:
    """Короткая строка находки для «топ-3 действий» (детерминированная, из facts)."""
    fa = f.facts
    camp = fa.get("campaign", "")
    if f.check_id == "spend_no_conv":
        if lang == "en":
            return f"«{camp}»: spend {_money(fa.get('cost', 0), cur)}, {fa.get('clicks', 0)} clicks, 0 conversions — check keywords/landing page or pause."
        return f"«{camp}»: расход {_money(fa.get('cost', 0), cur)}, {fa.get('clicks', 0)} кликов, 0 конверсий — проверь ключи/посадочную или поставь на паузу."
    if f.check_id == "kill_rule":
        if lang == "en":
            return f"«{camp}»: CPA {_money(fa.get('cpa', 0), cur)} is {fa.get('factor', 0)}× target ({_money(fa.get('target_cpa', 0), cur)}). 3× rule — pause candidate (budget only on your command)."
        return f"«{camp}»: CPA {_money(fa.get('cpa', 0), cur)} — {fa.get('factor', 0)}× цели ({_money(fa.get('target_cpa', 0), cur)}). Правило 3× — кандидат на паузу (бюджет — только по твоей команде)."
    if f.check_id == "high_cpa":
        if lang == "en":
            return f"«{camp}»: CPA {_money(fa.get('cpa', 0), cur)} — {fa.get('factor', 0)}× the account average ({_money(fa.get('acct_cpa', 0), cur)}). Check bids and keywords."
        return f"«{camp}»: CPA {_money(fa.get('cpa', 0), cur)} — в {fa.get('factor', 0)}× выше среднего ({_money(fa.get('acct_cpa', 0), cur)}). Проверь ставки и ключи."
    if f.check_id == "wasteful_keyword":
        kw = fa.get("keyword", "")
        if lang == "en":
            return f"Query «{kw}» in «{camp}»: {_money(fa.get('cost', 0), cur)}, {fa.get('clicks', 0)} clicks, 0 conversions — negative-keyword candidate."
        return f"Запрос «{kw}» в «{camp}»: {_money(fa.get('cost', 0), cur)}, {fa.get('clicks', 0)} кликов, 0 конверсий — кандидат в минус-слова."
    if f.check_id == "wasteful_search_term":
        term = fa.get("search_term", "")
        if lang == "en":
            return f"Search term «{term}» in «{camp}»: {_money(fa.get('cost', 0), cur)}, {fa.get('clicks', 0)} clicks, 0 conversions — add as an exact negative."
        return f"Поисковый запрос «{term}» в «{camp}»: {_money(fa.get('cost', 0), cur)}, {fa.get('clicks', 0)} кликов, 0 конверсий — в минус-слова (точное соответствие)."
    if f.check_id == "no_conversion_tracking":
        if lang == "en":
            return f"Account spends {_money(fa.get('cost', 0), cur)} with no active conversion tracking — set up conversion actions."
        return f"Аккаунт тратит {_money(fa.get('cost', 0), cur)}, но активного отслеживания конверсий нет — настрой цели-конверсии."
    if f.check_id == "zero_conversions":
        if lang == "en":
            return f"Account spends {_money(fa.get('cost', 0), cur)} with tracking on but 0 conversions — check goals/landing page."
        return f"Аккаунт тратит {_money(fa.get('cost', 0), cur)}, отслеживание есть, но 0 конверсий — проверь цели/посадочную."
    if f.check_id == "is_budget_constrained":
        if lang == "en":
            return f"«{camp}»: losing {fa.get('budget_lost', 0)}% of impressions to budget — budget-constrained (change only on your command)."
        return f"«{camp}»: теряешь {fa.get('budget_lost', 0)}% показов из-за бюджета — упирается в бюджет (менять только по твоей команде)."
    if f.check_id == "is_lost_revenue":
        if lang == "en":
            return f"«{camp}»: losing {fa.get('budget_lost', 0)}% of impressions to budget — est. ~{fa.get('lost_conv', 0)} conversions / {_money(fa.get('lost_revenue', 0), cur)} left on the table (raise budget only on your command)."
        return f"«{camp}»: теряешь {fa.get('budget_lost', 0)}% показов из-за бюджета — оценка ~{fa.get('lost_conv', 0)} конв. / {_money(fa.get('lost_revenue', 0), cur)} упущено (бюджет — только по твоей команде)."
    if f.check_id == "is_rank_constrained":
        if lang == "en":
            return f"«{camp}»: losing {fa.get('rank_lost', 0)}% of impressions to rank — improve ad relevance/quality."
        return f"«{camp}»: теряешь {fa.get('rank_lost', 0)}% показов из-за ранга — подними релевантность/качество объявлений."
    if f.check_id == "low_ctr_ad":
        if lang == "en":
            return f"«{camp}»: CTR {fa.get('ctr', 0)}% below account average ({fa.get('acct_ctr', 0)}%) — refresh the ad copy."
        return f"«{camp}»: CTR {fa.get('ctr', 0)}% ниже среднего ({fa.get('acct_ctr', 0)}%) — освежи тексты объявлений."
    if f.check_id == "budget_imbalance":
        if lang == "en":
            return f"«{camp}» takes {fa.get('share', 0)}% of spend without better efficiency — review budget split."
        return f"«{camp}» забирает {fa.get('share', 0)}% расхода без лучшей отдачи — пересмотри распределение бюджета."
    if f.check_id == "single_campaign":
        if lang == "en":
            return f"All traffic in one campaign «{camp}» — consider splitting by theme/geo/match type."
        return f"Весь трафик в одной кампании «{camp}» — рассмотри разделение по темам/гео/типам соответствия."
    if f.check_id == "broad_unmanaged":
        n = fa.get("kw_count", 0)
        strat = fa.get("strategy_type", "")
        if fa.get("reason") == "no_negatives":  # Smart Bidding есть, минус-слов нет ни одного
            if lang == "en":
                return f"«{camp}»: {n} broad-match keywords spent {_money(fa.get('cost', 0), cur)} under Smart Bidding ({strat}) with no negative keywords at all — the algorithm has nothing to cut junk with; add a negative list."
            return f"«{camp}»: {n} BROAD-ключей на {_money(fa.get('cost', 0), cur)} под Smart Bidding ({strat}), но у кампании нет ни одного минус-слова — алгоритму нечем отсекать мусор; заведи список минус-слов."
        if lang == "en":
            return f"«{camp}»: {n} broad-match keywords spent {_money(fa.get('cost', 0), cur)} on {strat} without Smart Bidding — tighten match types or add negatives (needs curation)."
        return f"«{camp}»: {n} BROAD-ключей на {_money(fa.get('cost', 0), cur)} при стратегии {strat} без Smart Bidding — сузь типы соответствия или добавь минус-слова (нужна курация)."
    if f.check_id == "duplicate_conversions":
        cat = fa.get("category", "")
        n = fa.get("count", 0)
        if lang == "en":
            return f"Possible conversion double-counting: {n} enabled primary actions in category {cat} — make sure only one counts into “Conversions”."
        return f"Похоже на двойной счёт конверсий: {n} активных primary-действия категории {cat} — проверь, что в «Конверсии» пишет только одно."
    if f.check_id == "ads_disapproved":
        n = fa.get("count", 0)
        dis = fa.get("disapproved", 0)
        if lang == "en":
            return f"«{camp}»: {n} ads not fully serving ({dis} disapproved) — silently losing traffic; fix the ad/landing page."
        return f"«{camp}»: {n} объявлений не показываются полноценно ({dis} отклонено) — тихо теряешь трафик; почини объявление/посадочную."
    if f.check_id == "zero_impressions":
        if lang == "en":
            return f"«{camp}»: enabled but 0 impressions — not serving (broken ads / too-narrow targeting / empty ad groups)."
        return f"«{camp}»: включена, но 0 показов — не крутится (сломанные объявления / узкий таргет / пустые группы)."
    if f.check_id == "adgroup_bloat":
        n = fa.get("count", 0)
        if lang == "en":
            return f"{n} ad group(s) have more than {fa.get('cap', 20)} keywords (worst «{fa.get('worst_group', '')}»: {fa.get('worst_kw', 0)}) — split by theme so Quality Score isn't diluted."
        return f"{n} групп с числом ключей больше {fa.get('cap', 20)} (худшая «{fa.get('worst_group', '')}»: {fa.get('worst_kw', 0)}) — раздели по темам, иначе размывается релевантность/Quality Score."
    if f.check_id == "rsa_thin":
        n = fa.get("count", 0)
        if lang == "en":
            return f"{n} ad group(s) have fewer than {fa.get('need', 2)} enabled RSAs (worst «{fa.get('worst_group', '')}»: {fa.get('worst_rsa', 0)}) — add responsive search ads for more reach/optimization."
        return f"{n} групп с числом активных RSA меньше {fa.get('need', 2)} (худшая «{fa.get('worst_group', '')}»: {fa.get('worst_rsa', 0)}) — добавь адаптивные поисковые объявления."
    if f.check_id == "no_negative_list":
        if lang == "en":
            return "No negative keywords in the account at all — neither shared lists nor campaign-level ones; basic hygiene missing. Add themed lists (competitor/jobs/free/irrelevant) to cut wasted traffic."
        return "В аккаунте нет минус-слов вообще — ни списков, ни заданных прямо на кампаниях; базовая гигиена не настроена. Заведи тематические списки (конкуренты/работа/бесплатно/нерелевантное), чтобы отсечь мусорный трафик."
    if f.check_id == "qs_low":
        if lang == "en":
            return f"{fa.get('count', 0)} paying keyword(s) with Quality Score ≤ {fa.get('qs_fail', 4)} (worst «{fa.get('worst_kw', '')}»: {fa.get('worst_qs', 0)}) — fix relevance/CTR/landing page or drop them."
        return f"{fa.get('count', 0)} платящих ключей с Quality Score ≤ {fa.get('qs_fail', 4)} (худший «{fa.get('worst_kw', '')}»: {fa.get('worst_qs', 0)}) — подними релевантность/CTR/посадочную или убери."
    if f.check_id == "qs_ctr_below":
        if lang == "en":
            return f"{fa.get('share', 0)}% of keywords have below-average Expected CTR — rewrite ads to match search intent."
        return f"{fa.get('share', 0)}% ключей с ожидаемым CTR ниже среднего — перепиши объявления под интент запроса."
    if f.check_id == "qs_relevance_below":
        if lang == "en":
            return f"{fa.get('share', 0)}% of keywords have below-average Ad Relevance — align ad copy with the keyword themes."
        return f"{fa.get('share', 0)}% ключей с релевантностью объявления ниже среднего — согласуй тексты с темами ключей."
    if f.check_id == "qs_landing_below":
        if lang == "en":
            return f"{fa.get('share', 0)}% of keywords have below-average Landing Page Experience — improve page speed/relevance/mobile UX."
        return f"{fa.get('share', 0)}% ключей со слабым опытом посадочной — улучши скорость/релевантность/мобильную вёрстку страницы."
    if f.check_id == "manual_bid_high_vol":
        if lang == "en":
            return f"«{camp}»: {fa.get('conversions', 0)} conversions on manual bidding ({fa.get('strategy_type', '')}) — Smart Bidding would optimize better at this volume."
        return f"«{camp}»: {fa.get('conversions', 0)} конверсий на ручной стратегии ({fa.get('strategy_type', '')}) — при таком объёме Smart Bidding отработает лучше."
    if f.check_id == "geo_no_conv":
        reg = fa.get("region", "")
        if lang == "en":
            return f"«{camp}» / {reg}: {_money(fa.get('cost', 0), cur)}, {fa.get('clicks', 0)} clicks, 0 conversions — exclude or bid down this location."
        return f"«{camp}» / {reg}: {_money(fa.get('cost', 0), cur)}, {fa.get('clicks', 0)} кликов, 0 конверсий — исключи локацию или срежь ставку."
    if f.check_id == "schedule_waste":
        if lang == "en":
            return f"{fa.get('count', 0)} time slot(s) spend {_money(fa.get('cost', 0), cur)} with 0 conversions (worst {fa.get('worst_day', '')} @ {fa.get('worst_hour', 0)}h) — adjust ad schedule/bids by time."
        return f"{fa.get('count', 0)} временных ячеек тратят {_money(fa.get('cost', 0), cur)} при 0 конверсий (худшая {fa.get('worst_day', '')} в {fa.get('worst_hour', 0)}ч) — настрой расписание/ставки по времени."
    return camp or f.check_id


def finding_text(f: Finding, lang: str, cur: str) -> str:
    """Публичная строка одной находки (для per-finding сообщений bot-слоя с кнопкой «применить»)."""
    return _finding_line(f, lang, cur)


def audit_headline(result: AuditResult, lang: str = "ru") -> str:
    """Одна строка «здоровья» аккаунта для префикса /report (engine-only, без доп-чтений, крит-фикс C11).
    Пусто → нет активности (звать /audit не на чем)."""
    if not result.has_activity or result.score is None:
        return ""
    cur = result.currency
    if lang == "en":
        s = f"🩺 Health: {result.score}/100 · {result.grade}"
        if result.at_risk > 0:
            s += f" · at risk {_money(result.at_risk, cur)}"
        return s + " · /audit"
    s = f"🩺 Здоровье: {result.score}/100 · {result.grade}"
    if result.at_risk > 0:
        s += f" · под риском {_money(result.at_risk, cur)}"
    return s + " · /audit"


def render_audit(
    result: AuditResult,
    lang: str = "ru",
    *,
    actions: bool = True,
    period_label: str | None = None,
    momentary: bool = False,
) -> str:
    """Собрать карточку аудита из AuditResult. actions=True → самодостаточная (топ-3 + дисклеймер);
    actions=False → ОБЗОР (score + семьи + Google-балл) без топ-3/дисклеймера — действия шлёт bot-слой
    отдельными сообщениями с кнопками «применить». period_label — подпись выбранного периода в заголовке;
    momentary=True (аудит за произвольный ИСТОРИЧЕСКИЙ период) → баннер, что моментальные сигналы (Google-
    балл/рекомендации/статус/модерация/ставки/конверсии) — на СЕЙЧАС, не за период. Всегда offline."""
    lang = "en" if lang == "en" else "ru"
    cur = result.currency
    labels = _FAMILY_LABEL[lang]
    lines: list[str] = []

    _period = f" · {period_label}" if period_label else ""
    if lang == "en":
        lines.append(f"🩺 Audit · Account {result.customer_id}{_period}")
    else:
        lines.append(f"🩺 Аудит · Аккаунт {result.customer_id}{_period}")

    # Heartbeat: аккаунт приостановлен/отменён/закрыт — катастрофа, перекрывает всё ниже (в т.ч.
    # «нет активности» и «почини измерение»). Ставим ПЕРВЫМ, до раннего no-activity выхода.
    if result.account_status:
        st = result.account_status
        lines.append(
            f"⛔ Account {st}: ads are not serving — this overrides everything below."
            if lang == "en"
            else f"⛔ Аккаунт {st}: показы остановлены — это перекрывает всё ниже."
        )

    if not result.has_activity or result.score is None:
        lines.append("—")
        lines.append("No activity in this period." if lang == "en" else "Нет активности за период.")
        return "\n".join(lines)

    # N1.5: разрыв измерения — HEADLINE НАД score (score не подавляем: честность, не алармизм).
    if result.measurement_gap:
        lines.append(
            "⚠️ Fix measurement first: no enabled primary conversion action — the numbers below are incomplete."
            if lang == "en"
            else "⚠️ Сначала почини измерение: нет активной primary-конверсии — числа ниже неполные."
        )
    lines.append(f"{result.score}/100 · {result.grade}")
    # Аудит за произвольный исторический период: моментальные сигналы (конфигурация «на сейчас») не
    # относятся к выбранным датам — честно помечаем, чтобы клиент не принял их за состояние периода.
    if momentary:
        lines.append(
            "ℹ️ Google score, recommendations, account status, ad approvals, bid strategies and "
            "conversion health reflect the account NOW, not the selected period."
            if lang == "en"
            else "ℹ️ Google-балл, рекомендации, статус аккаунта, модерация, стратегии ставок и "
            "здоровье конверсий — на СЕЙЧАС, не за выбранный период."
        )
    if result.optimization_score is not None:
        up = result.optimization_uplift or 0
        if lang == "en":
            lines.append(
                f"🔵 Google optimization score: {result.optimization_score}/100 (+{up} if all applied)"
            )
        else:
            lines.append(
                f"🔵 Оценка Google: {result.optimization_score}/100 (+{up}, если применить все)"
            )
    if result.google_recommendations:
        top = sorted(result.google_recommendations.items(), key=lambda kv: -kv[1])[:3]
        names = ", ".join(t.replace("_", " ").title() for t, _ in top)
        # Только показ: применять Google-рекомендации — вне объёма (при надобности через confirm-гейт).
        lines.append(
            f"🔵 Google recommends: {names}" if lang == "en" else f"🔵 Google советует: {names}"
        )
    if result.at_risk > 0:
        if lang == "en":
            lines.append(
                f"💸 At risk: {_money(result.at_risk, cur)} of {_money(result.total_spend, cur)} spend"
            )
        else:
            lines.append(
                f"💸 Под риском: {_money(result.at_risk, cur)} из {_money(result.total_spend, cur)} расхода"
            )
    # C: упущенная выгода — ОЦЕНКА (не потраченное). Отдельно от «под риском», явно помечена как оценка.
    if getattr(result, "lost_opportunity", 0.0) > 0:
        if lang == "en":
            lines.append(
                f"💡 Est. missed: ~{_money(result.lost_opportunity, cur)} in revenue lost to budget caps (estimate)"
            )
        else:
            lines.append(
                f"💡 Упущено (оценка): ~{_money(result.lost_opportunity, cur)} выручки из-за упора в бюджет"
            )

    # Семьи с находками — по штрафу убыв.
    fam_items = sorted(result.families.items(), key=lambda kv: -kv[1]["penalty"])
    if fam_items:
        lines.append("")
        lines.append(
            "What matters (worst first):" if lang == "en" else "Что важно (худшее сверху):"
        )
        for fam, info in fam_items:
            label = labels.get(fam, fam)
            n = info["count"]
            noun = ("findings" if n != 1 else "finding") if lang == "en" else "находки"
            money = info["at_risk"]
            if money > 0:
                lines.append(f"{_family_emoji(fam)} {label} — {n} {noun} · ~{_money(money, cur)}")
            else:
                lines.append(f"{_family_emoji(fam)} {label} — {n} {noun}")

    # N1.3: «нет данных» ≠ «в норме» (GR8). Только когда collect-слой ЯВНО отчитался о сигналах
    # (data_gaps is not None) — engine-only вызовы (/report health) семьи не комментируют вовсе.
    # «✅ в норме» — лишь семьи БЕЗ находок, чьи доп-сигналы получены; упавшие сигналы — отдельной
    # строкой ℹ️ без штрафа score.
    if result.data_gaps is not None:
        gaps = set(result.data_gaps)
        ok_fams = [
            f
            for f in _IMPLEMENTED_FAMILIES
            if f not in result.families and not (set(_FAMILY_SIGNALS.get(f, ())) & gaps)
        ]
        if ok_fams or gaps:
            lines.append("")
            if ok_fams:
                # Префиксная форма: лейблы семей проблемно-ориентированы («Слив бюджета»),
                # «{лейбл} — в норме» читался бы наоборот (ревью 2026-07-08).
                names = " · ".join(labels.get(f, f) for f in ok_fams)
                lines.append(
                    f"✅ Checked, no issues: {names}"
                    if lang == "en"
                    else f"✅ Проверено, проблем нет: {names}"
                )
            if gaps:
                slabels = _SIGNAL_LABEL[lang]
                names = ", ".join(slabels.get(s, s) for s in sorted(gaps))
                lines.append(
                    f"ℹ️ Not enough data: {names} (no score impact)"
                    if lang == "en"
                    else f"ℹ️ Недостаточно данных: {names} (на score не влияет)"
                )

    # Топ-3 действия + дисклеймер — только в самодостаточной карточке (actions=True). При actions=False
    # это ОБЗОР: действия идут отдельными сообщениями с кнопками (bot-слой), дисклеймер шлётся там же.
    if actions:
        top = result.findings[:3]
        if top:
            lines.append("")
            lines.append("Do this now (top 3):" if lang == "en" else "Что сделать сейчас (топ-3):")
            for i, f in enumerate(top, 1):
                lines.append(
                    f"{i}. {_SEV_EMOJI.get(f.severity, '•')} {_finding_line(f, lang, cur)}"
                )
        lines.append("")
        lines.append(
            "These are suggestions — I don't change anything myself. Decide and give the command."
            if lang == "en"
            else "Это подсказки — сам я ничего не меняю. Реши и дай команду."
        )
    return "\n".join(lines)


def _family_emoji(family: str) -> str:
    return {
        "waste": "❗",
        "conversion_tracking": "🔴",
        "budget": "💰",
        "keywords": "🔑",
        "rsa": "🟡",
        "structure": "🧱",
        "bidding": "🎯",
        "delivery": "🚫",
        "geo": "📍",
        "assets": "🔗",
    }.get(family, "•")
