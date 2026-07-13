"""§19.7.2: ассеты Call / Price / Promotion из РЕАЛЬНЫХ данных профиля клиента (§20).

В отличие от adcopy.assets_gen (LLM-автоген sitelinks/callouts/snippets/business_name), эти семейства
несут ФАКТЫ (телефон, цены, скидка) — их НЕЛЬЗЯ выдумывать (golden rule «реальные данные»). Строим
строго из структурированного профиля клиента; если нужных данных нет — бросаем ValueError с понятной
причиной, и вызывающий пропускает семейство (ничего не создаём). Итоговые params валидируем теми же
Pydantic-схемами, что и ручной ввод (agent.tools.schemas), — гарантия SDK-совместимости и лимитов.
"""

from __future__ import annotations

import re
from typing import Any

from adcopy.validate import asset_len_ok

# Семейства, наполняемые из фактов профиля (не LLM). Диспетчер — build_profile_asset.
PROFILE_ASSET_FAMILIES = ("call", "price", "promotion")

_PRICE_NUM_RE = re.compile(r"\d[\d\s.,]*")
_DISCOUNT_RE = re.compile(r"скид|акци|распродаж|sale|discount|промо|%\s*off|\boff\b", re.IGNORECASE)
_PCT_RE = re.compile(r"(\d{1,3})\s*%")


def _clean(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def _fit(text: str, kind: str) -> str:
    """Обрезать текст до лимита ассета (кириллица=1, считает КОД). Пустой → ''."""
    t = _clean(text)
    while t and not asset_len_ok(t, kind)[0]:
        t = t[:-1].strip()
    return t


def _first_phone(profile: dict) -> str | None:
    for c in profile.get("contacts") or []:
        if (c.get("kind") or "").lower() == "phone" and c.get("value"):
            return str(c["value"]).strip()
    return None


def _detect_currency(s: str) -> str | None:
    low = s.lower()
    if "$" in s or "usd" in low or "долл" in low:
        return "USD"
    if "€" in s or "eur" in low or "евро" in low:
        return "EUR"
    if "₴" in s or "грн" in low or "uah" in low or "гривн" in low:
        return "UAH"
    return None


def _to_number(raw_num: str) -> float | None:
    """Число из локале-разной записи цены. «,» и «.» — И десятичный разделитель, И разделитель
    тысяч (в разных локалях), поэтому решает ФОРМА, а не литерал: последний разделитель считается
    десятичным, только если за ним 1–2 цифры и он в строке один. Иначе — тысячи (выбрасываем).

    Раньше запятая безусловно считалась разделителем тысяч: «1 250,50 грн» → 125 050 (цена в 100 раз
    больше), и такой офер уходил в price-ассет — то есть клиенту показывали неверную цену."""
    t = re.sub(r"\s", "", raw_num).strip(".,")
    if not t:
        return None
    dec = max(t.rfind("."), t.rfind(","))
    if dec >= 0:
        sep, tail = t[dec], t[dec + 1 :]
        if len(tail) in (1, 2) and t.count(sep) == 1:  # «1250,50» / «20.5» → десятичная часть
            t = f"{re.sub(r'[.,]', '', t[:dec])}.{tail}"
        else:  # «4,000» / «1.234.567» → разделители тысяч
            t = re.sub(r"[.,]", "", t)
    try:
        return float(t)
    except ValueError:
        return None


def _parse_price(raw: Any) -> tuple[float | None, str | None]:
    """Свободная строка цены → (число, валюта). «от $4000»→(4000,'USD'); «10 000 грн»→(10000,'UAH');
    «1 250,50 грн»→(1250.5,'UAH'). Не распарсили число → (None, cur)."""
    if not raw:
        return None, None
    s = str(raw)
    cur = _detect_currency(s)
    m = _PRICE_NUM_RE.search(s)
    if not m:
        return None, cur
    val = _to_number(m.group(0))
    if val is None:
        return None, cur
    return (val if val > 0 else None), cur


def build_call_asset(profile: dict, *, country_code: str | None = None) -> dict:
    """Call-ассет из телефона профиля. Нет телефона → ValueError (не выдумываем номер).
    country_code=None → страна из конфига деплоя (D7), а не «UA» литералом."""
    from agent.tools.schemas import AddCallAsset

    phone = _first_phone(profile)
    if not phone:
        raise ValueError("в профиле клиента нет телефона (§20) — call-ассет не создаём")
    v = AddCallAsset(
        campaign="_",
        phone_number=phone,
        **({"country_code": country_code} if country_code else {}),  # пусто → default_factory схемы
    )
    return {
        "family": "call",
        "params": {"phone_number": v.phone_number, "country_code": v.country_code},
    }


def build_price_asset(
    profile: dict,
    *,
    final_url: str,
    language_code: str | None = None,
    currency_hint: str | None = None,
) -> dict:
    """Price-ассет из услуг профиля с ЧИСЛОВОЙ ценой (≥3, единая валюта). Меньше 3 распарсенных
    оферов → ValueError (не добираем выдуманными ценами). language_code=None → язык из конфига
    деплоя (D7), а не «uk» литералом."""
    from agent.tools.schemas import AddPriceAsset, PriceOfferingItem

    if not final_url:
        raise ValueError("нет final_url — прайс-ассет требует посадочную у каждого офера")
    # Подсказка валюты — ЛЮБОЙ ISO-код (валюта аккаунта): прежний список ("USD","UAH","EUR") молча
    # ронял подсказку на AUD/UGX-аккаунте → цены без символа валюты отбрасывались → «<3 услуг».
    hint = (currency_hint or "").strip().upper()
    hint = hint if re.fullmatch(r"[A-Z]{3}", hint) else None
    offers: list[PriceOfferingItem] = []
    currency: str | None = None
    for svc in profile.get("services") or []:
        name = _clean(svc.get("name"))
        if not name:
            continue
        val, cur = _parse_price(svc.get("price"))
        if val is None:
            continue
        cur = cur or hint
        if cur is None:
            continue
        if currency is None:
            currency = cur
        if cur != currency:  # смешанные валюты — берём только первую (прайс-ассет одновалютный)
            continue
        header = _fit(name, "price_header")
        desc = _fit(_clean(svc.get("description")) or name, "price_desc")
        if not header or not desc:
            continue
        offers.append(
            PriceOfferingItem(header=header, description=desc, price_units=val, final_url=final_url)
        )
    if len(offers) < 3:
        raise ValueError("в профиле <3 услуг с числовой ценой — прайс-ассет не создаём")
    v = AddPriceAsset(
        campaign="_",
        currency=currency,
        offerings=offers[:8],
        **({"language_code": language_code} if language_code else {}),  # пусто → default_factory
    )
    return {
        "family": "price",
        "params": {
            "price_type": v.price_type,
            "currency": v.currency,
            "language_code": v.language_code,
            "offerings": [o.model_dump() for o in v.offerings],
        },
    }


def _promo_target(text: str, pct_match: re.Match) -> str:
    """Реальный объект акции из текста профиля: слова сразу после «N%» (до знака препинания)."""
    tail = text[pct_match.end() : pct_match.end() + 40]
    tail = re.split(r"[.;!?\n]", tail, maxsplit=1)[0].strip(" -–—:,")
    return tail


def build_promotion_asset(profile: dict, *, final_url: str) -> dict:
    """Promotion-ассет ТОЛЬКО при ЯВНОЙ скидке в профиле (ключевое слово скидки + число %). Объект
    акции — реальный текст профиля. Нет явной скидки/процента → ValueError (не выдумываем акцию)."""
    from agent.tools.schemas import AddPromotion

    if not final_url:
        raise ValueError("нет final_url — promotion-ассет требует посадочную")
    services = profile.get("services") or []
    text = " ".join(
        x
        for x in [
            _clean(profile.get("notes")),
            _clean(profile.get("business_desc")),
            *[_clean(s.get("name")) for s in services],
            *[_clean(s.get("description")) for s in services],
        ]
        if x
    )
    if not _DISCOUNT_RE.search(text):
        raise ValueError("в профиле нет явной акции/скидки — promotion не создаём")
    m = _PCT_RE.search(text)
    if not m:
        raise ValueError("в профиле нет числа скидки (%) — promotion не создаём")
    pct = int(m.group(1))
    if not (1 <= pct <= 100):
        raise ValueError("скидка вне диапазона 1–100% — promotion не создаём")
    target = _fit(_promo_target(text, m), "promotion_target") or _fit(
        _clean(profile.get("brand")), "promotion_target"
    )
    if not target:
        raise ValueError("не удалось определить объект акции из профиля — promotion не создаём")
    v = AddPromotion(
        campaign="_", promotion_target=target, final_url=final_url, percent_off=float(pct)
    )
    return {
        "family": "promotion",
        "params": {
            "promotion_target": v.promotion_target,
            "final_url": v.final_url,
            "percent_off": v.percent_off,
            "money_off_units": None,
            "currency": None,
            "promo_code": None,
        },
    }


def build_profile_asset(
    family: str,
    profile: dict,
    *,
    final_url: str | None = None,
    country_code: str | None = None,  # None → страна из конфига деплоя (D7), не «UA»
    language_code: str | None = None,  # None → язык из конфига деплоя (D7), не «uk»
    currency_hint: str | None = None,
) -> dict:
    """Диспетчер §19.7.2: family (call|price|promotion) + структурированный профиль → asset_spec.
    ValueError, если реальных данных не хватает (вызывающий пропускает семейство — не выдумываем)."""
    if family == "call":
        return build_call_asset(profile, country_code=country_code)
    if family == "price":
        return build_price_asset(
            profile,
            final_url=final_url or "",
            language_code=language_code,
            currency_hint=currency_hint,
        )
    if family == "promotion":
        return build_promotion_asset(profile, final_url=final_url or "")
    raise ValueError(f"семейство «{family}» не строится из профиля")
