"""§2A: клон кампании «как в кампании X». READ-ONLY конфиг + агент-намерение + сборка черновика.

Без живого Google Ads — фейковый клиент с маршрутизацией GAQL по FROM-таблице. Проверяем:
- read_campaign_config: парс кампании/групп/ключей/RSA; None если кампании нет; чужой аккаунт → PermissionError;
- agent.loop: clone_campaign → clone_intent (НЕ мутация, НЕ в SUPPORTED_OPERATIONS); плохие args → текст;
- сводка fmt_clone_proposal_summary честно сообщает, что НЕ переносится.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent.loop as L  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from ads.read import read_campaign_config  # noqa: E402
from bot import texts  # noqa: E402
from core.config import settings  # noqa: E402


@contextmanager
def allowed_ids(value: str):
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


# ── Фейковый SDK с маршрутизацией GAQL по таблице ────────────────────────────────────
def _txt(s: str):
    return SimpleNamespace(text=s)


def _enum(name: str):
    return SimpleNamespace(name=name)


class _RoutingGA:
    """ga.search routes by query content (read_campaign_config делает 4 разных запроса)."""

    def __init__(self, *, base=None, groups=(), keywords=(), rsa=()):
        self._base = base
        self._groups = list(groups)
        self._keywords = list(keywords)
        self._rsa = list(rsa)

    def search(self, *, customer_id, query):
        if "FROM ad_group_criterion" in query:
            return list(self._keywords)
        if "FROM ad_group_ad" in query:
            return list(self._rsa)
        if "FROM ad_group" in query:
            return list(self._groups)
        if "FROM campaign" in query:
            return [self._base] if self._base is not None else []
        return []


class _Client:
    def __init__(self, ga):
        self._ga = ga

    def get_service(self, name):
        assert name == "GoogleAdsService"
        return self._ga


def _base_row(*, cid=10, name="Образец", status="ENABLED", channel="SEARCH", budget=40_000_000):
    return SimpleNamespace(
        campaign=SimpleNamespace(
            id=cid,
            name=name,
            status=_enum(status),
            advertising_channel_type=_enum(channel),
        ),
        campaign_budget=SimpleNamespace(amount_micros=budget),
    )


def _group_row(*, gid=100, name="AG1", cpc=750_000):
    return SimpleNamespace(ad_group=SimpleNamespace(id=gid, name=name, cpc_bid_micros=cpc))


def _kw_row(*, gid=100, text="ключ", match="PHRASE"):
    return SimpleNamespace(
        ad_group=SimpleNamespace(id=gid),
        ad_group_criterion=SimpleNamespace(
            keyword=SimpleNamespace(text=text, match_type=_enum(match))
        ),
    )


def _rsa_row(*, gid=100, heads=(), descs=(), url="https://example.com", p1="", p2=""):
    return SimpleNamespace(
        ad_group=SimpleNamespace(id=gid),
        ad_group_ad=SimpleNamespace(
            ad=SimpleNamespace(
                final_urls=[url],
                responsive_search_ad=SimpleNamespace(
                    headlines=[_txt(h) for h in heads],
                    descriptions=[_txt(d) for d in descs],
                    path1=p1,
                    path2=p2,
                ),
            )
        ),
    )


# ── read_campaign_config ──────────────────────────────────────────────────────────
def test_read_campaign_config_assembles_full():
    ga = _RoutingGA(
        base=_base_row(name="Search Spring", budget=40_000_000),
        groups=[_group_row(gid=100, name="AG1", cpc=750_000)],
        keywords=[
            _kw_row(text="купить телефон", match="EXACT"),
            _kw_row(text="смартфон", match="BROAD"),
        ],
        rsa=[
            _rsa_row(
                heads=["Заголовок A", "Заголовок B"],
                descs=["Описание 1"],
                url="https://shop.ua",
                p1="sale",
            )
        ],
    )
    with allowed_ids(DRAFT_ACCOUNT_ID):
        cfg = read_campaign_config(_Client(ga), DRAFT_ACCOUNT_ID, "Search Spring")
    assert cfg is not None
    assert cfg.channel_type == "SEARCH"
    assert cfg.budget_micros == 40_000_000
    assert len(cfg.ad_groups) == 1
    ag = cfg.ad_groups[0]
    assert ag.cpc_bid_micros == 750_000
    assert [k.text for k in ag.keywords] == ["купить телефон", "смартфон"]
    assert [k.match_type for k in ag.keywords] == ["exact", "broad"]
    assert ag.headlines == ["Заголовок A", "Заголовок B"]
    assert ag.descriptions == ["Описание 1"]
    assert ag.final_url == "https://shop.ua"
    assert ag.path1 == "sale"


def test_read_campaign_config_none_when_absent():
    with allowed_ids(DRAFT_ACCOUNT_ID):
        cfg = read_campaign_config(_Client(_RoutingGA(base=None)), DRAFT_ACCOUNT_ID, "нет такой")
    assert cfg is None


def test_read_campaign_config_rejects_foreign_account():
    with allowed_ids(DRAFT_ACCOUNT_ID):
        with pytest.raises(PermissionError):
            read_campaign_config(_Client(_RoutingGA(base=_base_row())), "1234567890", "X")


# ── agent.loop: clone_campaign → clone_intent ────────────────────────────────────────
def _tc(name, args):
    return SimpleNamespace(
        function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False))
    )


def _msg(tool_calls=None, content=""):
    return SimpleNamespace(tool_calls=tool_calls, content=content)


def _chat_returning(*msgs):
    calls = {"n": 0}

    async def _chat(messages, **kwargs):
        i = min(calls["n"], len(msgs) - 1)
        calls["n"] += 1
        return msgs[i]

    return _chat, calls


async def test_clone_campaign_returns_clone_intent():
    args = {"new_name": "Уганда машины", "source_campaign": "Search Spring"}
    fake, _ = _chat_returning(_msg([_tc("clone_campaign", args)]))
    with patched(L, "chat", fake):
        out = await L.handle_command("сделай кампанию Уганда машины как в Search Spring", chat_id=7)
    assert out["type"] == "clone_intent"
    assert out["brief"]["new_name"] == "Уганда машины"
    assert out["brief"]["source_campaign"] == "Search Spring"


async def test_clone_campaign_bad_args_to_text():
    fake, _ = _chat_returning(
        _msg([_tc("clone_campaign", {"source_campaign": "X"})])
    )  # нет new_name
    with patched(L, "chat", fake):
        out = await L.handle_command("клонируй кампанию", chat_id=7)
    assert out["type"] == "text"


def test_clone_campaign_not_in_supported_operations():
    from ads.service import SUPPORTED_OPERATIONS

    assert "clone_campaign" not in SUPPORTED_OPERATIONS  # это намерение, не исполняемая мутация


# ── сводка: честно про «не переносится» ──────────────────────────────────────────────
def test_clone_summary_mentions_not_transferred():
    params = {
        "final_url": "https://shop.ua",
        "headlines": ["A", "B", "C"],
        "descriptions": ["d1", "d2"],
        "keywords": ["k1"],
        "match_type": "phrase",
    }
    s_ru = texts.fmt_clone_proposal_summary(
        "Новая", "Образец", 40.0, params, dropped_texts=2, regenerated=False, lang="ru"
    )
    assert "Клон из «Образец»" in s_ru
    assert "Не переносится" in s_ru
    assert "отброшено" in s_ru
    s_en = texts.fmt_clone_proposal_summary("New", "Src", 40.0, params, lang="en")
    assert "Not copied automatically" in s_en
