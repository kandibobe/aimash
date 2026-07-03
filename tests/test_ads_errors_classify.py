"""A3/A4: is_account_access_error — классификатор «аккаунт недоступен на чтение».

Ловит gRPC-уровень PERMISSION_DENIED (_InactiveRpcError.code()), коды GoogleAdsFailure
(CUSTOMER_NOT_ENABLED и т.п.) и текстовый fallback. Дакт-фейки повторяют форму реальных ошибок.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.ads_errors import is_account_access_error  # noqa: E402


class _Code:
    def __init__(self, name: str) -> None:
        self.name = name


class _Err:
    def __init__(self, name: str) -> None:
        self.error_code = _Code(name)


class _Failure:
    def __init__(self, names: list[str]) -> None:
        self.errors = [_Err(n) for n in names]


class _AdsFailureExc(Exception):
    """GoogleAdsException-подобная со структурой failure.errors[].error_code.name."""

    def __init__(self, names: list[str]) -> None:
        super().__init__("ads failure")
        self.failure = _Failure(names)


class _RpcErr(Exception):
    def code(self):  # _InactiveRpcError.code() → grpc.StatusCode
        return _Code("PERMISSION_DENIED")


class _AdsGrpcExc(Exception):
    """GoogleAdsException-подобная без structured failure, но с RpcError на .error (gRPC-уровень)."""

    def __init__(self) -> None:
        super().__init__("The caller does not have permission")
        self.failure = None
        self.error = _RpcErr()


def test_customer_not_enabled_is_access_error():
    assert is_account_access_error(_AdsFailureExc(["CUSTOMER_NOT_ENABLED"]))


def test_user_permission_denied_code_is_access_error():
    assert is_account_access_error(_AdsFailureExc(["USER_PERMISSION_DENIED"]))


def test_grpc_permission_denied_is_access_error():
    assert is_account_access_error(_AdsGrpcExc())


def test_text_fallback_deactivated():
    assert is_account_access_error(Exception("account can't be accessed... has been deactivated"))
    assert is_account_access_error(Exception("account is not yet enabled"))


def test_benign_error_is_not_access_error():
    assert not is_account_access_error(ValueError("boom"))
    assert not is_account_access_error(_AdsFailureExc(["DUPLICATE_CAMPAIGN_NAME"]))
