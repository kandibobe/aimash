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
        "geo": "Geo & targeting",
        "assets": "Assets",
    },
}

_SEV_EMOJI = {"warning": "❗", "info": "🟡"}


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


def render_audit(result: AuditResult, lang: str = "ru", *, actions: bool = True) -> str:
    """Собрать карточку аудита из AuditResult. actions=True → самодостаточная (топ-3 + дисклеймер);
    actions=False → ОБЗОР (score + семьи + Google-балл) без топ-3/дисклеймера — действия шлёт bot-слой
    отдельными сообщениями с кнопками «применить». Всегда доступна offline (fallback)."""
    lang = "en" if lang == "en" else "ru"
    cur = result.currency
    labels = _FAMILY_LABEL[lang]
    lines: list[str] = []

    if lang == "en":
        lines.append(f"🩺 Audit · Account {result.customer_id}")
    else:
        lines.append(f"🩺 Аудит · Аккаунт {result.customer_id}")

    if not result.has_activity or result.score is None:
        lines.append("—")
        lines.append("No activity in this period." if lang == "en" else "Нет активности за период.")
        return "\n".join(lines)

    lines.append(f"{result.score}/100 · {result.grade}")
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
        "geo": "📍",
        "assets": "🔗",
    }.get(family, "•")
