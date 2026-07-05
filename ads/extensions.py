"""SDK-билдеры ассетов-расширений Google Ads (§3-assets). Синхронные, вызываются из ads.mutations
через asyncio.to_thread (цепочка create-asset → link НЕ идемпотентна → НЕ run_ads_call: защита от
дублей — confirm.store.claim one-shot).

Здесь НЕТ гейтов (замок/confirmation) — их держит ads.mutations.apply_*. Здесь только сборка
операций SDK: AssetService.mutate_assets (создать ассет) → CampaignAssetService.mutate_campaign_assets
(привязать к кампании с нужным field_type). ads.assets остаётся под image/Pillow.
"""

from __future__ import annotations


def _campaign_rn(client, customer_id: str, campaign_id: str) -> str:
    return client.get_service("CampaignService").campaign_path(str(customer_id), str(campaign_id))


def _link_campaign_assets(
    client, customer_id: str, campaign_rn: str, asset_rns, field_type
) -> list[str]:
    """Привязать ассеты к кампании (campaign_asset) с заданным field_type. Возвращает resource_names
    созданных СВЯЗЕЙ (их и удаляют при откреплении)."""
    ca_svc = client.get_service("CampaignAssetService")
    ops = []
    for rn in asset_rns:
        op = client.get_type("CampaignAssetOperation")
        op.create.campaign = campaign_rn
        op.create.asset = rn
        op.create.field_type = field_type
        ops.append(op)
    resp = ca_svc.mutate_campaign_assets(customer_id=str(customer_id), operations=ops)
    return [r.resource_name for r in resp.results]


def _add_sitelinks_via_sdk(client, customer_id, campaign_id, sitelinks: list[dict]) -> dict:
    """sitelink_asset (+ final_urls на .create Asset, НЕ на sitelink_asset — v24) → link SITELINK."""
    cid = str(customer_id)
    asset_svc = client.get_service("AssetService")
    aops = []
    for s in sitelinks:
        op = client.get_type("AssetOperation")
        sl = op.create.sitelink_asset
        sl.link_text = s["link_text"]
        if s.get("description1"):
            sl.description1 = s["description1"]
        if s.get("description2"):
            sl.description2 = s["description2"]
        op.create.final_urls.append(
            s["final_url"]
        )  # final_urls — на Asset (.create), не на sitelink_asset
        aops.append(op)
    asset_rns = [
        r.resource_name for r in asset_svc.mutate_assets(customer_id=cid, operations=aops).results
    ]
    field_type = client.enums.AssetFieldTypeEnum.SITELINK
    link_rns = _link_campaign_assets(
        client, cid, _campaign_rn(client, cid, campaign_id), asset_rns, field_type
    )
    return {
        "customer_id": cid,
        "campaign_id": str(campaign_id),
        "kind": "sitelinks",
        "assets": asset_rns,
        "links": link_rns,
        "count": len(link_rns),
        "applied": True,
    }


def _add_callouts_via_sdk(client, customer_id, campaign_id, callouts: list[str]) -> dict:
    """callout_asset (callout_text) → link CALLOUT."""
    cid = str(customer_id)
    asset_svc = client.get_service("AssetService")
    aops = []
    for text in callouts:
        op = client.get_type("AssetOperation")
        op.create.callout_asset.callout_text = text
        aops.append(op)
    asset_rns = [
        r.resource_name for r in asset_svc.mutate_assets(customer_id=cid, operations=aops).results
    ]
    field_type = client.enums.AssetFieldTypeEnum.CALLOUT
    link_rns = _link_campaign_assets(
        client, cid, _campaign_rn(client, cid, campaign_id), asset_rns, field_type
    )
    return {
        "customer_id": cid,
        "campaign_id": str(campaign_id),
        "kind": "callouts",
        "assets": asset_rns,
        "links": link_rns,
        "count": len(link_rns),
        "applied": True,
    }


def _add_structured_snippets_via_sdk(
    client, customer_id, campaign_id, header: str, values: list[str]
) -> dict:
    """structured_snippet_asset (header из канонического англ. списка + values) → link STRUCTURED_SNIPPET."""
    cid = str(customer_id)
    asset_svc = client.get_service("AssetService")
    op = client.get_type("AssetOperation")
    ss = op.create.structured_snippet_asset
    ss.header = header
    ss.values.extend(values)
    asset_rns = [
        r.resource_name for r in asset_svc.mutate_assets(customer_id=cid, operations=[op]).results
    ]
    field_type = client.enums.AssetFieldTypeEnum.STRUCTURED_SNIPPET
    link_rns = _link_campaign_assets(
        client, cid, _campaign_rn(client, cid, campaign_id), asset_rns, field_type
    )
    return {
        "customer_id": cid,
        "campaign_id": str(campaign_id),
        "kind": "structured_snippets",
        "header": header,
        "assets": asset_rns,
        "links": link_rns,
        "count": len(link_rns),
        "applied": True,
    }


def _attach_image_asset_via_sdk(
    client, customer_id, campaign_id, image_bytes: bytes, name: str
) -> dict:
    """Загрузить image-ассет (reuse ads.assets.upload_image_asset) → привязать к кампании
    (campaign_asset MARKETING_IMAGE). Бинарь приходит из временного хранилища (см. service)."""
    from ads.assets import upload_image_asset

    cid = str(customer_id)
    asset_rn = upload_image_asset(client, cid, image_bytes, name)
    field_type = client.enums.AssetFieldTypeEnum.MARKETING_IMAGE
    link_rns = _link_campaign_assets(
        client, cid, _campaign_rn(client, cid, campaign_id), [asset_rn], field_type
    )
    return {
        "customer_id": cid,
        "campaign_id": str(campaign_id),
        "kind": "image",
        "assets": [asset_rn],
        "links": link_rns,
        "count": len(link_rns),
        "applied": True,
    }


def _add_call_asset_via_sdk(
    client, customer_id, campaign_id, phone_number: str, country_code: str
) -> dict:
    """call_asset (country_code + phone_number) → link CALL. Конверс-трекинг звонков не задаём
    (аккаунтное по умолчанию)."""
    cid = str(customer_id)
    asset_svc = client.get_service("AssetService")
    op = client.get_type("AssetOperation")
    call = op.create.call_asset
    call.country_code = country_code
    call.phone_number = phone_number
    asset_rns = [
        r.resource_name for r in asset_svc.mutate_assets(customer_id=cid, operations=[op]).results
    ]
    field_type = client.enums.AssetFieldTypeEnum.CALL
    link_rns = _link_campaign_assets(
        client, cid, _campaign_rn(client, cid, campaign_id), asset_rns, field_type
    )
    return {
        "customer_id": cid,
        "campaign_id": str(campaign_id),
        "kind": "call",
        "assets": asset_rns,
        "links": link_rns,
        "count": len(link_rns),
        "applied": True,
    }


def _add_promotion_via_sdk(
    client,
    customer_id,
    campaign_id,
    *,
    promotion_target: str,
    final_url: str,
    percent_off: float | None = None,
    money_off_units: float | None = None,
    currency: str | None = None,
    promo_code: str | None = None,
) -> dict:
    """promotion_asset (percent_off ИЛИ money_amount_off) → link PROMOTION. final_urls — на сам
    Asset (.create), не на promotion_asset. ВАЖНО (сверено с v24-исходником): percent_off в
    шкале «1_000_000 = 100%» → percent × 10_000."""
    cid = str(customer_id)
    asset_svc = client.get_service("AssetService")
    op = client.get_type("AssetOperation")
    asset = op.create
    asset.final_urls.append(final_url)  # final_urls — на Asset, НЕ на promotion_asset
    pa = asset.promotion_asset
    pa.promotion_target = promotion_target
    if percent_off is not None:
        pa.percent_off = int(round(float(percent_off) * 10_000))  # 1_000_000 == 100%
    else:
        pa.money_amount_off.amount_micros = int(round(float(money_off_units) * 1_000_000))
        pa.money_amount_off.currency_code = currency
    if promo_code:
        pa.promotion_code = promo_code
    asset_rns = [
        r.resource_name for r in asset_svc.mutate_assets(customer_id=cid, operations=[op]).results
    ]
    field_type = client.enums.AssetFieldTypeEnum.PROMOTION
    link_rns = _link_campaign_assets(
        client, cid, _campaign_rn(client, cid, campaign_id), asset_rns, field_type
    )
    return {
        "customer_id": cid,
        "campaign_id": str(campaign_id),
        "kind": "promotion",
        "assets": asset_rns,
        "links": link_rns,
        "count": len(link_rns),
        "applied": True,
    }


def _add_price_asset_via_sdk(
    client,
    customer_id,
    campaign_id,
    *,
    price_type: str,
    currency: str,
    language_code: str,
    offerings: list[dict],
) -> dict:
    """price_asset (type_ + language + 3–8 price_offerings) → link PRICE. Money: 1_000_000 micros =
    1 единица валюты. type_/unit — enum-члены в UPPER_CASE."""
    cid = str(customer_id)
    asset_svc = client.get_service("AssetService")
    op = client.get_type("AssetOperation")
    pa = op.create.price_asset
    pa.type_ = getattr(client.enums.PriceExtensionTypeEnum, price_type.upper())
    pa.language_code = language_code
    for o in offerings:
        off = client.get_type("PriceOffering")
        off.header = o["header"]
        off.description = o["description"]
        off.price.amount_micros = int(round(float(o["price_units"]) * 1_000_000))
        off.price.currency_code = currency
        off.final_url = o["final_url"]
        if o.get("unit"):
            off.unit = getattr(client.enums.PriceExtensionPriceUnitEnum, str(o["unit"]).upper())
        pa.price_offerings.append(off)
    asset_rns = [
        r.resource_name for r in asset_svc.mutate_assets(customer_id=cid, operations=[op]).results
    ]
    field_type = client.enums.AssetFieldTypeEnum.PRICE
    link_rns = _link_campaign_assets(
        client, cid, _campaign_rn(client, cid, campaign_id), asset_rns, field_type
    )
    return {
        "customer_id": cid,
        "campaign_id": str(campaign_id),
        "kind": "price",
        "assets": asset_rns,
        "links": link_rns,
        "count": len(link_rns),
        "offerings": len(offerings),
        "applied": True,
    }


# ── §19.7.1: новые семейства ассетов (Business name / Logo) + дисп. для composite-создания ──
def _link_customer_assets(client, customer_id: str, asset_rns, field_type) -> list[str]:
    """Привязать ассеты на уровне АККАУНТА (customer_asset) — для Business name/Logo, которые в
    Search показываются на уровне бренда/аккаунта. Возвращает resource_names связей."""
    svc = client.get_service("CustomerAssetService")
    ops = []
    for rn in asset_rns:
        op = client.get_type("CustomerAssetOperation")
        op.create.asset = rn
        op.create.field_type = field_type
        ops.append(op)
    resp = svc.mutate_customer_assets(customer_id=str(customer_id), operations=ops)
    return [r.resource_name for r in resp.results]


def _add_business_name_via_sdk(client, customer_id, business_name: str) -> dict:
    """Business name (§19.7.1): TextAsset → link BUSINESS_NAME на уровне аккаунта (customer_asset).
    ⚠️ Аккаунтный side-effect (имя бренда показывается во всех объявлениях аккаунта)."""
    cid = str(customer_id)
    asset_svc = client.get_service("AssetService")
    op = client.get_type("AssetOperation")
    op.create.text_asset.text = business_name
    asset_rns = [
        r.resource_name for r in asset_svc.mutate_assets(customer_id=cid, operations=[op]).results
    ]
    field_type = client.enums.AssetFieldTypeEnum.BUSINESS_NAME
    link_rns = _link_customer_assets(client, cid, asset_rns, field_type)
    return {
        "customer_id": cid,
        "kind": "business_name",
        "assets": asset_rns,
        "links": link_rns,
        "count": len(link_rns),
        "applied": True,
    }


def _add_business_logo_via_sdk(client, customer_id, image_bytes: bytes, name: str) -> dict:
    """Logo (§19.7.1): ImageAsset (reuse upload_image_asset) → link BUSINESS_LOGO на уровне аккаунта.
    ⚠️ Аккаунтный side-effect. Требования к изображению (1:1/4:1, ≤5120КБ) проверяет Pillow-слой."""
    from ads.assets import upload_image_asset

    cid = str(customer_id)
    asset_rn = upload_image_asset(client, cid, image_bytes, name)
    field_type = client.enums.AssetFieldTypeEnum.BUSINESS_LOGO
    link_rns = _link_customer_assets(client, cid, [asset_rn], field_type)
    return {
        "customer_id": cid,
        "kind": "logo",
        "assets": [asset_rn],
        "links": link_rns,
        "count": len(link_rns),
        "applied": True,
    }


def _add_lead_form_via_sdk(
    client,
    customer_id,
    campaign_id,
    *,
    business_name: str,
    headline: str,
    description: str,
    call_to_action_description: str,
    privacy_policy_url: str,
    cta_type: str = "GET_QUOTE",
    field_types=("FULL_NAME", "EMAIL", "PHONE_NUMBER"),
    post_submit_headline: str | None = None,
    post_submit_description: str | None = None,
) -> dict:
    """§19.7.1: lead_form_asset (v24) → link LEAD_FORM к кампании. CTA-тип и поля формы — по ИМЕНАМ
    enum (LeadFormCallToActionType / LeadFormFieldUserInputType), версионно-безопасно. Требует принятого
    на аккаунте Lead Form ToS и валидного privacy_policy_url — если аккаунт не готов, API отклоняет
    mutate_assets; вызывающий (`_attach_asset_specs_via_sdk`) ловит и кладёт ассет в `assets_skipped`
    (кампания-черновик $0/PAUSED не роняется). Стандартная пара create-asset → CampaignAsset-link."""
    cid = str(customer_id)
    asset_svc = client.get_service("AssetService")
    op = client.get_type("AssetOperation")
    lf = op.create.lead_form_asset
    lf.business_name = business_name
    lf.call_to_action_type = getattr(client.enums.LeadFormCallToActionTypeEnum, cta_type)
    lf.call_to_action_description = call_to_action_description
    lf.headline = headline
    lf.description = description
    lf.privacy_policy_url = privacy_policy_url
    if post_submit_headline:
        lf.post_submit_headline = post_submit_headline
    if post_submit_description:
        lf.post_submit_description = post_submit_description
    for ft in field_types:
        fld = client.get_type("LeadFormField")
        fld.input_type = getattr(client.enums.LeadFormFieldUserInputTypeEnum, ft)
        lf.fields.append(fld)
    asset_rn = asset_svc.mutate_assets(customer_id=cid, operations=[op]).results[0].resource_name
    field_type = client.enums.AssetFieldTypeEnum.LEAD_FORM
    link_rns = _link_campaign_assets(
        client, cid, _campaign_rn(client, cid, campaign_id), [asset_rn], field_type
    )
    return {
        "customer_id": cid,
        "campaign_id": str(campaign_id),
        "kind": "lead_form",
        "assets": [asset_rn],
        "links": link_rns,
        "count": len(link_rns),
        "applied": True,
    }


# Семейства, требующие внешней конфигурации аккаунта / отсутствующие в API v24 — НЕ создаём из
# профиля/текста (нет данных для валидной сборки). Чтобы не уронить composite-создание, дисп. бросает
# NotImplementedError, а вызывающий ловит и пропускает такой ассет с пометкой (assets_skipped). Точный
# статус в v24 (сверено интроспекцией установленного google-ads):
#   • location — AssetFieldType.LOCATION в v24 НЕТ: location-ассеты идут через AssetSet + place_id/
#     привязанный Google Business Profile (иной механизм, аккаунт-зависимо) — вне авто-генерации;
#   • affiliate_location — AffiliateLocationAsset в v24 ОТСУТСТВУЕТ (Google депрекировал) — недоступно.
# lead_form РЕАЛИЗОВАН выше (стандартная пара asset→link), из _CONFIG_GATED убран.
_CONFIG_GATED = {
    "location": "Location в v24 — через AssetSet + place_id/Google Business Profile (аккаунт-зависимо)",
    "affiliate_location": "Affiliate location удалён из Google Ads API v24 (депрекирован)",
}


def apply_asset_spec_via_sdk(client, customer_id, campaign_id, spec: dict) -> dict:
    """Дисп. для composite-создания: {family, params} → соответствующий _add_*_via_sdk на campaign_id.
    Семейства из _CONFIG_GATED бросают NotImplementedError (вызывающий ловит и пропускает в MVP)."""
    family = str(spec.get("family") or "")
    p = dict(spec.get("params") or {})
    if family in _CONFIG_GATED:
        raise NotImplementedError(_CONFIG_GATED[family])
    if family == "sitelinks":
        return _add_sitelinks_via_sdk(client, customer_id, campaign_id, p["sitelinks"])
    if family == "callouts":
        return _add_callouts_via_sdk(client, customer_id, campaign_id, p["callouts"])
    if family == "structured_snippets":
        return _add_structured_snippets_via_sdk(
            client, customer_id, campaign_id, p["header"], p["values"]
        )
    if family == "lead_form":
        return _add_lead_form_via_sdk(client, customer_id, campaign_id, **p)
    if family == "call":
        return _add_call_asset_via_sdk(
            client, customer_id, campaign_id, p["phone_number"], p["country_code"]
        )
    if family == "promotion":
        return _add_promotion_via_sdk(client, customer_id, campaign_id, **p)
    if family == "price":
        return _add_price_asset_via_sdk(client, customer_id, campaign_id, **p)
    if family == "business_name":
        return _add_business_name_via_sdk(client, customer_id, p["business_name"])
    if family == "business_logo":
        # §19.7.1: фото логотипа лежит во временном хранилище по media_id (бинарь НЕ в params);
        # квадратный кадр 1:1 подготовил бот. После успешной привязки чистим файлы (best-effort).
        from ads.assets import clear_pending_media, load_pending_media

        _, square = load_pending_media(p["media_id"])
        res = _add_business_logo_via_sdk(client, customer_id, square, p.get("name") or "logo")
        clear_pending_media(p["media_id"])
        return res
    raise ValueError(f"неизвестное семейство ассета: {family}")


def _remove_campaign_assets_via_sdk(client, customer_id, link_resource_names: list[str]) -> dict:
    """Открепить ассеты от кампании: CampaignAssetOperation.remove на каждый campaign_asset.
    Удаляется СВЯЗЬ, не сам Asset (ассет может быть привязан к другим кампаниям)."""
    cid = str(customer_id)
    ca_svc = client.get_service("CampaignAssetService")
    ops = []
    for rn in link_resource_names:
        op = client.get_type("CampaignAssetOperation")
        op.remove = rn
        ops.append(op)
    resp = ca_svc.mutate_campaign_assets(customer_id=cid, operations=ops)
    return {
        "customer_id": cid,
        "removed": [r.resource_name for r in resp.results],
        "count": len(resp.results),
        "applied": True,
    }
