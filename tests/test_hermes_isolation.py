"""Гарды изоляции разрешений от памяти/скилов/external-контента (пивот Hermes, §4 — И1…И8).

Инкремент «MCP READ» несёт ТОЛЬКО read-релевантное зерно инвариантов; полные И1–И8 + injection-корпус
— шаг ПЕРЕД WRITE (deploy/hermes/HERMES_SPEC.md §4, дорожная карта шаги 2/15). Здесь живыми проверяются:

  • **И4 (зерно)** — construction-time assert в `mcp_server.server`: READ-инструменты физически не
    пересекаются с мутационными (`agent.tools.schemas.MUTATION_TOOLS`). Импорт роняет процесс, если
    мутация просочилась в read-фазу. Тот же паттерн S4, что защищает `ANALYSIS_TOOLS`.
  • **read-lock на границе MCP** — параметризованно по ВСЕМ обёрткам: инструмент на аккаунте вне
    allow-list отказывает ДО первого обращения наружу (все ридеры застаблены взрывом), отдаёт
    редактированный error-конверт с `error_code == "forbidden_account"` — не сырое исключение и не
    данные; обратная половина доказывает, что замок пропускает разрешённый аккаунт.

Остальные И1–И3/И5–И8 — каркас со `skip("шаг перед WRITE")` с ДОСЛОВНОЙ формулировкой инварианта,
чтобы файл рос, а не переписывался, и следующий шаг наполнил их корпусом атак (инлайн, как
tests/test_dossier.py / test_export_formula_injection.py).

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

    from agent.tools.schemas import MUTATION_TOOLS
    from mcp_server.tools_read import READ_MCP_TOOLS

    assert READ_MCP_TOOLS, "реестр READ-инструментов пуст"
    assert READ_MCP_TOOLS.isdisjoint(MUTATION_TOOLS), (
        "И4: READ-инструменты MCP пересеклись с мутационными: "
        f"{sorted(READ_MCP_TOOLS & MUTATION_TOOLS)}"
    )
    # 25 = 24 Google Ads READ (в т.ч. §8 MCC: get_mcc_summary/get_mcc_deep и служебный get_quota —
    # счётчик наш, Ads не опрашивается) + recall_client (память клиента §20). Точный счёт держит
    # реестр от тихого разрастания: новая обёртка обязана осознанно бампнуть его вместе с
    # config.yaml/_ACCOUNT_ARG.
    assert len(READ_MCP_TOOLS) == 25, f"ожидалось 25 READ-инструментов, стало {len(READ_MCP_TOOLS)}"


def test_i4_seed_server_builds_and_registers_only_read():
    # Сервер строится и регистрирует РОВНО READ-набор — ни одного лишнего/мутационного имени.
    from mcp_server.server import build_server
    from mcp_server.tools_read import READ_MCP_TOOLS

    srv = build_server()
    names = {t.name for t in asyncio.run(srv.list_tools())}
    assert names == set(READ_MCP_TOOLS), f"реестр FastMCP разошёлся с READ_MCP_TOOLS: {names}"


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
    "get_campaign_stats": "account",
    "get_adgroup_stats": "account",
    "get_keywords": "account",
    "get_ads": "account",
    "get_search_terms": "account",
    "get_negatives": "account",
    "get_budgets": "account",
    "get_auction_insights": "account",
    "get_account_audit": "account",
    "get_change_history": "account",
    "get_account_changes": "account",  # Р6: журнал правок Google (НЕ наш audit-trail)
    "keyword_ideas": "account",
    "list_campaigns": "account",
    "read_campaign_targeting": "account",
    "read_campaign_config": "account",
    "get_bidding_strategy": "account",
    "get_report_breakdown": "account",
    "list_audiences": "account",
    "list_attached_audiences": "account",
    "get_quota": "account",  # ридер НАШЕГО счётчика (core.quota) — замок только на границе
    "recall_client": "account",  # ридер НАШЕЙ БД (ClientProfileStore) — замок только на границе
    # §8 MCC: адресуют не лист, а УПРАВЛЯЮЩИЙ аккаунт — тот же чокпойнт, что у list_accounts
    # (ensure_manager_allowed). Дочерние аккаунты внутри обхода фильтрует сам `reports/mcc.py`
    # по read-allow-листу, поэтому граница проверяет ровно то, что адресовал вызывающий.
    "get_mcc_summary": "manager_id",
    "get_mcc_deep": "manager_id",
    "list_negative_shared_sets": "account",
}


# Обязательные НЕ-аккаунтные аргументы обёрток. Без них вызов не собрался бы вовсе (TypeError), а
# «вызов не собрался» неотличимо от «замок отказал» — инвариант проверял бы синтаксис, не замок.
# Значения фиктивные и до ридера не доезжают ни в одной половине: в первой отказывает замок, во
# второй взрывается стаб. Полноту словаря держит `test_required_args_are_declared_for_lock_invariant`.
_EXTRA_ARGS: dict[str, dict[str, object]] = {
    "read_campaign_targeting": {"campaign_id": "1"},
    "read_campaign_config": {"campaign_name": "X"},
    "list_attached_audiences": {"campaign_id": "1"},
    # dimension валидируется ДО выхода наружу — фиктивное имя дало бы invalid_argument вместо
    # internal, то есть обратная половина инварианта не доказала бы прохода замка. Нужно допустимое.
    "get_report_breakdown": {"dimension": "device"},
}


class _ReaderCalled(RuntimeError):
    """Стаб выхода наружу (SDK/БД). Прилетел ⇒ тело инструмента исполнилось, замок пропустил."""


@contextmanager
def _readers_explode():
    """Заменить ВСЕ выходы `tools_read` наружу на взрыв. Замок обязан сработать раньше любого из них;
    заодно тест офлайн и детерминирован (ни SDK, ни БД не поднимаются)."""
    from mcp_server import tools_read as tr

    names = (
        "build_client_async",
        "run_ads_read_call",
        "gather_audit",
        "list_recent_applied_by_customer",
        "ClientProfileStore",  # recall_client: ридер НАШЕЙ БД — тоже за границу, тест офлайн
        "quota_snapshot",  # get_quota: Ads не опрашивает, но за границу (наша БД) выходит
    )
    saved = {n: getattr(tr, n) for n in names}

    def _boom(*_a, **_kw):
        raise _ReaderCalled("ридер вызван — замок не отработал до выхода наружу")

    for n in names:
        setattr(tr, n, _boom)
    try:
        yield
    finally:
        for n, fn in saved.items():
            setattr(tr, n, fn)


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

    assert env["error_code"] == "internal", (
        f"{tool_name}: разрешённый аккаунт не дошёл до ридера (код {env['error_code']!r}) — "
        "замок отказывает всем, инвариант отказа выше это не поймал бы"
    )


def test_reference_hermes_config_exposes_exactly_the_read_registry():
    """Четвёртое место lock-step: `tools.include` в `~/.hermes/config.yaml`.

    Это ВТОРОЙ allow-list, живущий вне репозитория: инструмент, зарегистрированный в FastMCP, но не
    вписанный туда, агенту НЕВИДИМ — то есть работа сделана, а функции у профессионала нет. Тест
    держит эталон `deploy/hermes/config.yaml` синхронным с реестром; копию на VPS он не видит, но
    расхождение эталона ловит до деплоя, а список для ручной правки печатает в сообщении.
    (`yaml` берём из транзитивной зависимости `google-ads` — новой зависимости не заводим.)
    """
    import yaml

    from mcp_server.tools_read import READ_MCP_TOOLS

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
    missing, extra = sorted(set(READ_MCP_TOOLS) - include), sorted(include - set(READ_MCP_TOOLS))
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


# ── Каркас полных инвариантов И1–И8 (наполняется шагом ПЕРЕД WRITE) ──────────────────
# Формулировки — дословно из deploy/hermes/HERMES_SPEC.md §4. Корпус атак (client_site_pages, скилы в
# shell/HTTP/файлы, страницы конкурентов) — инлайн, как в существующих injection-тестах.

_WRITE_STEP = (
    "наполняется шагом ПЕРЕД WRITE (deploy/hermes/HERMES_SPEC.md §4, дорожная карта шаги 2/15)"
)
# И3 — исключение: инвариант УЖЕ проверен живьём в tests/test_provenance_gate.py (на настоящем
# ConfirmStore). Заглушка держит дословную формулировку в общем файле изоляции, но не «ждёт WRITE».
_I3_COVERED = "живьём покрыт tests/test_provenance_gate.py — заглушка держит формулировку И3"
# И1 расщеплён на две половины с разным статусом. Execution-half (замок на исполнении) — УЖЕ живой
# в tests/test_execute_account_binding.py на настоящем ConfirmStore: execute_confirmed берёт аккаунт
# из proposal.customer_id и заново проходит ensure_allowed до SDK и до claim. Creation-half (внешний
# источник не подменяет customer_id при СОЗДАНИИ черновика) ждёт propose-surface (шаг перед WRITE).
_I1_EXEC_COVERED = (
    "живьём покрыт tests/test_execute_account_binding.py "
    "(test_execute_confirmed_uses_proposal_customer_id / _foreign_customer_id_denied / "
    "_empty_stamp_fail_closed) — заглушка держит формулировку execution-half И1"
)


@pytest.mark.skip(reason=_WRITE_STEP)
def test_i1_external_source_cannot_change_proposal_customer_id():
    """И1 (creation-half): ни скил/память/факт/текст с сайта клиента не меняет customer_id в proposal
    при СОЗДАНИИ черновика. Ждёт propose-surface (MCP PLAN/propose — шаг перед WRITE): пока черновики
    рождает только кнопочный слой, инъекции в customer_id на создании неоткуда взяться в тесте.
    Execution-half того же инварианта — отдельным тестом ниже, уже живым."""


@pytest.mark.skip(reason=_I1_EXEC_COVERED)
def test_i1_execution_binds_account_from_proposal_customer_id():
    """И1 (execution-half): аккаунт исполнения берётся из proposal.customer_id (штамп доверенного
    входа), а не из аргумента/контекста разговора, и ЗАНОВО проходит ensure_allowed — чужой/пустой
    штамп → PermissionError ДО SDK и ДО claim (одноразовый черновик не сожжён отказом замка).

    Живьём УЖЕ проверен — не здесь, а в tests/test_execute_account_binding.py (настоящий ConfirmStore
    + temp SQLite): _uses_proposal_customer_id (штамп доезжает до apply_*), _foreign_customer_id_denied
    (чужой id вне ALLOWED_CEILING → PermissionError, apply_* не вызван, claim цел), _empty_stamp_fail_
    closed (пустой штамп НЕ откатывается молча на Draft). Заглушка держит дословную формулировку
    execution-half И1 в общем файле изоляции; дублировать покрытие незачем (YAGNI)."""


@pytest.mark.skip(reason=_WRITE_STEP)
def test_i2_injection_text_creates_no_proposal_and_no_user_initiated():
    """И2: внешний текст с инструкцией («игнорируй указания»/«подтверди операцию») не создаёт proposal
    и не выставляет user_initiated. Корпус атак, включая client_site_pages."""


@pytest.mark.skip(reason=_I3_COVERED)
def test_i3_user_initiated_stamped_at_creation_never_by_confirmation():
    """И3: бит провенанса (`user_initiated` + `origin_human_turn`) штампует доверенный слой в момент
    СОЗДАНИЯ черновика — и только если ход триггернуло входящее сообщение человека из whitelist по
    доверенному каналу. Ни скил, ни cron, ни self-improvement fork его не выставляют; аргументом
    инструмента он не задаётся. Подтверждение человеком бит НЕ повышает.

    Живьём этот инвариант УЖЕ проверен — не здесь, а в tests/test_provenance_gate.py (на настоящем
    ConfirmStore + temp SQLite): выпускной гейт «машинный черновик, подтверждённый живым человеком,
    всё равно даёт PermissionError», оба бита обязательны ни один недостаточен, `save_proposal` не
    принимает провенанс аргументом, а `human_turn(` имеет мета-гард allow-list'а call-site'ов.
    Заглушка оставлена как якорь дословной формулировки И3 в общем файле изоляции; дублировать
    покрытие незачем (YAGNI). Прежняя дырявая редакция §4 HERMES_SPEC.md («выставляется по реплай-
    подтверждению живого человека») уже переписана — правка И3 в §4 приземлилась.
    """


@pytest.mark.skip(reason=_WRITE_STEP)
def test_i5_self_written_skill_calls_only_registered_mcp_tools():
    """И5: самонаписанный скил не может вызвать ничего, кроме зарегистрированных MCP-инструментов.
    Корпус скилов, пытающихся выйти в shell/HTTP/файлы."""


@pytest.mark.skip(reason=_WRITE_STEP)
def test_i6_memory_and_history_search_filtered_by_client_id():
    """И6: поиск по памяти и истории фильтруется по client_id текущего топика (изоляция клиента A от
    топика клиента B — session_search в Hermes ищет по всей БД)."""


@pytest.mark.skip(reason=_WRITE_STEP)
def test_i7_external_content_taints_turn_and_disables_mutations():
    """И7: в ходе, где прочитан external-контент (страница/досье/CSV конкурентов), MCP-мутации
    физически недоступны; таинт по thread_id, снимается только сигналом нового человеческого сообщения."""


@pytest.mark.skip(reason=_WRITE_STEP)
def test_i8_at_most_one_pending_proposal_per_turn():
    """И8: не более одного pending proposal на ассистентский ход. Энфорсмент — счётчик pending в нашем
    MCP-слое (на прогон/тред), не надежда на ограничения модели."""
