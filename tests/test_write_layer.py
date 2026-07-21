"""Офлайн-тесты write-слоя Фазы 1 (Блок A): новые мутации за двумя гейтами + capability-guard.

Закрывает дыру из аудита: «write-путь (resolve/service/store) не покрыт тестами».
Без живого Google Ads — SDK-исполнители (_*_via_sdk) подменяются monkeypatch'ем; БД — временный
SQLite (см. tests/conftest.py). Проверяем:
- каждый apply_* проходит ОБА гейта (замок аккаунта + confirm) и финализирует audit;
- ставки (деньги) — только user_initiated; чужой аккаунт/без подтверждения — отказ;
- длину ключевых слов считает КОД (golden rule #4) ДО вызова SDK;
- capability-guard: неподдержанную операцию (отложенный geo) отклоняем ДО кнопок и в execute_confirmed;
- store roundtrip: save → confirm → finalize пишет audit_log [confirmed]→[applied].
"""

from __future__ import annotations

import json
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

import ads.mutations as mut  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from core.config import settings  # noqa: E402


# ── Хелперы (зеркало test_safety_core, чтобы файл был самодостаточным) ───────────
@contextmanager
def allowed_ids(value: str):
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = value
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


@dataclass
class FakeProposal:
    operation: str
    status: str
    user_initiated: bool
    # Волна 1.4: второй бит провенанса. None ⇒ зеркалим user_initiated — здесь проверяется SDK-путь,
    # а не провенанс, и расщепление битов у настоящего ConfirmStore живёт в test_provenance_gate.py.
    origin_human_turn: bool | None = None

    def __post_init__(self) -> None:
        if self.origin_human_turn is None:
            self.origin_human_turn = self.user_initiated


class FakeStore:
    def __init__(self, proposal=None):
        self._p = proposal
        self.finalized = False
        self._claimed = False

    async def claim(self, confirmation_id, *, operation):
        # Зеркало ConfirmStore.claim: атомарно/одноразово, только confirmed + совпавшая операция.
        p = self._p
        if p is None or p.status != "confirmed" or p.operation != operation or self._claimed:
            return None
        self._claimed = True
        return p

    async def finalize(self, confirmation_id, *, result):
        self.finalized = True


class _FakeEnums:
    class CampaignStatusEnum:
        ENABLED = "ENABLED"
        PAUSED = "PAUSED"

    class AdGroupStatusEnum:
        ENABLED = "ENABLED"
        PAUSED = "PAUSED"

    class AdGroupAdStatusEnum:  # C6: статус отдельного объявления
        ENABLED = "ENABLED"
        PAUSED = "PAUSED"


class _FakeClient:
    enums = _FakeEnums()


@contextmanager
def patched(obj, name, value):
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


# ── apply_update_bid: ставка = деньги (оба гейта + user_initiated) ───────────────
async def test_apply_update_bid_happy_path():
    called = {}

    def fake(client, customer_id, campaign_id, bids):
        called["args"] = (customer_id, campaign_id, list(bids))
        return {"customer_id": customer_id, "campaign_id": campaign_id, "applied": True}

    store = FakeStore(FakeProposal("update_bid", "confirmed", user_initiated=True))
    with patched(mut, "_apply_bid_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_update_bid(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="7",
            bids=[("42", 1_500_000)],
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
        )
    assert res["applied"] is True
    assert called["args"][0] == DRAFT_ACCOUNT_ID and called["args"][1] == "7"
    assert store.finalized is True


async def test_apply_update_bid_blocked_when_not_user_initiated():
    store = FakeStore(FakeProposal("update_bid", "confirmed", user_initiated=False))
    with allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await mut.apply_update_bid(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="7",
                bids=[("42", 1_500_000)],
                confirmation_id="x",
                confirm_store=store,
                ads_client=object(),
            )
            raise AssertionError("ожидался PermissionError (ставка не по команде)")
        except PermissionError:
            pass
    assert store.finalized is False


async def test_apply_update_bid_rejects_foreign_account():
    store = FakeStore(FakeProposal("update_bid", "confirmed", user_initiated=True))
    with allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await mut.apply_update_bid(
                customer_id="1234567890",
                campaign_id="7",
                bids=[("42", 1_500_000)],
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
            )
            raise AssertionError("ожидался PermissionError (чужой аккаунт)")
        except PermissionError:
            pass
    assert store.finalized is False


async def test_apply_update_bid_rejected_without_confirmation():
    store = FakeStore(proposal=None)
    with allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await mut.apply_update_bid(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="7",
                bids=[("42", 1_500_000)],
                confirmation_id="bogus",
                confirm_store=store,
                ads_client=object(),
            )
            raise AssertionError("ожидался PermissionError (нет confirmation)")
        except PermissionError:
            pass


# ── Ф1: apply_update_keyword_bid — ставка на уровне КЛЮЧА (те же гейты, что и у группы) ──
def _kw_bid_store(user_initiated=True, status="confirmed"):
    return FakeStore(FakeProposal("update_keyword_bid", status, user_initiated=user_initiated))


async def test_apply_update_keyword_bid_happy_path():
    called = {}

    def fake(client, customer_id, campaign_id, bids):
        called["args"] = (customer_id, campaign_id, list(bids))
        return {"customer_id": customer_id, "campaign_id": campaign_id, "applied": True}

    store = _kw_bid_store()
    with patched(mut, "_apply_keyword_bid_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_update_keyword_bid(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="7",
            bids=[("42", "9001", 1_500_000)],
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
        )
    assert res["applied"] is True
    assert called["args"][0] == DRAFT_ACCOUNT_ID and called["args"][1] == "7"
    assert called["args"][2] == [("42", "9001", 1_500_000)]
    assert store.finalized is True


async def test_apply_update_keyword_bid_blocked_when_not_user_initiated():
    """Golden rule #3: ставка ключа — те же деньги. Из scheduler/anomaly (user_initiated=False)
    операция недостижима, SDK не зовём."""
    called = {"n": 0}

    def fake(*a, **k):
        called["n"] += 1
        return {"applied": True}

    store = _kw_bid_store(user_initiated=False)
    with patched(mut, "_apply_keyword_bid_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await mut.apply_update_keyword_bid(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="7",
                bids=[("42", "9001", 1_500_000)],
                confirmation_id="x",
                confirm_store=store,
                ads_client=object(),
            )
            raise AssertionError("ожидался PermissionError (ставка не по команде пользователя)")
        except PermissionError:
            pass
    assert called["n"] == 0 and store.finalized is False


async def test_apply_update_keyword_bid_replay_is_one_shot():
    """Тот же confirmation_id второй раз → PermissionError, SDK вызван РОВНО один раз (claim
    одноразовый — защита от double-spend)."""
    calls = {"n": 0}

    def fake(client, customer_id, campaign_id, bids):
        calls["n"] += 1
        return {"applied": True}

    store = _kw_bid_store()
    kw = {
        "customer_id": DRAFT_ACCOUNT_ID,
        "campaign_id": "7",
        "bids": [("42", "9001", 1_500_000)],
        "confirmation_id": "ok",
        "ads_client": object(),
    }
    with patched(mut, "_apply_keyword_bid_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        await mut.apply_update_keyword_bid(confirm_store=store, **kw)
        try:
            await mut.apply_update_keyword_bid(confirm_store=store, **kw)
            raise AssertionError("replay должен падать PermissionError")
        except PermissionError:
            pass
    assert calls["n"] == 1


def test_keyword_bid_via_sdk_keys_by_ad_group_and_criterion_pair():
    """Регрессия (ревизия волны, ДЕНЬГИ): criterion_id уникален лишь В ПРЕДЕЛАХ ГРУППЫ — один и тот
    же ключ («ремонт», PHRASE) в двух группах несёт ОДИН И ТОТ ЖЕ id. Словарь применённых ставок,
    ключёванный только по criterion_id, схлопывал такие строки: второй группе уходила ставка первой,
    а audit-строка (и построенный на ней откат) врали про обе. Ключ — ПАРА (группа, критерий)."""
    ops_seen = []

    class _Op:
        def __init__(self):
            self.update = SimpleNamespace(resource_name=None, cpc_bid_micros=None, _pb=object())
            self.update_mask = SimpleNamespace()

    class _Crit:
        def ad_group_criterion_path(self, cid, ag, crit):
            return f"customers/{cid}/adGroupCriteria/{ag}~{crit}"

        def mutate_ad_group_criteria(self, customer_id, operations):
            ops_seen.extend(operations)
            return SimpleNamespace(results=[])

    class _Client:
        def get_service(self, name):
            return _Crit()

        def get_type(self, name):
            return _Op()

        def copy_from(self, dst, src):
            return None

    # Одна и та же criterion_id «9001» в группах 42 и 77, ставки РАЗНЫЕ.
    bids = [("42", "9001", 1_500_000), ("77", "9001", 3_000_000)]
    with (
        patched(mut, "_assert_manual_cpc", lambda *a, **k: None),
        patched(mut, "_round_money", lambda _c, _cid, m: int(m)),
        patched(mut, "protobuf_helpers", SimpleNamespace(field_mask=lambda a, b: None)),
    ):
        res = mut._apply_keyword_bid_via_sdk(_Client(), DRAFT_ACCOUNT_ID, "7", bids)

    sent = {(o.update.resource_name.split("/")[-1]): o.update.cpc_bid_micros for o in ops_seen}
    assert sent == {"42~9001": 1_500_000, "77~9001": 3_000_000}  # каждой группе — СВОЯ ставка
    # …и audit-строка повторяет ровно то, что ушло в SDK (иначе откат вернёт чужое значение).
    assert [(k["ad_group_id"], k["new_cpc_bid_micros"]) for k in res["keywords"]] == [
        ("42", 1_500_000),
        ("77", 3_000_000),
    ]


def test_docs_mutations_table_matches_supported_operations():
    """`docs/MUTATIONS.md` — карта того, что код умеет менять в чужом аккаунте; отставший док опаснее
    отсутствующего (в нём было 29 операций из 39, и среди пропавших — ДЕНЕЖНАЯ update_keyword_bid).
    Таблица обязана перечислять ровно `SUPPORTED_OPERATIONS`, а денежные — быть помечены деньгами."""
    import pathlib
    import re

    from ads.resolve import MONEY_OPS
    from ads.service import SUPPORTED_OPERATIONS

    doc = (pathlib.Path(__file__).resolve().parents[1] / "docs" / "MUTATIONS.md").read_text(
        encoding="utf-8"
    )
    rows = dict(re.findall(r"^\| `([a-z_]+)` \|[^|]*\|[^|]*\|([^|]*)\|", doc, re.M))
    assert set(rows) == set(SUPPORTED_OPERATIONS), (
        f"док разошёлся с кодом: нет строк для {sorted(set(SUPPORTED_OPERATIONS) - set(rows))}, "
        f"лишние строки {sorted(set(rows) - set(SUPPORTED_OPERATIONS))}"
    )
    # Денежные операции (гейт user_initiated) не должны числиться в доке безобидными.
    for op in MONEY_OPS:
        assert "Да" in rows[op], f"{op} — деньги, а в доке помечена как «{rows[op].strip()}»"


async def test_apply_update_keyword_bid_validates_range_before_claim():
    """Диапазон считает КОД и ДО claim: абсурдная ставка не должна сжигать одноразовый черновик."""
    store = _kw_bid_store()
    with allowed_ids(DRAFT_ACCOUNT_ID):
        for bad in (0, -1, mut.MAX_AMOUNT_MICROS + 1):
            try:
                await mut.apply_update_keyword_bid(
                    customer_id=DRAFT_ACCOUNT_ID,
                    campaign_id="7",
                    bids=[("42", "9001", bad)],
                    confirmation_id="ok",
                    confirm_store=store,
                    ads_client=object(),
                )
                raise AssertionError(f"ставка {bad} должна отвергаться ValueError")
            except ValueError:
                pass
    assert store._claimed is False and store.finalized is False


# ── apply_add_keywords / negatives: длину считает КОД, оба гейта ─────────────────
async def test_apply_add_keywords_happy_path():
    called = {}

    def fake(client, customer_id, ad_group_ids, keywords, match_type):
        called.update(
            customer_id=customer_id,
            ad_group_ids=list(ad_group_ids),
            keywords=list(keywords),
            match_type=match_type,
        )
        return {"applied": True, "count": len(ad_group_ids) * len(keywords)}

    store = FakeStore(FakeProposal("add_keywords", "confirmed", user_initiated=True))
    with patched(mut, "_add_keywords_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_add_keywords(
            customer_id=DRAFT_ACCOUNT_ID,
            ad_group_ids=["1", "2"],
            keywords=["  купить цветы  ", "доставка"],
            match_type="phrase",
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
        )
    assert res["applied"] is True
    assert called["match_type"] == "phrase"
    assert called["keywords"][0] == "купить цветы"  # код обрезал пробелы
    assert store.finalized is True


async def test_apply_add_keywords_validates_length_before_sdk():
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return {"applied": True}

    store = FakeStore(FakeProposal("add_keywords", "confirmed", user_initiated=True))
    with patched(mut, "_add_keywords_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await mut.apply_add_keywords(
                customer_id=DRAFT_ACCOUNT_ID,
                ad_group_ids=["1"],
                keywords=["а" * 81],  # >80 символов (кириллица = 1) → код отклоняет
                match_type="broad",
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
            )
            raise AssertionError("ожидался ValueError (>80 символов)")
        except ValueError:
            pass
    assert calls["n"] == 0  # SDK не вызван
    assert store.finalized is False  # audit не финализирован


async def test_apply_add_negative_keywords_happy_path():
    called = {}

    def fake(client, customer_id, campaign_id, keywords, match_type):
        called.update(campaign_id=campaign_id, keywords=list(keywords), match_type=match_type)
        return {"applied": True, "count": len(keywords)}

    store = FakeStore(FakeProposal("add_negative_keywords", "confirmed", user_initiated=True))
    with patched(mut, "_add_negative_keywords_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_add_negative_keywords(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="23",
            keywords=["бесплатно"],
            match_type="broad",
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
        )
    assert res["applied"] is True
    assert called["campaign_id"] == "23"
    assert store.finalized is True


# ── 3.2б: ad_group_id сужает уровень до ОДНОЙ группы (негативный ad_group_criterion) ──
async def test_apply_add_negative_keywords_adgroup_level():
    """ad_group_id задан → вызывается ГРУППОВОЙ SDK-исполнитель, campaign-level НЕ трогается."""
    called = {}
    campaign_level = {"n": 0}

    def fake_adgroup(client, customer_id, campaign_id, ad_group_id, keywords, match_type):
        called.update(campaign_id=campaign_id, ad_group_id=ad_group_id, keywords=list(keywords))
        return {"applied": True, "count": len(keywords)}

    def fake_campaign(*a, **k):
        campaign_level["n"] += 1
        return {"applied": True}

    store = FakeStore(FakeProposal("add_negative_keywords", "confirmed", user_initiated=True))
    with (
        patched(mut, "_add_negative_keywords_adgroup_via_sdk", fake_adgroup),
        patched(mut, "_add_negative_keywords_via_sdk", fake_campaign),
        allowed_ids(DRAFT_ACCOUNT_ID),
    ):
        res = await mut.apply_add_negative_keywords(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="23",
            keywords=["  бесплатно  "],
            match_type="phrase",
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
            ad_group_id="42",
        )
    assert res["applied"] is True
    assert called["ad_group_id"] == "42" and called["campaign_id"] == "23"
    assert called["keywords"] == ["бесплатно"]  # код обрезал пробелы (golden rule #4)
    assert campaign_level["n"] == 0  # уровень кампании НЕ вызывался
    assert store.finalized is True


async def test_apply_add_negative_keywords_adgroup_replay_is_one_shot():
    """Тот же confirmation_id второй раз → PermissionError, групповой SDK вызван РОВНО один раз."""
    calls = {"n": 0}

    def fake(client, customer_id, campaign_id, ad_group_id, keywords, match_type):
        calls["n"] += 1
        return {"applied": True}

    store = FakeStore(FakeProposal("add_negative_keywords", "confirmed", user_initiated=True))
    kw = {
        "customer_id": DRAFT_ACCOUNT_ID,
        "campaign_id": "23",
        "keywords": ["бесплатно"],
        "match_type": "broad",
        "confirmation_id": "ok",
        "ads_client": object(),
        "ad_group_id": "42",
    }
    with (
        patched(mut, "_add_negative_keywords_adgroup_via_sdk", fake),
        allowed_ids(DRAFT_ACCOUNT_ID),
    ):
        await mut.apply_add_negative_keywords(confirm_store=store, **kw)
        try:
            await mut.apply_add_negative_keywords(confirm_store=store, **kw)
            raise AssertionError("replay должен падать PermissionError")
        except PermissionError:
            pass
    assert calls["n"] == 1


async def test_apply_add_negative_keywords_adgroup_foreign_account_blocked():
    """Замок аккаунта срабатывает ДО диспатча уровня: чужой customer_id — отказ, claim не съеден."""
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return {"applied": True}

    store = FakeStore(FakeProposal("add_negative_keywords", "confirmed", user_initiated=True))
    with (
        patched(mut, "_add_negative_keywords_adgroup_via_sdk", fake),
        allowed_ids(DRAFT_ACCOUNT_ID),
    ):
        try:
            await mut.apply_add_negative_keywords(
                customer_id="1234567890",  # НЕ Draft
                campaign_id="23",
                keywords=["бесплатно"],
                match_type="broad",
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
                ad_group_id="42",
            )
            raise AssertionError("чужой аккаунт должен отвергаться PermissionError")
        except PermissionError:
            pass
    assert calls["n"] == 0
    assert store._claimed is False and store.finalized is False


# ── 3.2б-2: общий список минус-слов — наполнение (create-if-missing) и привязка ───
async def test_apply_add_negatives_to_shared_set_happy_path():
    """Существующий список (id отрезолвлен в execute_confirmed) → SDK позван с этим id, finalize."""
    called = {}

    def fake(client, customer_id, shared_set_id, name, keywords, match_type):
        called.update(shared_set_id=shared_set_id, name=name, keywords=list(keywords))
        return {"applied": True, "count": len(keywords)}

    # user_initiated=False: минус-слова — не деньги, гейтом user_initiated не блокируются.
    store = FakeStore(
        FakeProposal("add_negatives_to_shared_set", "confirmed", user_initiated=False)
    )
    with patched(mut, "_add_negatives_to_shared_set_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_add_negatives_to_shared_set(
            customer_id=DRAFT_ACCOUNT_ID,
            shared_set_name="Общие минуса",
            shared_set_id="77",
            keywords=["  бесплатно  "],
            match_type="broad",
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
        )
    assert res["applied"] is True
    assert called["shared_set_id"] == "77" and called["name"] == "Общие минуса"
    assert called["keywords"] == ["бесплатно"]  # код обрезал пробелы (golden rule #4)
    assert store.finalized is True


async def test_apply_add_negatives_to_shared_set_creates_missing_list():
    """id=None (списка не было на резолве) → исполнитель позван с None: создание ВНУТРИ него,
    строго ПОСЛЕ claim (мутаций до подтверждения не бывает, golden rule #1)."""
    called = {}

    def fake(client, customer_id, shared_set_id, name, keywords, match_type):
        called.update(shared_set_id=shared_set_id, name=name)
        return {"applied": True, "shared_set_created": True}

    store = FakeStore(
        FakeProposal("add_negatives_to_shared_set", "confirmed", user_initiated=False)
    )
    with patched(mut, "_add_negatives_to_shared_set_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_add_negatives_to_shared_set(
            customer_id=DRAFT_ACCOUNT_ID,
            shared_set_name="Новый список",
            shared_set_id=None,
            keywords=["бесплатно"],
            match_type="broad",
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
        )
    assert res["shared_set_created"] is True
    assert called["shared_set_id"] is None and called["name"] == "Новый список"
    assert store.finalized is True


async def test_apply_add_negatives_to_shared_set_bad_name_before_claim():
    """Имя списка валидирует КОД ДО claim (кириллица = 1): плохое имя не съедает черновик."""
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return {"applied": True}

    store = FakeStore(
        FakeProposal("add_negatives_to_shared_set", "confirmed", user_initiated=False)
    )
    with patched(mut, "_add_negatives_to_shared_set_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        for bad in ("", "  ", "я" * 256):
            try:
                await mut.apply_add_negatives_to_shared_set(
                    customer_id=DRAFT_ACCOUNT_ID,
                    shared_set_name=bad,
                    shared_set_id=None,
                    keywords=["бесплатно"],
                    match_type="broad",
                    confirmation_id="ok",
                    confirm_store=store,
                    ads_client=object(),
                )
                raise AssertionError(f"ожидался ValueError (имя {bad!r})")
            except ValueError:
                pass
    assert calls["n"] == 0
    assert store._claimed is False and store.finalized is False


async def test_apply_add_negatives_to_shared_set_replay_is_one_shot():
    """Тот же confirmation_id второй раз → PermissionError, SDK вызван РОВНО один раз."""
    calls = {"n": 0}

    def fake(client, customer_id, shared_set_id, name, keywords, match_type):
        calls["n"] += 1
        return {"applied": True}

    store = FakeStore(
        FakeProposal("add_negatives_to_shared_set", "confirmed", user_initiated=False)
    )
    kw = {
        "customer_id": DRAFT_ACCOUNT_ID,
        "shared_set_name": "Общие минуса",
        "shared_set_id": "77",
        "keywords": ["бесплатно"],
        "match_type": "broad",
        "confirmation_id": "ok",
        "ads_client": object(),
    }
    with patched(mut, "_add_negatives_to_shared_set_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        await mut.apply_add_negatives_to_shared_set(confirm_store=store, **kw)
        try:
            await mut.apply_add_negatives_to_shared_set(confirm_store=store, **kw)
            raise AssertionError("replay должен падать PermissionError")
        except PermissionError:
            pass
    assert calls["n"] == 1


async def test_apply_attach_shared_set_happy_path():
    called = {}

    def fake(client, customer_id, campaign_id, shared_set_id):
        called.update(campaign_id=campaign_id, shared_set_id=shared_set_id)
        return {"applied": True}

    store = FakeStore(FakeProposal("attach_shared_set", "confirmed", user_initiated=False))
    with patched(mut, "_attach_shared_set_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_attach_shared_set(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="23",
            shared_set_id="77",
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
        )
    assert res["applied"] is True
    assert called == {"campaign_id": "23", "shared_set_id": "77"}
    assert store.finalized is True


async def test_apply_attach_shared_set_requires_resolved_id():
    """Пустой shared_set_id — ошибка оркестрации, НЕ «создай сам»: отказ ДО claim (fail-closed;
    резолв имени → id и отказ на несуществующем списке живут в execute_confirmed)."""
    store = FakeStore(FakeProposal("attach_shared_set", "confirmed", user_initiated=False))
    with allowed_ids(DRAFT_ACCOUNT_ID):
        for bad in ("", "  ", None):
            try:
                await mut.apply_attach_shared_set(
                    customer_id=DRAFT_ACCOUNT_ID,
                    campaign_id="23",
                    shared_set_id=bad,
                    confirmation_id="ok",
                    confirm_store=store,
                    ads_client=object(),
                )
                raise AssertionError(f"ожидался ValueError (shared_set_id={bad!r})")
            except ValueError:
                pass
    assert store._claimed is False and store.finalized is False


async def test_apply_attach_shared_set_replay_is_one_shot():
    calls = {"n": 0}

    def fake(client, customer_id, campaign_id, shared_set_id):
        calls["n"] += 1
        return {"applied": True}

    store = FakeStore(FakeProposal("attach_shared_set", "confirmed", user_initiated=False))
    kw = {
        "customer_id": DRAFT_ACCOUNT_ID,
        "campaign_id": "23",
        "shared_set_id": "77",
        "confirmation_id": "ok",
        "ads_client": object(),
    }
    with patched(mut, "_attach_shared_set_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        await mut.apply_attach_shared_set(confirm_store=store, **kw)
        try:
            await mut.apply_attach_shared_set(confirm_store=store, **kw)
            raise AssertionError("replay должен падать PermissionError")
        except PermissionError:
            pass
    assert calls["n"] == 1


# ── apply_remove_negative_keywords: симметрично add (по тексту+типу), НЕ деньги ───
async def test_apply_remove_negative_keywords_happy_path():
    called = {}

    def fake(client, customer_id, campaign_id, keywords, match_type):
        called.update(campaign_id=campaign_id, keywords=list(keywords), match_type=match_type)
        return {"applied": True, "removed": ["rn1"], "count": 1, "not_found": []}

    # user_initiated=False: снятие минус-слова — не деньги, гейтом не блокируется.
    store = FakeStore(FakeProposal("remove_negative_keywords", "confirmed", user_initiated=False))
    with patched(mut, "_remove_negative_keywords_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_remove_negative_keywords(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="23",
            keywords=["  бесплатно  "],
            match_type="broad",
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
        )
    assert res["applied"] is True
    assert called["campaign_id"] == "23"
    assert called["keywords"][0] == "бесплатно"  # normalize обрезал пробелы
    assert store.finalized is True


def test_remove_negative_keywords_via_sdk_resolves_text_and_removes_only_matched():
    rows = [
        SimpleNamespace(
            campaign_criterion=SimpleNamespace(
                resource_name="rn1", keyword=SimpleNamespace(text="бесплатно")
            )
        ),
        SimpleNamespace(
            campaign_criterion=SimpleNamespace(
                resource_name="rn2", keyword=SimpleNamespace(text="скачать")
            )
        ),
    ]

    class _GA:
        def search(self, customer_id, query):
            return rows

    class _Crit:
        def mutate_campaign_criteria(self, customer_id, operations):
            return SimpleNamespace(
                results=[SimpleNamespace(resource_name=o.remove) for o in operations]
            )

    class _Cmp:
        def campaign_path(self, cid, campid):
            return f"customers/{cid}/campaigns/{campid}"

    class _Client:
        def get_service(self, name):
            return {
                "GoogleAdsService": _GA(),
                "CampaignCriterionService": _Crit(),
                "CampaignService": _Cmp(),
            }[name]

        def get_type(self, name):
            return SimpleNamespace(remove=None)

    res = mut._remove_negative_keywords_via_sdk(
        _Client(), DRAFT_ACCOUNT_ID, "23", ["бесплатно"], "broad"
    )
    assert res["removed"] == ["rn1"]  # снят только запрошенный «бесплатно», не «скачать»
    assert res["count"] == 1 and res["not_found"] == []


# ── apply_detach_audience: обратная к attach (резолв rn аудитории → criterion → remove) ─
async def test_apply_detach_audience_happy_path():
    called = {}

    def fake(client, customer_id, campaign_id, audience_resource_names):
        called.update(campaign_id=campaign_id, rns=list(audience_resource_names))
        return {"applied": True, "detached": ["crit1"], "count": 1, "not_found": []}

    store = FakeStore(FakeProposal("detach_audience", "confirmed", user_initiated=False))
    with patched(mut, "_detach_audience_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_detach_audience(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="23",
            audience_resource_names=["customers/7753643025/userLists/999"],
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
        )
    assert res["applied"] is True
    assert called["campaign_id"] == "23"
    assert store.finalized is True


async def test_apply_detach_audience_validates_rns_before_claim():
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return {"applied": True}

    store = FakeStore(FakeProposal("detach_audience", "confirmed", user_initiated=True))
    with patched(mut, "_detach_audience_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await mut.apply_detach_audience(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="23",
                audience_resource_names=["not-an-audience-rn"],  # невалидный → ValueError ДО claim
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
            )
            raise AssertionError("ожидался ValueError (некорректный resource_name)")
        except ValueError:
            pass
    assert calls["n"] == 0 and store.finalized is False


def test_detach_audience_via_sdk_resolves_and_removes_only_matched():
    rows = [
        SimpleNamespace(
            campaign_criterion=SimpleNamespace(
                resource_name="crit1",
                user_list=SimpleNamespace(user_list="customers/1/userLists/999"),
                audience=SimpleNamespace(audience=""),
            )
        ),
        SimpleNamespace(
            campaign_criterion=SimpleNamespace(
                resource_name="crit2",
                user_list=SimpleNamespace(user_list="customers/1/userLists/888"),
                audience=SimpleNamespace(audience=""),
            )
        ),
    ]

    class _GA:
        def search(self, customer_id, query):
            return rows

    class _Crit:
        def mutate_campaign_criteria(self, customer_id, operations):
            return SimpleNamespace(
                results=[SimpleNamespace(resource_name=o.remove) for o in operations]
            )

    class _Cmp:
        def campaign_path(self, cid, campid):
            return f"customers/{cid}/campaigns/{campid}"

    class _Client:
        def get_service(self, name):
            return {
                "GoogleAdsService": _GA(),
                "CampaignCriterionService": _Crit(),
                "CampaignService": _Cmp(),
            }[name]

        def get_type(self, name):
            return SimpleNamespace(remove=None)

    res = mut._detach_audience_via_sdk(
        _Client(), DRAFT_ACCOUNT_ID, "23", ["customers/1/userLists/999"]
    )
    assert res["detached"] == ["crit1"]  # снят только запрошенный список 999, не 888
    assert res["count"] == 1 and res["not_found"] == []


# ── apply_remove_keywords: симметрично add (по тексту+типу), оба гейта ───────────
async def test_apply_remove_keywords_happy_path():
    called = {}

    def fake(client, customer_id, ad_group_ids, keywords, match_type):
        called.update(
            ad_group_ids=list(ad_group_ids), keywords=list(keywords), match_type=match_type
        )
        return {"applied": True, "removed": ["rn1"], "count": 1, "not_found": []}

    store = FakeStore(FakeProposal("remove_keywords", "confirmed", user_initiated=True))
    with patched(mut, "_remove_keywords_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_remove_keywords(
            customer_id=DRAFT_ACCOUNT_ID,
            ad_group_ids=["1", "2"],
            keywords=["  цветы  ", "доставка"],
            match_type="phrase",
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
        )
    assert res["applied"] is True
    assert called["match_type"] == "phrase"
    assert called["keywords"][0] == "цветы"  # normalize_keywords обрезал пробелы
    assert store.finalized is True


def test_remove_keywords_via_sdk_resolves_text_and_removes_only_matched():
    rows = [
        SimpleNamespace(
            ad_group_criterion=SimpleNamespace(
                resource_name="rn1", keyword=SimpleNamespace(text="цветы")
            )
        ),
        SimpleNamespace(
            ad_group_criterion=SimpleNamespace(
                resource_name="rn2", keyword=SimpleNamespace(text="трава")
            )
        ),
    ]

    class _GA:
        def search(self, customer_id, query):
            return rows

    class _Crit:
        def mutate_ad_group_criteria(self, customer_id, operations):
            return SimpleNamespace(
                results=[SimpleNamespace(resource_name=o.remove) for o in operations]
            )

    class _Client:
        def get_service(self, name):
            return _GA() if name == "GoogleAdsService" else _Crit()

        def get_type(self, name):
            return SimpleNamespace(remove=None)

    res = mut._remove_keywords_via_sdk(_Client(), DRAFT_ACCOUNT_ID, ["1"], ["цветы"], "broad")
    assert res["removed"] == ["rn1"]  # удалён только запрошенный «цветы», не «трава»
    assert res["count"] == 1 and res["not_found"] == []


def test_remove_keywords_via_sdk_reports_not_found():
    class _GA:
        def search(self, customer_id, query):
            return []  # ничего не нашлось

    class _Client:
        def get_service(self, name):
            return _GA()

        def get_type(self, name):
            return SimpleNamespace(remove=None)

    res = mut._remove_keywords_via_sdk(_Client(), DRAFT_ACCOUNT_ID, ["1"], ["нетакого"], "exact")
    assert res["removed"] == [] and res["not_found"] == ["нетакого"]  # явно, без «тихого» молчания


def test_remove_keywords_query_excludes_negatives_and_removed():
    """B11: GAQL резолва ключей к удалению фильтрует negative=FALSE и status!=REMOVED — иначе
    «удали ключ X» снёс бы и групповое МИНУС-слово X (оба type=KEYWORD)."""
    seen = {}

    class _GA:
        def search(self, customer_id, query):
            seen["q"] = query
            return []

    class _Client:
        def get_service(self, name):
            return _GA()

        def get_type(self, name):
            return SimpleNamespace(remove=None)

    mut._remove_keywords_via_sdk(_Client(), DRAFT_ACCOUNT_ID, ["1"], ["x"], "broad")
    assert "ad_group_criterion.negative = FALSE" in seen["q"]
    assert "ad_group_criterion.status != 'REMOVED'" in seen["q"]


def test_remove_keywords_does_not_touch_group_negative_of_same_text():
    """Функциональный B11: групповой минус «x» (negative=TRUE) не попадает под удаление позитивного
    ключа «x» — status-aware фейк уважает фильтр negative=FALSE, как реальный сервер."""
    positive = SimpleNamespace(
        ad_group_criterion=SimpleNamespace(
            resource_name="rn_pos", keyword=SimpleNamespace(text="x"), negative=False
        )
    )
    negative = SimpleNamespace(
        ad_group_criterion=SimpleNamespace(
            resource_name="rn_neg", keyword=SimpleNamespace(text="x"), negative=True
        )
    )

    class _GA:
        def search(self, customer_id, query):
            all_rows = [positive, negative]
            if "ad_group_criterion.negative = FALSE" in query:
                return [r for r in all_rows if not r.ad_group_criterion.negative]
            return all_rows

    class _Crit:
        def mutate_ad_group_criteria(self, customer_id, operations):
            return SimpleNamespace(
                results=[SimpleNamespace(resource_name=o.remove) for o in operations]
            )

    class _Client:
        def get_service(self, name):
            return _GA() if name == "GoogleAdsService" else _Crit()

        def get_type(self, name):
            return SimpleNamespace(remove=None)

    res = mut._remove_keywords_via_sdk(_Client(), DRAFT_ACCOUNT_ID, ["1"], ["x"], "broad")
    assert res["removed"] == ["rn_pos"]  # снят только позитивный ключ
    assert "rn_neg" not in res["removed"]  # групповой минус НЕ тронут


# ── apply_resume_campaign: реюз статус-исполнителя со статусом ENABLED ───────────
async def test_apply_resume_campaign_happy_path():
    called = {}

    def fake(client, customer_id, campaign_id, status):
        called.update(customer_id=customer_id, campaign_id=campaign_id, status=status)
        return {"applied": True, "status": status}

    store = FakeStore(FakeProposal("resume_campaign", "confirmed", user_initiated=True))
    with patched(mut, "_set_campaign_status_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_resume_campaign(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="23",
            confirmation_id="ok",
            confirm_store=store,
            ads_client=_FakeClient(),
        )
    assert res["applied"] is True
    assert called["status"] == "ENABLED"  # resume → ENABLED
    assert store.finalized is True


# ── apply_update_campaign (§3 «изменение»): переименование, НЕ деньги (без user_initiated) ──
async def test_apply_update_campaign_happy_path():
    called = {}

    def fake(client, customer_id, campaign_id, new_name):
        called.update(customer_id=customer_id, campaign_id=campaign_id, new_name=new_name)
        return {"applied": True, "new_name": new_name}

    # user_initiated=False намеренно: переименование — не деньги, гейтом user_initiated НЕ блокируется.
    store = FakeStore(FakeProposal("update_campaign", "confirmed", user_initiated=False))
    with patched(mut, "_update_campaign_name_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_update_campaign(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="23",
            new_name="  Весна 2026  ",
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
        )
    assert res["applied"] is True
    assert called["campaign_id"] == "23"
    assert called["new_name"] == "Весна 2026"  # код обрезал пробелы ДО SDK
    assert store.finalized is True


async def test_apply_update_campaign_validates_empty_name_before_claim():
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return {"applied": True}

    store = FakeStore(FakeProposal("update_campaign", "confirmed", user_initiated=True))
    with patched(mut, "_update_campaign_name_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await mut.apply_update_campaign(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="23",
                new_name="   ",  # пусто после strip → ValueError ДО claim
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
            )
            raise AssertionError("ожидался ValueError (пустое имя)")
        except ValueError:
            pass
    assert calls["n"] == 0 and store.finalized is False


# ── apply_set_campaign_network (§19.3): тумблер поисковых партнёров, НЕ деньги ────
async def test_apply_set_campaign_network_happy_path():
    called = {}

    def fake(client, customer_id, campaign_id, search_partners):
        called.update(
            customer_id=customer_id, campaign_id=campaign_id, search_partners=search_partners
        )
        return {"applied": True, "search_partners": search_partners}

    # user_initiated=False намеренно: сети — не деньги, гейтом user_initiated НЕ блокируются.
    store = FakeStore(FakeProposal("set_campaign_network", "confirmed", user_initiated=False))
    with patched(mut, "_set_campaign_network_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_set_campaign_network(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="23",
            search_partners=False,
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
        )
    assert res["applied"] is True
    assert called["campaign_id"] == "23" and called["search_partners"] is False
    assert store.finalized is True


def test_set_campaign_network_via_sdk_mask_and_field_real_proto():
    """На РЕАЛЬНЫХ прото: маска — лист network_settings.target_search_network (иначе сервер
    отверг бы FIELD_HAS_SUBFIELDS); явный False тоже попадает в маску (это и есть «выключить»);
    ограниченная partner_search_network и КМС в маске НЕ появляются никогда."""
    for flag in (False, True):
        client = _RealProtoClient()
        res = mut._set_campaign_network_via_sdk(client, DRAFT_ACCOUNT_ID, "23", flag)
        assert res["applied"] is True and res["search_partners"] is flag
        op = client.captured["op"]
        paths = list(op.update_mask.paths)
        assert "network_settings.target_search_network" in paths, paths
        _assert_mask_paths_are_leaf(paths)
        assert bool(op.update.network_settings.target_search_network) is flag
        assert not any("partner_search_network" in p or "content_network" in p for p in paths), (
            paths
        )


# ── G12: apply_set_campaign_display_network — КМС на кампании, НЕ деньги ──────────
async def test_apply_set_campaign_display_network_happy_path():
    called = {}

    def fake(client, customer_id, campaign_id, display_network):
        called.update(campaign_id=campaign_id, display_network=display_network)
        return {"applied": True, "display_network": display_network}

    # user_initiated=False намеренно: сети — не деньги (как и у тумблера партнёров).
    store = FakeStore(FakeProposal("set_campaign_display_network", "confirmed", False))
    with patched(mut, "_set_campaign_display_network_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_set_campaign_display_network(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="23",
            display_network=False,
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
        )
    assert res["applied"] is True
    assert called["campaign_id"] == "23" and called["display_network"] is False
    assert store.finalized is True


async def test_apply_set_campaign_display_network_replay_is_one_shot():
    """Тот же confirmation_id второй раз → PermissionError, SDK вызван РОВНО один раз.
    («Чужой аккаунт» и «без подтверждения» для этой операции покрывает матрица _apply_case.)"""
    calls = {"n": 0}

    def fake(client, customer_id, campaign_id, display_network):
        calls["n"] += 1
        return {"applied": True}

    async def call(store):
        return await mut.apply_set_campaign_display_network(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="23",
            display_network=False,
            confirmation_id="c1",
            confirm_store=store,
            ads_client=object(),
        )

    store = FakeStore(FakeProposal("set_campaign_display_network", "confirmed", False))
    with patched(mut, "_set_campaign_display_network_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        await call(store)
        try:
            await call(store)
            raise AssertionError("replay должен падать PermissionError")
        except PermissionError:
            pass
    assert calls["n"] == 1


def test_set_campaign_display_network_via_sdk_mask_and_field_real_proto():
    """На РЕАЛЬНЫХ прото: маска — лист network_settings.target_content_network; явный False в маске
    (иначе proto3 не увидел бы «выключить»); партнёрские сети в маске НЕ появляются."""
    for flag in (False, True):
        client = _RealProtoClient()
        res = mut._set_campaign_display_network_via_sdk(client, DRAFT_ACCOUNT_ID, "23", flag)
        assert res["applied"] is True and res["display_network"] is flag
        op = client.captured["op"]
        paths = list(op.update_mask.paths)
        assert paths == ["network_settings.target_content_network"], paths
        _assert_mask_paths_are_leaf(paths)
        assert bool(op.update.network_settings.target_content_network) is flag
        assert not any("search_network" in p for p in paths), paths


# ── G11: apply_set_campaign_geo_target_type — «присутствие ИЛИ интерес» → «присутствие» ──
async def test_apply_set_campaign_geo_target_type_happy_path():
    called = {}

    def fake(client, customer_id, campaign_id, geo_target_type):
        called.update(campaign_id=campaign_id, geo_target_type=geo_target_type)
        return {"applied": True, "geo_target_type": geo_target_type}

    store = FakeStore(FakeProposal("set_campaign_geo_target_type", "confirmed", False))
    with (
        patched(mut, "_set_campaign_geo_target_type_via_sdk", fake),
        allowed_ids(DRAFT_ACCOUNT_ID),
    ):
        res = await mut.apply_set_campaign_geo_target_type(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="23",
            geo_target_type="presence",  # регистр нормализует КОД, не модель
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
        )
    assert res["applied"] is True
    assert called["geo_target_type"] == "PRESENCE" and store.finalized is True


async def test_apply_set_campaign_geo_target_type_rejects_unknown_value_before_claim():
    """Мусорное значение (в т.ч. SEARCH_INTEREST — он вне allow-list) отвергается ДО claim:
    одноразовый черновик остаётся жив, SDK не зовётся."""
    calls = {"n": 0}

    def fake(client, customer_id, campaign_id, geo_target_type):
        calls["n"] += 1
        return {"applied": True}

    for bad in ("SEARCH_INTEREST", "", "DROP TABLE"):
        store = FakeStore(FakeProposal("set_campaign_geo_target_type", "confirmed", False))
        with (
            patched(mut, "_set_campaign_geo_target_type_via_sdk", fake),
            allowed_ids(DRAFT_ACCOUNT_ID),
        ):
            try:
                await mut.apply_set_campaign_geo_target_type(
                    customer_id=DRAFT_ACCOUNT_ID,
                    campaign_id="23",
                    geo_target_type=bad,
                    confirmation_id="ok",
                    confirm_store=store,
                    ads_client=object(),
                )
                raise AssertionError(f"ожидался ValueError на «{bad}»")
            except ValueError:
                pass
        assert store.finalized is False
        # черновик НЕ съеден: после отказа его всё ещё можно заклеймить корректным значением
        assert await store.claim("ok", operation="set_campaign_geo_target_type") is not None
    assert calls["n"] == 0


async def test_apply_set_campaign_geo_target_type_replay_is_one_shot():
    calls = {"n": 0}

    def fake(client, customer_id, campaign_id, geo_target_type):
        calls["n"] += 1
        return {"applied": True}

    async def call(store):
        return await mut.apply_set_campaign_geo_target_type(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="23",
            geo_target_type="PRESENCE",
            confirmation_id="c1",
            confirm_store=store,
            ads_client=object(),
        )

    store = FakeStore(FakeProposal("set_campaign_geo_target_type", "confirmed", False))
    with (
        patched(mut, "_set_campaign_geo_target_type_via_sdk", fake),
        allowed_ids(DRAFT_ACCOUNT_ID),
    ):
        await call(store)
        try:
            await call(store)
            raise AssertionError("replay должен падать PermissionError")
        except PermissionError:
            pass
    assert calls["n"] == 1


def test_set_campaign_geo_target_type_via_sdk_mask_and_enum_real_proto():
    """На РЕАЛЬНЫХ прото: маска — лист geo_target_type_setting.positive_geo_target_type; значение
    ложится настоящим enum'ом; negative_geo_target_type НЕ трогаем."""
    from google.ads.googleads.v24.enums.types.positive_geo_target_type import (
        PositiveGeoTargetTypeEnum,
    )

    for value in ("PRESENCE", "PRESENCE_OR_INTEREST"):
        client = _RealProtoClient()
        res = mut._set_campaign_geo_target_type_via_sdk(client, DRAFT_ACCOUNT_ID, "23", value)
        assert res["applied"] is True and res["geo_target_type"] == value
        op = client.captured["op"]
        paths = list(op.update_mask.paths)
        assert paths == ["geo_target_type_setting.positive_geo_target_type"], paths
        _assert_mask_paths_are_leaf(paths)
        assert op.update.geo_target_type_setting.positive_geo_target_type == getattr(
            PositiveGeoTargetTypeEnum.PositiveGeoTargetType, value
        )
        assert not any("negative" in p for p in paths), paths


# ── C6: пауза/возобновление/удаление ОТДЕЛЬНОГО объявления (AdGroupAdService) ─────
async def test_apply_pause_ad_happy_path():
    called = {}

    def fake(client, customer_id, ad_group_id, ad_id, status):
        called.update(ad_group_id=ad_group_id, ad_id=ad_id, status=status)
        return {"applied": True, "status": status}

    store = FakeStore(FakeProposal("pause_ad", "confirmed", user_initiated=False))
    with patched(mut, "_set_ad_status_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_pause_ad(
            customer_id=DRAFT_ACCOUNT_ID,
            ad_group_id="77",
            ad_id="101",
            confirmation_id="ok",
            confirm_store=store,
            ads_client=_FakeClient(),
        )
    assert res["applied"] is True and called["ad_id"] == "101" and called["status"] == "PAUSED"
    assert store.finalized is True


async def test_apply_remove_ad_happy_path():
    called = {}

    def fake(client, customer_id, ad_group_id, ad_id):
        called.update(ad_group_id=ad_group_id, ad_id=ad_id)
        return {"removed": True, "applied": True}

    store = FakeStore(FakeProposal("remove_ad", "confirmed", user_initiated=False))
    with patched(mut, "_remove_ad_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_remove_ad(
            customer_id=DRAFT_ACCOUNT_ID,
            ad_group_id="77",
            ad_id="101",
            confirmation_id="ok",
            confirm_store=store,
            ads_client=_FakeClient(),
        )
    assert res["removed"] is True and called["ad_id"] == "101"
    assert store.finalized is True


def test_ad_status_and_remove_via_sdk_resource_and_mask():
    """SDK-слой C6: resource_name adGroupAds/{ag}~{ad}; у update маска несёт status; remove — путь."""
    captured: dict[str, object] = {}

    class _Svc:
        def ad_group_ad_path(self, cid, ag, ad):
            return f"customers/{cid}/adGroupAds/{ag}~{ad}"

        def mutate_ad_group_ads(self, customer_id, operations):
            captured["op"] = operations[0]

    class _Client:
        def get_service(self, name):
            assert name == "AdGroupAdService"
            return _Svc()

        def get_type(self, name):
            from google.ads.googleads.v24.services.types import AdGroupAdOperation

            assert name == "AdGroupAdOperation"
            return AdGroupAdOperation()

        @staticmethod
        def copy_from(destination, origin):
            import proto

            if isinstance(origin, proto.Message):
                origin = origin._pb
            destination.CopyFrom(origin)

    res = mut._set_ad_status_via_sdk(_Client(), DRAFT_ACCOUNT_ID, "77", "101", 3)  # 3=PAUSED enum
    assert res["applied"] is True
    op = captured["op"]
    assert op.update.resource_name.endswith("/adGroupAds/77~101")
    assert "status" in list(op.update_mask.paths)

    res2 = mut._remove_ad_via_sdk(_Client(), DRAFT_ACCOUNT_ID, "77", "101")
    assert res2["removed"] is True
    assert captured["op"].remove.endswith("/adGroupAds/77~101")


# ── apply_pause_ad_group / apply_resume_ad_group (§16 AdGroupService): оба гейта, без денег ──
async def test_apply_pause_ad_group_happy_path():
    called = {}

    def fake(client, customer_id, ad_group_id, status):
        called.update(ad_group_id=ad_group_id, status=status)
        return {"applied": True, "status": status}

    store = FakeStore(FakeProposal("pause_ad_group", "confirmed", user_initiated=True))
    with patched(mut, "_set_ad_group_status_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_pause_ad_group(
            customer_id=DRAFT_ACCOUNT_ID,
            ad_group_id="77",
            confirmation_id="ok",
            confirm_store=store,
            ads_client=_FakeClient(),
        )
    assert res["applied"] is True
    assert called["ad_group_id"] == "77" and called["status"] == "PAUSED"  # pause → PAUSED
    assert store.finalized is True


async def test_apply_resume_ad_group_happy_path():
    called = {}

    def fake(client, customer_id, ad_group_id, status):
        called.update(status=status)
        return {"applied": True, "status": status}

    store = FakeStore(FakeProposal("resume_ad_group", "confirmed", user_initiated=True))
    with patched(mut, "_set_ad_group_status_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_resume_ad_group(
            customer_id=DRAFT_ACCOUNT_ID,
            ad_group_id="77",
            confirmation_id="ok",
            confirm_store=store,
            ads_client=_FakeClient(),
        )
    assert res["applied"] is True
    assert called["status"] == "ENABLED"  # resume → ENABLED
    assert store.finalized is True


async def test_apply_pause_ad_group_replay_one_shot():
    """Replay: тот же confirmation_id второй раз — PermissionError, SDK зван РОВНО один раз."""
    calls = {"n": 0}

    def fake(client, customer_id, ad_group_id, status):
        calls["n"] += 1
        return {"applied": True, "status": status}

    store = FakeStore(FakeProposal("pause_ad_group", "confirmed", user_initiated=True))
    with patched(mut, "_set_ad_group_status_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        await mut.apply_pause_ad_group(
            customer_id=DRAFT_ACCOUNT_ID,
            ad_group_id="77",
            confirmation_id="ok",
            confirm_store=store,
            ads_client=_FakeClient(),
        )
        try:
            await mut.apply_pause_ad_group(
                customer_id=DRAFT_ACCOUNT_ID,
                ad_group_id="77",
                confirmation_id="ok",
                confirm_store=store,
                ads_client=_FakeClient(),
            )
            raise AssertionError("ожидался PermissionError (replay)")
        except PermissionError:
            pass
    assert calls["n"] == 1  # SDK зван ровно один раз (одноразовый claim)


# ── apply_set_geo_proximity (A-geo): оба гейта, address-driven, без геокодинга ───
async def test_apply_set_geo_proximity_happy_path():
    called = {}

    def fake(client, customer_id, campaign_id, radius_km, address):
        called.update(campaign_id=campaign_id, radius_km=radius_km, address=dict(address))
        return {"applied": True, "radius_km": radius_km}

    store = FakeStore(FakeProposal("set_geo_proximity", "confirmed", user_initiated=True))
    with patched(mut, "_set_geo_proximity_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_set_geo_proximity(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="23",
            radius_km=10.0,
            address={"city_name": "Киев", "country_code": "UA"},
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
        )
    assert res["applied"] is True
    assert called["radius_km"] == 10.0
    assert called["address"]["city_name"] == "Киев"  # структурный адрес дошёл до SDK
    assert store.finalized is True


async def test_apply_set_geo_proximity_rejects_zero_radius_before_sdk():
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return {"applied": True}

    store = FakeStore(FakeProposal("set_geo_proximity", "confirmed", user_initiated=True))
    with patched(mut, "_set_geo_proximity_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await mut.apply_set_geo_proximity(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="23",
                radius_km=0,  # код отклоняет ДО claim
                address={"city_name": "Киев", "country_code": "UA"},
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
            )
            raise AssertionError("ожидался ValueError (радиус 0)")
        except ValueError:
            pass
    assert calls["n"] == 0  # SDK не вызван
    assert store.finalized is False


async def test_apply_set_geo_proximity_rejects_foreign_account():
    store = FakeStore(FakeProposal("set_geo_proximity", "confirmed", user_initiated=True))
    with (
        patched(mut, "_set_geo_proximity_via_sdk", lambda *a, **k: {"applied": True}),
        allowed_ids(DRAFT_ACCOUNT_ID),
    ):
        try:
            await mut.apply_set_geo_proximity(
                customer_id="1234567890",  # чужой → замок отклоняет
                campaign_id="23",
                radius_km=5,
                address={"city_name": "Киев", "country_code": "UA"},
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
            )
            raise AssertionError("ожидался PermissionError (чужой аккаунт)")
        except PermissionError:
            pass


async def test_apply_set_geo_proximity_validates_address_before_claim():
    """Пустой city_name → ValueError ДО claim (golden rule #4): SDK не зван, черновик не съеден."""
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return {"applied": True}

    store = FakeStore(FakeProposal("set_geo_proximity", "confirmed", user_initiated=True))
    with patched(mut, "_set_geo_proximity_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await mut.apply_set_geo_proximity(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="23",
                radius_km=5,
                address={"city_name": "", "country_code": "UA"},  # пустой город
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
            )
            raise AssertionError("ожидался ValueError (пустой city_name)")
        except ValueError:
            pass
    assert calls["n"] == 0  # SDK не вызван
    assert store.finalized is False  # черновик не финализирован


# ── apply_set_geo_location (§3 страна/город): оба гейта, резолв названий → constants ─
async def test_apply_set_geo_location_happy_path():
    called = {}

    def fake(client, customer_id, campaign_id, locations, country_code, locale):
        called.update(
            campaign_id=campaign_id, locations=list(locations), cc=country_code, locale=locale
        )
        return {"applied": True, "count": len(locations)}

    store = FakeStore(FakeProposal("set_geo_location", "confirmed", user_initiated=True))
    with patched(mut, "_set_geo_location_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_set_geo_location(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="23",
            locations=["Украина", "Киев"],
            country_code="UA",
            locale="ru",
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
        )
    assert res["applied"] is True and called["campaign_id"] == "23"
    assert called["locations"] == ["Украина", "Киев"] and called["cc"] == "UA"
    assert store.finalized is True


async def test_apply_set_geo_location_validates_empty_before_claim():
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return {"applied": True}

    store = FakeStore(FakeProposal("set_geo_location", "confirmed", user_initiated=True))
    with patched(mut, "_set_geo_location_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await mut.apply_set_geo_location(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="23",
                locations=["   "],  # пустые после strip → ValueError ДО claim
                country_code="UA",
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
            )
            raise AssertionError("ожидался ValueError (пустые локации)")
        except ValueError:
            pass
    assert calls["n"] == 0 and store.finalized is False


def test_set_geo_location_via_sdk_resolves_and_replaces():
    """Резолв названий → geoTargetConstant (топ-подсказка на термин, дубли не задваиваются) +
    remove-before-create: 1 старый LOCATION заменён 2 новыми."""
    captured = {}

    class _Cmp:
        def campaign_path(self, cid, campid):
            return f"customers/{cid}/campaigns/{campid}"

    class _GA:
        def search(self, customer_id, query):
            return [SimpleNamespace(campaign_criterion=SimpleNamespace(resource_name="old_loc1"))]

    class _GTC:
        def suggest_geo_target_constants(self, request):
            sugg = [
                SimpleNamespace(
                    search_term="Украина",
                    geo_target_constant=SimpleNamespace(
                        resource_name="geoTargetConstants/2804", name="Ukraine"
                    ),
                ),
                SimpleNamespace(
                    search_term="Киев",
                    geo_target_constant=SimpleNamespace(
                        resource_name="geoTargetConstants/1012852", name="Kyiv"
                    ),
                ),
                SimpleNamespace(  # дубль термина — игнорируется (берём первую подсказку)
                    search_term="Украина",
                    geo_target_constant=SimpleNamespace(
                        resource_name="geoTargetConstants/9999", name="Ukraine alt"
                    ),
                ),
            ]
            return SimpleNamespace(geo_target_constant_suggestions=sugg)

    class _Crit:
        def mutate_campaign_criteria(self, customer_id, operations):
            captured["ops"] = list(operations)
            return SimpleNamespace(results=[SimpleNamespace(resource_name="n") for _ in operations])

    class _Client:
        def get_service(self, name):
            return {
                "CampaignService": _Cmp(),
                "GoogleAdsService": _GA(),
                "GeoTargetConstantService": _GTC(),
                "CampaignCriterionService": _Crit(),
            }[name]

        def get_type(self, name):
            if name == "SuggestGeoTargetConstantsRequest":
                return SimpleNamespace(
                    locale="", country_code="", location_names=SimpleNamespace(names=[])
                )
            if name == "CampaignCriterionOperation":
                return SimpleNamespace(
                    create=SimpleNamespace(
                        campaign=None, location=SimpleNamespace(geo_target_constant=None)
                    ),
                    remove=None,
                )
            raise AssertionError(name)

    res = mut._set_geo_location_via_sdk(
        _Client(), DRAFT_ACCOUNT_ID, "23", ["Украина", "Киев"], "UA", "ru"
    )
    assert res["applied"] is True
    assert res["count"] == 2 and res["removed_location"] == 1
    assert set(res["locations"]) == {"Ukraine", "Kyiv"}
    ops = captured["ops"]
    assert sum(1 for o in ops if o.remove is not None) == 1  # один remove (старый LOCATION)
    assert (
        sum(1 for o in ops if o.create.location.geo_target_constant) == 2
    )  # два create (новые гео)


def test_set_geo_location_via_sdk_raises_when_nothing_resolved():
    """Ни одна локация не распознана → НЕ трогаем кампанию (raise), чтобы не стереть гео в пустоту."""

    class _GTC:
        def suggest_geo_target_constants(self, request):
            return SimpleNamespace(geo_target_constant_suggestions=[])

    class _Client:
        def get_service(self, name):
            if name == "GeoTargetConstantService":
                return _GTC()
            return SimpleNamespace(campaign_path=lambda c, i: "rn")

        def get_type(self, name):
            return SimpleNamespace(
                locale="", country_code="", location_names=SimpleNamespace(names=[])
            )

    try:
        mut._set_geo_location_via_sdk(
            _Client(), DRAFT_ACCOUNT_ID, "23", ["абракадабра"], "UA", "ru"
        )
        raise AssertionError("ожидался ValueError (ничего не распознано)")
    except ValueError:
        pass


def test_set_geo_proximity_via_sdk_replaces_and_maps_address():
    """A-geo inner: remove-before-create proximity (2 старых заменены 1 новым) + маппинг адреса/
    радиуса. Радиус-юниты ставит КОД (KILOMETERS), не модель; address-поля переносятся структурно."""
    captured = {}

    class _Cmp:
        def campaign_path(self, cid, campid):
            return f"customers/{cid}/campaigns/{campid}"

    class _GA:
        def search(self, customer_id, query):
            # два существующих PROXIMITY-критерия → оба должны попасть в remove
            return [
                SimpleNamespace(campaign_criterion=SimpleNamespace(resource_name="old_prox1")),
                SimpleNamespace(campaign_criterion=SimpleNamespace(resource_name="old_prox2")),
            ]

    class _Crit:
        def mutate_campaign_criteria(self, customer_id, operations):
            captured["ops"] = list(operations)
            captured["customer_id"] = customer_id
            return SimpleNamespace(results=[SimpleNamespace(resource_name="new_prox")])

    def _new_op():
        return SimpleNamespace(
            create=SimpleNamespace(
                campaign=None,
                proximity=SimpleNamespace(
                    radius=None,
                    radius_units=None,
                    address=SimpleNamespace(
                        street_address=None,
                        city_name=None,
                        province_code=None,
                        postal_code=None,
                        country_code=None,
                    ),
                ),
            ),
            remove=None,
        )

    class _Client:
        enums = SimpleNamespace(ProximityRadiusUnitsEnum=SimpleNamespace(KILOMETERS="KM"))

        def get_service(self, name):
            return {
                "CampaignService": _Cmp(),
                "GoogleAdsService": _GA(),
                "CampaignCriterionService": _Crit(),
            }[name]

        def get_type(self, name):
            assert name == "CampaignCriterionOperation"
            return _new_op()

    address = {"city_name": "Kyiv", "country_code": "UA", "street_address": "Main St 1"}
    res = mut._set_geo_proximity_via_sdk(_Client(), DRAFT_ACCOUNT_ID, "23", 5.0, address)

    assert res["applied"] is True
    assert res["removed_proximity"] == 2  # оба старых proximity заменены
    assert res["radius_km"] == 5.0
    assert res["resource_name"] == "new_prox"  # resp.results[-1]
    ops = captured["ops"]
    assert sum(1 for o in ops if o.remove is not None) == 2  # два remove
    creates = [o for o in ops if o.remove is None]
    assert len(creates) == 1  # ровно один create, последним
    prox = creates[0].create.proximity
    assert prox.radius == 5.0
    assert prox.radius_units == "KM"  # юнит выставил КОД (client.enums), не модель
    assert prox.address.city_name == "Kyiv"
    assert prox.address.country_code == "UA"
    assert prox.address.street_address == "Main St 1"
    assert prox.address.postal_code is None  # не передан → не выставляется


async def test_set_geo_location_supported_as_proposal():
    """set_geo_location поддержан → агент предлагает черновик с кнопками (не отклоняет)."""
    import agent.loop as L

    fake = _fake_chat(
        "set_geo_location",
        {"campaign": "X", "locations": ["Украина", "Киев"], "country_code": "UA"},
    )
    with patched(L, "chat", fake):
        res = await L.handle_command("таргет на Украину и Киев в кампании X", chat_id=1)
    assert res["type"] == "proposal" and res["operation"] == "set_geo_location"


# ── apply_set_bidding_strategy (§3): ДЕНЬГИ → оба гейта + user_initiated ──────────
async def test_apply_set_bidding_strategy_happy_path():
    called = {}

    def fake(client, customer_id, campaign_id, strategy, target_cpa_micros, target_roas, enh):
        called.update(
            strategy=strategy, target_cpa_micros=target_cpa_micros, campaign_id=campaign_id
        )
        return {"applied": True, "strategy": strategy}

    store = FakeStore(FakeProposal("set_bidding_strategy", "confirmed", user_initiated=True))
    with patched(mut, "_set_bidding_strategy_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_set_bidding_strategy(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="23",
            strategy="maximize_conversions",
            target_cpa=5.0,
            confirmation_id="ok",
            confirm_store=store,
            ads_client=object(),
        )
    assert res["applied"] and called["strategy"] == "maximize_conversions"
    assert called["target_cpa_micros"] == 5_000_000  # единицы → micros (КОД)
    assert store.finalized is True


async def test_apply_set_bidding_strategy_blocked_when_not_user_initiated():
    store = FakeStore(FakeProposal("set_bidding_strategy", "confirmed", user_initiated=False))
    with allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await mut.apply_set_bidding_strategy(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="23",
                strategy="manual_cpc",
                confirmation_id="x",
                confirm_store=store,
                ads_client=object(),
            )
            raise AssertionError("ожидался PermissionError (стратегия = деньги)")
        except PermissionError:
            pass
    assert store.finalized is False


async def test_apply_set_bidding_strategy_validates_target_cpa_before_claim():
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return {"applied": True}

    store = FakeStore(FakeProposal("set_bidding_strategy", "confirmed", user_initiated=True))
    with patched(mut, "_set_bidding_strategy_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await mut.apply_set_bidding_strategy(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="23",
                strategy="maximize_conversions",
                target_cpa=2_000_000,  # > 1e6 → ValueError ДО claim
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
            )
            raise AssertionError("ожидался ValueError (target_cpa за потолком)")
        except ValueError:
            pass
    assert calls["n"] == 0 and store.finalized is False


def test_set_bidding_strategy_via_sdk_maximize_conversions_sets_mask():
    captured = {}

    class _Cmp:
        def campaign_path(self, cid, campid):
            return f"customers/{cid}/campaigns/{campid}"

        def mutate_campaigns(self, customer_id, operations):
            captured["op"] = operations[0]
            return SimpleNamespace(results=[SimpleNamespace(resource_name="rn")])

    class _Client:
        def get_service(self, name):
            return _Cmp()

        def get_type(self, name):
            assert name == "CampaignOperation"
            # update_mask — поле ОПЕРАЦИИ (sibling of `update`), НЕ Campaign: реальный proto
            # бросает "Unknown field for Campaign: update_mask", если ставить его на op.update.
            return SimpleNamespace(
                update=SimpleNamespace(
                    resource_name=None,
                    manual_cpc=SimpleNamespace(enhanced_cpc_enabled=False),
                    maximize_conversions=SimpleNamespace(target_cpa_micros=0),
                    maximize_conversion_value=SimpleNamespace(target_roas=0.0),
                    target_spend=SimpleNamespace(),
                ),
                update_mask=SimpleNamespace(paths=[]),
            )

    res = mut._set_bidding_strategy_via_sdk(
        _Client(), DRAFT_ACCOUNT_ID, "23", "maximize_conversions", 5_000_000, None, False
    )
    assert res["applied"] and res["strategy"] == "maximize_conversions"
    assert res["target_cpa_micros"] == 5_000_000
    op = captured["op"]
    assert op.update.maximize_conversions.target_cpa_micros == 5_000_000
    # маска — на ЛИСТ (подполе), а не bare-имя стратегии: bare-message Google Ads отвергает
    # (FIELD_HAS_SUBFIELDS), а лист и переключает oneof, и задаёт target. И именно на op.update_mask.
    assert "maximize_conversions.target_cpa_micros" in op.update_mask.paths
    assert "maximize_conversions" not in op.update_mask.paths  # bare-имя НЕ должно попадать в маску


class _RealProtoClient:
    """Фейк GoogleAdsClient на РЕАЛЬНЫХ v24-прото (а не SimpleNamespace) — ловит ошибки уровня
    схемы, которые «всё-принимающие» моки пропускали дважды: update_mask на Campaign и bare-имя
    стратегии в маске. get_type отдаёт настоящие сообщения, поэтому неверное поле бросит, как SDK."""

    def __init__(self):
        self.captured: dict = {}

    def get_service(self, name):
        assert name == "CampaignService"
        client = self

        class _Svc:
            def campaign_path(self, cid, campid):
                return f"customers/{cid}/campaigns/{campid}"

            def mutate_campaigns(self, customer_id, operations):
                client.captured["op"] = operations[0]
                return SimpleNamespace(results=[SimpleNamespace(resource_name="rn")])

        return _Svc()

    def get_type(self, name):
        from google.ads.googleads.v24.common.types import (
            ManualCpc,
            MaximizeConversions,
            MaximizeConversionValue,
            TargetSpend,
        )
        from google.ads.googleads.v24.services.types import CampaignOperation

        return {
            "CampaignOperation": CampaignOperation,
            "MaximizeConversions": MaximizeConversions,
            "MaximizeConversionValue": MaximizeConversionValue,
            "TargetSpend": TargetSpend,
            "ManualCpc": ManualCpc,
        }[name]()

    @property
    def enums(self):
        """Зеркало google.ads…client._EnumGetter: отдаёт ВНУТРЕННИЙ enum (ProtoEnumMeta), а не
        сообщение-обёртку. Значения настоящие — подстановка мусора в прото упадёт, как у SDK."""
        from google.ads.googleads.v24.enums.types.positive_geo_target_type import (
            PositiveGeoTargetTypeEnum,
        )

        return SimpleNamespace(
            PositiveGeoTargetTypeEnum=PositiveGeoTargetTypeEnum.PositiveGeoTargetType
        )

    @staticmethod
    def copy_from(destination, origin):  # как google.ads…GoogleAdsClient.copy_from
        import proto

        if isinstance(origin, proto.Message):
            origin = origin._pb
        destination._pb.CopyFrom(origin)


def _assert_mask_paths_are_leaf(paths):
    """Эмулирует серверную проверку Google Ads FieldMaskError.FIELD_HAS_SUBFIELDS: каждый путь
    маски должен заканчиваться на ЛИСТ (скаляр) реального Campaign, а не на message-поле."""
    from google.protobuf.descriptor import FieldDescriptor

    from google.ads.googleads.v24.resources.types import Campaign

    desc = Campaign.pb(Campaign()).DESCRIPTOR
    for path in paths:
        cur, fd = desc, None
        for part in path.split("."):
            assert cur is not None, f"{path}: '{part}' за пределами message"
            fd = cur.fields_by_name.get(part)
            assert fd is not None, f"{path}: нет поля '{part}' в Campaign"
            cur = fd.message_type if fd.type == FieldDescriptor.TYPE_MESSAGE else None
        assert fd.type != FieldDescriptor.TYPE_MESSAGE, (
            f"маска '{path}' указывает на message-поле (с подполями) → API отвергнет "
            "FIELD_HAS_SUBFIELDS; нужен ЛИСТ-путь"
        )


def test_set_bidding_strategy_via_sdk_mask_is_leaf_for_all_strategies_real_proto():
    """Регресс FIELD_HAS_SUBFIELDS: на РЕАЛЬНЫХ прото для всех стратегий (вкл. ПУСТУЮ без target —
    кейс пользователя) маска указывает на лист-подполе и реально переключает oneof стратегии."""
    cases = [
        # strategy, target_cpa_micros, target_roas, enhanced, ожидаемый oneof
        ("manual_cpc", None, None, True, "manual_cpc"),
        ("manual_cpc", None, None, False, "manual_cpc"),
        ("maximize_conversions", 5_000_000, None, False, "maximize_conversions"),
        ("maximize_conversions", None, None, False, "maximize_conversions"),  # ПУСТАЯ — кейс юзера
        ("maximize_conversion_value", None, 4.0, False, "maximize_conversion_value"),
        ("maximize_conversion_value", None, None, False, "maximize_conversion_value"),  # пустая
        ("target_spend", None, None, False, "target_spend"),
    ]
    for strategy, tcpa, troas, enh, oneof in cases:
        client = _RealProtoClient()
        res = mut._set_bidding_strategy_via_sdk(
            client, DRAFT_ACCOUNT_ID, "23", strategy, tcpa, troas, enh
        )
        assert res["applied"] and res["strategy"] == strategy
        op = client.captured["op"]
        paths = list(op.update_mask.paths)
        assert paths, f"{strategy}: пустая маска — стратегия не переключится"
        _assert_mask_paths_are_leaf(paths)  # серверная проверка FIELD_HAS_SUBFIELDS
        # oneof стратегии реально выставлен, и каждый путь маски — под этой стратегией.
        assert op.update._pb.WhichOneof("campaign_bidding_strategy") == oneof
        assert all(p.startswith(oneof + ".") for p in paths), (strategy, paths)


async def test_set_bidding_strategy_supported_as_proposal():
    import agent.loop as L

    fake = _fake_chat(
        "set_bidding_strategy",
        {"campaign": "X", "strategy": "maximize_conversions", "target_cpa": 5.0},
    )
    with patched(L, "chat", fake):
        res = await L.handle_command(
            "стратегия максимум конверсий target CPA 5 в кампании X", chat_id=1
        )
    assert res["type"] == "proposal" and res["operation"] == "set_bidding_strategy"


async def test_update_campaign_supported_as_proposal():
    """§3 «изменение» кампании: «переименуй X в Y» → черновик update_campaign с кнопками."""
    import agent.loop as L

    fake = _fake_chat("update_campaign", {"campaign": "Старое имя", "new_name": "Новое имя"})
    with patched(L, "chat", fake):
        res = await L.handle_command("переименуй кампанию «Старое имя» в «Новое имя»", chat_id=1)
    assert res["type"] == "proposal" and res["operation"] == "update_campaign"


async def test_remove_negative_keywords_supported_as_proposal():
    """§3 «минус-слова»: «убери минус-слово X из кампании Y» → черновик remove_negative_keywords."""
    import agent.loop as L

    fake = _fake_chat(
        "remove_negative_keywords",
        {"campaign": "X", "keywords": ["бесплатно"], "match_type": "broad"},
    )
    with patched(L, "chat", fake):
        res = await L.handle_command("убери минус-слово «бесплатно» из кампании X", chat_id=1)
    assert res["type"] == "proposal" and res["operation"] == "remove_negative_keywords"


# ── Валидатор длины ключевых слов (golden rule #4: код, кириллица = 1) ───────────
def test_assert_keyword_ok_counts_cyrillic_as_one():
    assert mut._assert_keyword_ok("  цветы  ") == "цветы"
    assert mut._assert_keyword_ok("а" * 80) == "а" * 80  # ровно 80 — ок
    for bad in ["а" * 81, "   ", "слово " * 11]:
        try:
            mut._assert_keyword_ok(bad)
            raise AssertionError(f"должно было упасть: {bad!r}")
        except ValueError:
            pass


# ── Резолвер: escape и пересчёт micros (используется для bid) ────────────────────
def test_gaql_escape():
    from ads.resolve import _gaql_escape

    assert _gaql_escape("O'Brien") == "O\\'Brien"
    assert _gaql_escape("a\\b") == "a\\\\b"


def test_compute_new_micros_modes():
    from ads.resolve import compute_new_micros

    assert compute_new_micros(1_000_000, "set_to", 3, currency="USD") == 3_000_000
    assert compute_new_micros(1_000_000, "increase_by_percent", 20, currency="USD") == 1_200_000
    assert compute_new_micros(1_000_000, "increase_by_amount", 2, currency="USD") == 3_000_000


# ── Capability-guard на уровне agent.loop: отказ ДО показа кнопок ────────────────
class _FakeFunc:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _FakeCall:
    def __init__(self, name, arguments):
        self.function = _FakeFunc(name, arguments)


class _FakeMsg:
    def __init__(self, tool_calls=None, content=None):
        self.tool_calls = tool_calls
        self.content = content


def _fake_chat(name, arguments):
    async def _chat(messages, role=None, tools=None):
        return _FakeMsg(tool_calls=[_FakeCall(name, json.dumps(arguments))])

    return _chat


async def test_set_geo_proximity_now_supported_as_proposal():
    """A-geo активирован: set_geo_proximity со структурным адресом → черновик с кнопками."""
    import agent.loop as L

    fake = _fake_chat(
        "set_geo_proximity",
        {"campaign": "X", "city_name": "Киев", "country_code": "UA", "radius_km": 5},
    )
    with patched(L, "chat", fake):
        res = await L.handle_command("таргет в радиусе 5 км от Киева", chat_id=1)
    assert res["type"] == "proposal"  # geo поддержан → НЕ отклоняется
    assert res["operation"] == "set_geo_proximity"


async def test_capability_guard_declines_unsupported_mutation(monkeypatch):
    """Capability-guard (механизм): объявленную в TOOLS, но НЕ в SUPPORTED_OPERATIONS мутацию
    агент отклоняет ДО кнопок. Симулируем «отложенную» операцию, временно убрав update_bid
    из SUPPORTED (loop импортирует SUPPORTED_OPERATIONS лениво → monkeypatch виден)."""
    import agent.loop as L
    import ads.service as svc

    monkeypatch.setattr(svc, "SUPPORTED_OPERATIONS", svc.SUPPORTED_OPERATIONS - {"update_bid"})
    fake = _fake_chat("update_bid", {"campaign": "X", "mode": "set_to", "value": 1.5})
    with patched(L, "chat", fake):
        res = await L.handle_command("ставка 1.5 в кампании X", chat_id=1)
    assert res["type"] == "text"  # НЕ proposal → кнопок не будет
    assert "не поддерживается" in res["text"]


async def test_capability_guard_allows_supported_bid_as_proposal():
    import agent.loop as L

    fake = _fake_chat("update_bid", {"campaign": "X", "mode": "set_to", "value": 1.5})
    with patched(L, "chat", fake):
        res = await L.handle_command("ставку до 1.5 в кампании X", chat_id=1)
    assert res["type"] == "proposal"
    assert res["operation"] == "update_bid"


# ── Capability-guard / defense-in-depth на уровне execute_confirmed ──────────────
async def test_execute_confirmed_rejects_unsupported_op():
    """Defense-in-depth: операцию вне SUPPORTED_OPERATIONS execute_confirmed отвергает даже при
    дыре в loop-гейте. Используем заведомо несуществующую операцию-плейсхолдер."""
    from ads.service import execute_confirmed

    cp = SimpleNamespace(
        operation="totally_unsupported_op_xyz",
        status="confirmed",
        params={"campaign": "X"},
    )

    class _S:
        async def get_confirmed(self, cid):
            return cp

    try:
        await execute_confirmed(_S(), "cid")
        raise AssertionError("ожидался PermissionError (операция не поддержана)")
    except PermissionError:
        pass


# ── Store roundtrip: save → confirm → finalize пишет audit [confirmed]→[applied] ─
async def test_store_roundtrip_writes_audit():
    from confirm.store import ConfirmStore
    from db.models import AuditLog
    from db.session import Session, init_db

    await init_db()
    store = ConfirmStore()
    cid = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid,
        operation="add_keywords",
        customer_id=DRAFT_ACCOUNT_ID,
        params={"campaign": "X", "keywords": ["цветы"], "match_type": "broad"},
        summary="add_keywords X: +1 ключ",
        chat_id=1,
        user_initiated=True,
    )
    assert (await store.get_confirmed(cid)).status == "pending"

    assert await store.confirm(cid, chat_id=1) is True
    assert (await store.get_confirmed(cid)).status == "confirmed"
    assert await store.confirm(cid, chat_id=1) is False  # одноразово

    # claim (как apply_* перед SDK): confirmed → executing, АТОМАРНО и ОДНОРАЗОВО.
    snap = await store.claim(cid, operation="add_keywords")
    assert snap is not None and snap.status == "executing"
    assert await store.claim(cid, operation="add_keywords") is None  # повтор заблокирован (replay)
    assert (await store.get_confirmed(cid)).status == "executing"

    await store.finalize(cid, result={"applied": True, "count": 1})
    assert (await store.get_confirmed(cid)).status == "applied"  # терминальный статус

    async with Session() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(AuditLog.confirmation_id == cid).order_by(AuditLog.id)
                )
            )
            .scalars()
            .all()
        )
    statuses = [r.status for r in rows]
    assert "confirmed" in statuses and "applied" in statuses
    assert all(r.customer_id == DRAFT_ACCOUNT_ID for r in rows)


# ── W2-хардненинг: finalize() на не-executing черновике не плодит applied-строку ──
async def test_finalize_on_failed_proposal_is_noop_no_audit():
    """Хардненинг finalize() (зеркало record_failure): если черновик уже failed (упал в исполнении),
    finalize НЕ пишет спурьёзную applied-строку в журнал и НЕ понижает терминальный статус."""
    from confirm.store import ConfirmStore
    from db.models import AuditLog
    from db.session import Session, init_db

    await init_db()
    store = ConfirmStore()
    cid = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid,
        operation="pause_campaign",
        customer_id=DRAFT_ACCOUNT_ID,
        params={"campaign": "X"},
        summary="pause X",
        chat_id=1,
        user_initiated=True,
    )
    await store.confirm(cid, chat_id=1)
    await store.claim(cid, operation="pause_campaign")  # confirmed → executing
    await store.record_failure(cid, error="boom")  # executing → failed (+audit failed)
    assert (await store.get_confirmed(cid)).status == "failed"

    await store.finalize(cid, result={"applied": True})  # не-executing → no-op
    assert (await store.get_confirmed(cid)).status == "failed"  # терминальный не понижен

    async with Session() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(AuditLog.confirmation_id == cid).order_by(AuditLog.id)
                )
            )
            .scalars()
            .all()
        )
    statuses = [r.status for r in rows]
    assert "applied" not in statuses  # спурьёзной applied-строки нет
    assert "failed" in statuses


# ── FIX 1: replay/double-spend заблокирован на реальном сторе (claim одноразов) ───
async def test_real_store_apply_is_single_use_replay_blocked():
    from confirm.store import ConfirmStore
    from db.session import init_db

    await init_db()
    store = ConfirmStore()
    cid = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid,
        operation="resume_campaign",
        customer_id=DRAFT_ACCOUNT_ID,
        params={"campaign": "X"},
        summary="resume X",
        chat_id=1,
        user_initiated=True,
    )
    assert await store.confirm(cid, chat_id=1) is True

    calls = {"n": 0}

    def fake(client, customer_id, campaign_id, status):
        calls["n"] += 1
        return {"applied": True, "status": getattr(status, "name", status)}

    with patched(mut, "_set_campaign_status_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res1 = await mut.apply_resume_campaign(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="5",
            confirmation_id=cid,
            confirm_store=store,
            ads_client=_FakeClient(),
        )
        assert res1["applied"] is True
        # Повтор с тем же confirmation_id — claim вернёт None → PermissionError, SDK НЕ вызван.
        try:
            await mut.apply_resume_campaign(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="5",
                confirmation_id=cid,
                confirm_store=store,
                ads_client=_FakeClient(),
            )
            raise AssertionError("повторное выполнение должно быть заблокировано (replay)")
        except PermissionError:
            pass
    assert calls["n"] == 1  # SDK-исполнитель вызван РОВНО один раз (нет double-spend)
    assert (await store.get_confirmed(cid)).status == "applied"  # терминальный статус


# ── record_failure: статус и audit согласованы; терминальный applied не понижается ─
async def test_record_failure_terminalizes_confirmed_but_not_applied():
    from confirm.store import ConfirmStore
    from db.session import init_db

    await init_db()
    store = ConfirmStore()

    # (1) ошибка ДО claim (резолв имени): confirmed → failed (статус совпал с audit).
    cid = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid,
        operation="update_budget",
        customer_id=DRAFT_ACCOUNT_ID,
        params={"campaign": "X", "mode": "set_to", "value": 10},
        summary="b",
        chat_id=1,
        user_initiated=True,
    )
    await store.confirm(cid, chat_id=1)
    await store.record_failure(cid, error="resolve failed")
    assert (await store.get_confirmed(cid)).status == "failed"

    # (2) уже применённый (applied) НЕ понижается поздней записью ошибки.
    cid2 = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid2,
        operation="resume_campaign",
        customer_id=DRAFT_ACCOUNT_ID,
        params={"campaign": "X"},
        summary="r",
        chat_id=1,
        user_initiated=True,
    )
    await store.confirm(cid2, chat_id=1)
    await store.claim(cid2, operation="resume_campaign")
    await store.finalize(cid2, result={"applied": True})
    assert (await store.get_confirmed(cid2)).status == "applied"
    await store.record_failure(cid2, error="late error")
    assert (await store.get_confirmed(cid2)).status == "applied"  # терминальный не понижен


async def test_record_failure_redacts_secret_in_audit():
    """Авторитетная редакция на границе БД (golden rule #5): секрет в тексте ошибки НЕ попадает в
    audit_log. Прежний тест подавал ЧИСТУЮ строку — удаление redact_text на этой границе его бы
    не уронило (находка аудита: ветка редакции не покрыта)."""
    from sqlalchemy import select

    from confirm.store import ConfirmStore
    from core.logging import REDACTED
    from db.models import AuditLog
    from db.session import Session, init_db

    await init_db()
    store = ConfirmStore()
    cid = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid,
        operation="update_budget",
        customer_id=DRAFT_ACCOUNT_ID,
        params={"campaign": "X", "mode": "set_to", "value": 10},
        summary="b",
        chat_id=1,
        user_initiated=True,
    )
    await store.confirm(cid, chat_id=1)
    secret = "1//0SECRETrefreshTOKENvalue123"  # gitleaks:allow — форма refresh-токена
    await store.record_failure(cid, error=f"auth failed refresh_token={secret} denied")

    async with Session() as s:
        row = (
            await s.execute(
                select(AuditLog).where(AuditLog.confirmation_id == cid, AuditLog.status == "failed")
            )
        ).scalar_one()
    blob = str(row.result)
    assert secret not in blob  # секрет вычищен на границе audit_log
    assert REDACTED in blob


# ── FIX 1: confirmation_id одной операции нельзя «переиграть» в другую (wrong-op) ─
async def test_apply_rejects_wrong_operation_confirmation():
    store = FakeStore(FakeProposal("add_keywords", "confirmed", user_initiated=True))
    with allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await mut.apply_update_bid(  # confirmation_id подтверждён для add_keywords, не bid
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="7",
                bids=[("42", 1_500_000)],
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
            )
            raise AssertionError("ожидался PermissionError (операция не совпадает)")
        except PermissionError:
            pass
    assert store.finalized is False


# ── FIX 2: user_initiated по умолчанию False (fail-closed), деньги — заблокированы ─
async def test_save_proposal_defaults_user_initiated_false():
    from confirm.store import ConfirmStore
    from db.session import init_db

    await init_db()
    store = ConfirmStore()
    cid = uuid.uuid4().hex
    await store.save_proposal(  # БЕЗ user_initiated — должен лечь False
        confirmation_id=cid,
        operation="update_budget",
        customer_id=DRAFT_ACCOUNT_ID,
        params={"campaign": "X", "mode": "set_to", "value": 10},
        summary="budget X",
        chat_id=1,
    )
    snap = await store.get_confirmed(cid)
    assert snap.user_initiated is False


async def test_budget_blocked_when_default_user_initiated():
    # Полный путь: proposal без user_initiated (default False) → бюджет заблокирован гейтом.
    store = FakeStore(FakeProposal("update_budget", "confirmed", user_initiated=False))
    with allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await mut.apply_update_budget(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="1",
                new_budget_micros=50_000_000,
                confirmation_id="x",
                confirm_store=store,
                ads_client=object(),
            )
            raise AssertionError("ожидался PermissionError (бюджет не по команде)")
        except PermissionError:
            pass
    assert store.finalized is False


# ── FIX 6: абсолютный потолок суммы у границы SDK (defense-in-depth поверх схемы) ─
async def test_apply_update_budget_rejects_absurd_amount():
    store = FakeStore(FakeProposal("update_budget", "confirmed", user_initiated=True))
    with allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await mut.apply_update_budget(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="1",
                new_budget_micros=mut.MAX_AMOUNT_MICROS + 1,
                confirmation_id="ok",
                confirm_store=store,
                ads_client=object(),
            )
            raise AssertionError("ожидался ValueError (сумма за потолком)")
        except ValueError:
            pass
    assert store.finalized is False


# ── П1: общий бюджет — fail-closed гард в _apply_budget_via_sdk ───────────────────
# Меняя CampaignBudget, мы меняем его для ВСЕХ привязанных кампаний. Если к бюджету привязана
# другая (неудалённая) кампания, а её общий scope НЕ раскрыт (disclosed_shared_scope=False) —
# отказ ДО мутации. Гард читает живой аккаунт (TOCTOU-safe), не флаг из черновика.
class _ReachedBudgetMutate(Exception):
    """Сигнал: гард пройден и код дошёл до CampaignBudgetService (реальной мутации)."""


class _BudgetGuardClient:
    """Фейк-клиент для _apply_budget_via_sdk: GoogleAdsService.search маршрутизирует по запросу
    (resolve budget_rn vs campaigns_sharing_budget), любой другой сервис = «дошли до мутации»."""

    def __init__(self, budget_rn: str, linked):
        self._budget_rn = budget_rn
        self._linked = linked  # [(id, name), ...] — кампании на этом бюджете

    def get_service(self, name):
        if name == "GoogleAdsService":
            client = self

            class _GA:
                def search(self, *, customer_id, query):
                    if "campaign.campaign_budget =" in query:  # campaigns_sharing_budget
                        return [
                            SimpleNamespace(campaign=SimpleNamespace(id=int(i), name=n))
                            for i, n in client._linked
                        ]
                    # resolve budget_rn по campaign.id
                    return [
                        SimpleNamespace(campaign=SimpleNamespace(campaign_budget=client._budget_rn))
                    ]

            return _GA()
        raise _ReachedBudgetMutate(name)


def test_shared_budget_blocked_without_disclosure():
    # К бюджету привязана ДРУГАЯ кампания (id=2), scope не раскрыт → PermissionError ДО мутации.
    client = _BudgetGuardClient("customers/1/campaignBudgets/9", linked=[("1", "A"), ("2", "B")])
    with allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            mut._apply_budget_via_sdk(client, DRAFT_ACCOUNT_ID, "1", 50_000_000, False)
            raise AssertionError("ожидался PermissionError (общий бюджет без раскрытия)")
        except PermissionError:
            pass  # гард сработал, до CampaignBudgetService не дошли


def test_shared_budget_allowed_when_disclosed():
    # Тот же общий бюджет, но scope раскрыт (disclosed=True) → гард пропускает к мутации.
    client = _BudgetGuardClient("customers/1/campaignBudgets/9", linked=[("1", "A"), ("2", "B")])
    with allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            mut._apply_budget_via_sdk(client, DRAFT_ACCOUNT_ID, "1", 50_000_000, True)
            raise AssertionError("ожидался _ReachedBudgetMutate (гард пройден)")
        except _ReachedBudgetMutate:
            pass


def test_solo_budget_allowed_without_disclosure():
    # Бюджет только у этой кампании (нет «соседей») → гард не срабатывает даже без раскрытия.
    client = _BudgetGuardClient("customers/1/campaignBudgets/9", linked=[("1", "A")])
    with allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            mut._apply_budget_via_sdk(client, DRAFT_ACCOUNT_ID, "1", 50_000_000, False)
            raise AssertionError("ожидался _ReachedBudgetMutate (гард пройден)")
        except _ReachedBudgetMutate:
            pass


# ── pause_campaign: happy path (был вообще без теста) ────────────────────────────
async def test_apply_pause_campaign_happy_path():
    called = {}

    def fake(client, customer_id, campaign_id, status):
        called.update(customer_id=customer_id, campaign_id=campaign_id, status=status)
        return {"applied": True, "status": status}

    store = FakeStore(FakeProposal("pause_campaign", "confirmed", user_initiated=True))
    with patched(mut, "_set_campaign_status_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_pause_campaign(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="23",
            confirmation_id="ok",
            confirm_store=store,
            ads_client=_FakeClient(),
        )
    assert res["applied"] is True
    assert called["status"] == "PAUSED"  # pause → PAUSED
    assert store.finalized is True


# ── P1-6: удаление кампании/группы (необратимо; оба гейта + replay-one-shot) ──────
async def test_apply_remove_campaign_happy_path():
    called = {}

    def fake(client, customer_id, campaign_id):
        called.update(customer_id=customer_id, campaign_id=campaign_id)
        return {"customer_id": customer_id, "campaign_id": campaign_id, "removed": True}

    store = FakeStore(FakeProposal("remove_campaign", "confirmed", user_initiated=True))
    with patched(mut, "_remove_campaign_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_remove_campaign(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="23",
            confirmation_id="ok",
            confirm_store=store,
            ads_client=_FakeClient(),
        )
    assert res["removed"] is True and called["campaign_id"] == "23"
    assert store.finalized is True


async def test_apply_remove_campaign_replay_one_shot():
    calls = {"n": 0}

    def fake(client, customer_id, campaign_id):
        calls["n"] += 1
        return {"removed": True}

    store = FakeStore(FakeProposal("remove_campaign", "confirmed", user_initiated=True))
    with patched(mut, "_remove_campaign_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        await mut.apply_remove_campaign(
            customer_id=DRAFT_ACCOUNT_ID,
            campaign_id="23",
            confirmation_id="ok",
            confirm_store=store,
            ads_client=_FakeClient(),
        )
        try:
            await mut.apply_remove_campaign(
                customer_id=DRAFT_ACCOUNT_ID,
                campaign_id="23",
                confirmation_id="ok",
                confirm_store=store,
                ads_client=_FakeClient(),
            )
            raise AssertionError("replay удаления должен падать PermissionError")
        except PermissionError:
            pass
    assert calls["n"] == 1  # SDK-исполнитель вызван РОВНО один раз


async def test_apply_remove_ad_group_happy_path():
    called = {}

    def fake(client, customer_id, ad_group_id):
        called.update(customer_id=customer_id, ad_group_id=ad_group_id)
        return {"removed": True}

    store = FakeStore(FakeProposal("remove_ad_group", "confirmed", user_initiated=True))
    with patched(mut, "_remove_ad_group_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await mut.apply_remove_ad_group(
            customer_id=DRAFT_ACCOUNT_ID,
            ad_group_id="77",
            confirmation_id="ok",
            confirm_store=store,
            ads_client=_FakeClient(),
        )
    assert res["removed"] is True and called["ad_group_id"] == "77"
    assert store.finalized is True


# ── Негатив-матрица: чужой аккаунт / без подтверждения для ВСЕХ apply_* ───────────
def _apply_case(op):
    """(apply_fn, kwargs без customer_id/confirm_store) для каждой поддержанной операции."""
    base = {"confirmation_id": "ok", "ads_client": _FakeClient()}
    if op == "update_budget":
        return mut.apply_update_budget, {
            "campaign_id": "1",
            "new_budget_micros": 50_000_000,
            **base,
        }
    if op == "update_bid":
        return mut.apply_update_bid, {"campaign_id": "7", "bids": [("42", 1_500_000)], **base}
    if op == "update_keyword_bid":  # Ф1: (ad_group_id, criterion_id, micros)
        return mut.apply_update_keyword_bid, {
            "campaign_id": "7",
            "bids": [("42", "9001", 1_500_000)],
            **base,
        }
    if op == "add_keywords":
        return mut.apply_add_keywords, {
            "ad_group_ids": ["1"],
            "keywords": ["цветы"],
            "match_type": "broad",
            **base,
        }
    if op == "add_negative_keywords":
        return mut.apply_add_negative_keywords, {
            "campaign_id": "7",
            "keywords": ["бесплатно"],
            "match_type": "broad",
            **base,
        }
    if op == "remove_negative_keywords":
        return mut.apply_remove_negative_keywords, {
            "campaign_id": "7",
            "keywords": ["бесплатно"],
            "match_type": "broad",
            **base,
        }
    if op == "add_negatives_to_shared_set":
        return mut.apply_add_negatives_to_shared_set, {
            "shared_set_name": "Общие минуса",
            "shared_set_id": "77",
            "keywords": ["бесплатно"],
            "match_type": "broad",
            **base,
        }
    if op == "attach_shared_set":
        return mut.apply_attach_shared_set, {
            "campaign_id": "7",
            "shared_set_id": "77",
            **base,
        }
    if op == "detach_audience":
        return mut.apply_detach_audience, {
            "campaign_id": "7",
            "audience_resource_names": ["customers/7753643025/userLists/999"],
            **base,
        }
    if op == "resume_campaign":
        return mut.apply_resume_campaign, {"campaign_id": "7", **base}
    if op == "launch_campaign":
        return mut.apply_launch_campaign, {"campaign_id": "7", **base}
    if op == "pause_campaign":
        return mut.apply_pause_campaign, {"campaign_id": "7", **base}
    if op == "update_campaign":
        return mut.apply_update_campaign, {"campaign_id": "7", "new_name": "Новое имя", **base}
    if op == "set_campaign_display_network":
        return mut.apply_set_campaign_display_network, {
            "campaign_id": "7",
            "display_network": False,
            **base,
        }
    if op == "set_campaign_geo_target_type":
        return mut.apply_set_campaign_geo_target_type, {
            "campaign_id": "7",
            "geo_target_type": "PRESENCE",
            **base,
        }
    if op == "set_campaign_network":
        return mut.apply_set_campaign_network, {
            "campaign_id": "7",
            "search_partners": False,
            **base,
        }
    if op == "pause_ad_group":
        return mut.apply_pause_ad_group, {"ad_group_id": "77", **base}
    if op == "resume_ad_group":
        return mut.apply_resume_ad_group, {"ad_group_id": "77", **base}
    if op == "pause_ad":
        return mut.apply_pause_ad, {"ad_group_id": "77", "ad_id": "101", **base}
    if op == "resume_ad":
        return mut.apply_resume_ad, {"ad_group_id": "77", "ad_id": "101", **base}
    if op == "remove_ad":
        return mut.apply_remove_ad, {"ad_group_id": "77", "ad_id": "101", **base}
    if op == "remove_campaign":
        return mut.apply_remove_campaign, {"campaign_id": "7", **base}
    if op == "remove_ad_group":
        return mut.apply_remove_ad_group, {"ad_group_id": "77", **base}
    if op == "set_geo_location":
        return mut.apply_set_geo_location, {
            "campaign_id": "7",
            "locations": ["Украина"],
            "country_code": "UA",
            **base,
        }
    if op == "set_bidding_strategy":
        return mut.apply_set_bidding_strategy, {
            "campaign_id": "7",
            "strategy": "manual_cpc",
            **base,
        }
    # ── §аудит-2026-07: недостающие 16 операций — та же негативная матрица для ВСЕХ 29 ──
    if op == "set_geo_proximity":
        return mut.apply_set_geo_proximity, {
            "campaign_id": "7",
            "radius_km": 20.0,
            "address": {"city_name": "Kyiv", "country_code": "UA"},
            **base,
        }
    if op == "attach_audience":
        return mut.apply_attach_audience, {
            "campaign_id": "7",
            "audience_resource_names": ["customers/7753643025/userLists/999"],
            **base,
        }
    if op == "add_sitelinks":
        return mut.apply_add_sitelinks, {
            "campaign_id": "7",
            "sitelinks": [{"link_text": "Каталог", "final_url": "https://x.example/c"}],
            **base,
        }
    if op == "add_callouts":
        return mut.apply_add_callouts, {
            "campaign_id": "7",
            "callouts": ["Гарантия 12 мес", "Trade-in"],
            **base,
        }
    if op == "add_structured_snippets":
        return mut.apply_add_structured_snippets, {
            "campaign_id": "7",
            "header": "Models",
            "values": ["Sedan", "SUV", "Hatchback"],
            **base,
        }
    if op == "attach_image_asset":
        return mut.apply_attach_image_asset, {
            "campaign_id": "7",
            "image_bytes": b"\x89PNG-fake",
            "name": "img",
            **base,
        }
    if op == "add_call_asset":
        return mut.apply_add_call_asset, {
            "campaign_id": "7",
            "phone_number": "+380 12 345 6789",
            "country_code": "UA",
            **base,
        }
    if op == "add_promotion":
        return mut.apply_add_promotion, {
            "campaign_id": "7",
            "promotion_target": "Лето",
            "final_url": "https://x.example/promo",
            "percent_off": 20.0,
            **base,
        }
    if op == "add_price_asset":
        return mut.apply_add_price_asset, {
            "campaign_id": "7",
            "price_type": "SERVICES",
            "currency": "USD",
            "language_code": "uk",
            "offerings": [
                {
                    "header": f"Тариф {i}",
                    "description": "Описание",
                    "price_units": 9.99,
                    "final_url": "https://x.example/p",
                }
                for i in range(3)
            ],
            **base,
        }
    if op == "remove_asset_link":
        return mut.apply_remove_asset_link, {
            "link_resource_names": ["customers/7753643025/campaignAssets/1~2~SITELINK"],
            **base,
        }
    if op == "create_rsa":
        return mut.apply_create_rsa, {
            "ad_group_id": "77",
            "headlines": ["Заголовок раз", "Заголовок два", "Заголовок три"],
            "descriptions": ["Описание первое.", "Описание второе."],
            "final_url": "https://x.example/",
            **base,
        }
    if op == "remove_keywords":
        return mut.apply_remove_keywords, {
            "ad_group_ids": ["77"],
            "keywords": ["used cars"],
            "match_type": "phrase",
            **base,
        }
    if op == "create_search_campaign":
        return mut.apply_create_search_campaign, {
            "campaign_name": "Тест",
            "final_url": "https://x.example/",
            "headlines": ["Заголовок раз", "Заголовок два", "Заголовок три"],
            "descriptions": ["Описание первое.", "Описание второе."],
            "budget_daily_micros": 10_000_000,
            **base,
        }
    if op == "create_gdn_campaign":
        return mut.apply_create_gdn_campaign, {
            "campaign_name": "Тест GDN",
            "landscape_bytes": b"img-l",
            "square_bytes": b"img-s",
            "headlines": ["Заголовок"],
            "long_headline": "Длинный заголовок объявления",
            "descriptions": ["Описание."],
            "business_name": "Бренд",
            "final_url": "https://x.example/",
            "budget_daily_micros": 10_000_000,
            **base,
        }
    if op == "create_demand_gen_campaign":
        return mut.apply_create_demand_gen_campaign, {
            "campaign_name": "Тест DG",
            "youtube_video_id": "dQw4w9WgXcQ",
            "headlines": ["Заголовок"],
            "long_headline": "Длинный заголовок объявления",
            "descriptions": ["Описание."],
            "business_name": "Бренд",
            "final_url": "https://x.example/",
            "budget_daily_micros": 10_000_000,
            **base,
        }
    if op == "create_video_campaign":
        return mut.apply_create_video_campaign, {
            "campaign_name": "Тест Video",
            "youtube_video_id": "dQw4w9WgXcQ",
            "headlines": ["Заголовок"],
            "long_headline": "Длинный заголовок объявления",
            "descriptions": ["Описание."],
            "business_name": "Бренд",
            "final_url": "https://x.example/",
            "budget_daily_micros": 10_000_000,
            **base,
        }
    raise AssertionError(op)


def _all_ops() -> list[str]:
    """ВСЕ поддержанные операции (ads.service.SUPPORTED_OPERATIONS) — негативная матрица не может
    молча отстать при добавлении новой операции: незнакомый op уронит _apply_case AssertionError."""
    from ads.service import SUPPORTED_OPERATIONS

    return sorted(SUPPORTED_OPERATIONS)


_ALL_OPS = _all_ops()


async def test_all_apply_reject_foreign_account():
    """29/29: КАЖДАЯ поддержанная операция отвергает чужой аккаунт (замок ensure_allowed) ДО
    какого-либо SDK/finalize."""
    for op in _ALL_OPS:
        fn, kw = _apply_case(op)
        store = FakeStore(FakeProposal(op, "confirmed", user_initiated=True))
        with allowed_ids(DRAFT_ACCOUNT_ID):
            try:
                await fn(customer_id="1234567890", confirm_store=store, **kw)
                raise AssertionError(f"{op}: чужой аккаунт должен падать PermissionError")
            except PermissionError:
                pass
        assert store.finalized is False, op


async def test_all_apply_reject_without_confirmation():
    """29/29: КАЖДАЯ поддержанная операция без подтверждённого черновика (claim=None) —
    PermissionError, finalize не вызван (golden rule 2)."""
    for op in _ALL_OPS:
        fn, kw = _apply_case(op)
        store = FakeStore(proposal=None)  # нет подтверждённого черновика
        with allowed_ids(DRAFT_ACCOUNT_ID):
            try:
                await fn(customer_id=DRAFT_ACCOUNT_ID, confirm_store=store, **kw)
                raise AssertionError(f"{op}: без confirmation должен падать PermissionError")
            except PermissionError:
                pass
        assert store.finalized is False, op


# ── FIX: account-lock на уровне РЕЗОЛВЕРОВ (find_campaign_by_name / find_ad_groups) ─
def test_resolvers_reject_foreign_account():
    from ads.resolve import find_ad_groups, find_campaign_by_name, find_shared_set_by_name

    with allowed_ids(DRAFT_ACCOUNT_ID):
        # 3.2б-2: find_shared_set_by_name — тот же замок ДО SDK (живёт на пути исполнения мутаций)
        for fn in (find_campaign_by_name, find_ad_groups, find_shared_set_by_name):
            try:
                fn(object(), "1234567890", "X")  # ensure_allowed до любого обращения к SDK
                raise AssertionError(f"{fn.__name__}: чужой аккаунт должен падать")
            except PermissionError:
                pass
        from ads.resolve import find_ads_in_group, find_keywords

        try:  # C6: резолвер объявлений — тот же замок ДО SDK
            find_ads_in_group(object(), "1234567890", "X", "G", "101")
            raise AssertionError("find_ads_in_group: чужой аккаунт должен падать")
        except PermissionError:
            pass

        try:  # Ф1: резолвер ключей (ставка на уровне ключа) — тот же замок ДО SDK
            find_keywords(object(), "1234567890", "X", "ремонт окон")
            raise AssertionError("find_keywords: чужой аккаунт должен падать")
        except PermissionError:
            pass


# ── C6: find_ads_in_group — id-точное совпадение / подстрока заголовка / все объявления ──
def _ads_rows():
    def _row(ad_id, headline, status="ENABLED"):
        return SimpleNamespace(
            ad_group=SimpleNamespace(id=77),
            ad_group_ad=SimpleNamespace(
                status=SimpleNamespace(name=status),
                ad=SimpleNamespace(
                    id=ad_id,
                    responsive_search_ad=SimpleNamespace(
                        headlines=[SimpleNamespace(text=headline)]
                    ),
                ),
            ),
        )

    return [_row(101, "Доставка цветов"), _row(102, "Букеты недорого", "PAUSED")]


class _AdsGA:
    def search(self, customer_id, query):
        assert "!= 'REMOVED'" in query  # удалённые не воскрешаем и не показываем
        return _ads_rows()


class _AdsClient:
    def get_service(self, name):
        return _AdsGA()


def test_find_ads_in_group_by_id_headline_and_all():
    from ads.resolve import find_ads_in_group

    with allowed_ids(DRAFT_ACCOUNT_ID):
        by_id = find_ads_in_group(_AdsClient(), DRAFT_ACCOUNT_ID, "C", "G", "102")
        assert [a.ad_id for a in by_id] == ["102"] and by_id[0].status == "PAUSED"
        by_head = find_ads_in_group(_AdsClient(), DRAFT_ACCOUNT_ID, "C", "G", "цветов")
        assert [a.ad_id for a in by_head] == ["101"] and by_head[0].headline == "Доставка цветов"
        all_ads = find_ads_in_group(_AdsClient(), DRAFT_ACCOUNT_ID, "C", "G", "")
        assert [a.ad_id for a in all_ads] == ["101", "102"]  # пустой needle = весь список
        none = find_ads_in_group(_AdsClient(), DRAFT_ACCOUNT_ID, "C", "G", "нетакого")
        assert none == []


# ── find_ad_group_by_name: выбор группы по имени внутри кампании (pause/resume группы) ──
def test_find_ad_group_by_name_filters_case_insensitive():
    """Резолвер выбирает группу по имени (регистронезависимо точно) из find_ad_groups; нет
    совпадения / пустое имя → None (вызывающий отвергнет ДО любой записи)."""
    from ads import resolve

    groups = [
        resolve.AdGroupRef("1", "rn1", "Brand", "ENABLED", 0, "9"),
        resolve.AdGroupRef("2", "rn2", "Generic", "PAUSED", 0, "9"),
    ]
    with patched(resolve, "find_ad_groups", lambda c, cid, camp: groups):
        got = resolve.find_ad_group_by_name(object(), DRAFT_ACCOUNT_ID, "Camp", "generic")
        assert got is not None and got.id == "2" and got.status == "PAUSED"
        assert resolve.find_ad_group_by_name(object(), DRAFT_ACCOUNT_ID, "Camp", "missing") is None
        assert resolve.find_ad_group_by_name(object(), DRAFT_ACCOUNT_ID, "Camp", "  ") is None


# ── FIX 3: ensure_manager_allowed — обход MCC только настроенного менеджера ───────
def test_ensure_manager_allowed():
    from ads.client import ensure_manager_allowed

    prev = settings.google_ads_login_customer_id
    try:
        settings.google_ads_login_customer_id = ""  # не задан → fail-closed
        try:
            ensure_manager_allowed("123")
            raise AssertionError("ожидался PermissionError (login_customer_id пуст)")
        except PermissionError:
            pass

        settings.google_ads_login_customer_id = "9998887777"
        ensure_manager_allowed("999-888-7777")  # нормализация → совпало → ок
        try:
            ensure_manager_allowed("1112223333")
            raise AssertionError("ожидался PermissionError (чужой менеджер)")
        except PermissionError:
            pass
    finally:
        settings.google_ads_login_customer_id = prev


# ── execute_confirmed: fail-closed на None и статус != confirmed (defense-in-depth) ─
async def test_execute_confirmed_rejects_unconfirmed_and_missing():
    from ads.service import execute_confirmed

    class _S:
        def __init__(self, p):
            self._p = p

        async def get_confirmed(self, cid):
            return self._p

    # status=pending → PermissionError
    pending = SimpleNamespace(operation="update_budget", status="pending", params={})
    try:
        await execute_confirmed(_S(pending), "cid")
        raise AssertionError("ожидался PermissionError (не confirmed)")
    except PermissionError:
        pass

    # None → ValueError
    try:
        await execute_confirmed(_S(None), "cid")
        raise AssertionError("ожидался ValueError (черновик не найден)")
    except ValueError:
        pass
