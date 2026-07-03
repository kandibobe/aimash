"""Генерация RSA-текстов (заголовки/описания) через LLM (OpenRouter, роль "copy").

ЖЁСТКО (golden rule #4): длину считает КОД, не модель. Любой элемент сверх лимита
(headline ≤30, description ≤90; кириллица = 1 символ) ОТБРАСЫВАЕТСЯ; при нехватке —
догенерация (до MAX_ATTEMPTS). Модель не трогает SDK — это только текст; применение к
объявлению идёт отдельно через mutation-гейт после курации (§10/§19.5.2: поэлементное
подтверждение ✅/✏️/❌ либо правка списком — adcopy.session + bot.main)."""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import BaseModel, Field

from adcopy.validate import LIMITS, rsa_len, validate
from agent.router import chat

DEFAULT_HEADLINES = 15
DEFAULT_DESCRIPTIONS = 4
MAX_ATTEMPTS = 3


class CopyBrief(BaseModel):
    """Контекст для генерации (заполняет агент/пользователь, валидирует код)."""

    topic: str
    keywords: list[str] = Field(default_factory=list, max_length=50)
    usp: str | None = None  # уникальное торговое предложение (обычно из посадочной страницы)
    profile: str | None = None  # §20: контекст профиля клиента (бренд/бизнес/услуги/УТП/цены)
    tone: str | None = None  # тон (напр. «деловой», «дружелюбный»)
    geo: str | None = None
    language: str = "ru"
    n_headlines: int = Field(default=DEFAULT_HEADLINES, ge=1, le=30)
    n_descriptions: int = Field(default=DEFAULT_DESCRIPTIONS, ge=1, le=10)


@dataclass
class RsaDraft:
    headlines: list[str]
    descriptions: list[str]
    dropped_headlines: int = 0  # отброшено кодом за превышение лимита
    dropped_descriptions: int = 0
    attempts: int = 0


_SYSTEM = (
    "Ты — копирайтер Google Ads (RSA). Возвращай СТРОГО JSON-объект вида "
    '{"headlines": ["…"], "descriptions": ["…"]} без пояснений и без markdown. '
    "Заголовок — до 30 видимых символов, описание — до 90 (кириллица считается как 1 символ). "
    "ЛИМИТ ДЛИНЫ — ЖЁСТКИЙ: вариант сверх лимита ОТБРАСЫВАЕТСЯ кодом, поэтому пиши компактно и "
    "мысленно считай символы (описание ≤90 — это примерно короткое предложение, а не два). "
    "Без КАПСА и спама, с призывом к действию, варианты уникальны и не дублируют друг друга."
)

# B1: ремонт слишком длинных вариантов — отдельный проход «сократи до лимита» (модель считает
# символы плохо → сначала просим её же ужать конкретные длинные строки, затем детерминированный трим).
_REPAIR_SYSTEM = (
    "Ты — редактор Google Ads. Тебе дают строки, которые ПРЕВЫСИЛИ лимит длины. Перепиши КАЖДУЮ "
    "короче, СОХРАНИВ смысл, язык и призыв к действию, чтобы гарантированно уложиться в лимит "
    '(кириллица = 1 символ). Верни СТРОГО JSON {"items": ["…"]} без пояснений и без markdown.'
)


def _user_prompt(brief: CopyBrief, need_h: int, need_d: int) -> str:
    parts = [
        # §19.5: тексты — на языке АУДИТОРИИ кампании (для Кении en/sw), а не языка менеджера.
        f"Пиши ВСЕ заголовки и описания на языке аудитории: {brief.language}.",
        f"Тематика (что рекламируем): {brief.topic}.",
    ]
    if brief.keywords:
        parts.append("Ключевые слова: " + ", ".join(brief.keywords) + ".")
    if brief.profile:
        # §20: профиль клиента — первым (бренд/УТП авторитетнее лендинга), с капом (токены).
        parts.append(f"О клиенте: {brief.profile[:1200]}.")
    if brief.usp:
        parts.append(f"УТП: {brief.usp}.")
    if brief.tone:
        parts.append(f"Тон: {brief.tone}.")
    if brief.geo:
        parts.append(f"Гео: {brief.geo}.")
    # B1: просим ТОЛЬКО недостающий вид (раньше в догенерации летело «Сгенерируй 0 заголовков …» —
    # prompt-smell, а бюджет попыток съедали заголовки, оставляя описания недобранными).
    reqs: list[str] = []
    if need_h > 0:
        reqs.append(f"{need_h} заголовков (≤{LIMITS['headline']} символов каждый)")
    if need_d > 0:
        reqs.append(f"{need_d} описаний (≤{LIMITS['description']} символов каждое)")
    parts.append("Сгенерируй " + " и ".join(reqs) + ".")
    if need_d > 0:
        parts.append(
            f"КРИТИЧНО: каждое описание — строго ≤{LIMITS['description']} символов "
            "(кириллица=1); превысишь — вариант выбросят. Лучше короче."
        )
    return " ".join(parts)


def _parse(content: str) -> tuple[list[str], list[str]]:
    """Достаёт JSON-объект из ответа модели (терпимо к ```-обёрткам/тексту вокруг)."""
    s = content or ""
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end < start:
        return [], []
    try:
        data = json.loads(s[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return [], []
    if not isinstance(data, dict):
        return [], []
    h = [str(x) for x in data.get("headlines", []) if isinstance(x, (str, int, float))]
    d = [str(x) for x in data.get("descriptions", []) if isinstance(x, (str, int, float))]
    return h, d


def _ingest(
    raw: list[str],
    kind: str,
    limit: int,
    out: list[str],
    seen: set[str],
    too_long: list[str] | None = None,
) -> int:
    """Принять валидные элементы в out (с дедупом), вернуть число отброшенных за длину.
    Длину/лимит считает КОД (validate) — модель не может протащить слишком длинный текст.
    Отброшенные тоже попадают в seen — чтобы тот же текст не пере-считывался в догенерации;
    их оригиналы копим в too_long для последующего ремонта/трима (B1)."""
    dropped = 0
    for item in raw:
        if len(out) >= limit:
            break
        t = (item or "").strip()
        if not t:
            continue
        key = t.casefold()
        if key in seen:
            continue
        seen.add(key)  # запомнить (принятый ИЛИ отброшенный) — без повторной оценки
        ok, _ = validate(t, kind)
        if not ok:
            dropped += 1
            if too_long is not None:
                too_long.append(t)  # B1: кандидат на ремонт «сократи до лимита» / трим
            continue
        out.append(t)
    return dropped


def _parse_items(content: str) -> list[str]:
    """Достать список строк из ответа-ремонтника ({"items": ["…"]}), терпимо к обёрткам."""
    s = content or ""
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        data = json.loads(s[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return []
    items = data.get("items") if isinstance(data, dict) else None
    return [str(x) for x in items or [] if isinstance(x, (str, int, float))]


async def _repair_too_long(
    brief: CopyBrief, kind: str, too_long: list[str], out: list[str], seen: set[str], target: int
) -> None:
    """B1: попросить модель ужать конкретные слишком длинные строки до лимита. Best-effort —
    сбой ремонта НЕ роняет генерацию (детерминированный трим добьёт минимум ниже)."""
    if len(out) >= target or not too_long:
        return
    limit = LIMITS[kind]
    prompt = (
        f"Язык: {brief.language}. Сократи каждую строку до ≤{limit} видимых символов (кириллица=1), "
        "сохрани смысл и призыв. Строки:\n" + "\n".join(f"- {t}" for t in too_long[: target * 3])
    )
    try:
        msg = await chat(
            [
                {"role": "system", "content": _REPAIR_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            role="copy",
            temperature=0.3,
        )
    except Exception:  # noqa: BLE001 — ремонт опционален, не роняем весь генератор
        return
    _ingest(_parse_items(getattr(msg, "content", "") or ""), kind, target, out, seen)


def _hard_trim(text: str, limit: int) -> str:
    """Детерминированно обрезать строку до ≤limit видимых символов (rsa_len) по границе слова;
    если одно слово длиннее лимита — режем по code points. Гарантия минимума, когда модель
    упорно возвращает слишком длинное (пользователь всё равно проверит в списке §10)."""
    if rsa_len(text) <= limit:
        return text
    words = text.split()
    while words:
        cand = " ".join(words)
        if rsa_len(cand) <= limit:
            return cand.rstrip(" ,.;:—-")
        words.pop()
    t = text
    while t and rsa_len(t) > limit:
        t = t[:-1]
    return t.strip()


def _fill_by_trim(
    too_long: list[str], kind: str, out: list[str], seen: set[str], target: int
) -> None:
    """Добить out до target детерминированным тримом слишком длинных кандидатов (B1)."""
    limit = LIMITS[kind]
    for original in too_long:
        if len(out) >= target:
            break
        trimmed = _hard_trim(original, limit).strip()
        if not trimmed:
            continue
        key = trimmed.casefold()
        if key in seen:
            continue
        ok, _ = validate(trimmed, kind)
        if ok:
            seen.add(key)
            out.append(trimmed)


async def generate_rsa(brief: CopyBrief) -> RsaDraft:
    """Сгенерировать валидные RSA-тексты. Догенерирует, пока не наберёт нужное число или
    не исчерпает MAX_ATTEMPTS. Возвращает только уложившиеся в лимит элементы."""
    headlines: list[str] = []
    descriptions: list[str] = []
    seen_h: set[str] = set()
    seen_d: set[str] = set()
    too_long_h: list[str] = []  # B1: слишком длинные кандидаты — на ремонт/трим
    too_long_d: list[str] = []
    dropped_h = dropped_d = attempts = 0

    while attempts < MAX_ATTEMPTS and (
        len(headlines) < brief.n_headlines or len(descriptions) < brief.n_descriptions
    ):
        attempts += 1
        need_h = brief.n_headlines - len(headlines)
        need_d = brief.n_descriptions - len(descriptions)
        msg = await chat(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _user_prompt(brief, need_h, need_d)},
            ],
            role="copy",
            # B1: на ретраях снижаем температуру — меньше «фантазии по длине», стабильнее лимит.
            temperature=0.8 if attempts == 1 else 0.5,
        )
        raw_h, raw_d = _parse(getattr(msg, "content", "") or "")
        before = (len(headlines), len(descriptions))
        dropped_h += _ingest(raw_h, "headline", brief.n_headlines, headlines, seen_h, too_long_h)
        dropped_d += _ingest(
            raw_d, "description", brief.n_descriptions, descriptions, seen_d, too_long_d
        )
        if not raw_h and not raw_d:
            break  # модель не вернула ничего парсимого — нет смысла повторять
        if (len(headlines), len(descriptions)) == before:
            break  # попытка не дала НИ одного нового валидного варианта — модель «застряла»

    # B1: описания стабильно переполняют лимит (длинные предложения) — если недобрали, чиним:
    # 1) LLM-ремонт «сократи конкретные длинные строки до ≤лимита» (лучше по качеству),
    # 2) детерминированный трим по границе слова — ГАРАНТИЯ минимума (модель не считает символы).
    if len(descriptions) < brief.n_descriptions and too_long_d:
        await _repair_too_long(
            brief, "description", too_long_d, descriptions, seen_d, brief.n_descriptions
        )
        _fill_by_trim(too_long_d, "description", descriptions, seen_d, brief.n_descriptions)
    if len(headlines) < brief.n_headlines and too_long_h:
        await _repair_too_long(brief, "headline", too_long_h, headlines, seen_h, brief.n_headlines)
        _fill_by_trim(too_long_h, "headline", headlines, seen_h, brief.n_headlines)

    return RsaDraft(headlines, descriptions, dropped_h, dropped_d, attempts)
