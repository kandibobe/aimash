"""§20 досье: два артефакта из одного объекта — файл владельцу и контекст для генераторов.

Потребители разные, и правила у них разные:
  • `render_markdown` — .md-файл в Telegram и в БД. Здесь контакты ЕСТЬ: это досье клиента для
    владельца, ради него всё и затевалось.
  • `render_llm_context` — то, что уезжает в промпт генератора RSA/ключей. Здесь контактов и имён
    сотрудников НЕТ (правило 5: PII не уезжает в LLM; тот же инвариант, что у
    `CrawlResult.combined_text` — `test_combined_text_excludes_contacts_pii`). Рекламному тексту
    телефон директора не нужен, а утечка — нужна ещё меньше.
    Здесь же нет и фактов ОТРАСЛИ (`Fact.industry` — цифры рынка из блога клиента): в объявлении они
    превратились бы в ложное утверждение о самом клиенте, а это снятие объявлений Google.

Здоровье аккаунта (`render_health_markdown` + `with_health`) стоит СБОКУ от обоих: его нет ни в схеме
`Dossier`, ни в сторе. Считается при отдаче файла, вклеивается в готовый markdown — см. `with_health`.
"""

from __future__ import annotations

from clients.dossier_schema import Dossier

CONTEXT_MAX_CHARS = 3000  # дефолт бюджета контекста в промпте (см. settings.profile_ctx_chars)
HEALTH_TOP = 5  # находок в секции здоровья: досье — не карточка /audit, это врезка «что горит»

_HEALTH_HEAD = {"ru": "## 🩺 Здоровье аккаунта", "en": "## 🩺 Account health"}
_HEALTH_NOTE = {
    "ru": "_Аудит посчитан в момент открытия досье (окно 30 дней) и в файле не хранится: балл живёт "
    "днями, а досье — неделями. Полный разбор — /audit._",
    "en": "_Audited when this dossier was opened (30-day window); not stored in the file: a score "
    "lives for days, a dossier for weeks. Full breakdown — /audit._",
}


def _bullets(title: str, items: list[str]) -> list[str]:
    if not items:
        return []
    return [f"## {title}", *[f"- {i}" for i in items], ""]


def render_markdown(d: Dossier, *, generated_at: str = "") -> str:
    """Досье целиком, человеку. Контакты включены (файл идёт владельцу, не в модель)."""
    name = d.company.legal_name or d.domain or "Клиент"
    out: list[str] = [f"# Досье: {name}", ""]

    meta = [x for x in (d.website or d.domain, f"страниц обойдено: {d.pages_count}") if x]
    if generated_at:
        meta.append(generated_at)
    out += [" · ".join(meta), ""]

    if d.overview:
        out += ["## Кратко", d.overview, ""]
    if d.positioning:
        out += ["## Позиционирование", d.positioning, ""]

    c = d.company
    company_rows = [
        (label, val)
        for label, val in (
            ("Юр. название", c.legal_name),
            ("Основана", c.founded),
            ("Рег. номер", c.reg_no),
            ("Адрес", c.address),
            ("Чем занимается", c.mission),
        )
        if val
    ]
    if company_rows:
        out += ["## Компания", *[f"- **{k}:** {v}" for k, v in company_rows], ""]

    if d.services:
        out += [f"## Услуги ({len(d.services)})"]
        for s in d.services:
            head = f"### {s.name}"
            if s.price:
                head += f" — {s.price}"
            out.append(head)
            if s.audience:
                out.append(f"- Для кого: {s.audience}")
            if s.description:
                out += ["", s.description]
            out.append("")

    if d.people:
        out += [f"## Команда ({len(d.people)})"]
        out += [f"- **{p.name}**" + (f" — {p.role}" if p.role else "") for p in d.people]
        out.append("")

    own = [f for f in d.facts if not f.industry]
    industry = [f for f in d.facts if f.industry]
    if own:
        out += [f"## Факты ({len(own)})"]
        for f in own:
            src = f" ([источник]({f.source_url}))" if f.source_url else ""
            out.append(f"- {f.claim}{src}")
        out.append("")
    if industry:
        # Отдельно и с оговоркой: это цифры отрасли из блога клиента, а не его достижения. В тексты
        # объявлений они не пойдут (их нет в render_llm_context) — владельцу видны как справка.
        out += [f"## Отраслевой контекст из блога ({len(industry)}) — НЕ факты о клиенте"]
        for f in industry:
            src = f" ([источник]({f.source_url}))" if f.source_url else ""
            out.append(f"- {f.claim}{src}")
        out.append("")

    out += _bullets("Рынки", d.markets)
    out += _bullets("Преимущества (как заявлено на сайте)", d.usp)

    if d.faq:
        out += ["## FAQ"]
        for q in d.faq:
            out.append(f"**{q.q}**")
            if q.a:
                out += ["", q.a]
            out.append("")

    contact_rows = [f"- {c_['kind']}: {c_['value']}" for c_ in d.contacts if c_.get("value")]
    contact_rows += [f"- {k}: {v}" for k, v in d.socials.items() if v]
    if contact_rows:
        out += ["## Контакты", *contact_rows, ""]

    out += [
        "---",
        f"_Собрано автоматически: обход сайта → извлечение фактов ({d.map_calls} фрагм.) → сведе́ние. "
        "Проверьте перед использованием: бот берёт только то, что написано на сайте._",
    ]
    return "\n".join(out).strip() + "\n"


def render_health_markdown(result, lang: str = "ru", *, trend: str = "") -> str:
    """Секция «здоровье аккаунта» из `AuditResult`. Чистая: всю прозу берём из `audit.render` (та же
    строка, что видит клиент в карточке /audit и в совете) — здесь только markdown-обвязка, ни одного
    своего слова про находку. '' → аккаунт без активности: секции не будет.

    `result` — намеренно без аннотации: модуль `clients/` не тянет `audit.engine` в импорты краула."""
    from audit.render import audit_headline, family_label, finding_text

    head = audit_headline(result, lang)
    if not head:
        return ""
    key = "en" if lang == "en" else "ru"
    out = [_HEALTH_HEAD[key], "", head]
    if (trend or "").strip():
        out.append(trend.strip())
    cur = getattr(result, "currency", "") or ""
    top = list(getattr(result, "findings", []) or [])[:HEALTH_TOP]
    if top:
        out.append("")
        out += [f"- **{family_label(f.family, lang)}** — {finding_text(f, lang, cur)}" for f in top]
    return "\n".join([*out, "", _HEALTH_NOTE[key], ""])


def with_health(markdown: str, health: str) -> str:
    """Вклеить секцию здоровья в УЖЕ СОБРАННОЕ досье — над сгибом, перед первой секцией («что горит»
    видно раньше, чем «чем занимается»). Пустая секция → markdown байт-в-байт как был.

    Именно вклеить, а не отрендерить внутри `render_markdown`: .md пишется ОДИН раз краулом
    (`client_dossiers.markdown`) и живёт неделями, а балл — днями; вмороженное число, по которому примут
    решение через месяц, хуже, чем никакого. Плюс здоровье в схеме `Dossier` рано или поздно утекло бы в
    `render_llm_context` → в промпт RSA. Вне схемы это невозможно по построению."""
    if not (health or "").strip():
        return markdown
    block = health.rstrip("\n").split("\n")
    lines = markdown.split("\n")
    for i, line in enumerate(lines):  # первая секция или футер «---», что встретится раньше
        if line.startswith("## ") or line.rstrip() == "---":
            return "\n".join([*lines[:i], *block, "", *lines[i:]])
    return markdown.rstrip("\n") + "\n\n" + "\n".join(block) + "\n"


def render_llm_context(d: Dossier, *, max_chars: int = CONTEXT_MAX_CHARS) -> str:
    """Контекст для генерации RSA/ключей. БЕЗ контактов и БЕЗ имён сотрудников (правило 5).

    Порядок секций = приоритет: если бюджет символов кончился, первым отваливается FAQ, а не то, чем
    компания занимается."""
    parts: list[str] = []
    c = d.company
    head = c.legal_name or d.domain
    if head:
        parts.append(f"Клиент: {head}" + (f" ({d.website})" if d.website else ""))
    if d.overview:
        parts.append(f"О компании: {d.overview}")
    elif c.mission:
        parts.append(f"О компании: {c.mission}")
    if c.founded:
        parts.append(f"На рынке: с {c.founded}")
    if d.positioning:
        parts.append(f"Позиционирование: {d.positioning}")
    if d.markets:
        parts.append("Рынки: " + ", ".join(d.markets[:20]))
    if d.services:
        svc = [f"{s.name}" + (f" — {s.price}" if s.price else "") for s in d.services[:25]]
        parts.append("Услуги: " + "; ".join(svc))
    # Только факты О КЛИЕНТЕ: статистика отрасли из блога (Fact.industry) сюда не попадает — модель
    # приписала бы её клиенту («мы продали 3 млн авто»), а Google снимает такие объявления.
    own_facts = [f.claim for f in d.facts if not f.industry]
    if own_facts:
        parts.append("Факты: " + "; ".join(own_facts[:20]))
    if d.usp:
        parts.append("Преимущества: " + "; ".join(d.usp[:10]))
    if d.faq:
        parts.append("Частые вопросы: " + "; ".join(q.q for q in d.faq[:5]))

    out: list[str] = []
    budget = max(0, max_chars)
    for p in parts:
        if len(p) + 1 > budget:
            break
        out.append(p)
        budget -= len(p) + 1
    return "\n".join(out).strip()
