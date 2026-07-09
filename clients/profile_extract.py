"""§20.3: извлечение профиля клиента из СВОБОДНОГО текста менеджера через LLM.

Менеджер пишет «Клиент Kasi Motors — автодилер в Найроби… сайт kasimotors.co.ke, телефон …».
Модель (роль parsing — дёшево) раскладывает это в типизированный ClientProfileExtract. Это
advisory-разбор: БД не трогается напрямую — реальное сохранение проходит confirm-гейт как
memory-операция (clients.execute). Незамапленный текст модель складывает в notes (§20.3, чтобы
не терять информацию). Паттерн строгого JSON зеркалит agent.campaign_settings / keywords.cluster.

Тот же путь используется для сведе́ния краулинга (structure_crawl): собранный со страниц текст →
профиль. PII (телефоны/e-mail) в логи сырьём не пишем.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, field_validator

from agent.router import chat


class ServiceItem(BaseModel):
    name: str
    description: str | None = None
    price: str | None = None  # отображаемый текст («от $4000»), НЕ micros
    category: str | None = None


class ContactItem(BaseModel):
    kind: str = "other"  # phone|email|address|social|messenger|other
    value: str


class ClientProfileExtract(BaseModel):
    """Извлечённый профиль. Все поля опциональны: None/[] ⇒ «не указано» (мердж не затирает)."""

    brand: str | None = None
    business_desc: str | None = None
    geo: str | None = None
    language: str | None = None
    website: str | None = None
    socials: dict[str, str] = Field(default_factory=dict)  # {"instagram": "@x", "facebook": "..."}
    notes: str | None = None  # заметки менеджера + всё, что не легло в поля (§20.3)
    services: list[ServiceItem] = Field(default_factory=list)
    contacts: list[ContactItem] = Field(default_factory=list)
    # §20.5: полная ЗАМЕНА категории — только когда менеджер ЯВНО просит («замени услуги на…»,
    # «оставь только…»). Дефолт False = мердж (ничего не теряется). Fail-safe-коэрция ниже.
    replace_services: bool = False
    replace_contacts: bool = False

    @field_validator("replace_services", "replace_contacts", mode="before")
    @classmethod
    def _coerce_replace(cls, v):
        return v is True  # всё, кроме литерального true, → False (мердж; fail-safe)

    # Терпимость к кривому ответу модели: не-список/мусорные элементы → отбрасываем, а не роняем
    # весь разбор (field-salvage на уровне поля). Оставляем только dict'ы с обязательным ключом.
    @field_validator("services", mode="before")
    @classmethod
    def _coerce_services(cls, v):
        if not isinstance(v, list):
            return []
        return [x for x in v if isinstance(x, dict) and str(x.get("name", "")).strip()]

    @field_validator("contacts", mode="before")
    @classmethod
    def _coerce_contacts(cls, v):
        if not isinstance(v, list):
            return []
        return [x for x in v if isinstance(x, dict) and str(x.get("value", "")).strip()]

    @field_validator("socials", mode="before")
    @classmethod
    def _coerce_socials(cls, v):
        if isinstance(v, dict):
            return {str(k): str(val) for k, val in v.items() if val}
        return {}

    def is_empty(self) -> bool:
        return not any(
            [
                self.brand,
                self.business_desc,
                self.geo,
                self.language,
                self.website,
                self.socials,
                self.notes,
                self.services,
                self.contacts,
            ]
        )

    def to_patch(self) -> dict:
        """dict для clients.store.apply_upsert (мердж §20.5). Пустые/None не кладём — не затирать.
        replace_*-флаги кладём ТОЛЬКО когда True (patch остаётся компактным в proposal.params)."""
        patch: dict[str, Any] = {}
        for f in ("brand", "business_desc", "geo", "language", "website", "notes"):
            v = getattr(self, f)
            if v:
                patch[f] = v
        if self.socials:
            patch["socials"] = dict(self.socials)
        if self.services:
            patch["services"] = [s.model_dump() for s in self.services]
            if self.replace_services:
                patch["replace_services"] = True
        if self.contacts:
            patch["contacts"] = [c.model_dump() for c in self.contacts]
            if self.replace_contacts:
                patch["replace_contacts"] = True
        return patch


_SYSTEM = (
    "Ты — ассистент по управлению рекламой. Извлеки из текста менеджера профиль клиента и верни "
    "СТРОГО один JSON-объект без пояснений и markdown со СЛЕДУЮЩИМИ полями (любое неизвестное — "
    "null или []): "
    '{"brand": "имя/бренд клиента или null", '
    '"business_desc": "чем занимается, ниша, регион, УТП/преимущества или null", '
    '"geo": "страны/города работы клиента или null", '
    '"language": "язык(и) аудитории или null", '
    '"website": "основной URL сайта или null", '
    '"socials": {"instagram": "…", "facebook": "…"} (пусто {} если нет), '
    '"notes": "всё важное, что не легло в поля выше (тон, что подчёркивать/не использовать) или null", '
    '"services": [{"name": "услуга/товар", "description": "или null", "price": "как в тексте, напр. '
    '\'от $4000\', или null", "category": "или null"}], '
    '"contacts": [{"kind": "phone|email|address|social|messenger", "value": "значение"}], '
    '"replace_services": true ТОЛЬКО если менеджер ЯВНО просит заменить/стереть прежний список услуг '
    "(«замени услуги на…», «оставь только…», «удали все услуги, теперь…»); иначе false, "
    '"replace_contacts": true по тому же правилу для контактов; иначе false}. '
    "Цены переноси как текст (символ валюты сохраняй), НЕ считай. Не выдумывай данные, которых нет. "
    "Обычное дополнение/упоминание новой услуги — это НЕ замена (false): существующие данные сохраняются."
)


def _extract_json_object(content: str) -> dict | None:
    """Первый JSON-объект из ответа модели (как agent.campaign_settings)."""
    s = content or ""
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        data = json.loads(s[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _coerce(data: dict) -> ClientProfileExtract:
    """dict модели → ClientProfileExtract, отбрасывая мусор (Pydantic + field-salvage)."""
    try:
        return ClientProfileExtract.model_validate(data)
    except Exception:  # noqa: BLE001 — кривой ответ модели не должен ронять разбор
        safe = {k: data.get(k) for k in ClientProfileExtract.model_fields if k in data}
        try:
            return ClientProfileExtract.model_validate(safe)
        except Exception:  # noqa: BLE001
            return ClientProfileExtract()


async def extract_profile(text: str, *, language: str = "ru") -> ClientProfileExtract:
    """Распарсить свободный текст менеджера в ClientProfileExtract (роль keywords). Пустой ввод →
    пустой объект без вызова LLM. Fallback при сбое — пустой объект (менеджер увидит и дополнит)."""
    body = (text or "").strip()
    if not body:
        return ClientProfileExtract()
    try:
        msg = await chat(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": f"Язык интерфейса: {language}.\nТекст:\n{body}"},
            ],
            role="keywords",
            temperature=0.2,
        )
        data = _extract_json_object(getattr(msg, "content", "") or "")
    except Exception:  # noqa: BLE001 — разбор не критичен, есть fallback
        data = None
    return _coerce(data) if data is not None else ClientProfileExtract()


async def structure_crawl(
    *, pages_text: str, website: str | None = None, language: str = "ru"
) -> ClientProfileExtract:
    """§20.4: свести собранный краулером текст страниц в профиль (тот же LLM-путь, что и текстовый
    ввод). website подсказывается модели как основной URL. Пустой текст → пустой объект без LLM."""
    body = (pages_text or "").strip()
    if not body:
        return ClientProfileExtract()
    hint = f"Основной сайт клиента: {website}\n" if website else ""
    extract = await extract_profile(f"{hint}{body}", language=language)
    if website and not extract.website:
        extract.website = website
    return extract
