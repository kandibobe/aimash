"""§19.7.2: автогенерация наполнения ассета-расширения под клиента (LLM) для визарда создания.

Модель предлагает наполнение (sitelinks/callouts/structured snippets/business name) по теме
кампании, URL и контенту страницы; ДЛИНУ/состав считает КОД (adcopy.validate, кириллица=1 —
golden rule #4). Возвращает asset_spec {family, params} в формате, который понимает
ads.extensions.apply_asset_spec_via_sdk (composite-создание). На вход в SDK ассеты идут только
после финального confirm-гейта (визард накапливает их в черновике).

Семейства с реальными данными (call/promotion/price/location/logo) НЕ автогенерим — модель не
должна выдумывать телефон/цены/адрес; они добавляются вручную (через /campaigns) или требуют
профиля клиента. Поддержанные здесь — текстовые, выводимые из темы/сайта.
"""

from __future__ import annotations

import json

from adcopy.validate import (
    STRUCTURED_SNIPPET_HEADERS,
    asset_len_ok,
    assert_asset_len,
)
from agent.router import chat

# Семейства, для которых поддержана автогенерация текста (из темы/сайта).
AUTOGEN_FAMILIES = ("sitelinks", "callouts", "structured_snippets", "business_name")


def _extract_json(content: str, opener: str, closer: str):
    s = content or ""
    start, end = s.find(opener), s.rfind(closer)
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(s[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None


async def _llm(system: str, user: str, opener: str, closer: str):
    msg = await chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        role="copy",
        temperature=0.5,
    )
    return _extract_json(getattr(msg, "content", "") or "", opener, closer)


def _ctx(topic: str, url: str | None, profile: str, language: str) -> str:
    out = f"Язык: {language}. Тема кампании: {topic}."
    if url:
        out += f"\nURL: {url}"
    if (profile or "").strip():
        out += f"\nО клиенте/странице:\n{profile[:1200]}"
    return out


async def _gen_sitelinks(
    topic, url, profile, language, site_pages: list[dict] | None = None
) -> dict:
    """§20.6: при наличии карты страниц краула (site_pages из client_site_pages) модель обязана
    брать url ТОЛЬКО из этого списка — ссылки ведут на РЕАЛЬНЫЕ страницы, а не выдуманные. КОД
    валидирует возвращённый url по allow-set (не из списка → fallback base_url). Без site_pages —
    прежнее поведение (все ссылки на base_url)."""
    pages = [p for p in (site_pages or []) if p.get("url")]
    allowed = {p["url"] for p in pages}
    if pages:
        page_lines = "\n".join(
            f"{i}. {p['url']} — {p.get('title') or ''} ({p.get('page_type') or 'page'})"
            for i, p in enumerate(pages, 1)
        )
        system = (
            "Ты — специалист по Google Ads. Предложи 4–6 быстрых ссылок (sitelinks) на ключевые "
            "страницы сайта по теме. url бери СТРОГО из списка «Реальные страницы сайта» ниже "
            "(никаких выдуманных путей). Верни СТРОГО JSON-массив объектов "
            '[{"link_text":"≤25 симв","description1":"≤35","description2":"≤35","url":"из списка"}] '
            "без пояснений."
        )
        user = _ctx(topic, url, profile, language) + (
            f"\nРеальные страницы сайта (бери url ТОЛЬКО отсюда):\n{page_lines}"
        )
    else:
        system = (
            "Ты — специалист по Google Ads. Предложи 4–6 быстрых ссылок (sitelinks) на ключевые "
            "страницы сайта по теме. Верни СТРОГО JSON-массив объектов "
            '[{"link_text":"≤25 симв","description1":"≤35","description2":"≤35"}] без пояснений.'
        )
        user = _ctx(topic, url, profile, language)
    data = await _llm(system, user, "[", "]")
    base_url = url or ""
    out: list[dict] = []
    for o in data or []:
        if not isinstance(o, dict):
            continue
        lt = (o.get("link_text") or "").strip()
        if not lt or not asset_len_ok(lt, "sitelink_text")[0]:
            continue
        # golden rule #4/#6: url из ответа модели принимаем ТОЛЬКО из allow-set краула;
        # всё прочее (выдумка/мусор) — честный fallback на base_url.
        page_url = (o.get("url") or "").strip()
        final_url = page_url if page_url in allowed else base_url
        item = {"link_text": lt, "final_url": final_url}
        for k, kind in (("description1", "sitelink_desc"), ("description2", "sitelink_desc")):
            d = (o.get(k) or "").strip()
            if d and asset_len_ok(d, kind)[0]:
                item[k] = d
        out.append(item)
        if len(out) >= 6:
            break
    if not out or not base_url:
        raise ValueError("не удалось сгенерировать корректные sitelinks (нужен URL кампании)")
    return {"family": "sitelinks", "params": {"sitelinks": out}}


async def _gen_callouts(topic, url, profile, language) -> dict:
    data = await _llm(
        "Ты — специалист по Google Ads. Предложи 4–6 коротких уточнений (callouts) — УТП без "
        "ссылок, каждое ≤25 символов. Верни СТРОГО JSON-массив строк без пояснений.",
        _ctx(topic, url, profile, language),
        "[",
        "]",
    )
    out: list[str] = []
    seen: set[str] = set()
    for x in data or []:
        if not isinstance(x, (str, int, float)):
            continue
        t = str(x).strip()
        if t and asset_len_ok(t, "callout")[0] and t.casefold() not in seen:
            seen.add(t.casefold())
            out.append(t)
        if len(out) >= 6:
            break
    if not out:
        raise ValueError("не удалось сгенерировать корректные callouts")
    return {"family": "callouts", "params": {"callouts": out}}


async def _gen_snippets(topic, url, profile, language) -> dict:
    headers = ", ".join(sorted(STRUCTURED_SNIPPET_HEADERS))
    data = await _llm(
        "Ты — специалист по Google Ads. Подбери ОДНО структурное описание: header строго из списка "
        f"[{headers}] и 4–8 значений (каждое ≤25 симв) по теме. Верни СТРОГО JSON-объект "
        '{"header":"...","values":["..."]} без пояснений.',
        _ctx(topic, url, profile, language),
        "{",
        "}",
    )
    if not isinstance(data, dict):
        raise ValueError("не удалось сгенерировать структурное описание")
    header = (data.get("header") or "").strip()
    if header not in STRUCTURED_SNIPPET_HEADERS:
        raise ValueError("header не из канонического списка Google")
    values = [
        str(v).strip()
        for v in (data.get("values") or [])
        if str(v).strip() and asset_len_ok(str(v).strip(), "snippet_value")[0]
    ]
    if len(values) < 3:
        raise ValueError("нужно минимум 3 значения структурного описания")
    return {"family": "structured_snippets", "params": {"header": header, "values": values[:10]}}


async def _gen_business_name(topic, url, profile, language) -> dict:
    data = await _llm(
        "Ты — специалист по Google Ads. Предложи короткое название бренда/бизнеса (≤25 символов) "
        'по теме/сайту. Верни СТРОГО JSON-объект {"business_name":"..."} без пояснений.',
        _ctx(topic, url, profile, language),
        "{",
        "}",
    )
    name = ((data or {}).get("business_name") or "").strip() if isinstance(data, dict) else ""
    if not name:
        raise ValueError("не удалось сгенерировать название бизнеса")
    assert_asset_len(name, "business_name")  # ≤25, кириллица=1
    return {"family": "business_name", "params": {"business_name": name}}


_GENERATORS = {
    "sitelinks": _gen_sitelinks,
    "callouts": _gen_callouts,
    "structured_snippets": _gen_snippets,
    "business_name": _gen_business_name,
}


async def generate_asset(
    family: str,
    *,
    topic: str,
    url: str | None = None,
    profile: str = "",
    language: str = "ru",
    site_pages: list[dict] | None = None,
) -> dict:
    """Сгенерировать наполнение ассета семейства family → asset_spec {family, params}. Бросает
    ValueError, если семейство не автогенерится или наполнение не прошло код-валидацию (длины и т.п.).
    site_pages (§20.6, только sitelinks) — карта страниц краула: ссылки на РЕАЛЬНЫЕ url сайта."""
    gen = _GENERATORS.get(family)
    if gen is None:
        raise ValueError(f"автогенерация не поддержана для семейства «{family}»")
    if family == "sitelinks":
        return await _gen_sitelinks(topic, url, profile, language, site_pages=site_pages)
    return await gen(topic, url, profile, language)
