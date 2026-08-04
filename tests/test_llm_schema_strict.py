"""Fail-closed validation contract for every model-facing tool schema."""

from __future__ import annotations

import inspect

import pytest
from pydantic import BaseModel, ValidationError

import llm.schemas as schemas


def _local_models() -> list[type[BaseModel]]:
    return sorted(
        {
            obj
            for obj in vars(schemas).values()
            if inspect.isclass(obj)
            and issubclass(obj, BaseModel)
            and obj.__module__ == schemas.__name__
        },
        key=lambda model: model.__name__,
    )


def test_all_tool_models_forbid_extra_and_use_strict_types():
    models = _local_models()
    assert schemas.ToolArgs in models
    assert all(model.model_config.get("extra") == "forbid" for model in models)
    assert all(model.model_config.get("strict") is True for model in models)
    assert all(model.model_json_schema().get("additionalProperties") is False for model in models)


def test_create_rsa_rejects_hallucinated_argument():
    with pytest.raises(ValidationError) as exc_info:
        schemas.CreateRsa(
            campaign="Search",
            ad_group_id="42",
            final_url="https://example.com",
            headlines=["Заголовок 1", "Заголовок 2", "Заголовок 3"],
            descriptions=["Описание первое", "Описание второе"],
            hallucinated_scope="foreign-campaign",
        )

    assert {error["type"] for error in exc_info.value.errors()} == {"extra_forbidden"}


def test_create_rsa_campaign_id_is_strict_and_numeric():
    params = {
        "campaign": "Search",
        "ad_group_id": "42",
        "final_url": "https://example.com",
        "headlines": ["Заголовок 1", "Заголовок 2", "Заголовок 3"],
        "descriptions": ["Описание первое", "Описание второе"],
    }

    assert schemas.CreateRsa(**params, campaign_id="123").campaign_id == "123"
    with pytest.raises(ValidationError):
        schemas.CreateRsa(**params, campaign_id=123)
    with pytest.raises(ValidationError):
        schemas.CreateRsa(**params, campaign_id="not-an-id")


@pytest.mark.parametrize(
    ("model", "params"),
    [
        (
            schemas.AddKeywords,
            {
                "campaign": "Search",
                "keywords": ["ремонт окон"],
                "match_type": "phrase",
                "_target_campaign_id": "123",
                "_target_ad_group_ids": ["42"],
            },
        ),
        (
            schemas.AddNegativeKeywords,
            {
                "campaign": "Search",
                "keywords": ["бесплатно"],
                "_target_campaign_id": "123",
            },
        ),
    ],
)
def test_llm_cannot_supply_internal_target_identity(model, params):
    with pytest.raises(ValidationError) as exc_info:
        model(**params)

    assert {error["type"] for error in exc_info.value.errors()} == {"extra_forbidden"}
    assert all(error["loc"][0].startswith("_target_") for error in exc_info.value.errors())


def test_empty_and_nested_models_also_reject_extra_fields():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        schemas.GetSearchTermsArgs(hallucinated="value")

    with pytest.raises(ValidationError) as exc_info:
        schemas.AddSitelinks(
            campaign="Search",
            sitelinks=[
                {
                    "link_text": "Каталог",
                    "final_url": "https://example.com/catalog",
                    "hallucinated": "value",
                }
            ],
        )

    assert exc_info.value.errors()[0]["loc"] == ("sitelinks", 0, "hallucinated")
    assert exc_info.value.errors()[0]["type"] == "extra_forbidden"
    assert (
        schemas.AddSitelinks.model_json_schema()["$defs"]["Sitelink"]["additionalProperties"]
        is False
    )


@pytest.mark.parametrize(
    ("model", "params", "error_type"),
    [
        (schemas.PauseCampaign, {"campaign": 123}, "string_type"),
        (
            schemas.SetCampaignNetwork,
            {"campaign": "Search", "search_partners": "false"},
            "bool_type",
        ),
        (
            schemas.UpdateBudget,
            {"campaign": "Search", "mode": "set_to", "value": "20"},
            "float_type",
        ),
    ],
)
def test_tool_models_do_not_coerce_llm_scalar_types(model, params, error_type):
    with pytest.raises(ValidationError) as exc_info:
        model(**params)

    assert error_type in {error["type"] for error in exc_info.value.errors()}
