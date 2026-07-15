"""§20 досье: оркестрация map → merge → синтез и вывод патча профиля из готового досье.

Один вход для бота (`build_dossier`) и один — для карточки клиента (`dossier_patch`). Почему патч
профиля берётся ИЗ ДОСЬЕ, а не отдельным вызовом модели: жалоба владельца «информация о клиенте
слишком короткая» — про КАРТОЧКУ, а не только про файл. Досье уже видело весь сайт целиком, поэтому
и business_desc, и полный список услуг в карточке берутся из него — вторая LLM-сводка (structure_crawl)
остаётся фолбэком на случай, когда досье собрать не удалось (модель недоступна/лимит/мало текста).
"""

from __future__ import annotations

from clients.dossier_map import map_pages
from clients.dossier_merge import merge_extracts, normalize_ru, synthesize
from clients.dossier_schema import Dossier
from core.config import settings
from core.logging import log

# Меньше этого текста на весь сайт — собирать нечего (заглушка/парковка домена): не тратим вызовы.
MIN_SITE_CHARS = 800

_NOTES_MAX = 1200  # сколько фактов/УТП дописываем в notes профиля (merge_notes режет на 4000)


async def build_dossier(
    *,
    pages: list[dict],
    domain: str = "",
    website: str | None = None,
    contacts: list[dict] | None = None,
    socials: dict[str, str] | None = None,
    chat_id: int | None = None,
    language: str = "ru",
) -> Dossier | None:
    """Страницы краула → досье. None, если собирать нечего (мало текста / модель ничего не извлекла).

    `pages` — payload карты страниц ({url,title,page_type,text}): из свежего краула или из БД
    (client_site_pages.text — ради этого текст и сохраняется, миграция 0028: пересобрать досье без
    повторного обхода сайта).

    LLMBudgetExceededError НЕ глушится: исчерпанный дневной лимит — это ответ пользователю, а не
    молчаливо пустое досье.
    """
    total = sum(len((p.get("text") or "")) for p in pages or [])
    if total < MIN_SITE_CHARS:
        log.info("dossier: текста мало (%d симв.) — досье не собираем", total)
        return None

    # Точная редакция PII: то, что код УЖЕ нашёл на сайте (tel:/mailto:/JSON-LD), в промпт не поедет
    # ни в каком написании (clients.dossier_map.redact_pii).
    known = [
        str(c.get("value") or "")
        for c in (contacts or [])
        if str(c.get("kind") or "") in ("phone", "email")
    ]
    mapped = await map_pages(pages, chat_id=chat_id, language=language, known_contacts=known)
    if not mapped.extracts:
        log.info("dossier: модель не извлекла фактов (%d вызовов)", mapped.calls)
        return None

    d = merge_extracts(
        mapped.extracts,
        domain=domain,
        website=website,
        contacts=list(contacts or []),
        socials=dict(socials or {}),
        pages_count=len(pages or []),
        map_calls=mapped.calls,
    )
    # Свести двуязычный сайт к одному языку и схлопнуть кросс-язычные дубли ДО синтеза прозы — иначе
    # проза (и патч карточки) строилась бы по задвоенным EN+RU спискам. Рынки уже канонизированы кодом
    # в merge_extracts; здесь — услуги/УТП/факты/роли/компания. Fail-open: сбой оставит код-сведённые
    # списки, LLMBudgetExceededError пробрасывается (как synthesize).
    if settings.dossier_normalize_ru:
        d = await normalize_ru(d, chat_id=chat_id, language=language)
    d = await synthesize(d, chat_id=chat_id, language=language)
    log.info(
        "dossier: собрано (страниц %d, вызовов %d, услуг %d, людей %d, фактов %d)",
        d.pages_count,
        d.map_calls,
        len(d.services),
        len(d.people),
        len(d.facts),
    )
    return d


def _notes_from(d: Dossier) -> str | None:
    """Факты и УТП — в notes профиля (в схеме client_profiles им места нет, а терять их нельзя).
    merge_notes идемпотентен: повторный краул с тем же текстом не размножит заметку.

    Имён СОТРУДНИКОВ здесь нет намеренно: снапшот профиля уезжает в client_profile_history, а она
    ПЕРЕЖИВАЕТ «🗑 Очистить профиль» (ключ — customer_id, не FK) — чужая PII осталась бы в БД после
    удаления. Команда живёт в client_dossiers, которая удаляется вместе с профилем."""
    lines: list[str] = []
    own_facts = [f.claim for f in d.facts if not f.industry]  # без статистики отрасли из блога
    if own_facts:
        lines.append("Факты с сайта: " + "; ".join(own_facts[:15]))
    if d.usp:
        lines.append("Преимущества: " + "; ".join(d.usp[:8]))
    if d.company.founded:
        lines.append(f"Основана: {d.company.founded}")
    if d.company.reg_no:
        lines.append(f"Рег. номер: {d.company.reg_no}")
    txt = "\n".join(lines).strip()
    return txt[:_NOTES_MAX] or None


def dossier_patch(d: Dossier) -> dict:
    """Досье → патч профиля (clients.store.apply_upsert). Контакты сюда НЕ кладём: их подмешивает
    вызывающий из результата краула (bm._crawl_patch_from_result) — источник у них общий, но правила
    дедупа контактов живут в store.merge_contacts.

    replace_services/replace_contacts здесь НЕТ и быть не может: краулёный текст — это ДАННЫЕ, а не
    команда «сотри услуги» (правило 8, инвариант test_crawl_text_cannot_wipe_services)."""
    patch: dict = {}
    if d.company.legal_name:
        patch["brand"] = d.company.legal_name
    desc = d.overview or d.company.mission
    if d.positioning:
        desc = f"{desc}\n\n{d.positioning}" if desc else d.positioning
    if desc:
        patch["business_desc"] = desc
    if d.markets:
        patch["geo"] = ", ".join(d.markets[:10])
    if d.website:
        patch["website"] = d.website
    if d.services:
        patch["services"] = [
            {
                "name": s.name,
                "description": s.description,
                "price": s.price,
                "category": None,
            }
            for s in d.services
        ]
    notes = _notes_from(d)
    if notes:
        patch["notes"] = notes
    return patch
