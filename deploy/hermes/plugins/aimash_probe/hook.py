# ─────────────────────────────────────────────────────────────────────────────
# aimash_probe — ВРЕМЕННЫЙ диагностический хук Hermes. Ставится на время прогона
# V1–V19 (`deploy/hermes/OPERATIONS.md` §12) и УДАЛЯЕТСЯ после. Не часть продукта.
#
# ⚠️ КОНТРАКТ ХУКОВ v0.17 — [Непроверено]. Доками проекта подтверждены только ИМЕНА:
#    `pre_gateway_dispatch` (входящее сообщение; skip/rewrite/allow) и `pre_tool_call`
#    (block) — `HERMES_SPEC.md:1234`, `:229`, `:627`. Сигнатуры, форма возврата и
#    поведение gateway при исключении внутри хука НЕ проверены ни по докам, ни живьём;
#    ровно это и меряют V4/V7–V10. Поэтому здесь:
#      • обе функции принимают `*args, **kwargs` — неверная сигнатура не должна давать
#        TypeError, иначе замер V10 (fail-open/fail-closed) выродится в замер опечатки;
#      • по умолчанию хук ТОЛЬКО ЛОГИРУЕТ и возвращает None — самая безобидная форма
#        «не вмешиваться» из всех, что встречаются у подобных рантаймов;
#      • попытка перезаписи аргументов включается явно (`AIMASH_PROBE_REWRITE=1`) и
#        только ПОСЛЕ того, как V4 зафиксировал фактическую форму payload'а.
#
# ⚠️ Правило 5 применимо здесь буквально: хук пишет во внешний файл всё, что до него
#    доехало, а в payload'е gateway'я может оказаться токен бота или ключ модели. Своей
#    редакции у хука быть обязано — импортировать `core.logging` он не может (крутится в
#    процессе Hermes на хосте, а не в venv нашего репозитория). См. `_redact` ниже.
#
# ⚠️ Пока плагин установлен, он висит на живом gateway'е. Ошибка внутри него — это
#    поведение боевого канала, а не песочницы. Отсюда: весь код в try/except, все замеры
#    — на инстансе, где ещё нет мутаций (пилот READ-only by construction).
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import json
import os
import re
import time

_LOG_PATH = os.environ.get("AIMASH_PROBE_LOG") or os.path.expanduser("~/.hermes/aimash_probe.log")
# Перезапись аргументов (V9) — только после V4. По умолчанию выключено: хук-наблюдатель не должен
# менять измеряемую систему до того, как известно, ЧТО именно он меняет.
_REWRITE = os.environ.get("AIMASH_PROBE_REWRITE") == "1"
# Значение, которое хук подставляет вместо модельного (V9): по нему в эхе `probe_echo` видно, кто
# заполнил поле — доверенный канал или LLM. Умышленно НЕ похоже на настоящий chat_id.
_HOOK_MARK = "HOOK_SUBSTITUTED"

# Минимальная автономная редакция (аналога `core.logging.redact_text` под рукой нет).
# Фильтруем по ФОРМЕ секрета, а не по имени поля: имя поля в чужом payload'е нам неизвестно.
_SECRET_PATTERNS = [
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"),  # telegram bot token
    re.compile(r"\bya29\.[A-Za-z0-9._-]+"),  # google access token
    re.compile(r"\b1//[A-Za-z0-9._-]{10,}"),  # google refresh token
    re.compile(r"\bAIza[A-Za-z0-9._-]{10,}"),  # google api key
    re.compile(r"\bsk-[A-Za-z0-9._-]{10,}"),  # openai/openrouter key
    # Класс значения ограничен НЕ `\S+`: в JSON-строке жадный `\S+` съедает и закрывающую кавычку со
    # скобками — лог становится невалидным JSON, то есть нечитаемым ровно тогда, когда в нём что-то
    # нашлось. Останавливаемся на кавычке/запятой/скобке.
    re.compile(
        r"(?i)\b(token|secret|password|api[_-]?key|refresh[_-]?token)\b\s*[=:]\s*[^\s\"',}\]]+"
    ),
]


def _redact(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub("***REDACTED***", text)
    return text


def _write(event: str, payload: dict) -> None:
    """Одна JSON-строка на событие. Никаких исключений наружу: хук-наблюдатель не имеет права
    ронять gateway — иначе V10 меряет наш баг, а не поведение Hermes."""
    try:
        line = json.dumps(
            {"ts": time.time(), "event": event, **payload}, ensure_ascii=False, default=repr
        )
        with open(_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(_redact(line) + "\n")
    except Exception:  # noqa: BLE001 — молча: сломанный лог не повод ломать канал
        pass


def _snapshot(args: tuple, kwargs: dict) -> dict:
    """Что доехало — как есть, но безопасно. `default=repr` в `_write` разберёт любые объекты,
    поэтому структуру не расплющиваем: форма payload'а — это и есть предмет замера V4."""
    return {"args": list(args), "kwargs": kwargs}


# ── Маркер загрузки. КРИТИЧНО для чтения результатов: без него «в логе пусто» читается
#    и как «хук не получил метаданных», и как «хук вообще не загрузился» — а это разные
#    выводы с разными последствиями для WRITE-пути.
_write("plugin_loaded", {"rewrite_enabled": _REWRITE, "log_path": _LOG_PATH, "pid": os.getpid()})


def pre_gateway_dispatch(*args, **kwargs):  # noqa: ANN002, ANN003 — сигнатура v0.17 неизвестна (V4)
    """V7: доходят ли до нашего кода доверенные метаданные входящего Telegram-сообщения
    (`message_id`, `reply_to_message_id`, `chat_id`, `user_id`) и в каком виде.
    Возврат None = «не вмешиваться» [Непроверено — форма allow проверяется в V5]."""
    _write("pre_gateway_dispatch", _snapshot(args, kwargs))
    if os.environ.get("AIMASH_PROBE_RAISE") == "gateway":
        # V10: управляемое падение хука. Меряем ФАКТ — пройдёт вызов дальше (fail-open) или нет.
        raise RuntimeError("aimash_probe: намеренное исключение V10 (pre_gateway_dispatch)")
    return None


def pre_tool_call(*args, **kwargs):  # noqa: ANN002, ANN003 — сигнатура v0.17 неизвестна (V4)
    """V8/V9: виден ли хуку вызов инструмента вместе с контекстом gateway-сообщения и может ли он
    ПЕРЕПИСАТЬ аргумент, который сгенерировала модель. Это и есть проверка критерия (a) §8.4:
    без доверенного канала подтверждение подделывается моделью, и WRITE в прод не выпускается."""
    _write("pre_tool_call.before", _snapshot(args, kwargs))
    if os.environ.get("AIMASH_PROBE_RAISE") == "tool":
        raise RuntimeError("aimash_probe: намеренное исключение V10 (pre_tool_call)")

    if _REWRITE:
        # Ищем словарь аргументов инструмента в неизвестном payload'е — по факту наличия ключей,
        # которые кладёт `probe_echo`. Найдём — подменим и покажем «до/после» в логе; не найдём —
        # это тоже результат замера (V9 = «переписать нельзя», критерий (a) не выполнен).
        target = _find_tool_arguments(args, kwargs)
        if target is None:
            _write("pre_tool_call.rewrite", {"result": "arguments_dict_not_found"})
        else:
            before = dict(target)
            target["actor_chat_id"] = _HOOK_MARK
            target["reply_to_message_id"] = _HOOK_MARK
            _write("pre_tool_call.rewrite", {"before": before, "after": dict(target)})
    return None


def _find_tool_arguments(args: tuple, kwargs: dict):
    """Первый попавшийся dict, похожий на аргументы `probe_echo` (есть хоть один из наших ключей).
    Поиск в ширину по вложенным dict/list — глубина ограничена, чтобы не гулять по всему объекту
    рантайма. Возвращает САМ словарь (мутируем на месте: как вернуть изменения — часть V4)."""
    probe_keys = {"note", "actor_chat_id", "reply_to_message_id", "delay_seconds"}
    queue: list = [*args, kwargs]
    seen = 0
    while queue and seen < 200:
        item = queue.pop(0)
        seen += 1
        if isinstance(item, dict):
            if probe_keys & set(map(str, item.keys())):
                return item
            queue.extend(item.values())
        elif isinstance(item, (list, tuple)):
            queue.extend(item)
        elif hasattr(item, "__dict__"):
            queue.append(vars(item))
    return None
