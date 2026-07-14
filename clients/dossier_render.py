"""§20 досье: два артефакта из одного объекта — файл владельцу и контекст для генераторов.

Потребители разные, и правила у них разные:
  • `render_markdown` — .md-файл в Telegram и в БД. Здесь контакты ЕСТЬ: это досье клиента для
    владельца, ради него всё и затевалось.
  • `render_llm_context` — то, что уезжает в промпт генератора RSA/ключей. Здесь контактов и имён
    сотрудников НЕТ (правило 5: PII не уезжает в LLM; тот же инвариант, что у
    `CrawlResult.combined_text` — `test_combined_text_excludes_contacts_pii`). Рекламному тексту
    телефон директора не нужен, а утечка — нужна ещё меньше.
"""

from __future__ import annotations

from clients.dossier_schema import Dossier

CONTEXT_MAX_CHARS = 3000  # дефолт бюджета контекста в промпте (см. settings.profile_ctx_chars)


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

    if d.facts:
        out += [f"## Факты ({len(d.facts)})"]
        for f in d.facts:
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
    if d.facts:
        parts.append("Факты: " + "; ".join(f.claim for f in d.facts[:20]))
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
