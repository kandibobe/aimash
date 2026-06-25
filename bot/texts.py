"""Тексты и шаблоны сообщений бота (RU). Вынесены отдельно — упрощает правки и будущую
EN-локализацию (ТЗ §4). Формат — HTML (parse_mode='HTML' на стороне отправки в bot.main);
ВСЕ динамические данные (имена кампаний, текст ошибок) обязательно через esc().
"""

from __future__ import annotations

import html


def esc(s: object) -> str:
    """Экранирование для HTML parse_mode (имена кампаний/ошибки могут содержать < & >)."""
    return html.escape(str(s), quote=False)


def _thou(n: float, dec: int = 0) -> str:
    """Число с пробелом-разделителем тысяч: 12480 -> '12 480', 4512.3 -> '4 512.30'."""
    return f"{n:,.{dec}f}".replace(",", " ")


def status_human(status: str) -> str:
    return {"ENABLED": "включена ▶️", "PAUSED": "на паузе ⏸", "REMOVED": "удалена 🗑"}.get(
        status, status
    )


# ── Статичные тексты ─────────────────────────────────────────────────────────────
START = (
    "👋 <b>Aimash на связи.</b>\n\n"
    "Я читаю Google Ads и предлагаю изменения. Любое изменение — "
    "<b>только после твоего «да»</b>: покажу «было → станет» и кнопки.\n\n"
    "Пиши обычным текстом, например:\n"
    "• <i>покажи статистику за 7 дней</i>\n"
    "• <i>повысь бюджет кампании Search Spring на 20%</i>\n"
    "• <i>поставь на паузу кампанию Brand</i>\n\n"
    "Или жми кнопки меню ниже. /help — подробнее."
)

HELP = (
    "<b>Что я умею сейчас</b>\n"
    "Перед любым изменением показываю «было → станет» и жду подтверждения «да».\n\n"
    "<b>Изменения</b> (по тексту, с подтверждением):\n"
    "• бюджет, ставка CPC, ключевые слова, минус-слова, пауза/возобновление\n\n"
    "<b>Команды</b>\n"
    "/status — быстрая статистика (30 дн.)\n"
    "/campaigns — кампании + быстрые действия\n"
    "/report [7|30|90|MTD] — сводка за период (по умолч. 30 дн.)\n"
    "/export [7|30|90|MTD] — глубокий отчёт .xlsx\n"
    "/rsa — сгенерировать тексты объявления (RSA) с поэлементным подтверждением\n"
    "/cancel — отменить текущий черновик\n\n"
    "<i>Скоро: подбор ключевых слов, отчёты по расписанию.</i>"
)

KW_SOON = (
    "🔍 Подбор ключевых слов появится в одной из следующих фаз.\n"
    "Сейчас доступны: бюджет, ставка, добавление ключей вручную, минус-слова, "
    "пауза/возобновление кампании."
)

PROPOSAL_PENDING = "📝 <b>Черновик изменения</b>\n\n{summary}\n\nПодтвердить?"
EXECUTING = "⏳ Выполняю…"
APPLIED = "✅ <b>Готово.</b>\n{result}"
FAILED = "⚠️ Не удалось выполнить: {kind}: {err}"
REJECTED = "❌ Отменено"
STALE = "Черновик не найден или устарел"
NO_PROPOSAL = "Нет активного черновика для отмены."
NO_CAMPAIGNS = "Кампаний нет."
CAMP_LIST_STALE = "Список кампаний устарел — вызови /campaigns заново."

# ── RSA-генерация (фаза 2.C) ─────────────────────────────────────────────────────
RSA_PICK_CAMPAIGN = "✍️ <b>Генерация текстов объявления</b>\nВыбери кампанию:"
RSA_PICK_ADGROUP = "Выбери группу объявлений:"
RSA_NO_ADGROUPS = "В кампании нет групп объявлений — сначала создай группу."
RSA_ASK_BRIEF = (
    "Пришли <b>тематику</b> и <b>ссылку</b> объявления одним сообщением.\n"
    "Например: <code>доставка цветов | https://example.com</code>"
)
RSA_BAD_URL = "Не вижу корректной ссылки (http/https). Пришли тематику и URL ещё раз."
RSA_GENERATING = "⏳ Генерирую варианты…"
RSA_GEN_EMPTY = "Не удалось сгенерировать достаточно валидных вариантов. Попробуй ещё раз: /rsa"
RSA_SESSION_STALE = "Сессия генерации устарела — начни заново: /rsa"
RSA_REFINE_PROMPT = "✏️ Что поправить в этом элементе? Пришли короткую правку текстом."
RSA_REFINE_TOO_LONG = (
    "Доработанный вариант не уложился в лимит ({n}/{limit}). Пришли правку ещё раз."
)
RSA_BELOW_MIN = "Нужно ≥3 одобренных заголовка и ≥2 описания. Сейчас: {h} загол. / {d} опис."
RSA_CREATED = "✅ <b>Объявление создано (на паузе).</b>\n{result}"


def fmt_rsa_element(kind: str, idx: int, total: int, e: dict, campaign: str, ad_group: str) -> str:
    """Карточка одного элемента курации: тип, текст, длина/лимит, кампания/группа."""
    from adcopy.validate import LIMITS

    name = "Заголовок" if kind == "h" else "Описание"
    limit = LIMITS["headline" if kind == "h" else "description"]
    state = {
        "pending": "🟡 на рассмотрении",
        "approved": "✅ одобрен",
        "rejected": "❌ отклонён",
    }.get(e.get("state", "pending"), e.get("state", ""))
    return (
        f"<b>{name} {idx + 1}/{total}</b> · {e.get('len', 0)}/{limit} · {state}\n"
        f"«{esc(e.get('text', ''))}»\n\n"
        f"📋 {esc(campaign)} / {esc(ad_group)}"
    )


def fmt_rsa_overview(h_appr: int, d_appr: int, h_total: int, d_total: int) -> str:
    """Итоговый экран курации: сколько одобрено из скольких, готовность к созданию."""
    ready = "✅ можно создавать" if (h_appr >= 3 and d_appr >= 2) else "нужно ≥3 загол. и ≥2 опис."
    return (
        "📋 <b>Итог курации RSA</b>\n"
        f"Заголовки одобрены: <b>{h_appr}</b>/{h_total}\n"
        f"Описания одобрены: <b>{d_appr}</b>/{d_total}\n\n"
        f"{ready}"
    )


def fmt_rsa_proposal_summary(
    ad_group: str, headlines: list[str], descriptions: list[str], final_url: str
) -> str:
    """Плейн-текст сводка create_rsa для confirm-гейта (esc применяется при показе)."""
    h_lines = "\n".join(f"  • {h}" for h in headlines)
    d_lines = "\n".join(f"  • {d}" for d in descriptions)
    return (
        f"Создать объявление (RSA) в группе «{ad_group}» — на паузе.\n"
        f"Ссылка: {final_url}\n\n"
        f"Заголовки ({len(headlines)}):\n{h_lines}\n\n"
        f"Описания ({len(descriptions)}):\n{d_lines}"
    )


# ── Рендер с данными ─────────────────────────────────────────────────────────────
def fmt_stats(account: str, days: int, st: dict) -> str:
    """Статистика аккаунта с вычисленными в КОДЕ CTR/CPC (контракт read не трогаем)."""
    imp = int(st.get("impressions") or 0)
    clk = int(st.get("clicks") or 0)
    cost = float(st.get("cost") or 0)
    conv = float(st.get("conversions") or 0)
    cval = float(st.get("conv_value") or 0)
    ctr = (clk / imp * 100) if imp else 0.0
    cpc = (cost / clk) if clk else 0.0
    return (
        f"📊 <b>Аккаунт …{esc(str(account)[-4:])}</b> · {days} дн.\n\n"
        f"Показы:      <b>{_thou(imp)}</b>\n"
        f"Клики:       <b>{_thou(clk)}</b>  (CTR {ctr:.2f}%)\n"
        f"Расход:      <b>{_thou(cost, 2)}</b>\n"
        f"Ср. CPC:     <b>{_thou(cpc, 2)}</b>\n"
        f"Конверсии:   <b>{conv:g}</b>\n"
        f"Ценность:    <b>{_thou(cval, 2)}</b>"
    )


def campaigns_title(account: str) -> str:
    return f"📋 <b>Кампании аккаунта …{esc(str(account)[-4:])}</b>\nВыбери кампанию:"


def fmt_campaign_header(c: dict) -> str:
    return (
        f"📋 <b>{esc(c['name'])}</b>\n"
        f"Статус: {status_human(c.get('status', ''))}\n\n"
        "Выбери действие:"
    )
