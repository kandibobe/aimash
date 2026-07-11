"""partial_failure для батчевых мутаций ключей/минус-слов (1F6).

Одна плохая позиция (policy/серверный дубль) не валит весь батч: валидные применяются,
отклонённые возвращаются в result['rejected'] с редактированной причиной; все отклонены —
честный ValueError (record_failure). SDK подменён дакт-фейками (офлайн).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ads.mutations as mut  # noqa: E402
from core.ads_errors import partial_failure_errors  # noqa: E402

DRAFT = "7753643025"


class _NS(SimpleNamespace):
    """Автосоздание вложенных полей: op.create.keyword.text = … работает без описания структуры."""

    def __getattr__(self, name):
        val = _NS()
        setattr(self, name, val)
        return val


class _Req:
    def __init__(self) -> None:
        self.customer_id = ""
        self.operations: list = []
        self.partial_failure = False


def _pfe(*errs: tuple[int, str, str], code: int = 3):
    """Фейковый google.rpc.Status с details[0].errors = [(index, code_name, message), …]."""
    return SimpleNamespace(
        code=code,
        details=[
            SimpleNamespace(
                errors=[
                    SimpleNamespace(
                        message=msg,
                        error_code=SimpleNamespace(name=code_name),
                        location=SimpleNamespace(field_path_elements=[SimpleNamespace(index=idx)]),
                    )
                    for idx, code_name, msg in errs
                ]
            )
        ],
    )


def _client(resp, captured: dict):
    """Дакт-фейк GoogleAdsClient для _add_keywords_via_sdk/_add_negative_keywords_via_sdk."""

    class _AgSvc:
        def ad_group_path(self, cid, agid):
            return f"customers/{cid}/adGroups/{agid}"

    class _CmpSvc:
        def campaign_path(self, cid, campid):
            return f"customers/{cid}/campaigns/{campid}"

    class _CritSvc:
        def mutate_ad_group_criteria(self, request):
            captured["request"] = request
            return resp

        def mutate_campaign_criteria(self, request):
            captured["request"] = request
            return resp

    class _Enums:
        KeywordMatchTypeEnum = SimpleNamespace(BROAD="BROAD", PHRASE="PHRASE", EXACT="EXACT")
        AdGroupCriterionStatusEnum = SimpleNamespace(ENABLED="ENABLED")

    class _Client:
        enums = _Enums()

        def get_service(self, name):
            return {
                "AdGroupService": _AgSvc(),
                "CampaignService": _CmpSvc(),
                "AdGroupCriterionService": _CritSvc(),
                "CampaignCriterionService": _CritSvc(),
            }[name]

        def get_type(self, name):
            if name.startswith("Mutate"):
                return _Req()
            return _NS()  # AdGroupCriterionOperation / CampaignCriterionOperation

    return _Client()


def _results(*resource_names: str):
    return [SimpleNamespace(resource_name=rn) for rn in resource_names]


def test_add_keywords_partial_failure_split():
    """Индекс 1 отклонён → created=2, rejected=1 с текстом ключа операции 1."""
    captured: dict = {}
    resp = SimpleNamespace(
        results=_results("crit0", "", "crit2"),  # у отклонённой позиции пустой resource_name
        partial_failure_error=_pfe((1, "POLICY_FINDING", "policy violation")),
    )
    client = _client(resp, captured)
    res = mut._add_keywords_via_sdk(client, DRAFT, ["10"], ["ok1", "bad", "ok2"], "broad")
    assert res["count"] == 2
    assert res["created"] == ["crit0", "crit2"]
    assert res["rejected_count"] == 1
    assert res["rejected"][0]["keyword"] == "bad"
    assert "POLICY_FINDING" in res["rejected"][0]["reason"]
    assert res["rejected"][0]["ad_group_id"] == "10"
    assert captured["request"].partial_failure is True  # флаг реально выставлен


def test_add_keywords_all_rejected_raises():
    resp = SimpleNamespace(
        results=_results("", ""),
        partial_failure_error=_pfe((0, "DUPLICATE_KEYWORD", "dup"), (1, "POLICY_FINDING", "bad")),
    )
    client = _client(resp, {})
    with pytest.raises(ValueError, match="отклонил все"):
        mut._add_keywords_via_sdk(client, DRAFT, ["10"], ["a", "b"], "exact")


def test_add_keywords_no_failures_backcompat():
    """Без partial_failure_error — прежний результат, ключей rejected нет."""
    resp = SimpleNamespace(results=_results("crit0", "crit1"))  # атрибута pfe вовсе нет
    client = _client(resp, {})
    res = mut._add_keywords_via_sdk(client, DRAFT, ["10"], ["a", "b"], "phrase")
    assert res["count"] == 2 and "rejected" not in res


def test_add_negative_keywords_partial_failure_mirror():
    captured: dict = {}
    resp = SimpleNamespace(
        results=_results("neg0", ""),
        partial_failure_error=_pfe((1, "KEYWORD_HAS_INVALID_CHARS", "invalid chars")),
    )
    client = _client(resp, captured)
    res = mut._add_negative_keywords_via_sdk(client, DRAFT, "555", ["ok", "плох@й"], "broad")
    assert res["count"] == 1 and res["resource_names"] == ["neg0"]
    assert res["rejected"][0]["keyword"] == "плох@й"
    assert "ad_group_id" not in res["rejected"][0]  # уровень кампании
    assert captured["request"].partial_failure is True


# ── B3: ключи визарда §19 идут тем же контрактом, что /addkeys ───────────────────
def _composite_client(kw_resp, captured: dict):
    """Дакт-фейк SDK для _create_search_campaign_via_sdk: бюджет/кампания/группа/RSA — успех,
    батч ключей отвечает переданным kw_resp (в т.ч. с partial_failure_error)."""

    class _Svc:
        def mutate_campaign_budgets(self, *, customer_id, operations):
            return SimpleNamespace(results=_results("customers/x/campaignBudgets/1"))

        def mutate_campaigns(self, *, customer_id, operations):
            return SimpleNamespace(results=_results("customers/x/campaigns/2"))

        def mutate_ad_groups(self, *, customer_id, operations):
            return SimpleNamespace(results=_results("customers/x/adGroups/3"))

        def mutate_ad_group_ads(self, *, customer_id, operations):
            return SimpleNamespace(results=_results("customers/x/adGroupAds/3~4"))

        def mutate_ad_group_criteria(self, request):
            captured["request"] = request
            return kw_resp

    class _Proto(list):
        """Рекурсивный proto-подобный фейк: любой атрибут — снова _Proto (и это list → .append)."""

        def __getattr__(self, k):
            v = _Proto()
            object.__setattr__(self, k, v)
            return v

    class _Client:
        enums = _Proto()

        def get_service(self, name):
            return _Svc()

        def get_type(self, name):
            return _Req() if name.startswith("Mutate") else _Proto()

    return _Client()


def _run_composite(client, keywords):
    return mut._create_search_campaign_via_sdk(
        client,
        DRAFT,
        campaign_name="T",
        final_url="https://x/",
        headlines=["a", "b", "c"],
        descriptions=["d", "e"],
        budget_micros=40_000_000,
        keywords=keywords,
        match_type="phrase",
        cpc_bid_micros=120_000,
    )


def test_wizard_keywords_partial_failure_does_not_zero_the_campaign(monkeypatch):
    """B3: раньше ключи визарда шли БЕЗ partial_failure → один битый ключ («скидка 50%» →
    KEYWORD_HAS_INVALID_CHARS) валил весь батч, `except Exception` глотал причину, и Search-кампания
    создавалась с НУЛЁМ ключей. Теперь валидные создаются, отклонённые — в result['rejected']."""
    monkeypatch.setattr(mut, "_downgrade_bidding_if_no_conversions", lambda c, cid, b: (b, None))
    monkeypatch.setattr(mut, "_apply_bidding_on_create", lambda c, camp, b: None)
    captured: dict = {}
    kw_resp = SimpleNamespace(
        results=_results("crit0", "", "crit2"),
        partial_failure_error=_pfe((1, "KEYWORD_HAS_INVALID_CHARS", "invalid chars")),
    )
    res = _run_composite(_composite_client(kw_resp, captured), ["ok1", "скидка 50%", "ok2"])

    assert captured["request"].partial_failure is True  # флаг реально выставлен
    assert res["keywords"] == 2  # НЕ 0 — кампания уехала с валидными ключами
    assert res["rejected_count"] == 1
    assert res["rejected"][0]["keyword"] == "скидка 50%"
    assert "KEYWORD_HAS_INVALID_CHARS" in res["rejected"][0]["reason"]
    # частичный успех виден и в warnings (3C-контракт)
    assert {"part": "keywords", "requested": 3, "applied": 2} in res["warnings"]


def test_wizard_keywords_clean_batch_has_no_rejected(monkeypatch):
    monkeypatch.setattr(mut, "_downgrade_bidding_if_no_conversions", lambda c, cid, b: (b, None))
    monkeypatch.setattr(mut, "_apply_bidding_on_create", lambda c, camp, b: None)
    kw_resp = SimpleNamespace(results=_results("crit0", "crit1"))  # атрибута pfe вовсе нет
    res = _run_composite(_composite_client(kw_resp, {}), ["a", "b"])
    assert res["keywords"] == 2 and "rejected" not in res
    assert not [w for w in res.get("warnings", []) if w["part"] == "keywords"]


def test_partial_failure_errors_parser_duck():
    resp = SimpleNamespace(partial_failure_error=_pfe((0, "A_CODE", "msg0"), (2, "B_CODE", "msg2")))
    out = partial_failure_errors(object(), resp)  # client не нужен дакт-фейку (detail.errors)
    assert out == [(0, "A_CODE", "msg0"), (2, "B_CODE", "msg2")]
    # нет partial_failure (None / code=0) → пусто
    assert partial_failure_errors(object(), SimpleNamespace()) == []
    assert (
        partial_failure_errors(
            object(), SimpleNamespace(partial_failure_error=SimpleNamespace(code=0))
        )
        == []
    )


def test_rejected_reasons_redacted():
    """Серверный message с токен-подобной строкой → в reason только REDACTED (golden rule #5)."""
    secret = "1//0SECRETtokenVALUE42"  # gitleaks:allow — форма refresh-токена
    resp = SimpleNamespace(
        results=_results("ok", ""),
        partial_failure_error=_pfe((1, "AUTH", f"denied refresh_token={secret}")),
    )
    client = _client(resp, {})
    res = mut._add_keywords_via_sdk(client, DRAFT, ["10"], ["a", "b"], "broad")
    reason = res["rejected"][0]["reason"]
    assert secret not in reason
    assert "REDACTED" in reason
