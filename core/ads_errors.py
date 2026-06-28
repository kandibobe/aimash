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
