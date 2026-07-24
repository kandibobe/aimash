"""Офлайн-тесты создания RSA-объявления (фаза 2.C): apply_create_rsa за двумя гейтами.

Без живого Google Ads — SDK-исполнитель (_create_rsa_via_sdk) подменяется monkeypatch'ем.
Проверяем: оба гейта (замок аккаунта + confirm), статус PAUSED, минимумы/максимумы и длину
(кириллица=1) считает КОД ДО вызова SDK, final_url обязателен, path2 без path1 запрещён,
capability-guard (create_rsa поддержан, rsa_curation — нет), маршрутизация execute_confirmed.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ads.mutations as mut  # noqa: E402
import ads.service as svc  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from conftest import FakeConfirmStore, FakeProposal  # noqa: E402
from core.config import settings  # noqa: E402

_H3 = ["Заголовок один", "Заголовок два", "Заголовок три"]
_D2 = ["Описание первое — кратко и по делу.", "Описание второе — выгода и призыв."]
_URL = "https://example.com/"


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


async def _fake_client_async(*a, **k):
    # execute_confirmed зовёт await build_client_async() (сборка SDK вне loop) — фейк-заглушка.
    return object()


async def _call(
    store,
    *,
    customer_id=DRAFT_ACCOUNT_ID,
    headlines=None,
    descriptions=None,
    final_url=_URL,
    path1=None,
    path2=None,
    cid="ok",
):
    return await mut.apply_create_rsa(
        customer_id=customer_id,
        ad_group_id="42",
        headlines=_H3 if headlines is None else headlines,
        descriptions=_D2 if descriptions is None else descriptions,
        final_url=final_url,
        path1=path1,
        path2=path2,
        confirmation_id=cid,
        confirm_store=store,
        ads_client=object(),
    )


# ── Happy path: оба гейта, SDK получает правильные аргументы, статус PAUSED ───────
async def test_apply_create_rsa_happy_path():
    called = {}

    def fake(client, customer_id, ad_group_id, headlines, descriptions, final_url, path1, path2):
        called.update(
            customer_id=customer_id,
            ad_group_id=ad_group_id,
            headlines=list(headlines),
            descriptions=list(descriptions),
            final_url=final_url,
        )
        return {"applied": True, "status": "PAUSED", "resource_name": "rn/1"}

    store = FakeConfirmStore(FakeProposal("create_rsa", "confirmed", user_initiated=True))
    with patched(mut, "_create_rsa_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        res = await _call(store)
    assert res["applied"] is True and res["status"] == "PAUSED"
    assert called["customer_id"] == DRAFT_ACCOUNT_ID and called["ad_group_id"] == "42"
    assert called["headlines"] == _H3 and called["descriptions"] == _D2
    assert store.finalized is True


async def test_apply_create_rsa_rejects_foreign_account():
    store = FakeConfirmStore(FakeProposal("create_rsa", "confirmed", user_initiated=True))
    with allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await _call(store, customer_id="1234567890")
            raise AssertionError("ожидался PermissionError (чужой аккаунт)")
        except PermissionError:
            pass
    assert store.finalized is False


async def test_apply_create_rsa_rejected_without_confirmation():
    store = FakeConfirmStore(proposal=None)
    with allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await _call(store, cid="bogus")
            raise AssertionError("ожидался PermissionError (нет confirmation)")
        except PermissionError:
            pass
    assert store.finalized is False


async def test_apply_create_rsa_rejects_wrong_operation():
    store = FakeConfirmStore(FakeProposal("add_keywords", "confirmed", user_initiated=True))
    with allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await _call(store)
            raise AssertionError("ожидался PermissionError (операция не совпадает)")
        except PermissionError:
            pass
    assert store.finalized is False


# ── Минимумы/максимумы и длину считает КОД ДО SDK (golden rule #4) ───────────────
async def test_below_min_headlines_blocked_before_sdk():
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return {"applied": True}

    store = FakeConfirmStore(FakeProposal("create_rsa", "confirmed", user_initiated=True))
    with patched(mut, "_create_rsa_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await _call(store, headlines=_H3[:2])  # 2 заголовка < 3
            raise AssertionError("ожидался ValueError (мало заголовков)")
        except ValueError:
            pass
    assert calls["n"] == 0 and store.finalized is False


async def test_below_min_descriptions_blocked_before_sdk():
    store = FakeConfirmStore(FakeProposal("create_rsa", "confirmed", user_initiated=True))
    with allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await _call(store, descriptions=_D2[:1])  # 1 описание < 2
            raise AssertionError("ожидался ValueError (мало описаний)")
        except ValueError:
            pass
    assert store.finalized is False


async def test_too_many_headlines_blocked():
    store = FakeConfirmStore(FakeProposal("create_rsa", "confirmed", user_initiated=True))
    with allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await _call(store, headlines=[f"H{i}" for i in range(16)])  # 16 > 15
            raise AssertionError("ожидался ValueError (>15 заголовков)")
        except ValueError:
            pass
    assert store.finalized is False


async def test_overlong_headline_blocked_before_sdk():
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return {"applied": True}

    store = FakeConfirmStore(FakeProposal("create_rsa", "confirmed", user_initiated=True))
    with patched(mut, "_create_rsa_via_sdk", fake), allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await _call(store, headlines=["я" * 31, "ok2", "ok3"])  # 31 кир. символ > 30
            raise AssertionError("ожидался ValueError (заголовок > 30)")
        except ValueError:
            pass
    assert calls["n"] == 0 and store.finalized is False


async def test_missing_or_bad_final_url_blocked():
    store = FakeConfirmStore(FakeProposal("create_rsa", "confirmed", user_initiated=True))
    with allowed_ids(DRAFT_ACCOUNT_ID):
        for bad in ("", "ftp://x", "example.com"):
            try:
                await _call(store, final_url=bad)
                raise AssertionError(f"ожидался ValueError (final_url={bad!r})")
            except ValueError:
                pass
    assert store.finalized is False


async def test_path2_without_path1_blocked():
    store = FakeConfirmStore(FakeProposal("create_rsa", "confirmed", user_initiated=True))
    with allowed_ids(DRAFT_ACCOUNT_ID):
        try:
            await _call(store, path1=None, path2="акция")
            raise AssertionError("ожидался ValueError (path2 без path1)")
        except ValueError:
            pass
    assert store.finalized is False


# ── Capability-guard / маршрутизация ─────────────────────────────────────────────
def test_create_rsa_in_supported_operations():
    assert "create_rsa" in svc.SUPPORTED_OPERATIONS
    assert "rsa_curation" not in svc.SUPPORTED_OPERATIONS  # сессия неисполнима


async def test_execute_confirmed_routes_create_rsa():
    captured = {}

    async def fake_apply(**kwargs):
        captured.update(kwargs)
        return {"applied": True}

    cp = SimpleNamespace(
        operation="create_rsa",
        status="confirmed",
        customer_id=DRAFT_ACCOUNT_ID,  # 2A: execute_confirmed берёт аккаунт из черновика
        params={
            "ad_group_id": "42",
            "campaign": "X",
            "final_url": _URL,
            "headlines": _H3,
            "descriptions": _D2,
            "path1": None,
            "path2": None,
        },
    )

    class _S:
        async def get_confirmed(self, cid):
            return cp

    with (
        patched(svc, "build_client_async", _fake_client_async),
        patched(mut, "apply_create_rsa", fake_apply),
        allowed_ids(DRAFT_ACCOUNT_ID),  # 2A: повторный ensure_allowed на исполнении
    ):
        res = await svc.execute_confirmed(_S(), "cid")
    assert res["applied"] is True
    assert captured["ad_group_id"] == "42" and captured["headlines"] == _H3
    assert captured["customer_id"] == DRAFT_ACCOUNT_ID


# ── B2: перевод «operation not allowed for the given context» → понятный ValueError ──
class _FakeErrorCode:
    """Дакт-эквивалент protobuf error_code с активным oneof (error_code_name читает через WhichOneof)."""

    def __init__(self, name):
        self.op = SimpleNamespace(name=name)

    def WhichOneof(self, _field):
        return "op"


class _FakeGAE(mut.GoogleAdsException):
    """GoogleAdsException-подобная (подкласс → ловится except GoogleAdsException) с duck-failure."""

    def __init__(self, code_name, msg="boom"):
        Exception.__init__(self, msg)
        self._msg = msg
        self.failure = SimpleNamespace(
            errors=[SimpleNamespace(error_code=_FakeErrorCode(code_name), message=msg)]
        )
        self.error = None
        self.request_id = "req"

    def __str__(self):
        return self._msg


def _rsa_sdk_client(raise_exc):
    """Минимальный фейк-клиент для _create_rsa_via_sdk, чей mutate поднимает raise_exc."""
    rsa = SimpleNamespace(headlines=[], descriptions=[], path1="", path2="")
    ad = SimpleNamespace(final_urls=[], responsive_search_ad=rsa)
    op = SimpleNamespace(create=SimpleNamespace(ad=ad, ad_group="", status=None))

    class _AGAd:
        def mutate_ad_group_ads(self, customer_id, operations):
            raise raise_exc

    class _AG:
        def ad_group_path(self, cid, agid):
            return f"customers/{cid}/adGroups/{agid}"

    class _Client:
        enums = SimpleNamespace(AdGroupAdStatusEnum=SimpleNamespace(PAUSED="PAUSED"))

        def get_service(self, name):
            return _AGAd() if name == "AdGroupAdService" else _AG()

        def get_type(self, name):
            return op if name == "AdGroupAdOperation" else SimpleNamespace(text="")

    return _Client()


def test_create_rsa_translates_context_error_to_value_error():
    exc = _FakeGAE(
        "OPERATION_NOT_PERMITTED_FOR_CONTEXT", "The operation is not allowed for the given context"
    )
    client = _rsa_sdk_client(exc)
    try:
        mut._create_rsa_via_sdk(client, DRAFT_ACCOUNT_ID, "42", _H3, _D2, _URL, None, None)
        raise AssertionError("ожидался ValueError (context error переведён)")
    except ValueError as e:
        assert "Search" in str(e) and "стандарт" in str(e).lower()


def test_create_rsa_reraises_unrelated_ads_exception():
    exc = _FakeGAE("DUPLICATE_AD", "some other error")
    client = _rsa_sdk_client(exc)
    try:
        mut._create_rsa_via_sdk(client, DRAFT_ACCOUNT_ID, "42", _H3, _D2, _URL, None, None)
        raise AssertionError("ожидался проброс GoogleAdsException (не context error)")
    except mut.GoogleAdsException:
        pass  # неизвестная ошибка НЕ маскируется под ValueError — пробрасывается как есть


async def test_execute_confirmed_rejects_rsa_curation_session():
    cp = SimpleNamespace(operation="rsa_curation", status="confirmed", params={})

    class _S:
        async def get_confirmed(self, cid):
            return cp

    try:
        await svc.execute_confirmed(_S(), "cid")
        raise AssertionError("ожидался PermissionError (rsa_curation неисполнима)")
    except PermissionError:
        pass
