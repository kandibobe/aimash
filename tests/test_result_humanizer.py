"""3C: fmt_mutation_result — человекочитаемый итог операции вместо сырого Python-dict,
warnings частичного успеха composite-create показываются явно («гео НЕ применено (0 из 2)»)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import texts  # noqa: E402


def test_create_campaign_result_humanized_with_warnings():
    result = {
        "customer_id": "7753643025",
        "campaign_name": "Kenya Cars",
        "campaign": "customers/775/campaigns/1",
        "budget": "customers/775/campaignBudgets/2",
        "ad_group": "customers/775/adGroups/3",
        "ad": "customers/775/adGroupAds/3~4",
        "headlines": 15,
        "descriptions": 4,
        "keywords": 48,
        "geo": 0,
        "languages": 1,
        "ad_schedule": 0,
        "status": "PAUSED",
        "applied": True,
        "warnings": [{"part": "geo", "requested": 2, "applied": 0}],
    }
    out = texts.fmt_mutation_result("create_search_campaign", result)
    assert "Kenya Cars" in out and "PAUSED" in out
    assert "ключей: 48" in out
    assert "НЕ применено (0 из 2)" in out and "гео" in out  # warning виден явно
    assert "customers/775" not in out  # сырые resource_name не показываем


def test_create_result_surfaces_image_and_asset_drops():
    """§19.6/§19.7: тихие потери (картинки/ассеты) больше не молчат — added/requested, пропущенные
    с причиной, добавленные и переиспользованные видны менеджеру в итоге создания."""
    result = {
        "campaign_name": "Flowers",
        "campaign": "customers/775/campaigns/1",
        "status": "PAUSED",
        "applied": True,
        "images_requested": 3,
        "images_added": 1,  # 2 потеряны (неподходящий аккаунт)
        "assets_added": ["callouts", "sitelinks"],
        "assets_reused": 4,
        "assets_skipped": [{"family": "lead_form"}, {"family": "location"}],
    }
    out = texts.fmt_mutation_result("create_search_campaign", result, lang="ru")
    assert "1/3" in out and "Изображения" in out  # тихая потеря картинок видна
    assert "callouts" in out and "Ассеты добавлены" in out
    assert "Переиспользовано ассетов: 4" in out
    assert "lead_form" in out and "пропущены" in out  # пропущенные — с причиной


def test_create_result_all_images_ok_no_warning():
    result = {
        "campaign_name": "Ok",
        "status": "PAUSED",
        "applied": True,
        "images_requested": 2,
        "images_added": 2,
    }
    out = texts.fmt_mutation_result("create_search_campaign", result, lang="ru")
    assert "🖼" in out and "⚠️ Изображения" not in out  # всё прикреплено → без предупреждения


def test_keywords_result_humanized():
    out = texts.fmt_mutation_result(
        "add_keywords",
        {"count": 12, "match_type": "phrase", "created": ["c1"], "applied": True},
    )
    assert "12" in out and "phrase" in out
    assert "created" not in out  # технический ключ скрыт

    out2 = texts.fmt_mutation_result(
        "remove_keywords",
        {"count": 1, "match_type": "exact", "not_found": ["нетакого"], "applied": True},
    )
    assert "не найдено" in out2 and "нетакого" in out2


def test_memory_profile_result_humanized():
    out = texts.fmt_mutation_result(
        "profile_update",
        {"customer_id": "1", "created": False, "changed_fields": ["brand", "contacts"]},
    )
    assert "обновлён" in out and "brand" in out
    out2 = texts.fmt_mutation_result("profile_clear", {"cleared": True})
    assert "очищен" in out2


def test_fallback_dict_not_raw_repr():
    out = texts.fmt_mutation_result("update_budget", {"applied": True, "new_budget_micros": 60})
    assert "{" not in out and "'" not in out  # не repr-дамп
    assert "new_budget_micros" in out

    assert texts.fmt_mutation_result("x", "уже строка <b>") == "уже строка &lt;b&gt;"  # esc


def test_en_variant():
    out = texts.fmt_mutation_result(
        "create_search_campaign",
        {
            "campaign_name": "X",
            "headlines": 3,
            "warnings": [{"part": "keywords", "requested": 5, "applied": 2}],
        },
        lang="en",
    )
    assert "created" in out and "applied 2 of 5" in out
