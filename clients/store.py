"""§20: хранилище профилей клиентов (client_profiles + детали) на SQLAlchemy.

Чистый слой БД: читатели (карточка/контекст для генерации) + писатели (upsert/clear), которые
пишут историю версий (client_profile_history — переживает clear, для отката/аудита). Оркестрацию
confirm-гейта (claim → writer → finalize) держит clients.execute — сюда она не проникает, чтобы
store оставался тестируемым офлайн (как bot.campaign_wizard.store для §19).

«Один аккаунт — один профиль» (§20.2): ключ — customer_id (UNIQUE). Детали (контакты/услуги/
страницы) слабо связаны по profile_id без FK-констрейнта (идиома проекта: связь по id, проще heal).
Мердж на обновлении (§20.5): непустое поле patch перекрывает; непустой список категории заменяет
её целиком; пустое/None — оставляет как было. PII в логи сырьём не пишем (golden rule #5).
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
        """Создать/обновить профиль по patch (мердж §20.5) + записать историю «до». source —
        text|crawl (для аудита). crawl_extra — {'website','site_pages','last_crawled_at_now'} от
        краулинга. Возвращает result-словарь (без PII в логах — вернём агрегаты)."""
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
                val = _clean_str(patch.get(f))
                if val is not None and getattr(p, f) != val:
                    setattr(p, f, val)
                    changed.append(f)
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

            # контакты/услуги — заменяем категорию целиком, если patch дал непустой список
            contacts = _norm_contacts(patch.get("contacts"))
            if contacts:
                await s.execute(delete(ClientContact).where(ClientContact.profile_id == p.id))
                for c in contacts:
                    s.add(ClientContact(profile_id=p.id, kind=c["kind"], value=c["value"]))
                changed.append("contacts")
            services = _norm_services(patch.get("services"))
            if services:
                await s.execute(delete(ClientService).where(ClientService.profile_id == p.id))
                for sv in services:
                    s.add(
                        ClientService(
                            profile_id=p.id,
                            name=sv["name"],
                            description=sv["description"],
                            price=sv["price"],
                            category=sv["category"],
                        )
                    )
                changed.append("services")

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
        }
