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
        "bid_landscape": "ставки и оценки позиций",
    },
    "en": {
        "impression_share": "impression share",
        "conversion_actions": "conversion actions",
        "bidding": "bidding strategies",
        "search_terms": "search terms",
        "optimization_score": "Google optimization score",
        "recommendations": "Google recommendations",
        "ad_policy": "ad policy status",
        "bid_landscape": "bids & position estimates",
    },
}

# N1.3: семья → доп-сигналы, без которых «✅ в норме» утверждать нечестно (сигнал упал → семья
# не попадает в «в норме», а сигнал показывается строкой «недостаточно данных»).
_FAMILY_SIGNALS = {
    "keywords": ("search_terms", "negative_lists"),
    "budget": ("impression_share",),
    "rsa": ("impression_share", "adgroup_structure", "keyword_quality"),
    "conversion_tracking": ("conversion_actions",),
    # Ф1: без слоя ставок/позиций (bid_landscape) «ставки в норме» утверждать нечестно — именно там
    # живут «ставка ниже оценки Google» и «верх потерян по рангу».
    "bidding": ("bidding", "bid_landscape"),
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
        # КЛЮЧ (не поисковый запрос — тот у wasteful_search_term) + его тип соответствия: без типа
        # «ключ „ремонт“» неоднозначен (broad и exact «ремонт» — разные строки с разной ценой), а
        # минус one-tap зеркалит именно тип (см. audit.engine.check_wasteful_keyword).
        kw = fa.get("keyword", "")
        mt = str(fa.get("match_type", "") or "").lower()
        mt_s = f" ({mt})" if mt else ""
        if lang == "en":
            return f"Keyword «{kw}»{mt_s} in «{camp}»: {_money(fa.get('cost', 0), cur)}, {fa.get('clicks', 0)} clicks, 0 conversions — negative-keyword candidate."
        return f"Ключ «{kw}»{mt_s} в «{camp}»: {_money(fa.get('cost', 0), cur)}, {fa.get('clicks', 0)} кликов, 0 конверсий — кандидат в минус-слова."
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
        # Группа обязательна: без неё «CTR ниже среднего в кампании X» некуда применить — в кампании
        # десятки групп, а чинить надо ОДНУ (RSA живёт на уровне группы).
        ag = fa.get("ad_group", "")
        where = f"«{camp}» / {ag}" if ag else f"«{camp}»"
        if lang == "en":
            return f"An ad in {where}: CTR {fa.get('ctr', 0)}% below account average ({fa.get('acct_ctr', 0)}%) — refresh the ad copy."
        return f"Объявление в {where}: CTR {fa.get('ctr', 0)}% ниже среднего ({fa.get('acct_ctr', 0)}%) — освежи тексты объявлений."
    if f.check_id == "budget_imbalance":
        # CPA + оговорка правила #3 — НЕ косметика: строка предлагает тронуть бюджет, и бот обязан
        # сказать, что сам он его не тронет (гард test_audit_engine.test_finding_text_parity_*).
        cpa = _money(fa.get("cpa", 0), cur)
        if lang == "en":
            return f"«{camp}» takes {fa.get('share', 0)}% of spend at a worse-than-average CPA {cpa} — review budget split (I change budgets only on your direct command)."
        return f"«{camp}» забирает {fa.get('share', 0)}% расхода при CPA {cpa} хуже среднего — пересмотри распределение бюджета (бюджет меняю только по твоей прямой команде)."
    if f.check_id == "single_campaign":
        cost = _money(fa.get("cost", 0), cur)
        if lang == "en":
            return f"All traffic in one campaign «{camp}» ({cost}) — consider splitting by theme/geo/match type."
        return f"Весь трафик в одной кампании «{camp}» ({cost}) — рассмотри разделение по темам/гео/типам соответствия."
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
    if f.check_id == "rsa_keyword_coverage_low":
        ag = fa.get("ad_group", "")
        where = f"«{camp}» / {ag}" if ag else f"«{camp}»"
        missing = ", ".join(f"«{k}»" for k in (fa.get("missing") or [])[:3])
        if lang == "en":
            s = f"{fa.get('count', 0)} serving ad(s) barely use their own keywords in headlines (worst {where}: {fa.get('worst_pct', 0)}% of headlines, target ≥{fa.get('min_pct', 50)}%)"
            s += f" — missing: {missing}." if missing else "."
            return s + " Keywords in headlines lift relevance → Quality Score → cheaper clicks."
        s = f"{fa.get('count', 0)} работающих объявлений почти не используют свои же ключи в заголовках (худшее {where}: {fa.get('worst_pct', 0)}% заголовков при норме ≥{fa.get('min_pct', 50)}%)"
        s += f" — не хватает: {missing}." if missing else "."
        return s + " Ключ в заголовке = релевантность → Quality Score → дешевле клик."
    if f.check_id == "rsa_headlines_thin":
        ag = fa.get("ad_group", "")
        where = f"«{camp}» / {ag}" if ag else f"«{camp}»"
        if lang == "en":
            return f"{fa.get('count', 0)} ad(s) have fewer than {fa.get('need', 8)} headlines (worst {where}: {fa.get('worst_n', 0)}) — Google has little to combine; aim for 12–15."
        return f"{fa.get('count', 0)} объявлений с числом заголовков меньше {fa.get('need', 8)} (худшее {where}: {fa.get('worst_n', 0)}) — Google нечего комбинировать; норма 12–15."
    if f.check_id == "rsa_descriptions_thin":
        ag = fa.get("ad_group", "")
        where = f"«{camp}» / {ag}" if ag else f"«{camp}»"
        if lang == "en":
            return f"{fa.get('count', 0)} ad(s) have fewer than {fa.get('need', 3)} descriptions (worst {where}: {fa.get('worst_n', 0)}) — add up to 4."
        return f"{fa.get('count', 0)} объявлений с числом описаний меньше {fa.get('need', 3)} (худшее {where}: {fa.get('worst_n', 0)}) — доведи до 4."
    if f.check_id == "rsa_ad_strength_poor":
        ag = fa.get("ad_group", "")
        where = f"«{camp}» / {ag}" if ag else f"«{camp}»"
        if lang == "en":
            return f"{fa.get('count', 0)} serving ad(s) rated POOR by Google's own Ad Strength (worst {where}) — more distinct headlines, keywords in them, fewer pins."
        return f"{fa.get('count', 0)} работающих объявлений Google сам оценил как POOR (Ad Strength; худшее {where}) — больше разных заголовков, ключи в них, меньше пинов."
    if f.check_id == "rsa_overpinned":
        ag = fa.get("ad_group", "")
        where = f"«{camp}» / {ag}" if ag else f"«{camp}»"
        if lang == "en":
            return f"{fa.get('count', 0)} ad(s) pin all three headline positions (worst {where}: {fa.get('pinned', 0)} pinned) — rotation is dead, Google can't match the query. Unpin all but the mandatory one."
        return f"{fa.get('count', 0)} объявлений с пинами на ВСЕХ трёх позициях заголовка (худшее {where}: {fa.get('pinned', 0)} закреплено) — ротация мертва, Google не подберёт комбинацию под запрос. Оставь пин только там, где он обязателен."
    if f.check_id == "rsa_stale":
        ag = fa.get("ad_group", "")
        where = f"«{camp}» / {ag}" if ag else f"«{camp}»"
        if lang == "en":
            return f"{fa.get('count', 0)} ad group(s) haven't had a new ad in over {fa.get('days', 90)} days (worst {where}: {fa.get('worst_days', 0)} days) — copy fatigue; add a fresh RSA to test."
        return f"{fa.get('count', 0)} групп без новых объявлений дольше {fa.get('days', 90)} дней (худшая {where}: {fa.get('worst_days', 0)} дн.) — тексты выгорают; добавь свежий RSA на тест."
    if f.check_id == "no_negative_list":
        if lang == "en":
            return "No negative keywords in the account at all — neither shared lists nor campaign-level ones; basic hygiene missing. Add themed lists (competitor/jobs/free/irrelevant) to cut wasted traffic."
        return "В аккаунте нет минус-слов вообще — ни списков, ни заданных прямо на кампаниях; базовая гигиена не настроена. Заведи тематические списки (конкуренты/работа/бесплатно/нерелевантное), чтобы отсечь мусорный трафик."
    if f.check_id == "keyword_harvest":
        terms = fa.get("terms") or []
        cur_ = fa.get("currency") or cur
        top = "; ".join(
            f"«{t.get('term', '')}» — {t.get('conversions', 0)} конв., {_money(t.get('cost', 0), cur_)}"
            for t in terms[:3]
        )
        top_en = "; ".join(
            f"«{t.get('term', '')}» — {t.get('conversions', 0)} conv., {_money(t.get('cost', 0), cur_)}"
            for t in terms[:3]
        )
        if lang == "en":
            return f"{fa.get('count', 0)} search term(s) already convert but have NO keyword of their own: {top_en}. Add them as exact keywords (own bid, own ad copy) instead of paying for them blind via broad match."
        return f"{fa.get('count', 0)} поисковых запросов уже конвертят, но ключа под них НЕТ: {top}. Добавь точным соответствием (своя ставка, свой текст) — сейчас ты платишь за них вслепую через широкое."
    if f.check_id == "ngram_waste":
        words = fa.get("words") or []
        cur_ = fa.get("currency") or cur
        lst = ", ".join(
            f"«{w.get('text', '')}» ({_money(w.get('cost', 0), cur_)}, {w.get('terms', 0)} запр.)"
            for w in words[:5]
        )
        lst_en = ", ".join(
            f"«{w.get('text', '')}» ({_money(w.get('cost', 0), cur_)}, {w.get('terms', 0)} terms)"
            for w in words[:5]
        )
        if lang == "en":
            return f"{fa.get('count', 0)} word(s) systematically burn budget across many search terms with ZERO conversions: {lst_en}. Total {_money(fa.get('cost', 0), cur_)} — add them as phrase negatives (none of them appears in your own keywords)."
        return f"{fa.get('count', 0)} слов системно жгут бюджет в разных запросах и не дали НИ ОДНОЙ конверсии: {lst}. Итого {_money(fa.get('cost', 0), cur_)} — в минус фразовым соответствием (ни одно из них не входит в твои ключи)."
    if f.check_id == "keyword_cannibalization":
        places = ", ".join(fa.get("places") or [])
        cur_ = fa.get("currency") or cur
        if lang == "en":
            return f"{fa.get('count', 0)} keyword(s) duplicated across ad groups with the same match type — they compete with each other. Worst: «{fa.get('keyword', '')}» ({fa.get('match_type', '')}) in {places}, {_money(fa.get('cost', 0), cur_)} spent. Keep one, remove the rest."
        return f"{fa.get('count', 0)} ключей дублируются в разных группах с одним типом соответствия — конкурируют сами с собой. Худший: «{fa.get('keyword', '')}» ({fa.get('match_type', '')}) в {places}, расход {_money(fa.get('cost', 0), cur_)}. Оставь один, остальные убери."
    if f.check_id == "zero_impression_keywords":
        ex = ", ".join(f"«{k}»" for k in (fa.get("examples") or [])[:3])
        if lang == "en":
            return f"{fa.get('count', 0)} of {fa.get('total', 0)} active keywords ({fa.get('pct', 0)}%) got zero impressions — too narrow, out-ranked or duplicated ({ex}). They cost nothing but hide the real structure: clean them up or broaden."
        return f"{fa.get('count', 0)} из {fa.get('total', 0)} активных ключей ({fa.get('pct', 0)}%) не показались НИ РАЗУ — слишком узкие, задавлены рангом или дублируют друг друга ({ex}). Денег не стоят, но врут про структуру: почисти или расширь."
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
    if f.check_id == "target_cpa_too_low":
        cur_ = fa.get("currency") or cur
        tgt = _money(fa.get("target_cpa", 0), cur_)
        act = _money(fa.get("actual_cpa", 0), cur_)
        if lang == "en":
            return f"«{camp}»: target CPA {tgt} is far below the actual {act} ({fa.get('conversions', 0)} conversions) — Google cannot buy conversions at that price and throttles delivery. Raise the target close to the actual and lower it in steps."
        return f"«{camp}»: цель CPA {tgt} много ниже фактического {act} ({fa.get('conversions', 0)} конверсий) — по такой цене Google конверсий не находит и режет показы. Подними цель до факта и снижай шагами."
    if f.check_id == "brand_nonbrand_mixed":
        ex = ", ".join(f"«{k}»" for k in (fa.get("examples") or [])[:2])
        if lang == "en":
            return f"«{camp}»: brand and non-brand keywords share one campaign ({fa.get('brand_kw', 0)} brand: {ex}; {fa.get('other_kw', 0)} non-brand) — brand takes {fa.get('brand_share', 0)}% of the spend and flatters the average CPA, hiding how the non-brand part really performs. Split them into separate campaigns."
        return f"«{camp}»: бренд и не-бренд в одной кампании ({fa.get('brand_kw', 0)} брендовых: {ex}; {fa.get('other_kw', 0)} остальных) — бренд забирает {fa.get('brand_share', 0)}% расхода и вытягивает средний CPA, за ним не видно, как работает небрендовая часть. Раздели на две кампании."
    if f.check_id in ("assets_sitelinks_thin", "assets_callouts_thin", "assets_no_snippets"):
        n, need = fa.get("count", 0), fa.get("need", 0)
        what_en = {
            "assets_sitelinks_thin": ("sitelinks", f"{n} of {need}+"),
            "assets_callouts_thin": ("callouts", f"{n} of {need}+"),
            "assets_no_snippets": ("structured snippets", "none"),
        }[f.check_id]
        what_ru = {
            "assets_sitelinks_thin": ("ситилинков", f"{n} из {need}+"),
            "assets_callouts_thin": ("уточнений", f"{n} из {need}+"),
            "assets_no_snippets": ("структурированных описаний", "нет"),
        }[f.check_id]
        if lang == "en":
            return f"«{camp}»: {what_en[1]} {what_en[0]} — the ad takes less space in the results and gets a lower CTR at the same bid. Add them: /campaigns → the campaign → Extensions."
        return f"«{camp}»: {what_ru[0]} — {what_ru[1]}. Объявление занимает меньше места в выдаче и собирает меньше кликов при той же ставке. Добавить: /campaigns → карточка кампании → Расширения."
    if f.check_id == "display_on_search_campaign":
        cost = _money(fa.get("content_cost", 0), fa.get("currency") or cur)
        if lang == "en":
            return f"«{camp}»: the Display Network is on in a Search campaign — {cost} went to banners with zero conversions. Turn the content network off: /campaigns → the campaign → Networks."
        return f"«{camp}»: в поисковой кампании включена сеть КМС — {cost} ушло на баннеры, конверсий ноль. Выключить контекстно-медийную сеть: /campaigns → карточка кампании → Сети."
    if f.check_id == "geo_interest_waste":
        cost = _money(fa.get("outside_cost", 0), fa.get("currency") or cur)
        if lang == "en":
            return f"«{camp}»: location targeting is «presence OR interest» — {cost} was spent on clicks from people physically outside your regions, with zero conversions. Switch to «presence» (people in your locations): /campaigns → the campaign → Geo."
        return f"«{camp}»: гео-таргетинг «присутствие ИЛИ интерес» — {cost} потрачено на клики людей, физически находящихся ВНЕ ваших регионов, конверсий с них ноль. Переключить на «присутствие»: /campaigns → карточка кампании → Гео."
    if f.check_id in ("bid_below_first_page", "bid_below_top_of_page"):
        # Ставка/цель/на сколько поднять + оговорка правила #3 (бот сам ставку не тронет).
        kw = fa.get("keyword", "")
        mt = str(fa.get("match_type", "") or "").lower()
        mt_s = f" ({mt})" if mt else ""
        bid = _money(fa.get("bid", 0), cur)
        target = _money(fa.get("target_bid", 0), cur)
        up = fa.get("uplift_pct", 0)
        if f.check_id == "bid_below_first_page":
            if lang == "en":
                return f"Keyword «{kw}»{mt_s} in «{camp}»: bid {bid} is below Google's first-page estimate {target} (+{up}%) — it barely reaches page 1. Raise the bid (only on your direct command)."
            return f"Ключ «{kw}»{mt_s} в «{camp}»: ставка {bid} ниже оценки первой страницы {target} (+{up}%) — он почти не выходит на первую страницу. Подними ставку (только по твоей прямой команде)."
        conv = fa.get("conversions", 0)
        cpa = _money(fa.get("cpa", 0), cur)
        if lang == "en":
            return f"Keyword «{kw}»{mt_s} in «{camp}»: {conv} conversions at CPA {cpa}, but bid {bid} is below the top-of-page estimate {target} (+{up}%) — proven value without top slots. Raise the bid (only on your direct command)."
        return f"Ключ «{kw}»{mt_s} в «{camp}»: {conv} конв. при CPA {cpa}, но ставка {bid} ниже оценки верха страницы {target} (+{up}%) — доказанная ценность без верхних позиций. Подними ставку (только по твоей прямой команде)."
    if f.check_id == "top_is_rank_lost":
        if lang == "en":
            return f"{fa.get('count', 0)} paying keyword(s) lose top slots to RANK (worst «{fa.get('worst_kw', '')}»: {fa.get('worst_share', 0)}%, {_money(fa.get('cost', 0), cur)} spent) — rank = bid × quality: raise the bid or fix Quality Score."
        return f"{fa.get('count', 0)} платящих ключей теряют верхние позиции из-за РАНГА (худший «{fa.get('worst_kw', '')}»: {fa.get('worst_share', 0)}%, расход {_money(fa.get('cost', 0), cur)}) — ранг = ставка × качество: подними ставку или почини Quality Score."
    if f.check_id == "competitive_pressure":
        # Имён конкурентов тут нет и быть не может (Google не отдаёт их через API) — только цифры
        # аукциона. Имена даёт импорт CSV Auction Insights (/competitors), и мы честно об этом говорим.
        parts_ru = [
            f"Аукцион: ты берёшь {fa.get('share', 0)}% показов, {fa.get('rank_lost', 0)}% отдаёшь из-за РАНГА и {fa.get('budget_lost', 0)}% из-за БЮДЖЕТА (по {fa.get('campaigns', 0)} кампаниям)."
        ]
        parts_en = [
            f"Auction: you take {fa.get('share', 0)}% of impressions, lose {fa.get('rank_lost', 0)}% to RANK and {fa.get('budget_lost', 0)}% to BUDGET (across {fa.get('campaigns', 0)} campaigns)."
        ]
        if "top_is" in fa:
            parts_ru.append(f"Наверху страницы — {fa['top_is']}% показов.")
            parts_en.append(f"Top of page: {fa['top_is']}% of impressions.")
        if fa.get("paying"):
            parts_ru.append(
                f"По {fa.get('underbid', 0)} из {fa['paying']} платящих ключей твоя ставка ниже оценки верха страницы от Google."
            )
            parts_en.append(
                f"On {fa.get('underbid', 0)} of {fa['paying']} paying keywords your bid is below Google's top-of-page estimate."
            )
        if fa.get("verdict") == "rank":
            parts_ru.append(
                "Проигрываешь в РАНГЕ (ставка × качество) — это ставки и Quality Score, а не бюджет."
            )
            parts_en.append(
                "You are losing on RANK (bid × quality) — that's bids and Quality Score, not budget."
            )
        else:
            parts_ru.append(
                "Ограничение — БЮДЖЕТ: конкуренты тут ни при чём, показов не хватает своих денег."
            )
            parts_en.append("The limit is BUDGET: competitors aside, your own money runs out.")
        return " ".join(parts_en if lang == "en" else parts_ru)
    if f.check_id == "sim_bid_upside":
        # Цифры прироста — прогноз САМОГО Google (симулятор), не наша модель. Так и говорим.
        kw = fa.get("keyword", "")
        bid = _money(fa.get("bid", 0), cur)
        target = _money(fa.get("target_bid", 0), cur)
        add_conv = fa.get("add_conversions", 0)
        add_cost = _money(fa.get("add_cost", 0), cur)
        mcpa = _money(fa.get("marginal_cpa", 0), cur)
        if lang == "en":
            return f"Keyword «{kw}» in «{camp}»: Google's simulator estimates that raising the bid {bid} → {target} (+{fa.get('uplift_pct', 0)}%) yields +{add_conv} conversions for +{add_cost} (marginal CPA {mcpa}). Only on your direct command."
        return f"Ключ «{kw}» в «{camp}»: по симулятору Google ставка {bid} → {target} (+{fa.get('uplift_pct', 0)}%) даёт +{add_conv} конв. за +{add_cost} (предельный CPA {mcpa}). Только по твоей прямой команде."
    if f.check_id == "sim_budget_upside":
        bud = _money(fa.get("budget", 0), cur)
        target = _money(fa.get("target_budget", 0), cur)
        add_conv = fa.get("add_conversions", 0)
        add_cost = _money(fa.get("add_cost", 0), cur)
        mcpa = _money(fa.get("marginal_cpa", 0), cur)
        if lang == "en":
            return f"«{camp}»: Google's simulator estimates that raising the daily budget {bud} → {target} (+{fa.get('uplift_pct', 0)}%) yields +{add_conv} conversions for +{add_cost} (marginal CPA {mcpa}). Only on your direct command."
        return f"«{camp}»: по симулятору Google дневной бюджет {bud} → {target} (+{fa.get('uplift_pct', 0)}%) даёт +{add_conv} конв. за +{add_cost} (предельный CPA {mcpa}). Только по твоей прямой команде."
    if f.check_id == "google_recommendations_pending":
        # Ф2: цифры — оценка САМОГО Google (recommendation.impact), не наша. Так и говорим; и говорим,
        # что применение идёт через подтверждение (часть рекомендаций трогает бюджет/ставки).
        n = fa.get("count", 0)
        add_conv = fa.get("add_conversions", 0)
        names = ", ".join(_rec_type_human(t["type"]) for t in fa.get("top", []) if t.get("type"))
        if add_conv > 0:
            add_cost = _money(fa.get("add_cost", 0), cur)
            if lang == "en":
                return f"{n} pending Google recommendation(s): Google estimates +{add_conv} conversions for +{add_cost} ({names}). Review them — applying anything goes through confirmation."
            return f"{n} рекомендаций Google ждут: Google оценивает их в +{add_conv} конв. за +{add_cost} ({names}). Разбери — применение только через подтверждение."
        if lang == "en":
            return f"{n} pending Google recommendation(s): {names}. Google didn't estimate the conversion impact — check them manually."
        return f"{n} рекомендаций Google ждут: {names}. Эффект в конверсиях Google не оценил — посмотри вручную."
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


def _rec_type_human(t: str) -> str:
    """KEYWORD_RECOMMENDATION → «Keyword». Тип рекомендации — enum Google; суффикс «recommendation»
    в названии избыточен, он и так в контексте."""
    t = str(t or "").removesuffix("_RECOMMENDATION")
    return t.replace("_", " ").title()


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
        names = ", ".join(_rec_type_human(t) for t, _ in top)
        # Ф2: рядом с типами — ЦИФРА эффекта, которую даёт сам Google (recommendation.impact):
        # «3 типа» ни о чём не говорит, «+12 конверсий» — говорит. 0 → Google эффект не оценил, молчим
        # про прирост (нет данных ≠ «эффекта нет»). Только показ: применение — через confirm-гейт.
        grec_f = next(
            (f for f in result.findings if f.check_id == "google_recommendations_pending"), None
        )
        gain = float((grec_f.facts or {}).get("add_conversions", 0.0)) if grec_f else 0.0
        if lang == "en":
            s = f"🔵 Google recommends: {names}"
            if gain > 0:
                s += f" — Google estimates +{gain:g} conversions"
        else:
            s = f"🔵 Google советует: {names}"
            if gain > 0:
                s += f" — Google оценивает это в +{gain:g} конв."
        lines.append(s)
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
