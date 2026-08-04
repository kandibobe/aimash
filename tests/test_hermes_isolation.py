"""Гарды изоляции разрешений от памяти/скилов/external-контента (пивот Hermes, §4 — И1…И8).

Файл начался как READ-зерно, но перед публикацией WRITE получил живые И1–И8 и injection-корпус.
Здесь проверяются:

  • **И4 (зерно)** — construction-time assert в `mcp_server.server`: READ-инструменты физически не
    пересекаются с мутационными (`llm.schemas.MUTATION_TOOLS`). Импорт роняет процесс, если
    мутация просочилась в read-фазу. Тот же паттерн S4, что защищает `ANALYSIS_TOOLS`.
  • **read-lock на границе MCP** — параметризованно по ВСЕМ обёрткам: инструмент на аккаунте вне
    allow-list отказывает ДО первого обращения наружу (все ридеры застаблены взрывом), отдаёт
    редактированный error-конверт с `error_code == "forbidden_account"` — не сырое исключение и не
    данные; обратная половина доказывает, что замок пропускает разрешённый аккаунт.

  • **И1–И3/И5–И8** — exact-args HMAC, безаргументный execute, trusted provenance, закрытые host-
    поверхности, client-scoped memory, external-content phase lock и DB-счётчик proposal на ход.

Стиль — как tests/test_safety_core.py: `sys.path.insert` + `# noqa: E402`, contextmanager-фикстуры
allow-list поверх `settings`, in-process, `asyncio.run` для async-инструментов.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ads.client import DRAFT_ACCOUNT_ID, ensure_read_allowed  # noqa: E402
from core.config import settings  # noqa: E402

# Аккаунт заведомо вне любого allow-list/потолка/грантов (не Draft, не MCC) — «чужой боевой id».
_FOREIGN_ID = "9999999999"


@contextmanager
def _allow_lists(*, mutate: str = "", read: str = "", manager: str = ""):
    """Задать mutation-, read- и MCC-allow-list поверх settings (перебивает значения из .env) — чтобы
    доступ был детерминирован независимо от локального окружения. Пустые оба ⇒ чтение запрещено
    даже Draft'у: замок ЧТЕНИЯ (ads.client.ensure_read_allowed) смотрит allowed∪read∪_READ_DISCOVERED,
    а НЕ мутационный ALLOWED_CEILING — Draft читается, лишь если он в одном из списков (в dev — есть).
    `manager` правит login_customer_id: обход MCC (`list_accounts`) замыкается ДРУГИМ чокпойнтом
    (ensure_manager_allowed), и его набор из этих двух списков не строится."""
    prev_mut = settings.google_ads_allowed_customer_ids
    prev_read = settings.google_ads_read_customer_ids
    prev_mgr = settings.google_ads_login_customer_id
    settings.google_ads_allowed_customer_ids = mutate
    settings.google_ads_read_customer_ids = read
    settings.google_ads_login_customer_id = manager
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev_mut
        settings.google_ads_read_customer_ids = prev_read
        settings.google_ads_login_customer_id = prev_mgr


# ── И4 (зерно): READ-инструменты физически не пересекаются с мутационными ────────────
def test_i4_seed_read_tools_disjoint_from_mutations():
    # Импорт mcp_server.server несёт construction-time assert И4 — если он нарушен, импорт (а с ним
    # и весь MCP-процесс) падает здесь, а не «тихо открывает мутацию» в read-фазе.
    import mcp_server.server  # noqa: F401  — импорт ради живого assert И4

    from llm.schemas import MUTATION_TOOLS
    from mcp_server.tools_read import READ_MCP_TOOLS

    assert READ_MCP_TOOLS, "реестр READ-инструментов пуст"
    assert READ_MCP_TOOLS.isdisjoint(MUTATION_TOOLS), (
        "И4: READ-инструменты MCP пересеклись с мутационными: "
        f"{sorted(READ_MCP_TOOLS & MUTATION_TOOLS)}"
    )
    # 25 = 11 generic/account/profile READ + 14 bot-free workflow readers (report artifacts,
    # keyword/RSA primitives and structured client/crawl reads). Точный счёт держит
    # реестр от тихого разрастания: новая обёртка обязана осознанно бампнуть его вместе с
    # config.yaml/_ACCOUNT_ARG.
    assert len(READ_MCP_TOOLS) == 26, f"ожидалось 26 READ-инструментов, стало {len(READ_MCP_TOOLS)}"


def test_i4_seed_server_builds_and_registers_only_read():
    # Сервер строится и регистрирует РОВНО READ+META-набор — ни одного мутационного имени.
    from mcp_server.server import build_server
    from mcp_server.tools_meta import META_MCP_TOOLS
    from mcp_server.tools_read import READ_MCP_TOOLS

    srv = build_server()
    names = {t.name for t in asyncio.run(srv.list_tools())}
    expected = set(READ_MCP_TOOLS | META_MCP_TOOLS)
    assert names == expected, f"реестр FastMCP разошёлся с READ+META surface: {names}"


# ── read-lock на границе MCP: параметризованный инвариант по ВСЕМ обёрткам ───────────
# Прошлая версия проверяла ОДИН инструмент и лишь факт непустого `error`. Это тавтология: конверт
# отдавал байт-в-байт одну форму на любое исключение, поэтому тест оставался зелёным и при полностью
# снятом замке — падал бы разве что на опечатке. Теперь проверяются три разных утверждения:
#   (а) отказ классифицирован МАШИНОЙ — `error_code == "forbidden_account"`, а не «текст непуст»;
#   (б) отказ случился ДО выхода наружу — все ридеры застаблены взрывом, и он не прилетел;
#   (в) замок не «отказывает всем» — обратная половина доводит разрешённый аккаунт до ридера.
# Список ниже ведётся вручную НАМЕРЕННО: новая обёртка без записи роняет
# `test_every_read_tool_declares_account_arg`, то есть выставить ридер наружу, не сказав, каким
# аргументом он адресует аккаунт, нельзя.
_ACCOUNT_ARG: dict[str, str] = {
    "list_accounts": "manager_id",  # другой чокпойнт — ensure_manager_allowed
    "execute_google_ads_query": "account",
    "get_account_audit": "account",
    "analyze_account": "account",
    "get_change_history": "account",
    "get_account_changes": "account",  # Р6: журнал правок Google (НЕ наш audit-trail)
    "keyword_ideas": "account",
    "get_quota": "account",  # ридер НАШЕГО счётчика (core.quota) — замок только на границе
    "recall_client": "account",  # ридер НАШЕЙ БД (ClientProfileStore) — замок только на границе
    # §8 MCC: адресуют не лист, а УПРАВЛЯЮЩИЙ аккаунт — тот же чокпойнт, что у list_accounts
    # (ensure_manager_allowed). Дочерние аккаунты внутри обхода фильтрует сам `reports/mcc.py`
    # по read-allow-листу, поэтому граница проверяет ровно то, что адресовал вызывающий.
    "get_mcc_summary": "manager_id",
    "get_mcc_deep": "manager_id",
    "build_report": "account",
    "build_mcc_report": "manager_id",
    "export_keyword_report": "account",
    "seed_keywords": "account",
    "cluster_keywords": "account",
    "filter_keyword_relevance": "account",
    "suggest_negatives": "account",
    "parse_keywords_input": "account",
    "generate_rsa": "account",
    "validate_adcopy": "account",
    "build_display_path": "account",
    "get_client_card": "account",
    "list_client_facts_structured": "account",
    "list_site_pages": "account",
    "get_crawl_status": "account",
}


# Обязательные НЕ-аккаунтные аргументы обёрток. Без них вызов не собрался бы вовсе (TypeError), а
# «вызов не собрался» неотличимо от «замок отказал» — инвариант проверял бы синтаксис, не замок.
# Значения фиктивные и до ридера не доезжают ни в одной половине: в первой отказывает замок, во
# второй взрывается стаб. Полноту словаря держит `test_required_args_are_declared_for_lock_invariant`.
_EXTRA_ARGS: dict[str, dict[str, object]] = {
    "execute_google_ads_query": {"gaql_query": "SELECT customer.id FROM customer LIMIT 1"},
    "analyze_account": {"objective": "Найди главный источник потерь"},
    "seed_keywords": {"topic": "X"},
    "cluster_keywords": {"keywords": ["X"]},
    "filter_keyword_relevance": {"topic": "X", "keywords": ["X"]},
    "suggest_negatives": {"topic": "X", "keywords": ["X"]},
    "parse_keywords_input": {"text": "X"},
    "generate_rsa": {"topic": "X"},
    "validate_adcopy": {"headlines": ["A", "B", "C"], "descriptions": ["D", "E"]},
    "get_crawl_status": {"job_id": "x"},
}


class _ReaderCalled(RuntimeError):
    """Стаб выхода наружу (SDK/БД). Прилетел ⇒ тело инструмента исполнилось, замок пропустил."""


@contextmanager
def _readers_explode():
    """Заменить ВСЕ выходы `tools_read` наружу на взрыв. Замок обязан сработать раньше любого из них;
    заодно тест офлайн и детерминирован (ни SDK, ни БД не поднимаются)."""
    from mcp_server import tools_read as tr
    from mcp_server import tools_workflows as tw

    names = (
        "build_client_async",
        "run_ads_read_call",
        "gather_audit",
        "list_recent_applied_by_customer",
        "ClientProfileStore",  # recall_client: ридер НАШЕЙ БД — тоже за границу, тест офлайн
        "quota_snapshot",  # get_quota: Ads не опрашивает, но за границу (наша БД) выходит
    )
    saved = {n: getattr(tr, n) for n in names}
    workflow_names = (
        "build_client_async",
        "run_ads_read_call",
        "ClientProfileStore",
        "crawl_jobs",
        "_generate_rsa",
        "_cluster_keywords",
        "filter_relevance",
        "suggest_negative_keywords",
        "generate_seed_keywords",
        "parse_keywords_text",
    )
    workflow_saved = {n: getattr(tw, n) for n in workflow_names}

    def _boom(*_a, **_kw):
        raise _ReaderCalled("ридер вызван — замок не отработал до выхода наружу")

    for n in names:
        setattr(tr, n, _boom)
    for n in workflow_names:
        setattr(tw, n, _boom)
    try:
        yield
    finally:
        for n, fn in saved.items():
            setattr(tr, n, fn)
        for n, fn in workflow_saved.items():
            setattr(tw, n, fn)


def test_every_read_tool_declares_account_arg():
    from mcp_server.tools_read import READ_MCP_TOOLS

    assert set(_ACCOUNT_ARG) == set(READ_MCP_TOOLS), (
        "READ-обёртка не объявила, каким аргументом адресуется аккаунт (или исчезла); "
        f"расхождение: {sorted(set(_ACCOUNT_ARG) ^ set(READ_MCP_TOOLS))}"
    )


def test_required_args_are_declared_for_lock_invariant():
    """Новая обёртка с обязательным аргументом не должна ронять инвариант замка `TypeError`'ом:
    вызвать инструмент тест обязан ВАЛИДНО, иначе «замок отказал» неотличимо от «вызов не собрался».
    Полнота `_EXTRA_ARGS` проверяется по сигнатуре, а не глазами."""
    import inspect

    from mcp_server import tools_read as tr

    missing: dict[str, list[str]] = {}
    for name, fn in tr.READ_TOOL_FUNCS.items():
        required = {
            p.name
            for p in inspect.signature(fn).parameters.values()
            if p.default is inspect.Parameter.empty
        } - {_ACCOUNT_ARG[name]}
        gap = sorted(required - set(_EXTRA_ARGS.get(name, {})))
        if gap:
            missing[name] = gap
    assert not missing, f"обязательные аргументы не объявлены в _EXTRA_ARGS: {missing}"


def _call_args(tool_name: str, account_id: str) -> dict[str, object]:
    return {_ACCOUNT_ARG[tool_name]: account_id, **_EXTRA_ARGS.get(tool_name, {})}


@pytest.mark.parametrize("tool_name", sorted(_ACCOUNT_ARG))
def test_read_lock_denies_foreign_account_before_any_reader(tool_name):
    from mcp_server import tools_read as tr

    fn = tr.READ_TOOL_FUNCS[tool_name]
    with _allow_lists(mutate="", read="", manager=""), _readers_explode():
        env = asyncio.run(fn(**_call_args(tool_name, _FOREIGN_ID)))

    assert env["error_code"] == "forbidden_account", (
        f"{tool_name}: отказ не классифицирован как замок (получено {env['error_code']!r}). "
        "internal ⇒ до замка успел выполниться ридер; None ⇒ чужой аккаунт прочитан — fail-open"
    )
    assert env["rows"] == [] and env["total_rows"] == 0, "fail-closed нарушен: отказ вернул данные"
    # Правило 5: наружу только редактированный текст — не трасса и не сырой repr исключения.
    assert env["error"], "ожидался редактированный текст отказа в error"
    assert "Traceback" not in env["error"]


@pytest.mark.parametrize("tool_name", sorted(_ACCOUNT_ARG))
def test_read_lock_admits_allowed_account_and_reaches_reader(tool_name):
    """Обратная половина: без неё инвариант выше проходил бы и на реализации «отказывать всегда».
    Разрешённый аккаунт обязан дойти до ридера — там его встречает взрыв стаба (`internal`)."""
    from mcp_server import tools_read as tr

    fn = tr.READ_TOOL_FUNCS[tool_name]
    with (
        _allow_lists(mutate=DRAFT_ACCOUNT_ID, read="", manager=DRAFT_ACCOUNT_ID),
        _readers_explode(),
    ):
        env = asyncio.run(fn(**_call_args(tool_name, DRAFT_ACCOUNT_ID)))

    local_only = {"build_display_path", "validate_adcopy"}
    if tool_name in local_only:
        assert env["error_code"] is None, (
            f"{tool_name}: разрешённый аккаунт не прошёл локальную валидацию: {env}"
        )
        return
    assert env["error_code"] == "internal", (
        f"{tool_name}: разрешённый аккаунт не дошёл до ридера (код {env['error_code']!r}) — "
        "замок отказывает всем, инвариант отказа выше это не поймал бы"
    )


def test_reference_hermes_config_exposes_exactly_the_enabled_registry():
    """Четвёртое место lock-step: `tools.include` в `~/.hermes/config.yaml`.

    Это ВТОРОЙ allow-list, живущий вне репозитория: инструмент, зарегистрированный в FastMCP, но не
    вписанный туда, агенту НЕВИДИМ — то есть работа сделана, а функции у профессионала нет. Тест
    держит эталон `deploy/hermes/config.yaml` синхронным с реестром; копию на VPS он не видит, но
    расхождение эталона ловит до деплоя, а список для ручной правки печатает в сообщении.
    (`yaml` берём из транзитивной зависимости `google-ads` — новой зависимости не заводим.)
    """
    import yaml

    from mcp_server.tools_meta import META_MCP_TOOLS
    from mcp_server.tools_plan import PLAN_STATE_MCP_TOOLS
    from mcp_server.tools_read import READ_MCP_TOOLS
    from mcp_server.tools_write import PLAN_WRITE_MCP_TOOLS

    cfg_path = Path(__file__).resolve().parents[1] / "deploy" / "hermes" / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    servers = cfg.get("mcp_servers") or cfg.get("mcp") or {}
    include = None
    for srv in servers.values() if isinstance(servers, dict) else []:
        inc = ((srv or {}).get("tools") or {}).get("include")
        if inc is not None:
            include = set(inc)
            break
    assert include is not None, f"в {cfg_path.name} не нашёлся tools.include — эталон разошёлся"
    expected = set(READ_MCP_TOOLS | META_MCP_TOOLS | PLAN_WRITE_MCP_TOOLS | PLAN_STATE_MCP_TOOLS)
    missing, extra = sorted(expected - include), sorted(include - expected)
    assert not missing and not extra, (
        f"tools.include эталона разошёлся с реестром. Дописать: {missing}; лишние: {extra}. "
        "Не забыть ту же правку в ~/.hermes/config.yaml на VPS — иначе инструмент невидим агенту."
    )


def test_error_code_distinguishes_our_lock_from_google_refusal():
    """`forbidden_account` обязан означать «отказал НАШ замок». Отказ Google (`USER_PERMISSION_DENIED`)
    — другой инцидент и другое действие оператора: код `upstream_denied`. Схлопни их — и инвариант
    замка снова станет тавтологичным, теперь уже через ошибку доступа со стороны Google."""
    from mcp_server.envelope import classify_error, err

    class _FakeCode:
        name = "USER_PERMISSION_DENIED"

    class _FakeErr:
        error_code = _FakeCode()

    class _FakeFailure:
        errors = [_FakeErr()]

    class _FakeGoogleAdsException(Exception):
        failure = _FakeFailure()

    assert classify_error(PermissionError("замок")) == "forbidden_account"
    assert classify_error(_FakeGoogleAdsException()) == "upstream_denied"
    assert classify_error(ValueError("кривая дата")) == "invalid_argument"
    assert classify_error(RuntimeError("что-то")) == "internal"

    # Классификатор не смеет ронять инструмент: исключение, взрывающееся при интроспекции, — internal.
    class _Hostile(Exception):
        @property
        def failure(self):
            raise RuntimeError("интроспекция исключения сама бросает")

    assert err(_Hostile())["error_code"] == "internal"


def test_read_lock_allows_draft_but_still_denies_foreign():
    # Обратная сторона fail-closed: с Draft в allow-list (как в dev .env) замок ЧТЕНИЯ его пропускает,
    # но чужой id при том же наборе всё равно отклонён — это замок, а не «всё открыто».
    with _allow_lists(mutate=DRAFT_ACCOUNT_ID, read=""):
        ensure_read_allowed(DRAFT_ACCOUNT_ID)  # не должно бросить
        try:
            ensure_read_allowed(_FOREIGN_ID)
            raise AssertionError("чужой аккаунт прошёл замок ЧТЕНИЯ — fail-open")
        except PermissionError:
            pass


# ── Диагностический прибор `mcp_server.probe` не смеет стать частью боевого реестра ──
# Прибор (§12 OPERATIONS.md, прогон V1–V22) регистрируется ОТДЕЛЬНЫМ сервером `aimash-probe`.
# Соблазн «добавить probe_echo 13-м инструментом» ломает сразу два свойства: измеряемая система
# перестаёт быть измеряемой (прибор виден агенту и в бою), а прибор наследует доступ боевого
# сервера. Оба перекрываем тестом, а не комментарием.
def test_probe_is_not_registered_in_production_server():
    from mcp_server.probe import build_probe_server
    from mcp_server.server import build_server
    from mcp_server.tools_read import READ_MCP_TOOLS, READ_TOOL_FUNCS

    assert "probe_echo" not in READ_MCP_TOOLS
    assert "probe_echo" not in READ_TOOL_FUNCS
    prod = {t.name for t in asyncio.run(build_server().list_tools())}
    assert "probe_echo" not in prod, "прибор просочился в боевой MCP-реестр"
    probe = {t.name for t in asyncio.run(build_probe_server().list_tools())}
    assert probe == {"probe_echo"}, f"в приборе появилось лишнее: {sorted(probe)}"


def test_probe_has_no_access_to_money_path():
    """Прибор возвращает эхо аргументов и не более: ни Ads, ни БД, ни confirm-гейта он не тянет.
    Проверяем в ПОДПРОЦЕССЕ по факту загруженных модулей — импорт-граф врать не умеет, в отличие от
    докстринга. Гарантия нужна затем, что плагин-времянка какое-то время висит на живом gateway'е."""
    import json
    import subprocess

    # Голый `google` — namespace-пакет, который тянет сам интерпретатор через .pth в site-packages
    # (виден и в `python -c "import sys"`), поэтому ловим именно `google.ads`, а не префикс `google`.
    code = (
        "import json,sys;import mcp_server.probe;"
        "print(json.dumps(sorted(m for m in sys.modules "
        "if m.split('.')[0] in ('ads','db','confirm','bot','agent') "
        "or m.startswith('google.ads'))))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert out.returncode == 0, f"импорт mcp_server.probe упал: {out.stderr[-500:]}"
    leaked = json.loads(out.stdout.strip().splitlines()[-1])
    assert leaked == [], f"прибор притащил денежный/БД-слой: {leaked}"


def test_probe_echo_redacts_and_clamps():
    import json

    from mcp_server.probe import _MAX_DELAY_SECONDS, _clamp_delay, probe_echo

    env = asyncio.run(probe_echo(note="refresh_token=1//0gSECRETVALUE", actor_chat_id="123"))
    # Правило 5: в аргументы может прилететь что угодно, включая подставленное хуком из окружения.
    dumped = json.dumps(env, ensure_ascii=False, default=repr)
    assert "SECRETVALUE" not in dumped, f"эхо вынесло секрет наружу: {env}"
    assert env["received"]["actor_chat_id"] == "123"
    # `filled_fields` отличает «хук не подставил ничего» от «подставил пустое» — иначе дефолты
    # аргументов затирают разницу, а это разные исходы V8/V9.
    assert env["filled_fields"] == ["actor_chat_id", "note"]
    # Потолок задержки — через `_clamp_delay`, а не через живой вызов: иначе тест сам спал бы 5 минут.
    assert _clamp_delay(10**6) == _MAX_DELAY_SECONDS, "потолок снят — прибор подвесит gateway"
    assert _clamp_delay("мусор") == 0
    assert _clamp_delay(-5) == 0


async def test_i1_external_source_cannot_change_proposal_customer_id(monkeypatch):
    """И1 (creation-half): ни скил/память/факт/текст с сайта клиента не меняет customer_id в proposal
    при СОЗДАНИИ черновика: HMAC привязан к exact args, подмена account после hook отказывается."""
    from pydantic import SecretStr

    from mcp_server.trusted_transport import trusted_tool
    from tests.test_trusted_transport import KEY, _token

    called = False

    async def propose(account: str) -> dict:
        nonlocal called
        called = True
        return {"account": account}

    async def allowed(actor):  # noqa: ARG001
        return True

    async def account_allowed(actor, account):  # noqa: ARG001
        return None

    monkeypatch.setattr(settings, "aimash_trust_hmac_key", SecretStr(KEY))
    monkeypatch.setattr("core.access.is_whitelisted", allowed)
    monkeypatch.setattr("core.access.ensure_account_allowed_for_user", account_allowed)
    wrapped = trusted_tool("action_test", propose)
    token = _token("action_test", {"account": DRAFT_ACCOUNT_ID}, now=1, expires=120)
    # Freeze verifier time inside the signed lifetime without weakening production code.
    monkeypatch.setattr("mcp_server.trusted_transport.time.time", lambda: 60)
    result = await wrapped(account=_FOREIGN_ID, trusted_turn_token=token)
    assert result["status"] == "refused"
    assert called is False


def test_i1_execution_binds_account_from_proposal_customer_id():
    """И1 (execution-half): аккаунт исполнения берётся из proposal.customer_id (штамп доверенного
    входа), а не из аргумента/контекста разговора, и ЗАНОВО проходит ensure_allowed — чужой/пустой
    штамп → PermissionError ДО SDK и ДО claim (одноразовый черновик не сожжён отказом замка).

    Глубокий DB/SDK-корпус — tests/test_execute_account_binding.py; здесь держим публичную сигнатуру:
    модель не может передать ни account, ни confirmation id, ни actor/reply."""
    import inspect

    from mcp_server.tools_write import execute_confirmed

    assert inspect.signature(execute_confirmed).parameters == {}


def test_i2_external_text_cannot_forge_financial_confirmation(monkeypatch):
    """Private profile allows planning after external reads, but the trusted token contains only the
    real Telegram event. External text still cannot forge a reply anchor or execute confirmation."""
    from tests.test_hermes_trusted_transport_plugin import _env, _event, _load

    plugin = _load(monkeypatch, _env())
    plugin._capture_gateway_event(event=_event())
    assert (
        plugin._pre_tool_call(
            tool_name="mcp__tavily__tavily_extract",
            args={"urls": ["https://example.test/injection"]},
            session_id="i2",
            turn_id="attack",
        )
        is None
    )
    args = {"account": DRAFT_ACCOUNT_ID, "campaign": "X"}
    assert (
        plugin._pre_tool_call(
            tool_name="mcp__aimash__pause_campaign",
            args=args,
            session_id="i2",
            turn_id="attack",
        )
        is None
    )
    assert "trusted_turn_token" in args
    execute_args = {}
    blocked = plugin._pre_tool_call(
        tool_name="mcp__aimash__execute_confirmed",
        args=execute_args,
        session_id="i2",
        turn_id="attack",
    )
    assert blocked["action"] == "block"
    assert "trusted_turn_token" not in execute_args


def test_i3_user_initiated_stamped_at_creation_never_by_confirmation():
    """И3: бит провенанса (`user_initiated` + `origin_human_turn`) штампует доверенный слой в момент
    СОЗДАНИЯ черновика — и только если ход триггернуло входящее сообщение человека из whitelist по
    доверенному каналу. Ни скил, ни cron, ни self-improvement fork его не выставляют; аргументом
    инструмента он не задаётся. Подтверждение человеком бит НЕ повышает.

    Глубокий DB-корпус — test_provenance_gate; здесь подтверждаем, что LLM-callable не принимает бит."""
    import inspect

    from confirm.store import ConfirmStore

    params = inspect.signature(ConfirmStore.save_proposal).parameters
    assert not ({"origin_human_turn", "author_user_id", "run_id"} & set(params))


def test_i5_self_written_skills_stay_reviewed_and_cannot_inline_shell():
    """И5 для private-profile: native tools доступны доверенным операторам, но самонаписанный
    markdown-скил не исполняет inline shell и остаётся в review-контуре."""
    import yaml

    cfg = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "deploy/hermes/config.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert cfg["skills"]["inline_shell"] is False
    assert cfg["skills"]["guard_agent_created"] is True
    assert cfg["skills"]["write_approval"] is True
    disabled = set(cfg["agent"]["disabled_toolsets"])
    assert {"terminal", "file", "code_execution", "web", "browser"}.isdisjoint(disabled)


def test_i6_private_profile_accepts_shared_history_but_account_recall_stays_scoped():
    """И6 осознанно ослаблен: все пользователи — доверенная внутренняя команда, поэтому Hermes
    memory/session_search общие. Проектная память клиента по-прежнему требует явный account."""
    import inspect
    import yaml

    from mcp_server.tools_read import recall_client

    cfg = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "deploy/hermes/config.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert cfg["memory"]["memory_enabled"] is True
    assert cfg["memory"]["user_profile_enabled"] is True
    assert "session_search" not in cfg["agent"]["disabled_toolsets"]
    assert "account" in inspect.signature(recall_client).parameters


def test_private_profile_external_content_does_not_phase_lock_tools(monkeypatch):
    """READ/web/client context and Ads planning may coexist in one private-operator turn."""
    from tests.test_hermes_trusted_transport_plugin import _env, _event, _load

    plugin = _load(monkeypatch, _env())
    plugin._capture_gateway_event(event=_event())
    plugin._pre_tool_call(
        tool_name="mcp__aimash__recall_client",
        args={"account": DRAFT_ACCOUNT_ID},
        session_id="i7",
        turn_id="old",
    )
    same_turn_args = {"account": DRAFT_ACCOUNT_ID, "campaign": "X"}
    assert (
        plugin._pre_tool_call(
            tool_name="mcp__aimash__pause_campaign",
            args=same_turn_args,
            session_id="i7",
            turn_id="old",
        )
        is None
    )
    assert "trusted_turn_token" in same_turn_args
    fresh_args = {"account": DRAFT_ACCOUNT_ID, "campaign": "X"}
    assert (
        plugin._pre_tool_call(
            tool_name="mcp__aimash__pause_campaign",
            args=fresh_args,
            session_id="i7",
            turn_id="new",
        )
        is None
    )
    assert "trusted_turn_token" in fresh_args


def test_i8_at_most_one_pending_proposal_per_turn():
    """И8: DB uniqueness, not a SELECT-before-INSERT pre-check, owns proposal races."""
    import inspect

    from db.models import Proposal
    from mcp_server.tools_write import ACTION_TOOL_FUNCS, _propose, propose_composite_change

    pending_index = next(
        index for index in Proposal.__table__.indexes if index.name == "ux_proposals_pending_run_id"
    )
    idempotency_index = next(
        index
        for index in Proposal.__table__.indexes
        if index.name == "ux_proposals_idempotency_key"
    )
    assert pending_index.unique is True
    assert idempotency_index.unique is True

    source = inspect.getsource(_propose)
    assert "count_run_pending_proposals" not in source
    assert "source_message_id=" in source
    assert "idempotency_args=" in source
    assert "store.confirm(" not in source
    ordinary = list(ACTION_TOOL_FUNCS.values())
    assert all("_propose(" in inspect.getsource(fn) for fn in ordinary)
    composite_source = inspect.getsource(propose_composite_change)
    assert "count_run_pending_proposals" not in composite_source
    assert "source_message_id=" in composite_source
    assert "idempotency_args=" in composite_source
