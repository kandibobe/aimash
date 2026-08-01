"""§19.7/§19.8 — ассеты: состав виден ДО ✅, выбор подмножества, аккаунтный side-effect (волна 2.5).

Что закрыто:
1. §19.8 «полный набор ассетов» — и сводка Этапа 7, и карточка confirm-гейта показывали ТОЛЬКО
   счётчики («ассеты: 2 переисп., 3 новых»). Какой телефон уедет в объявление, какая скидка, какие
   доп. ссылки — менеджер не видел нигде и жал ✅ вслепую.
2. business_name/business_logo линкуются как CustomerAsset — это УРОВЕНЬ АККАУНТА (все кампании
   клиента). Нигде не раскрывалось.
3. §19.7 «выбрать ассеты» — бот линковал ВСЕ найденные на аккаунте, без пикера.
4. Аккаунтные ассеты (BUSINESS_NAME/LOGO) попадали в reuse-список из customer_asset и молча гибли
   при попытке линковки как CampaignAsset, раздувая счётчик «переиспользовано N».
5. Потеря группы переиспользуемых ассетов была видна лишь как «переиспользовано 3» вместо 7.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads import mutations as M  # noqa: E402
from ads.read import ACCOUNT_LEVEL_FIELD_TYPES  # noqa: E402
from core import texts  # noqa: E402

SPECS = [
    {
        "family": "sitelinks",
        "params": {
            "sitelinks": [
                {"link_text": "Услуги", "final_url": "https://x.ua/services"},
                {"link_text": "Цены", "final_url": "https://x.ua/price"},
            ]
        },
    },
    {"family": "callouts", "params": {"callouts": ["Гарантия 2 года", "Доставка 24ч"]}},
    {"family": "call", "params": {"phone_number": "+380671234567", "country_code": "UA"}},
    {
        "family": "promotion",
        "params": {"promotion_target": "мойка", "percent_off": 15.0, "final_url": "https://x.ua"},
    },
]
REUSE = [
    {"asset_resource_name": "customers/1/assets/1", "field_type": "SITELINK"},
    {"asset_resource_name": "customers/1/assets/2", "field_type": "SITELINK"},
    {"asset_resource_name": "customers/1/assets/3", "field_type": "CALLOUT"},
]


# ── §19.8: СОДЕРЖИМОЕ, а не счётчики ──────────────────────────────────────────────
def test_assets_block_shows_content_not_counters():
    block = texts.fmt_assets_block({"new": SPECS, "reuse_links": REUSE}, "ru", images=2)
    for must in ("Услуги", "https://x.ua/services", "Гарантия 2 года", "+380671234567", "-15%"):
        assert must in block, f"в блоке ассетов нет «{must}» — менеджер снова подтверждает вслепую"
    assert "SITELINK×2" in block and "CALLOUT×1" in block  # состав переиспользуемых
    assert "Изображения: 2" in block


def test_assets_block_empty_when_no_assets():
    assert texts.fmt_assets_block({"new": [], "reuse_links": []}, "ru") == ""
    assert texts.fmt_assets_block(None, "ru") == ""


def test_assets_block_warns_about_account_level_families():
    """Бренд-ассеты уходят в CustomerAsset → действуют на ВСЕ кампании клиента. Менеджер должен
    узнать это ДО ✅, а не из вопроса «почему в другой кампании поменялся логотип»."""
    block = texts.fmt_assets_block(
        {"new": [{"family": "business_logo", "params": {"name": "logo"}}]}, "ru"
    )
    assert "АККАУНТУ" in block and "во всех" in block
    en = texts.fmt_assets_block(
        {"new": [{"family": "business_name", "params": {"business_name": "X"}}]}, "en"
    )
    assert "ACCOUNT level" in en
    # обычный ассет предупреждения НЕ несёт (иначе баннер обесценится)
    assert "АККАУНТУ" not in texts.fmt_assets_block({"new": [SPECS[1]]}, "ru")


def test_final_summary_includes_asset_content():
    """Этап 7 (§19.8): сводка перед созданием несёт содержимое ассетов, а не «3 новых»."""
    state = {
        "settings": {"campaign_name": "C", "budget_daily_micros": 50_000_000, "currency": "USD"},
        "ad": {"final_url": "https://x.ua", "headlines": ["h"], "descriptions": ["d"]},
        "keywords": {"list": ["k"], "match_type": "phrase"},
        "images": {"media_ids": ["m1"]},
        "assets": {"new": SPECS, "reuse_links": REUSE},
        "url_options": {},
    }
    s = texts.fmt_cc_final_summary(state, "ru")
    assert "+380671234567" in s and "Гарантия 2 года" in s
    assert "SITELINK×2" in s
    assert "ассеты: 2 переисп." not in s  # старая строка-счётчик ушла


def test_confirm_card_includes_assets_plain_text():
    """Карточка ✅ (plain text, esc накладывает показ): ассеты создаются ТОЙ ЖЕ операцией — значит,
    видны в точке подтверждения. И без HTML-тегов: иначе показ даст `&lt;b&gt;`."""
    card = texts.fmt_search_proposal_summary(
        "C",
        "https://x.ua",
        50.0,
        ["h"],
        ["d"],
        ["k"],
        "phrase",
        "ru",
        currency="USD",
        assets={"new": SPECS, "reuse_links": REUSE},
        images=1,
    )
    assert "+380671234567" in card and "Услуги" in card and "SITELINK×2" in card
    assert "<b>" not in card and "&" not in card  # plain text, без двойного экранирования
    # без ассетов карточка не меняется (обратная совместимость /newsearch и /clone)
    plain = texts.fmt_search_proposal_summary(
        "C", "https://x.ua", 50.0, ["h"], ["d"], ["k"], "phrase", "ru", currency="USD"
    )
    assert "Ассеты" not in plain


def test_reuse_toggle_recomputes_links():
    """Логика тумблера (та же, что в cc_reuse_type): в мутацию уезжает ВЫБРАННОЕ подмножество."""
    st = {"assets": {"reuse_candidates": list(REUSE), "reuse_excluded": [], "reuse_links": []}}

    def toggle(ft: str) -> None:
        a = st["assets"]
        excl = set(a["reuse_excluded"])
        excl.symmetric_difference_update({ft})
        a["reuse_excluded"] = sorted(excl)
        a["reuse_links"] = [c for c in a["reuse_candidates"] if c["field_type"] not in excl]

    toggle("SITELINK")
    assert [c["field_type"] for c in st["assets"]["reuse_links"]] == ["CALLOUT"]
    toggle("SITELINK")  # обратно
    assert len(st["assets"]["reuse_links"]) == 3


# ── Аккаунтные типы не идут в reuse ───────────────────────────────────────────────
def test_account_level_types_excluded_from_reuse():
    assert {"BUSINESS_NAME", "BUSINESS_LOGO"} <= set(ACCOUNT_LEVEL_FIELD_TYPES)


def test_list_account_assets_drops_account_level(monkeypatch):
    """customer_asset отдаёт и BUSINESS_NAME — раньше он попадал в reuse, падал при линковке как
    CampaignAsset и молча исчезал (счётчик «переиспользовано» врал)."""
    import ads.read as R

    class _Row:
        def __init__(self, ft, rn):
            self.asset = type("A", (), {"resource_name": rn, "type": "SITELINK", "name": "n"})()  # noqa: E501
            ftobj = type("F", (), {"field_type": type("E", (), {"name": ft})()})()
            self.campaign_asset = ftobj
            self.customer_asset = ftobj

    class _GA:
        def search(self, *, customer_id, query):
            if "customer_asset" in query:
                return [_Row("BUSINESS_NAME", "customers/1/assets/9")]
            return [_Row("SITELINK", "customers/1/assets/1")]

    monkeypatch.setattr(R, "ensure_read_allowed", lambda cid: None)
    monkeypatch.setattr(R, "_asset_label", lambda ft, a: ft)
    out = R.list_account_assets(type("C", (), {"get_service": lambda self, n: _GA()})(), "123")
    assert [r.field_type for r in out] == ["SITELINK"]


# ── Потеря группы при линковке — честно, а не молча ───────────────────────────────
def test_link_existing_reports_skipped_groups():
    """Раньше упавшая группа просто исчезала из счётчика: «переиспользовано 1» из 3, без объяснения."""
    calls: list = []

    def _link(client, cid, camp_rn, rns, ft):
        calls.append(ft)
        if ft == "boom":
            raise RuntimeError("INVALID_ASSET")

    class _Enums:
        class AssetFieldTypeEnum:
            SITELINK = "ok"
            CALLOUT = "boom"

    class _Client:
        enums = _Enums()

    import ads.extensions as ext

    orig_link, orig_rn = ext._link_campaign_assets, ext._campaign_rn
    ext._link_campaign_assets = _link
    ext._campaign_rn = lambda c, cid, camp: "customers/1/campaigns/2"
    try:
        linked, skipped = M._link_existing_assets_via_sdk(_Client(), "1", "2", REUSE)
    finally:
        ext._link_campaign_assets, ext._campaign_rn = orig_link, orig_rn
    assert linked == 2  # два SITELINK привязаны
    assert skipped == [{"field_type": "CALLOUT", "n": 1, "reason": "RuntimeError"}]


def test_result_humanizer_shows_reuse_loss():
    out = texts.fmt_mutation_result(
        "create_search_campaign",
        {
            "campaign": "customers/1/campaigns/2",
            "assets_reused": 2,
            "assets_reuse_requested": 3,
            "assets_reuse_skipped": [{"field_type": "CALLOUT", "n": 1, "reason": "RuntimeError"}],
        },
        "ru",
    )
    assert "2/3" in out and "CALLOUT" in out
