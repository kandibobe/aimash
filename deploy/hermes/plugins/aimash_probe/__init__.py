# ─────────────────────────────────────────────────────────────────────────────
# aimash_probe — ВРЕМЕННЫЙ диагностический плагин Hermes. Ставится на время прогона
# V1–V22 (`deploy/hermes/OPERATIONS.md` §12) и УДАЛЯЕТСЯ после. Не часть продукта.
#
# КОНТРАКТ ПОДТВЕРЖДЁН ПЕРВОИСТОЧНИКОМ НА ПИНОВОМ ТЕГЕ `v2026.7.20` (= релиз v0.19.0;
# тега `v0.19.0` в git НЕТ, прямой фетч по нему даёт 404 — сверять только по дате-тегу):
#   • раскладка и точка входа — `website/docs/user-guide/features/plugins.md`:
#     каталог `~/.hermes/plugins/<name>/` с `plugin.yaml` + `__init__.py`, точка входа
#     `def register(ctx)`, подписка `ctx.register_hook(hook_name, callback)`;
#   • плагин обязан быть перечислен в `plugins.enabled` конфига — иначе обнаружение его
#     ВИДИТ, но ни хуки, ни инструменты не грузятся («General plugins and user-installed
#     backends are disabled by default»). Это главная причина «пустого лога»;
#   • `pre_gateway_dispatch(event, gateway, session_store, **kwargs)` — ТРИ позиционных
#     объекта, не один; возврат `None` | `{"action": "skip"|"rewrite"|"allow", …}`;
#   • `pre_tool_call(tool_name: str, args: dict, task_id: str, **kwargs)` — идентичности
#     актора В СИГНАТУРЕ НЕТ ВООБЩЕ (ни `user_id`, ни `chat_id`, ни `event`), а из
#     возвратов документирован ровно один: `{"action": "block", "message": …}`;
#   • `MessageEvent` (исходник `gateway/platforms/base.py` на теге) НЕСЁТ
#     `reply_to_message_id`, `reply_to_author_id`, `reply_to_is_own_message`,
#     `message_id`, `media_urls` («local file paths»), а идентичность — в `event.source`
#     (`gateway/session.py`, `SessionSource`: `user_id`, `chat_id`, `chat_type`,
#     `thread_id`). Это закрывает открытый вопрос `SPEC.md:1435` — поле есть;
#   • хуки фейлятся OPEN: «errors in any hook are caught and logged, never crashing the
#     agent», «the gateway always falls through to normal dispatch on error»;
#   • ⛔ колбэки плагин-хуков зовутся СИНХРОННО, без `await` (асинхронного `invoke_hook` в
#     `hermes_cli/plugins.py` нет). `async def` вернул бы корутину, тело не выполнилось бы,
#     а лог остался бы пустым — ещё один способ намерить «метаданные не доходят» вместо
#     «прибор написан не так». Поэтому все колбэки здесь — обычные `def`;
#   • ⛔ неизвестное имя хука НЕ бросает исключение: `register_hook` пишет `logger.warning`
#     и молча не подписывает. Значит `registered` сам по себе НЕ доказательство — сверяем
#     список с `_VALID_HOOKS` ниже (снят дословно с `hermes_cli/plugins.py` на теге).
#
# ⚠️ Доки — НЕ код. Прибор всё равно построен самодиагностирующим: он выписывает
#    ФАКТИЧЕСКУЮ форму объектов (`dir()`, имена ключей), а не верит подписи. Цена ошибки
#    здесь — не «неточность», а ложный отрицательный вывод по денежному пути.
#
# ⚠️ Правило 5 применимо буквально: хук пишет во внешний файл всё, что до него доехало,
#    а в payload'е gateway'я может оказаться токен бота или ключ модели. Своей редакции у
#    хука быть обязано — импортировать `core.logging` он не может (крутится в процессе
#    Hermes на хосте, а не в venv нашего репозитория). См. `_redact` ниже.
#    Отсюда же деление хуков на два класса: у LLM-хуков payload — это весь промпт и весь
#    ответ модели; для них пишется ТОЛЬКО отпечаток (имена ключей и типы), не значения.
#
# ⚠️ Пока плагин установлен, он висит на живом gateway'е. Ошибка внутри него — поведение
#    боевого канала, а не песочницы. Отсюда: весь код в try/except, все замеры — на
#    инстансе, где ещё нет мутаций (пилот READ-only by construction).
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import json
import os
import re
import time

_LOG_PATH = os.environ.get("AIMASH_PROBE_LOG") or os.path.expanduser("~/.hermes/aimash_probe.log")
# V9: попытка переписать аргументы модели ПРЯМО в `pre_tool_call`. Поведение НЕ документировано
# (единственный документированный возврат этого хука — `block`), поэтому по умолчанию выключено:
# хук-наблюдатель не должен менять измеряемую систему.
_REWRITE = os.environ.get("AIMASH_PROBE_REWRITE") == "1"
# V9b: документированное вето. Значение — имя инструмента, который надо заблокировать.
_BLOCK_TOOL = os.environ.get("AIMASH_PROBE_BLOCK") or ""
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


# ── Классы хуков ─────────────────────────────────────────────────────────────
# ПОЛНЫЙ payload пишем только там, где предмет замера — сами метаданные гейта.
_HOOKS_FULL = (
    "pre_gateway_dispatch",  # V7: метаданные входящего (главный замер)
    "pre_tool_call",  # V8/V9/V10: вызов инструмента, вето, перезапись
    "pre_approval_request",  # V12: поднимает ли Hermes approvals хоть на чём-то
    "post_approval_response",  # V12: и кто вправе отвечать
    "on_session_start",
    "on_session_end",
    "on_session_finalize",
    "on_session_reset",
    "subagent_start",  # И8/правило 13: fan-out делегирования
    "subagent_stop",
    "kanban_task_claimed",  # есть в developer-guide, нет в user-guide — проверяем существование
    "kanban_task_completed",
    "kanban_task_blocked",
)
# ОТПЕЧАТОК (имена ключей и типы, без значений): payload — весь промпт / весь ответ модели /
# вывод терминала. Писать значения = сложить в файл на VPS всю переписку. Правило 5.
_HOOKS_FINGERPRINT = (
    "post_tool_call",
    "pre_llm_call",
    "post_llm_call",
    "pre_verify",
    "transform_tool_result",
    "transform_terminal_output",
    "transform_llm_output",
    # ⛔ Исходящий HTTP к провайдеру: payload несёт заголовки, то есть `Authorization: Bearer
    #    <OPENROUTER_API_KEY>`. Значения не пишем НИКОГДА — только отпечаток. Подписаны они
    #    ради полноты переписи (см. `_VALID_HOOKS`): «хук не сработал» — это факт лишь тогда,
    #    когда доказано, что он вообще был подписан.
    "pre_api_request",
    "post_api_request",
    "api_request_error",
)
_HOOKS = _HOOKS_FULL + _HOOKS_FINGERPRINT

# Дословный `VALID_HOOKS` пиновой версии (`hermes_cli/plugins.py` @ `v2026.7.20`), 23 имени.
# Нужен потому, что `register_hook` на неизвестном имени НЕ бросает — пишет `logger.warning`
# и молча не подписывает. Без этой сверки «подписались все 23» неотличимо от «подписались 0».
_VALID_HOOKS = frozenset(
    {
        "pre_tool_call",
        "post_tool_call",
        "transform_terminal_output",
        "transform_tool_result",
        "transform_llm_output",
        "pre_llm_call",
        "post_llm_call",
        "pre_verify",
        "pre_api_request",
        "post_api_request",
        "api_request_error",
        "on_session_start",
        "on_session_end",
        "on_session_finalize",
        "on_session_reset",
        "subagent_start",
        "subagent_stop",
        "pre_gateway_dispatch",
        "pre_approval_request",
        "post_approval_response",
        "kanban_task_claimed",
        "kanban_task_completed",
        "kanban_task_blocked",
    }
)

# Поля, ради которых всё затевается. Берём через getattr с явным маркером отсутствия: «поля нет»
# и «поле есть, но пустое» — разные ответы, и путать их нельзя.
_ABSENT = "<ABSENT>"
_EVENT_FIELDS = (
    "message_id",
    "reply_to_message_id",
    "reply_to_text",
    "reply_to_author_id",
    "reply_to_author_name",
    "reply_to_is_own_message",
    "media_urls",  # R6: локальные пути скачанных вложений
    "media_types",
    "internal",
    "auto_skill",
)
_SOURCE_FIELDS = ("platform", "chat_id", "chat_type", "user_id", "user_name", "thread_id")


def _fingerprint(args: tuple, kwargs: dict) -> dict:
    """Форма payload'а без значений: сколько позиционных, каких типов, какие имена ключей."""
    return {
        "args_types": [type(a).__name__ for a in args],
        "kwargs_names": sorted(kwargs),
        "dict_keys": sorted(
            {str(k) for a in (*args, *kwargs.values()) if isinstance(a, dict) for k in a}
        ),
    }


def _capture_gateway_event(args: tuple, kwargs: dict) -> None:
    """V7 одним куском: фактические поля `MessageEvent` и `event.source`, как они реально приехали.
    `dir()` печатается намеренно — если доки разошлись с кодом, это видно сразу, а не выводится
    из молчания лога."""
    event = kwargs.get("event") or (args[0] if args else None)
    if event is None:
        _write("gateway_fields", {"result": "event_not_found", **_fingerprint(args, kwargs)})
        return
    source = getattr(event, "source", None)
    _write(
        "gateway_fields",
        {
            "event": {f: getattr(event, f, _ABSENT) for f in _EVENT_FIELDS},
            "source": (
                {f: getattr(source, f, _ABSENT) for f in _SOURCE_FIELDS} if source else _ABSENT
            ),
            # Фактический состав объектов — предмет V4. Значения НЕ печатаем, только имена.
            "event_attrs": sorted(a for a in dir(event) if not a.startswith("_")),
            "source_attrs": sorted(a for a in dir(source) if not a.startswith("_"))
            if source
            else None,
        },
    )


def _handle_pre_tool_call(args: tuple, kwargs: dict):
    """V8/V9: что видит хук на вызове инструмента и может ли он вмешаться.

    ⚠️ Переформулировано против прежней редакции. По первоисточнику в `pre_tool_call` НЕТ
    идентичности актора (`tool_name`, `args`, `task_id`) — значит «подставить доверенный
    `actor_chat_id` прямо здесь» невозможно в принципе, и прежний замер V9 проверял контракт,
    которого не существует. Что меряем теперь:
      (a) `_REWRITE` — работает ли НЕДОКУМЕНТИРОВАННАЯ мутация `args` на месте. Ответ «нет»
          ожидаем и не является провалом: он лишь означает, что доверенные метаданные обязаны
          сниматься в `pre_gateway_dispatch` и коррелироваться по `task_id`/ключу сессии;
      (b) `_BLOCK_TOOL` — работает ли ДОКУМЕНТИРОВАННОЕ вето. Это единственный механизм,
          на котором может стоять К9-гард, поэтому мерить его обязательно.
    """
    tool_name = kwargs.get("tool_name") or (args[0] if args else None)
    tool_args = kwargs.get("args")
    if tool_args is None and len(args) > 1:
        tool_args = args[1]
    _write(
        "pre_tool_call.before",
        {
            "tool_name": tool_name,
            "task_id": kwargs.get("task_id") or (args[2] if len(args) > 2 else _ABSENT),
            "args": tool_args,
            "shape": _fingerprint(args, kwargs),
        },
    )

    if _BLOCK_TOOL and str(tool_name) == _BLOCK_TOOL:
        _write("pre_tool_call.block", {"tool_name": tool_name})
        return {"action": "block", "message": f"aimash_probe V9b: вето на {tool_name}"}

    if _REWRITE and isinstance(tool_args, dict):
        before = dict(tool_args)
        tool_args["actor_chat_id"] = _HOOK_MARK
        tool_args["reply_to_message_id"] = _HOOK_MARK
        _write(
            "pre_tool_call.rewrite",
            {"mechanism": "in_place", "before": before, "after": dict(tool_args)},
        )
    elif _REWRITE:
        _write(
            "pre_tool_call.rewrite", {"result": "args_dict_not_found", **_fingerprint(args, kwargs)}
        )
    return None


def _probe(name: str):
    """Один универсальный колбэк на любой хук. Возврат — всегда `None`: по контракту это
    «allow / unchanged» для всех документированных хуков, а любое нераспознанное значение
    рантайм игнорирует. Наблюдатель не имеет права менять поведение канала."""

    def callback(*args, **kwargs):
        try:
            if name in _HOOKS_FULL:
                _write(name, {"args": list(args), "kwargs": kwargs})
            else:
                _write(name, _fingerprint(args, kwargs))

            if name == "pre_gateway_dispatch":
                _capture_gateway_event(args, kwargs)
                if os.environ.get("AIMASH_PROBE_RAISE") == "gateway":
                    # V10: управляемое падение. Меряем ФАКТ — пройдёт вызов дальше или нет.
                    raise RuntimeError(
                        "aimash_probe: намеренное исключение V10 (pre_gateway_dispatch)"
                    )

            if name == "pre_tool_call":
                if os.environ.get("AIMASH_PROBE_RAISE") == "tool":
                    raise RuntimeError("aimash_probe: намеренное исключение V10 (pre_tool_call)")
                return _handle_pre_tool_call(args, kwargs)
        except RuntimeError:
            raise  # V10 — единственное исключение, которое обязано уйти наружу
        except Exception as exc:  # noqa: BLE001 — баг прибора не должен читаться как ответ рантайма
            _write("probe_internal_error", {"hook": name, "err": repr(exc)})
        return None

    callback.__name__ = f"probe_{name}"
    return callback


# ── МАРКЕР A. Модуль импортирован. НЕ означает, что хуки подписаны, — прежняя редакция
#    писала на этом месте `plugin_loaded`, и V6 («самый важный негативный контроль») мог
#    позеленеть на плагине, не подписанном ни на что.
_write("module_imported", {"pid": os.getpid(), "log_path": _LOG_PATH, "rewrite_enabled": _REWRITE})


def register(ctx):  # noqa: ANN001 — тип ctx рантайма нам недоступен, он и есть предмет замера
    """Точка входа плагина. Вызывается ровно один раз на старте; падение здесь отключает
    ПЛАГИН, но не роняет Hermes — то есть отказ регистрации тихий, и распознаётся он только
    по отсутствию маркеров ниже."""
    # МАРКЕР B: фактический API контекста. Если `register_hook` назван иначе — видно сразу.
    _write("register_called", {"ctx_attrs": sorted(a for a in dir(ctx) if not a.startswith("_"))})
    registered, failed = [], {}
    for hook_name in _HOOKS:
        try:
            ctx.register_hook(hook_name, _probe(hook_name))
            registered.append(hook_name)
        except Exception as exc:  # noqa: BLE001 — неизвестное имя не должно ронять остальные
            failed[hook_name] = repr(exc)
    # МАРКЕР C: поимённо, что подписалось и что нет. Пустой `registered` = прибор мёртв.
    #   `unknown` — имена, которых нет в `VALID_HOOKS` пиновой версии: рантайм принял их молча,
    #     значит они не работают, и молчание такого хука ничего не измеряет;
    #   `not_covered` — обратное: имена версии, на которые прибор НЕ подписался. Пустой список —
    #     доказательство, что перепись исчерпывающая, а не «сколько вспомнили».
    _write(
        "hooks_registered",
        {
            "registered": registered,
            "failed": failed,
            "unknown": sorted(set(_HOOKS) - _VALID_HOOKS),
            "not_covered": sorted(_VALID_HOOKS - set(_HOOKS)),
        },
    )
