"""§20: хранилище профилей клиентов (client_profiles + детали) на SQLAlchemy.

Чистый слой БД: читатели (карточка/контекст для генерации) + писатели (upsert/clear), которые
пишут историю версий (client_profile_history — переживает clear, для отката/аудита). Оркестрацию
confirm-гейта (claim → writer → finalize) держит clients.execute — сюда она не проникает, чтобы
store оставался тестируемым офлайн (как bot.campaign_wizard.store для §19).

«Один аккаунт — один профиль» (§20.2): ключ — customer_id (UNIQUE). Детали (контакты/услуги/
страницы) слабо связаны по profile_id без FK-констрейнта (идиома проекта: связь по id, проще heal).

Мердж на обновлении (§20.3/§20.5, «ничего не теряется»): непустое СКАЛЯРНОЕ поле patch перекрывает;
notes — ДОПИСЫВАЮТСЯ (append, не replace); services/contacts — МЕРДЖ ПО КЛЮЧУ (новое добавляется,
совпавшее обновляется, остальное СОХРАНЯЕТСЯ). Полная замена категории — только по явным флагам
patch['replace_services']/['replace_contacts'] (LLM ставит их ТОЛЬКО когда менеджер прямо просит
«замени/оставь только…»); replace с пустым списком игнорируется (fail-safe: массовое удаление —
исключительно «🗑 Очистить профиль»). preview_merge() — ЕДИНЫЙ источник истины для «было→станет»
и исполнения (diff не может разойтись с фактическим апдейтом). PII в логи сырьём не пишем (#5).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, select

from db.models import (
    ClientContact,
    ClientProfile,
    ClientProfileHistory,
    ClientService,
    ClientSitePage,
)
from db.session import Session

# Скалярные поля профиля, которые модель извлекает/мерджит (client_profiles columns).
_SCALAR_FIELDS = ("brand", "business_desc", "geo", "language", "website", "notes")


def _clean_str(v: Any) -> str | None:
    """Непустая строка или None (пустое/пробелы → None, чтобы мердж не затирал поле пустотой)."""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _norm_contacts(raw: Any) -> list[dict]:
    """Список контактов [{kind,value}] из patch (терпим к форме модели): отбрасываем пустые."""
    out: list[dict] = []
    for c in raw or []:
        if isinstance(c, dict):
            kind = _clean_str(c.get("kind")) or "other"
            value = _clean_str(c.get("value"))
        else:
            kind, value = "other", _clean_str(c)
        if value:
            out.append({"kind": kind[:16], "value": value[:512]})
    return out


def _norm_services(raw: Any) -> list[dict]:
    """Список услуг [{name,description,price,category}] из patch: отбрасываем без name."""
    out: list[dict] = []
    for s in raw or []:
        if isinstance(s, dict):
            name = _clean_str(s.get("name"))
            if not name:
                continue
            price = _clean_str(s.get("price"))
            category = _clean_str(s.get("category"))
            out.append(
                {
                    "name": name[:255],
                    "description": _clean_str(s.get("description")),
                    "price": price[:128] if price else None,
                    "category": category[:128] if category else None,
                }
            )
        else:
            name = _clean_str(s)
            if name:
                out.append(
                    {"name": name[:255], "description": None, "price": None, "category": None}
                )
    return out


def _norm_socials(raw: Any) -> dict | None:
    """Соцсети → dict {kind: url}. Терпим к list[{kind,url}] и к готовому dict. Пустое → None."""
    if not raw:
        return None
    if isinstance(raw, dict):
        out = {str(k).strip()[:32]: str(v).strip()[:512] for k, v in raw.items() if v}
        return out or None
    out = {}
    for it in raw:
        if isinstance(it, dict):
            k = _clean_str(it.get("kind") or it.get("name"))
            u = _clean_str(it.get("url") or it.get("value"))
            if k and u:
                out[k[:32]] = u[:512]
    return out or None


# ── чистые merge-хелперы (§20.3/§20.5 «ничего не теряется») — офлайн-тестируемы ────
def svc_key(name: Any) -> str:
    """Ключ дедупа услуги: нормализованное имя (схлоп пробелов, casefold)."""
    return " ".join(str(name or "").split()).casefold()


# 3G: телефоноподобные kind'ы — value нормализуется до цифр («+38 (067) 123-45-67» ≡ «0671234567»
# по хвосту-10; иначе краул и ручной ввод плодили дубли одного номера в client_contacts).
_PHONE_KINDS = frozenset({"phone", "tel", "telephone", "whatsapp", "viber", "телефон"})


def _norm_phone(v: str) -> str:
    digits = "".join(ch for ch in str(v or "") if ch.isdigit())
    return digits[-10:] if len(digits) > 10 else digits  # 380671234567 ≡ 0671234567 (хвост-10)


def contact_key(c: dict) -> tuple[str, str]:
    """Ключ дедупа контакта: (kind, нормализованное value). Телефоны — по цифрам (хвост-10);
    прочее (email/site/соцсети) — trim+casefold. Оригинальное value в выводе сохраняется —
    нормализация только для ключа."""
    kind = str(c.get("kind") or "other")
    v = str(c.get("value") or "")
    if kind.casefold() in _PHONE_KINDS:
        return (kind, _norm_phone(v) or v.strip().casefold())
    return (kind, v.strip().casefold())


def merge_services(
    existing: list[dict], incoming: list[dict], *, replace: bool = False
) -> list[dict]:
    """Мердж услуг ПО ИМЕНИ: существующие сохраняются (и их порядок), совпавшее по svc_key
    обновляется непустыми полями incoming, новое добавляется в конец. replace=True → только
    нормализованный incoming (явная замена по прямой просьбе менеджера)."""
    if replace:
        return list(incoming)
    by_key = {svc_key(sv.get("name")): dict(sv) for sv in existing}
    order = [svc_key(sv.get("name")) for sv in existing]
    for inc in incoming:
        k = svc_key(inc.get("name"))
        if not k:
            continue
        if k in by_key:  # обновить непустыми полями, ничего не потеряв
            cur = by_key[k]
            for f in ("description", "price", "category"):
                if inc.get(f):
                    cur[f] = inc[f]
        else:
            by_key[k] = dict(inc)
            order.append(k)
    return [by_key[k] for k in order]


def merge_contacts(
    existing: list[dict], incoming: list[dict], *, replace: bool = False
) -> list[dict]:
    """Union контактов с дедупом по (kind, value): существующие сохраняются, новые добавляются.
    replace=True → только incoming (явная замена)."""
    if replace:
        return list(incoming)
    seen = {contact_key(c) for c in existing}
    out = [dict(c) for c in existing]
    for c in incoming:
        k = contact_key(c)
        if k[1] and k not in seen:
            seen.add(k)
            out.append(dict(c))
    return out


def merge_notes(old: str | None, new: str | None, *, max_chars: int = 4000) -> str | None:
    """Заметки менеджера ДОПИСЫВАЮТСЯ (catch-all §20.3 — ничего не теряется), не затираются.
    Идемпотентность: new уже внутри old → old (повторное сохранение того же текста не дублирует).
    Переполнение — режем ХВОСТОМ (новейшее выживает) с префиксом «…»."""
    old_s, new_s = (old or "").strip(), (new or "").strip()
    if not new_s:
        return old or None
    if not old_s:
        return new_s[:max_chars]
    if new_s in old_s:
        return old_s
    merged = f"{old_s}\n— {new_s}"
    if len(merged) > max_chars:
        merged = "…" + merged[-(max_chars - 1) :]
    return merged


def preview_merge(before: dict | None, patch: dict) -> dict:
    """Итог apply_upsert БЕЗ записи: ЕДИНЫЙ источник истины для «было→станет» (bot.main) и
    исполнения — рендер diff не может разойтись с фактическим апдейтом. Зеркалит apply_upsert:
    скаляры (непустое перекрывает), notes — merge_notes, socials — dict-merge,
    services/contacts — merge_* с учётом replace_*-флагов."""
    base = dict(before or {})
    out = dict(base)
    for f in _SCALAR_FIELDS:
        if f == "notes":
            continue  # notes — append-семантика ниже
        val = _clean_str(patch.get(f))
        if val is not None:
            out[f] = val
    out["notes"] = merge_notes(base.get("notes"), _clean_str(patch.get("notes")))
    socials = _norm_socials(patch.get("socials"))
    if socials:
        out["socials"] = {**(base.get("socials") or {}), **socials}
    contacts = _norm_contacts(patch.get("contacts"))
    if contacts:
        out["contacts"] = merge_contacts(
            list(base.get("contacts") or []), contacts, replace=bool(patch.get("replace_contacts"))
        )
    services = _norm_services(patch.get("services"))
    if services:
        out["services"] = merge_services(
            list(base.get("services") or []), services, replace=bool(patch.get("replace_services"))
        )
    return out


class ClientProfileStore:
    """Хранилище профилей клиентов (§20). Все методы async."""

    async def get_by_account(self, customer_id: str) -> dict | None:
        """Полный профиль (скаляры + контакты + услуги + число страниц) как dict, либо None."""
        async with Session() as s:
            p = await self._load(s, customer_id)
            if p is None:
                return None
            return await self._to_dict(s, p)

    async def site_page_hashes(self, customer_id: str) -> dict[str, str]:
        """§20.5: карта {url → content_hash} сохранённых страниц клиента (для diff при инкрементальном
        перекрауле). Нет профиля/страниц → {}. Страницы без хэша (старый краул) в карту не попадают."""
        async with Session() as s:
            p = await self._load(s, customer_id)
            if p is None:
                return {}
            rows = (
                await s.execute(
                    select(ClientSitePage.url, ClientSitePage.content_hash).where(
                        ClientSitePage.profile_id == p.id
                    )
                )
            ).all()
        return {url: h for url, h in rows if url and h}

    async def top_site_pages(self, customer_id: str, *, limit: int = 8) -> list[dict]:
        """§20.6: главные страницы последнего краула для sitelinks — [{url,title,page_type}].
        Порядок — полезность для быстрых ссылок (услуги/каталог/цены… , home ПОСЛЕДНЕЙ — она
        обычно уже final_url объявления). Нет профиля/краула → [] (генерация откатится на LLM)."""
        prio = {
            "services": 0,
            "catalog": 1,
            "price": 2,
            "prices": 2,
            "about": 3,
            "contacts": 4,
            "blog": 5,
            "other": 6,
            "home": 7,
        }
        async with Session() as s:
            p = await self._load(s, customer_id)
            if p is None:
                return []
            rows = (
                await s.execute(
                    select(
                        ClientSitePage.url, ClientSitePage.title, ClientSitePage.page_type
                    ).where(ClientSitePage.profile_id == p.id)
                )
            ).all()
        pages = [
            {"url": u, "title": t, "page_type": pt}
            for u, t, pt in rows
            if u  # пустые url бесполезны для sitelinks
        ]
        pages.sort(key=lambda pg: prio.get((pg.get("page_type") or "other").lower(), 6))
        return pages[:limit]

    async def accounts_with_profile(self, customer_ids: list[str]) -> set[str]:
        """Из переданных customer_id — те, у кого есть профиль (для отметки ✅ в списке аккаунтов)."""
        if not customer_ids:
            return set()
        async with Session() as s:
            rows = (
                (
                    await s.execute(
                        select(ClientProfile.customer_id).where(
                            ClientProfile.customer_id.in_(customer_ids)
                        )
                    )
                )
                .scalars()
                .all()
            )
        return set(rows)

    async def profile_context_text(self, customer_id: str, *, max_chars: int = 1500) -> str:
        """Компактный текст профиля как КОНТЕКСТ для генераторов (§10/§19). Нет профиля → ''.

        Порядок — от важного к второстепенному (бренд/бизнес/УТП/услуги/гео), чтобы усечение по
        max_chars срезало наименее важное. Телефоны/e-mail сюда НЕ кладём (генерации не нужны и это
        PII) — контакты используются отдельными ассетами (call), не текстом заголовков."""
        prof = await self.get_by_account(customer_id)
        if not prof:
            return ""
        parts: list[str] = []
        if prof.get("brand"):
            parts.append(f"Бренд: {prof['brand']}")
        if prof.get("business_desc"):
            parts.append(f"Бизнес: {prof['business_desc']}")
        if prof.get("geo"):
            parts.append(f"Гео: {prof['geo']}")
        if prof.get("language"):
            parts.append(f"Язык аудитории: {prof['language']}")
        services = prof.get("services") or []
        if services:
            svc = []
            for it in services[:12]:
                line = it.get("name", "")
                if it.get("price"):
                    line += f" ({it['price']})"
                svc.append(line)
            parts.append("Услуги/товары: " + "; ".join(x for x in svc if x))
        if prof.get("notes"):
            parts.append(f"Заметки: {prof['notes']}")
        return "\n".join(parts).strip()[:max_chars]

    # ── писатели (вызываются из clients.execute за confirm-гейтом, и из краулера auto-save) ──
    async def apply_upsert(
        self,
        customer_id: str,
        patch: dict,
        *,
        operation: str,
        confirmation_id: str | None = None,
        source: str = "text",
        crawl_extra: dict | None = None,
    ) -> dict:
        """Создать/обновить профиль по patch (мердж §20.3/§20.5 «ничего не теряется») + записать
        историю «до». Семантика зеркалит preview_merge (единый источник истины для «было→станет»):
        скаляры — непустое перекрывает; notes — append; services/contacts — мердж по ключу; полная
        замена категории — только по явным флагам replace_services/replace_contacts (пустой список
        при replace игнорируется — fail-safe). source — text|crawl (для аудита). crawl_extra —
        {'website','site_pages','last_crawled_at_now'}. Возвращает result-словарь (агрегаты, без PII)."""
        async with Session() as s:
            p = await self._load(s, customer_id)
            before = await self._to_dict(s, p) if p is not None else None
            # история «до» (переживает clear; для отката/аудита)
            s.add(
                ClientProfileHistory(
                    customer_id=customer_id,
                    snapshot=before,
                    operation=operation,
                    confirmation_id=confirmation_id,
                )
            )
            if p is None:
                p = ClientProfile(customer_id=customer_id)
                s.add(p)
                await s.flush()  # получить p.id для связки деталей

            changed: list[str] = []
            for f in _SCALAR_FIELDS:
                if f == "notes":
                    continue  # notes — append-семантика ниже (catch-all не затирается)
                val = _clean_str(patch.get(f))
                if val is not None and getattr(p, f) != val:
                    setattr(p, f, val)
                    changed.append(f)
            new_notes = merge_notes(p.notes, _clean_str(patch.get("notes")))
            if new_notes != (p.notes or None):
                p.notes = new_notes
                changed.append("notes")
            socials = _norm_socials(patch.get("socials"))
            if socials:
                p.socials = {**(p.socials or {}), **socials}
                changed.append("socials")

            # краул-специфика: сайт, дата краула, карта страниц
            if crawl_extra:
                if crawl_extra.get("website"):
                    p.website = str(crawl_extra["website"])[:2048]
                    changed.append("website")
                if crawl_extra.get("last_crawled_at_now"):
                    p.last_crawled_at = func.now()

            # контакты/услуги — МЕРДЖ ПО КЛЮЧУ с существующими (replace — только по явному флагу
            # И непустому списку; delete+reinsert смердженного — идиома слабой связки по id)
            contacts = _norm_contacts(patch.get("contacts"))
            if contacts:
                merged_c = merge_contacts(
                    list((before or {}).get("contacts") or []),
                    contacts,
                    replace=bool(patch.get("replace_contacts")),
                )
                if merged_c != list((before or {}).get("contacts") or []):
                    changed.append("contacts")
                await s.execute(delete(ClientContact).where(ClientContact.profile_id == p.id))
                for c in merged_c:
                    s.add(ClientContact(profile_id=p.id, kind=c["kind"], value=c["value"]))
            services = _norm_services(patch.get("services"))
            if services:
                merged_s = merge_services(
                    list((before or {}).get("services") or []),
                    services,
                    replace=bool(patch.get("replace_services")),
                )
                if merged_s != list((before or {}).get("services") or []):
                    changed.append("services")
                await s.execute(delete(ClientService).where(ClientService.profile_id == p.id))
                for sv in merged_s:
                    s.add(
                        ClientService(
                            profile_id=p.id,
                            name=sv["name"],
                            description=sv.get("description"),
                            price=sv.get("price"),
                            category=sv.get("category"),
                        )
                    )

            # карта страниц сайта (только краулинг) — заменяем целиком
            pages = (crawl_extra or {}).get("site_pages") or []
            if pages:
                await s.execute(delete(ClientSitePage).where(ClientSitePage.profile_id == p.id))
                for pg in pages[:200]:
                    s.add(
                        ClientSitePage(
                            profile_id=p.id,
                            url=str(pg.get("url", ""))[:2048],
                            title=_clean_str(pg.get("title")),
                            page_type=_clean_str(pg.get("page_type")),
                            key_links=pg.get("key_links") or None,
                            content_hash=_clean_str(pg.get("content_hash")),
                        )
                    )
                changed.append("site_pages")

            await s.commit()
            return {
                "customer_id": customer_id,
                "created": before is None,
                "changed_fields": changed,
                "source": source,
            }

    async def apply_clear(
        self,
        customer_id: str,
        *,
        operation: str = "profile_clear",
        confirmation_id: str | None = None,
    ) -> dict:
        """Удалить профиль клиента и все детали (§20 «Очистить профиль»). История «до» сохраняется."""
        async with Session() as s:
            p = await self._load(s, customer_id)
            before = await self._to_dict(s, p) if p is not None else None
            s.add(
                ClientProfileHistory(
                    customer_id=customer_id,
                    snapshot=before,
                    operation=operation,
                    confirmation_id=confirmation_id,
                )
            )
            if p is not None:
                pid = p.id
                await s.execute(delete(ClientContact).where(ClientContact.profile_id == pid))
                await s.execute(delete(ClientService).where(ClientService.profile_id == pid))
                await s.execute(delete(ClientSitePage).where(ClientSitePage.profile_id == pid))
                await s.delete(p)
            await s.commit()
            return {"customer_id": customer_id, "cleared": before is not None}

    # ── внутреннее ──────────────────────────────────────────────────────────────
    @staticmethod
    async def _load(s, customer_id: str) -> ClientProfile | None:
        return (
            await s.execute(select(ClientProfile).where(ClientProfile.customer_id == customer_id))
        ).scalar_one_or_none()

    @staticmethod
    async def _to_dict(s, p: ClientProfile) -> dict:
        contacts = (
            await s.execute(
                select(ClientContact.kind, ClientContact.value).where(
                    ClientContact.profile_id == p.id
                )
            )
        ).all()
        services = (
            await s.execute(
                select(
                    ClientService.name,
                    ClientService.description,
                    ClientService.price,
                    ClientService.category,
                ).where(ClientService.profile_id == p.id)
            )
        ).all()
        pages_count = (
            await s.execute(
                select(func.count())
                .select_from(ClientSitePage)
                .where(ClientSitePage.profile_id == p.id)
            )
        ).scalar_one()
        return {
            "customer_id": p.customer_id,
            "brand": p.brand,
            "business_desc": p.business_desc,
            "geo": p.geo,
            "language": p.language,
            "website": p.website,
            "socials": p.socials or {},
            "notes": p.notes,
            "contacts": [{"kind": k, "value": v} for k, v in contacts],
            "services": [
                {"name": n, "description": d, "price": pr, "category": c}
                for n, d, pr, c in services
            ],
            "site_pages_count": int(pages_count or 0),
            # §20.2: дата последнего краула для карточки клиента (persist есть, отдавали в UI — нет).
            "last_crawled_at": p.last_crawled_at,
        }
