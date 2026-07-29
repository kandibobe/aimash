"""Гарды поверхности `hermes_ops` — READ-окна в дашборд Hermes.

Почему гардов столько на 12 инструментов: дашборд держит 241 эндпоинт, среди которых запись файла
под root (`POST /api/fs/write-text`), чтение любого файла (`GET /api/fs/read-text` →
`/opt/aimash/.env` с OAuth-токенами) и прямая выдача секретов (`POST /api/env/reveal`). Свою
аутентификацию дашборд имеет (401 без сессии, замер 2026-07-29 — OPERATIONS.md §14), но она
защищает от чужого, а не от нашего: сессия будет у НАС, и с ней вся эта поверхность окажется в
досягаемости инструмента. Значит граница «что может дёрнуть агент» живёт целиком в нашем слое —
ровно как право на мутацию Google Ads (правило 8).

Проверяется не намерение, а конструкция: методов записи нет в классе, запрещённые пути не проходят
транспорт, живая поверхность равна одобренной, ответ редактируется.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re

import pytest

from core.logging import REDACTED
from hermes_ops import auth as auth_mod
from hermes_ops import client as client_mod
from hermes_ops.allowlist import FORBIDDEN_PREFIXES, READ_ENDPOINTS, _forbidden_hits
from hermes_ops.client import HermesReadClient, redact_deep, resolve_base_url
from hermes_ops.tools import MAX_RESPONSE_CHARS, HERMES_READ_TOOLS, HERMES_TOOL_FUNCS, _cap

PIN_PATH = pathlib.Path(__file__).resolve().parents[1] / "deploy/hermes/openapi-0.19.0.paths.json"
HERMES_OPS_DIR = pathlib.Path(__file__).resolve().parents[1] / "hermes_ops"

WRITE_METHODS = ("post", "put", "patch", "delete")


# ── Гард 1: транспорт физически не умеет писать ─────────────────────────────────────────


@pytest.mark.parametrize("method", WRITE_METHODS)
def test_client_has_no_write_methods(method: str) -> None:
    """И4-стиль: опасная операция не «не вызывается», а НЕ СУЩЕСТВУЕТ.

    Инструмент, забывший про allow-list, всё равно не сможет отправить POST — метода нет на классе.
    """
    assert not hasattr(HermesReadClient, method), (
        f"у HermesReadClient появился метод {method!r} — это снимает границу слоя: с нашей же "
        "сессией дашборд умеет писать файлы под root и раскрывать секреты."
    )


def test_no_write_calls_outside_the_auth_module() -> None:
    """`.post(`/`.put(`/`.patch(`/`.delete(` не встречаются нигде, кроме `auth.py`.

    Гард по ИСХОДНИКУ, а не по классу: внутренний `httpx.AsyncClient` эти методы имеет, и обойти
    границу можно было бы одной строкой `self._http.post(...)`. Разбор текста ловит такую строку в
    ревью, а не на живом VPS.

    Исключение ровно одно — вход в дашборд (`auth.py`), и оно сужено следующим тестом.
    """
    pattern = re.compile(r"\.(?:" + "|".join(WRITE_METHODS) + r")\s*\(")
    offenders = [
        f"{path.name}:{i}"
        for path in sorted(HERMES_OPS_DIR.glob("*.py"))
        if path.name != "auth.py"
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert not offenders, (
        f"вызов метода записи в hermes_ops: {offenders}. Слой обязан оставаться read-only "
        "по построению — см. докстринг hermes_ops/client.py."
    )


def test_auth_module_posts_only_to_the_fixed_login_path() -> None:
    """В `auth.py` ровно один POST, и его путь — константа модуля, а не аргумент.

    Смысл исключения: «инструмент не может ничего изменить на VPS» держится не тем, что POST
    запрещён словом, а тем, что подставить в него ЧУЖОЙ путь неоткуда. `http.post(path, ...)` с
    вычисляемым путём вернул бы модели всю write-поверхность дашборда (`/api/fs/write-text` и
    остальные 240 ручек) — поэтому разбираем AST, а не ищем строку глазами.
    """
    tree = ast.parse((HERMES_OPS_DIR / "auth.py").read_text(encoding="utf-8"))
    writes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in WRITE_METHODS
    ]
    assert len(writes) == 1, f"ожидался ровно один вызов записи, найдено {len(writes)}"

    first_arg = writes[0].args[0] if writes[0].args else None
    assert isinstance(first_arg, ast.Name) and first_arg.id == "LOGIN_PATH", (
        "путь POST-а обязан быть константой LOGIN_PATH, а не выражением: иначе вызывающий "
        "выбирает, куда уйдёт запись."
    )
    assert auth_mod.LOGIN_PATH == "/auth/password-login", auth_mod.LOGIN_PATH
    # У остальных методов записи и с константой делать нечего — их в модуле быть не должно.
    assert writes[0].func.attr == "post"


def test_login_refuses_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Нет логина/пароля ⇒ отказ с объяснением, а не тихий вход «как-нибудь» (правило 10)."""
    import asyncio

    monkeypatch.delenv(auth_mod.ENV_USERNAME, raising=False)
    monkeypatch.delenv(auth_mod.ENV_PASSWORD, raising=False)
    with pytest.raises(RuntimeError, match=auth_mod.ENV_USERNAME):
        asyncio.run(auth_mod.DashboardAuth()._password_login(object()))  # type: ignore[arg-type]


def test_session_token_is_scrubbed_from_responses() -> None:
    """Токен сессии дашборда не уходит наружу в теле ответа.

    `redact_text` знает шаблоны ключей OpenRouter/OAuth, но `token_urlsafe(32)` от них неотличим —
    значит вычищать его надо по ЗНАЧЕНИЮ. Дашборд возвращает собственный токен как минимум в
    `/api/config`, так что случай не гипотетический.
    """
    token = "Tk" + "z" * 41  # форма secrets.token_urlsafe(32)
    auth = auth_mod.DashboardAuth()
    auth._token = token  # noqa: SLF001 — тест конструкции, не поведения
    assert token in json.dumps({"session_token": token})
    cleaned = redact_deep({"session_token": token, "nested": [token]}, auth.secret_values())
    assert token not in json.dumps(cleaned, ensure_ascii=False)
    assert REDACTED in json.dumps(cleaned, ensure_ascii=False)


# ── Гард 2: allow-list путей, fail-closed ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "/api/env/reveal",  # выдача секретов открытым текстом
        "/api/fs/write-text",  # запись файла под root
        "/api/fs/read-text",  # чтение /opt/aimash/.env
        "/api/config/raw",  # перезапись конфига целиком (вернуть тулсет terminal)
        "/api/skills/hub/install",  # установка community-скилов (К14)
        "/api/ops/backup/download",  # бэкап целиком, включая секреты
    ],
)
def test_build_path_refuses_endpoints_outside_allowlist(path: str) -> None:
    """Путь не из allow-list ⇒ RuntimeError ДО выхода в сеть (правило 10)."""
    with pytest.raises(RuntimeError, match="allow-list"):
        HermesReadClient(base_url="https://example.invalid").build_path(path)


def test_path_params_cannot_escape_the_template() -> None:
    """Подстановка не выходит за пределы одного сегмента пути.

    Без квотирования `session_id="../../api/env/reveal"` дал бы обход allow-list через параметр:
    шаблон одобрен, а запрос уходит на запрещённую ручку.
    """
    built = HermesReadClient(base_url="https://example.invalid").build_path(
        "/api/sessions/{session_id}/messages", {"session_id": "../../api/env/reveal"}
    )
    assert "/api/env/reveal" not in built
    assert "%2F" in built, built


# ── Гард 3: пересечение с запретной поверхностью пусто ──────────────────────────────────


def test_allowlist_disjoint_from_forbidden_surface() -> None:
    """Ни один разрешённый путь не лежит под запретным префиксом.

    Дублирует construction-time гард в `allowlist.py` намеренно: тот роняет импорт, этот называет
    виновника в отчёте pytest.
    """
    assert _forbidden_hits() == []


def test_forbidden_prefixes_cover_the_dangerous_groups() -> None:
    """Сам список запретного не должен усохнуть незаметно."""
    must_cover = {
        "/api/env",
        "/api/fs",
        "/api/files",
        "/api/skills",
        "/api/credentials",
        "/api/git",
    }
    assert must_cover <= FORBIDDEN_PREFIXES, sorted(must_cover - FORBIDDEN_PREFIXES)


# ── Гард 4: сверка с пином схемы (дисциплина К10, перенесённая на API) ──────────────────


def test_allowlisted_paths_exist_in_pinned_schema_as_get() -> None:
    """Каждый разрешённый путь есть в схеме пина 0.19.0 И имеет GET.

    Hermes 0.x релизится каждые 5–10 дней. Без этой сверки обновление платформы, убравшее ручку,
    проявилось бы как молчаливый 404 внутри конверта, а не как красный тест.
    """
    pinned: dict[str, list[str]] = json.loads(PIN_PATH.read_text(encoding="utf-8"))
    missing = sorted(p for p in READ_ENDPOINTS if p not in pinned)
    assert not missing, f"нет в пине {PIN_PATH.name}: {missing}"
    not_get = sorted(p for p in READ_ENDPOINTS if "GET" not in pinned[p])
    assert not not_get, f"в пине без метода GET: {not_get}"


def test_pin_still_describes_the_dangerous_surface() -> None:
    """Контроль осмысленности пина: он обязан содержать те самые опасные ручки.

    Иначе тест выше зелёный на пустом/битом файле, и вся сверка становится декорацией.
    """
    pinned = json.loads(PIN_PATH.read_text(encoding="utf-8"))
    assert "POST" in pinned.get("/api/fs/write-text", [])
    assert "POST" in pinned.get("/api/env/reveal", [])
    assert "PUT" in pinned.get("/api/config/raw", [])


# ── Гард 5: живая поверхность равна одобренной ──────────────────────────────────────────


def test_registered_surface_equals_approved_set() -> None:
    """`build_server()` роняет старт при любом расхождении — равенство, не «⊆»."""
    from hermes_ops.server import build_server

    mcp = build_server()  # сам по себе барьер: require_registered_surface внутри
    names = frozenset(t.name for t in mcp._tool_manager.list_tools())
    assert names == HERMES_READ_TOOLS == frozenset(HERMES_TOOL_FUNCS)
    assert len(names) == 12, sorted(names)


def test_logs_tool_does_not_expose_file_parameter() -> None:
    """У `/api/logs` есть параметр `file` — второй путь к чтению произвольного файла на VPS.

    Он не должен появиться в сигнатуре инструмента: чего нет в схеме, того модель не подставит.
    """
    import inspect

    params = inspect.signature(HERMES_TOOL_FUNCS["hermes_logs"]).parameters
    assert "file" not in params, (
        "hermes_logs выставил параметр file — это обход allow-list на /api/fs"
    )


# ── Гард 6: редакция на выходе ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "secret",
    [
        "sk-or-v1-" + "a" * 40,  # ключ OpenRouter
        "1//" + "b" * 30,  # OAuth refresh token
    ],
)
def test_response_secrets_are_redacted(secret: str) -> None:
    """Секрет в ответе дашборда не доходит до клиента сырым (правило 5).

    `/api/config` и `/api/logs` — реальные носители: конфиг ссылается на переменные окружения, лог
    несёт заголовки запросов.
    """
    payload = {"env": {"OPENROUTER_API_KEY": secret}, "lines": [f"auth={secret}"]}
    cleaned = redact_deep(payload)
    assert secret not in json.dumps(cleaned, ensure_ascii=False)
    assert REDACTED in json.dumps(cleaned, ensure_ascii=False)


def test_redact_deep_preserves_structure() -> None:
    """Редакция не ломает форму ответа (поэтому идём по структуре, а не по сериализованному тексту)."""
    src = {"a": [1, {"b": "plain"}], "c": None, "d": True}
    assert redact_deep(src) == src


# ── Гард 7: потолок объёма ответа ───────────────────────────────────────────────────────


def test_oversized_response_is_capped_and_flagged() -> None:
    """Огромный ответ усечён и помечен — молчаливое усечение читалось бы как «это всё, что есть»."""
    data, truncated, note = _cap({"rows": ["x" * 500 for _ in range(200)]})
    assert truncated is True
    assert isinstance(data, str) and len(data) == MAX_RESPONSE_CHARS
    assert note and "усеч" in note


def test_small_response_is_untouched() -> None:
    data, truncated, note = _cap({"ok": 1})
    assert (data, truncated, note) == ({"ok": 1}, False, None)


# ── Гард 8: без базового URL слой не поднимается ────────────────────────────────────────


def test_missing_base_url_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Не задан `HERMES_DASHBOARD_URL` ⇒ отказ, а не дефолт (правило 10, fail-closed)."""
    monkeypatch.delenv(client_mod.ENV_BASE_URL, raising=False)
    with pytest.raises(RuntimeError, match=client_mod.ENV_BASE_URL):
        resolve_base_url()


@pytest.mark.parametrize("bad", ["file:///etc/passwd", "ftp://host/x", "gopher://x"])
def test_non_http_base_url_refuses(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv(client_mod.ENV_BASE_URL, bad)
    with pytest.raises(RuntimeError, match="http/https"):
        resolve_base_url()


# ── Поведение при недоступном дашборде: конверт, а не исключение ────────────────────────


async def test_tool_returns_redacted_envelope_when_dashboard_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FastMCP кладёт сырой `str(e)` в ToolError — поэтому каждый инструмент ловит всё сам.

    Исключение httpx несёт URL и заголовки; наружу обязан идти редактированный конверт.
    """
    monkeypatch.setenv(
        client_mod.ENV_BASE_URL, "http://127.0.0.1:9"
    )  # порт discard, соединения нет
    monkeypatch.setenv(client_mod.ENV_TIMEOUT, "2")
    envelope = await HERMES_TOOL_FUNCS["hermes_status"]()
    assert envelope["ok"] is False
    assert envelope["data"] is None  # fail-closed: частичных данных не отдаём
    assert envelope["error"]
