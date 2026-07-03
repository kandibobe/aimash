"""Человекочитаемое сообщение из GoogleAdsException (ТЗ §15: понятные уведомления об ошибках).

Извлекает текст ошибок Google Ads (failure.errors[].message) + код ошибки (по .name —
версионно-безопасно, как core.resilience._ads_error_names) + request_id для саппорта. Результат
редактируется (golden rule #5: str ошибки SDK может нести креды). Используется на путях, отдающих
ошибку пользователю (вместо «GoogleAdsException: <сырой технический текст>»).

Дакт-тайпинг: работает и с реальным protobuf-GoogleAdsException, и с тест-фейком (error_code.name).
"""

from __future__ import annotations

from core.logging import redact_text


def error_code_name(code: object) -> str:
    """Имя enum-кода ОДНОЙ ошибки. Реальный protobuf — через WhichOneof('error_code'); тест-фейк —
    .name. ЕДИНЫЙ источник извлечения имени кода (его же зовут core.resilience.error_code_names и
    проверка bid-mismatch в ads.mutations) — раньше логика была размножена в трёх местах."""
    if code is None:
        return ""
    which = getattr(code, "WhichOneof", None)  # реальный protobuf-oneof
    if callable(which):
        field = which("error_code")
        if field:
            return str(getattr(getattr(code, field, None), "name", "") or "")
        return ""
    return str(getattr(code, "name", "") or "")  # дакт-фейк в тестах


def error_code_names(exc: object) -> set[str]:
    """Множество имён enum-кодов из GoogleAdsException (реальный protobuf или тест-дакт-фейк).
    Единый источник для классификации ретраев (core.resilience) и любой логики по коду ошибки."""
    failure = getattr(exc, "failure", None)
    names: set[str] = set()
    for err in getattr(failure, "errors", None) or []:
        nm = error_code_name(getattr(err, "error_code", None))
        if nm:
            names.add(nm)
    return names


# Обратная совместимость: внутреннее имя сохранено как алиас (на него мог ссылаться код/тесты).
_error_code_name = error_code_name


# Коды/статусы «аккаунт недоступен на чтение» — ОЖИДАЕМОЕ состояние (деактивирован / нет прав /
# не рекламный), а не дефект бота. На плановых путях (scheduler) их логируем info, а НЕ как error
# (иначе /diag и Sentry тонут в повторах каждый цикл); в UI показываем причину «аккаунт отключён»,
# а не подсказку про Sheets-scope (§15).
_ACCESS_ERROR_CODES: frozenset[str] = frozenset(
    {
        "CUSTOMER_NOT_ENABLED",  # AuthorizationError: аккаунт не активирован/деактивирован
        "USER_PERMISSION_DENIED",  # AuthorizationError: нет прав у вызывающего
        "NOT_ADS_USER",  # AuthorizationError: у пользователя нет доступа к Google Ads
        "CUSTOMER_NOT_FOUND",  # HeaderError/AuthorizationError
        "MISSING_TOS",  # не приняты условия использования
    }
)


def _grpc_status_name(exc: object) -> str:
    """Имя gRPC-статуса (напр. 'PERMISSION_DENIED') из _InactiveRpcError. GoogleAdsException хранит
    исходный RpcError на .error; у него (или у самого exc) есть .code() → StatusCode. '' если нет."""
    for obj in (exc, getattr(exc, "error", None)):
        code = getattr(obj, "code", None)
        if callable(code):
            try:
                nm = getattr(code(), "name", None)
            except Exception:  # noqa: BLE001 — дакт-фейк без вызываемого code()
                nm = None
            if nm:
                return str(nm)
    return ""


def is_account_access_error(exc: BaseException) -> bool:
    """True, если ошибка означает «аккаунт недоступен на чтение» (деактивирован / нет прав / не
    рекламный). Ловит и gRPC-уровень PERMISSION_DENIED (_InactiveRpcError.code()), и коды
    GoogleAdsFailure (CUSTOMER_NOT_ENABLED и т.п.), и текстовый fallback. Дакт-безопасно для тестов.

    Используется: scheduler (не журналить ожидаемый отказ каждый цикл) и Sheets/xlsx-экспорт
    (честная причина «аккаунт отключён», а не подсказка про Sheets-scope)."""
    if error_code_names(exc) & _ACCESS_ERROR_CODES:
        return True
    if _grpc_status_name(exc) == "PERMISSION_DENIED":
        return True
    msg = str(getattr(exc, "message", "") or exc).lower()
    return "not yet enabled" in msg or "has been deactivated" in msg


def partial_failure_errors(client: object, response: object) -> list[tuple[int, str, str]]:
    """[(op_index, code_name, message)] из response.partial_failure_error (google.rpc.Status).

    Официальный паттерн Google: при partial_failure=True сервер кладёт отказ в
    partial_failure_error.details[] (Any → GoogleAdsFailure.deserialize), а индекс отклонённой
    операции батча — err.location.field_path_elements[0].index. Нет partial_failure
    (status пуст / code==0) ⇒ []. Дакт-безопасно: тест-фейк может нести errors прямо на detail."""
    status = getattr(response, "partial_failure_error", None)
    if status is None or not getattr(status, "code", 0):
        return []
    out: list[tuple[int, str, str]] = []
    for detail in getattr(status, "details", None) or []:
        try:
            failure = client.get_type("GoogleAdsFailure").deserialize(detail.value)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — тест-фейк: detail сам несёт errors
            failure = detail if hasattr(detail, "errors") else None
        for err in getattr(failure, "errors", None) or []:
            idx = -1
            elems = list(getattr(getattr(err, "location", None), "field_path_elements", None) or [])
            if elems:
                try:
                    idx = int(getattr(elems[0], "index", -1))
                except (TypeError, ValueError):
                    idx = -1
            out.append(
                (
                    idx,
                    error_code_name(getattr(err, "error_code", None)),
                    str(getattr(err, "message", "") or ""),
                )
            )
    return out


def _request_id(exc: object) -> str:
    rid = getattr(exc, "request_id", None)
    return str(rid) if rid else ""


def humanize_google_ads_error(exc: BaseException, *, max_errors: int = 3) -> str:
    """Понятный текст ошибки Google Ads для показа пользователю (§15). Собирает до max_errors
    сообщений с кодами + request_id; редактирует секреты. Если структуры нет (не GoogleAdsException)
    — fallback «ТипОшибки: текст» (тоже редактированный). Без секретов, без сырого трейсбэка."""
    failure = getattr(exc, "failure", None)
    errors = list(getattr(failure, "errors", None) or [])
    parts: list[str] = []
    for err in errors[:max_errors]:
        msg = str(getattr(err, "message", "") or "").strip()
        code = error_code_name(getattr(err, "error_code", None))
        if msg and code:
            parts.append(f"{msg} [{code}]")
        elif msg or code:
            parts.append(msg or code)
    extra = len(errors) - max_errors
    if extra > 0:
        parts.append(f"…и ещё {extra}")
    text = "; ".join(parts) if parts else f"{type(exc).__name__}: {exc}"
    rid = _request_id(exc)
    if rid:
        text += f" (request_id: {rid})"
    return redact_text(text)
