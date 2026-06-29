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
