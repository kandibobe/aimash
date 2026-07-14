"""§20 досье: типы map-фазы (что модель извлекает из чанка) и итогового документа.

Почему отдельная схема, а не расширение ClientProfileExtract: у профиля клиента нет и не должно быть
полей под команду/факты/УТП/рынки/FAQ (R8) — а класть их в `notes` строкой значит потерять структуру
на первом же мердже. Досье — отдельный артефакт со своей таблицей (миграция 0029) и своим рендером.

ПРАВИЛО 5 (PII не уезжает в LLM) прибито к схеме: в `DossierExtract` **нет ни одного контактного
поля** — модели физически некуда положить телефон или e-mail, даже если они уцелели в тексте. Контакты
собирает КОД (`clients/crawler.py::_extract_contacts` — tel:/mailto:/JSON-LD) и подмешивает в готовое
досье уже после модели (`Dossier.contacts`). Симметрично `combined_text` (инвариант
`test_combined_text_excludes_contacts_pii`).

Все поля опциональны: пустое = «на этой странице не сказано», а не «нет у клиента» — иначе один
чанк без команды стёр бы команду, найденную соседним.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


def _as_dict(v: Any) -> dict | None:
    """JSON-объект от модели ИЛИ уже готовый Service/Person/… (собрали кодом или тестом). Без второй
    ветки валидатор молча выбрасывал бы объекты-модели — data-loss на ровном месте."""
    if isinstance(v, dict):
        return v
    if isinstance(v, BaseModel):
        return v.model_dump()
    return None


def _dicts_with(key: str, v: Any) -> list[dict]:
    """Список dict'ов, у которых непустой `key` (field-salvage: мусор от модели отбрасываем, а не
    роняем весь разбор — тот же приём, что в clients/profile_extract._coerce_services)."""
    if not isinstance(v, list):
        return []
    out: list[dict] = []
    for x in v:
        d = _as_dict(x)
        if d is not None and str(d.get(key, "") or "").strip():
            out.append(d)
    return out


def _strings(v: Any) -> list[str]:
    if not isinstance(v, list):
        return []
    return [s.strip() for s in (str(x) for x in v if x is not None) if s.strip()]


class Company(BaseModel):
    """Юрлицо/организация. Адрес — публичный реквизит компании (не PII человека): его модель видит
    в тексте и может извлечь. Телефон и e-mail — НЕ здесь и нигде в схеме (см. докстринг модуля)."""

    legal_name: str | None = None
    founded: str | None = None  # как в тексте: «2016», «4 февраля 2016», «10 years in business»
    reg_no: str | None = None
    address: str | None = None
    mission: str | None = None


class Service(BaseModel):
    name: str
    description: str | None = None
    audience: str | None = None  # кому: «дилерам», «частным покупателям из СНГ»
    price: str | None = None  # текстом, как на сайте («от $4000»); НЕ считаем и не конвертируем


class Person(BaseModel):
    name: str
    role: str | None = None


class Fact(BaseModel):
    """Проверяемый факт с сайта («6000+ автомобилей», «100+ стран»). `source_url` проставляет КОД
    (чанк знает свои страницы) — модели URL не показываем, чтобы она их не выдумывала.

    `industry` — факт взят с БЛОГОВОЙ страницы, то есть это статистика ОТРАСЛИ, а не клиента
    («в 2023 на японских аукционах выставлено 3.08 млн авто»). Такие факты нужны владельцу в файле,
    но в контекст генерации RSA не идут: модель приписала бы их клиенту, а Google снимает объявления
    с недостоверными утверждениями (misrepresentation). Флаг ставит КОД по page_type страницы —
    не модель (правило: факты о деньгах решает код). См. `render_llm_context`."""

    claim: str
    source_url: str | None = None
    industry: bool = False


class FaqItem(BaseModel):
    q: str
    a: str | None = None


class DossierExtract(BaseModel):
    """Выход ОДНОГО map-вызова (один чанк текста). Сливаются кодом — clients/dossier_merge."""

    company: Company = Field(default_factory=Company)
    services: list[Service] = Field(default_factory=list)
    people: list[Person] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    markets: list[str] = Field(default_factory=list)  # страны/регионы работы
    usp: list[str] = Field(default_factory=list)  # чем лучше конкурентов (как заявлено на сайте)
    faq: list[FaqItem] = Field(default_factory=list)

    @field_validator("company", mode="before")
    @classmethod
    def _coerce_company(cls, v):
        return _as_dict(v) or {}

    @field_validator("services", mode="before")
    @classmethod
    def _coerce_services(cls, v):
        return _dicts_with("name", v)

    @field_validator("people", mode="before")
    @classmethod
    def _coerce_people(cls, v):
        return _dicts_with("name", v)

    @field_validator("facts", mode="before")
    @classmethod
    def _coerce_facts(cls, v):
        return _dicts_with("claim", v)

    @field_validator("faq", mode="before")
    @classmethod
    def _coerce_faq(cls, v):
        return _dicts_with("q", v)

    @field_validator("markets", "usp", mode="before")
    @classmethod
    def _coerce_strings(cls, v):
        return _strings(v)

    def is_empty(self) -> bool:
        return not any(
            [
                self.company.model_dump(exclude_none=True),
                self.services,
                self.people,
                self.facts,
                self.markets,
                self.usp,
                self.faq,
            ]
        )


class Dossier(BaseModel):
    """Сведённое досье (reduce). `overview`/`positioning` — единственная проза от модели; всё
    остальное собрано КОДОМ дедупом map-выходов (clients/dossier_merge), поэтому проверяемо тестом.

    `contacts` заполняет код из краула (tel:/mailto:/JSON-LD) — в LLM они не уезжали и не уедут:
    рендер `llm_context` их не печатает (clients/dossier_render)."""

    domain: str = ""
    website: str | None = None
    company: Company = Field(default_factory=Company)
    overview: str | None = None  # связное описание компании (проза, роль dossier)
    positioning: str | None = None  # чем берут клиента — сведённое УТП (проза, роль dossier)
    services: list[Service] = Field(default_factory=list)
    people: list[Person] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    markets: list[str] = Field(default_factory=list)
    usp: list[str] = Field(default_factory=list)
    faq: list[FaqItem] = Field(default_factory=list)
    contacts: list[dict] = Field(default_factory=list)  # [{kind, value}] — КОД, не модель
    socials: dict[str, str] = Field(default_factory=dict)
    pages_count: int = 0
    map_calls: int = 0  # сколько чанков реально ушло в модель (видно в логе и в футере файла)

    def counts(self) -> dict[str, int]:
        """Сводка для карточки подтверждения («услуг 12, людей 4, фактов 17») — без неё пользователь
        подтверждал бы невидимое (bot/texts.py перечисляет ИЗМЕНЁННЫЕ поля по именам)."""
        return {
            "services": len(self.services),
            "people": len(self.people),
            # Считаем факты О КЛИЕНТЕ: отраслевые (industry) лежат в файле справкой и в рекламу не идут —
            # обещать «фактов 17», где 9 из них про рынок, значит завышать в сводке подтверждения.
            "facts": len([f for f in self.facts if not f.industry]),
            "markets": len(self.markets),
            "usp": len(self.usp),
            "faq": len(self.faq),
            "pages": self.pages_count,
        }
