"""§20 (P1-11): авто-подтяжка ФАКТОВ о клиенте из Google Ads аккаунта — БЕЗ LLM.

Только реальные GAQL-значения (никаких выдуманных данных): имя аккаунта, валюта, таймзона, число
активных кампаний, гео/языки таргетинга, домен из final_url объявлений. Результат — facts-only
patch (форма `ClientProfileExtract.to_patch`), который дальше идёт через ТОТ ЖЕ confirm-гейт памяти,
что и ручной ввод/краул (bot.main._present_memory_proposal → clients.execute). READ-ONLY: замок
чтения (`ensure_read_allowed`) держат сами read-хелперы; замок мутаций Google Ads не затрагивается.
"""

from __future__ import annotations

from urllib.parse import urlparse

from ads.client import build_client_async, discovered_read_children_meta
from ads.read import (
    account_currency,
    account_timezone,
    list_campaigns,
    read_campaign_config,
    read_campaign_targeting,
)
from core.config import normalize_customer_id
from core.resilience import run_ads_read_call
from reports.queries import count_active_campaigns

_MAX_CAMPAIGNS_SCAN = (
    5  # bounded: сколько кампаний обойти для гео/языков (не грузим большой аккаунт)
)


async def _safe(fn, *args, default=None, label: str = "acct_facts"):
    """Обёртка read-хелпера: значение или default при любом сбое (факт необязателен — не роняем всё)."""
    try:
        return await run_ads_read_call(fn, *args, label=label)
    except Exception:  # noqa: BLE001 — отсутствующий факт просто пропускаем (никогда не выдумываем)
        return default


def _domain(url: str) -> str:
    """https://sub.example.com/path → example.com (или '' если не распарсить)."""
    try:
        host = (urlparse(url).netloc or "").strip().lower()
    except Exception:  # noqa: BLE001
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


async def fetch_account_facts(customer_id: str, *, brand_hint: str = "") -> dict:
    """Собрать facts-only patch о клиенте из его Google Ads аккаунта. Пустые/недоступные факты
    пропускаются (никогда не подставляем выдуманное). Возвращает patch-dict для preview_merge."""
    cid = normalize_customer_id(customer_id)
    client = await build_client_async(cid)

    # brand ← descriptive_name аккаунта (из meta обхода MCC; без лишнего GAQL), иначе подсказка.
    meta = discovered_read_children_meta().get(cid)
    brand = (getattr(meta, "name", "") or brand_hint or "").strip()

    currency = (await _safe(account_currency, client, cid, default="", label="acct_currency")) or ""
    timezone = (await _safe(account_timezone, client, cid, default="", label="acct_timezone")) or ""
    active = await _safe(count_active_campaigns, client, cid, default=None, label="acct_active")

    # Гео/языки — из таргетинга нескольких ENABLED-кампаний (bounded, дедуп). Пусто = не заполняем.
    campaigns = await _safe(list_campaigns, client, cid, default=[], label="acct_campaigns") or []
    enabled = [c for c in campaigns if (c.get("status") or "").upper() == "ENABLED"] or campaigns
    geo_set: list[str] = []
    lang_set: list[str] = []
    for c in enabled[:_MAX_CAMPAIGNS_SCAN]:
        tgt = await _safe(read_campaign_targeting, client, cid, c.get("id"), label="acct_targeting")
        if tgt is None:
            continue
        for loc in tgt.locations or []:
            if loc and loc not in geo_set:
                geo_set.append(loc)
        for lng in tgt.languages or []:
            if lng and lng not in lang_set:
                lang_set.append(lng)

    # Домен сайта — из final_url первой кампании (объявления). Best-effort, только реальный URL.
    website = ""
    if enabled:
        cfg = await _safe(
            read_campaign_config, client, cid, enabled[0].get("name"), label="acct_config"
        )
        for ag in (getattr(cfg, "ad_groups", None) or []) if cfg else []:
            dom = _domain(getattr(ag, "final_url", "") or "")
            if dom:
                website = "https://" + dom
                break

    # Факт-строка для заметок (append-only; merge_notes не затрёт ручные заметки менеджера).
    note_bits: list[str] = []
    if currency:
        note_bits.append(f"валюта {currency}")
    if timezone:
        note_bits.append(f"таймзона {timezone}")
    if active is not None:
        note_bits.append(f"активных кампаний: {active}")
    notes = ("Из аккаунта Google Ads — " + ", ".join(note_bits) + ".") if note_bits else ""

    patch: dict = {}
    if brand:
        patch["brand"] = brand
    if geo_set:
        patch["geo"] = ", ".join(geo_set[:10])
    if lang_set:
        patch["language"] = ", ".join(lang_set[:5])
    if website:
        patch["website"] = website
    if notes:
        patch["notes"] = notes
    # Никаких replace-флагов, services/contacts, socials — факты только ДОПОЛНЯЮТ (§20.5).
    return patch
