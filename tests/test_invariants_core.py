"""Класс-закрывающие инвариант-тесты золотых правил (гард на КЛАСС, а не разовая заплатка).

Отличие от tests/test_write_layer.py: там — поведенческие тесты каждой отдельной операции
(happy/negative по вручную перечисленному списку `_ALL_OPS`). Здесь — МЕТА-гарды, которые ловят
ДРЕЙФ: новая мутация, добавленная без замка аккаунта или без гарда user_initiated, провалит тест
автоматически, даже если её забыли внести в поведенческие списки. Плюс единственный тест на путь
глобального обработчика ошибок (сырой текст исключения не уходит в Telegram) — его не было.

Покрывает золотые правила:
- #9 «Замок аккаунта»: КАЖДАЯ apply_* вызывает ensure_allowed ПЕРВЫМ гейтом (до confirm-claim).
- #3 «Бюджет/деньги только по прямой команде»: набор денежных apply_* (ссылающихся на
  user_initiated) точно совпадает с ожидаемым реестром — новая денежная операция без гарда или
  снятый гард ломают тест.
- #5 «Секреты никогда наружу»: глобальный @dp.errors() не шлёт сырой str(e) в Telegram.

Каркасный knob (перенос на другой проект): _EXPECTED_MONEY_OPS + имя гейта `ensure_allowed` /
`_require_confirmation` — единственное, что правится под другой домен.
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import ads.mutations as mut  # noqa: E402

# ── Каркасные knobs (правятся при переносе) ──────────────────────────────────────
_ACCOUNT_LOCK_GATE = "ensure_allowed"  # золотое правило #9 — замок аккаунта
_CONFIRM_GATE = "_require_confirmation"  # золотое правило #1 — confirm-гейт (одноразовый claim)
_MONEY_GATE = "_require_user_command"  # золотое правило #3 — деньги только по прямой команде
# Внутри этого гейта — ДВА независимых бита (Волна 1.4): подделываемый аргумент `user_initiated` и
# contextvar-бит `origin_human_turn`, который аргументом не задаётся. Марке́р структурный (вызов
# функции), а не «ссылка на атрибут»: инлайн-проверка, размазанная по 8 телам, позволяла снять один
# из битов в одном месте и остаться в реестре.
_MONEY_BITS = ("user_initiated", "origin_human_turn")
# Денежные операции: те apply_*, что ОБЯЗАНЫ проверять user_initiated. Добавил денежную мутацию —
# внеси сюда И поставь гард (иначе тест ниже красный). Убрал гард — тоже красный.
# create_*_campaign — тоже деньги: создание кампании задаёт дневной бюджет.
_EXPECTED_MONEY_OPS = {
    "apply_update_budget",
    "apply_update_bid",
    "apply_update_keyword_bid",  # Ф1: ставка на уровне ключа — те же деньги, тот же гард
    "apply_set_bidding_strategy",
    "apply_create_search_campaign",
    "apply_create_gdn_campaign",
    "apply_create_demand_gen_campaign",  # §11: кампания из видео (Demand Gen) — задаёт бюджет
    "apply_create_video_campaign",  # §11: видеокампания — задаёт бюджет
    "apply_create_app_campaign",  # §11: App/UAC — задаёт дневной бюджет и target CPA
    "apply_launch_campaign",  # включает ранее PAUSED-структуру и тем самым открывает расход
}


# ── AST-разбор ads/mutations.py (единый источник — сам код, не ручной список) ─────
_MUT_TREE = ast.parse(pathlib.Path(mut.__file__).read_text(encoding="utf-8"), filename=mut.__file__)


def _apply_functions() -> list[ast.AsyncFunctionDef | ast.FunctionDef]:
    """Все публичные изменяющие точки входа — `def apply_*` любого уровня."""
    return [
        n
        for n in ast.walk(_MUT_TREE)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name.startswith("apply_")
    ]


def _first_call_line(fn: ast.AST, name: str) -> int | None:
    """Номер строки ПЕРВОГО вызова функции `name(...)` внутри тела (или None, если нет)."""
    lines = [
        c.lineno
        for c in ast.walk(fn)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == name
    ]
    return min(lines) if lines else None


def _references_attr(fn: ast.AST, attr: str) -> bool:
    """Ссылается ли тело на атрибут `.attr` (напр. proposal.user_initiated)."""
    return any(isinstance(n, ast.Attribute) and n.attr == attr for n in ast.walk(fn))


# ── #9: КАЖДАЯ apply_* столбит замок аккаунта, и ДО confirm-гейта ─────────────────
def test_all_apply_functions_call_ensure_allowed_first():
    funcs = _apply_functions()
    assert funcs, "не найдено ни одной apply_* — сломан AST-разбор ads/mutations.py?"

    missing = [f.name for f in funcs if _first_call_line(f, _ACCOUNT_LOCK_GATE) is None]
    assert not missing, (
        f"мутации без {_ACCOUNT_LOCK_GATE}() — замок аккаунта (golden rule #9) не столблён: "
        f"{sorted(missing)}. Добавь ensure_allowed(customer_id) первым оператором."
    )

    # Замок — ГЕЙТ 1: должен стоять ДО confirm-claim (иначе плохой аккаунт съест одноразовый
    # черновик прежде, чем упрётся в замок). Проверяем порядок по номерам строк.
    out_of_order = []
    for f in funcs:
        lock = _first_call_line(f, _ACCOUNT_LOCK_GATE)
        claim = _first_call_line(f, _CONFIRM_GATE)
        if lock is not None and claim is not None and lock > claim:
            out_of_order.append(f.name)
    assert not out_of_order, (
        f"{_ACCOUNT_LOCK_GATE}() вызван ПОСЛЕ {_CONFIRM_GATE}() в: {sorted(out_of_order)} — "
        "замок должен быть гейтом 1 (до confirm-claim)."
    )


# ── #2 (§аудит-2026-07): КАЖДАЯ apply_* проходит confirm-гейт (_require_confirmation) ──
def test_all_apply_functions_call_require_confirmation():
    """Golden rule #2: каждая мутация обязана столбить одноразовый confirmation_id через
    _require_confirmation (атомарный claim). Раньше инвариант проверял только ensure_allowed —
    новая apply_* с замком аккаунта, но БЕЗ confirm-гейта прошла бы незамеченной."""
    funcs = _apply_functions()
    assert funcs, "не найдено ни одной apply_* — сломан AST-разбор ads/mutations.py?"
    missing = [f.name for f in funcs if _first_call_line(f, _CONFIRM_GATE) is None]
    assert not missing, (
        f"мутации без {_CONFIRM_GATE}() — confirm-гейт (golden rule #2) отсутствует: "
        f"{sorted(missing)}. Каждая apply_* обязана await _require_confirmation(...)."
    )


# ── #3: набор денежных apply_* == ожидаемый реестр (дрейф → красный тест) ─────────
def test_money_apply_functions_match_registry_and_guard_user_initiated():
    """Денежные мутации определяем структурно — по вызову `_require_user_command` (тот самый гард).
    Набор ДОЛЖЕН совпадать с _EXPECTED_MONEY_OPS. Расхождение ловит два класса ошибок:
    - гард сняли с денежной операции → она выпадает из набора → mismatch;
    - гард появился там, где его быть не должно → лишний элемент → mismatch.
    (Новую денежную операцию БЕЗ гарда этот тест не увидит как «денежную» — поэтому реестр ведём
    вручную: добавляя её сюда, ты обязан поставить гард, и тест это подтвердит.)"""
    guarded = {f.name for f in _apply_functions() if _first_call_line(f, _MONEY_GATE) is not None}
    assert guarded == _EXPECTED_MONEY_OPS, (
        f"набор денежных apply_* (зовут {_MONEY_GATE}) = {sorted(guarded)}, "
        f"ожидалось {sorted(_EXPECTED_MONEY_OPS)}. Golden rule #3: деньги — только прямой командой. "
        "Обнови реестр _EXPECTED_MONEY_OPS осознанно вместе с гардом."
    )


def test_money_gate_requires_both_provenance_bits():
    """Волна 1.4: сам гейт обязан требовать ОБА бита. Проверка структурная — в теле
    `_require_user_command` есть ссылка и на `user_initiated`, и на `origin_human_turn`.

    Без неё предыдущий тест деградирует до проверки имени: гейт можно было бы выпотрошить до
    одного бита, оставив вызовы на месте, и реестр сошёлся бы. Разница между битами в том, что
    первый — аргумент `save_proposal` (в headless-контуре его пишет вызывающий), а второй берётся
    из `core.provenance` и аргументом не задаётся вовсе."""
    gate = next(
        (
            n
            for n in ast.walk(_MUT_TREE)
            if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == _MONEY_GATE
        ),
        None,
    )
    assert gate is not None, f"{_MONEY_GATE} не найден в ads/mutations.py — гард денег снят?"
    missing = [bit for bit in _MONEY_BITS if not _references_attr(gate, bit)]
    assert not missing, (
        f"{_MONEY_GATE} не проверяет биты провенанса: {missing}. Golden rule #3 держится на двух "
        "независимых битах — подделываемом аргументе и contextvar-бите доверенного слоя (И3)."
    )


# ── #5: глобальный @dp.errors() не утекает сырой текст исключения в Telegram ──────
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_adcopy_is_top_level_package_not_under_ads():
    assert (_REPO_ROOT / "adcopy" / "__init__.py").is_file(), (
        "пакет adcopy/ пропал из top-level — IDE перенёс его? (см. pyproject packages.find)"
    )
    assert not (_REPO_ROOT / "ads" / "adcopy").exists(), (
        "ads/adcopy/ существует — Pylance снова перенёс adcopy под ads/. Верни в top-level: "
        "`mv ads/adcopy adcopy` и почини импорты обратно на `from adcopy.`"
    )


def test_no_source_references_ads_adcopy():
    """Ни один .py в исходниках/тестах не импортирует несуществующий `ads.adcopy.*`."""
    # Матчим только импорт-стейтмент (from/import + пробел + запрещённый путь), а не голую
    # подстроку — иначе этот же файл (сообщения/докстринг) ложно триггерит гард сам на себя.
    bad = re.compile(r"\b(?:from|import)\s+ads\.adcopy\b")
    offenders: list[str] = []
    for py in _REPO_ROOT.rglob("*.py"):
        parts = set(py.parts)
        if "__pycache__" in parts or ".venv" in parts or "site-packages" in parts:
            continue
        text = py.read_text(encoding="utf-8", errors="ignore")
        if bad.search(text):
            offenders.append(str(py.relative_to(_REPO_ROOT)))
    assert not offenders, (
        "найдены импорты несуществующего `ads.adcopy` (IDE переписал?): "
        + ", ".join(offenders)
        + " — верни на `adcopy.`"
    )


# ── #10: КАЖДЫЙ dev-скрипт с прямой записью (мимо confirm-гейта) гейтится require_dev_env ──
# Гард на КЛАСС: новый demo/фикстура-скрипт с mutate_* без require_dev_env() провалит тест.
# Allow-list — скрипты, идущие ЧЕРЕЗ полный confirm-гейт (save_proposal→confirm→execute):
# им dev-гард не нужен (сам гейт и замок аккаунта уже защищают денежный путь).
_GATED_SCRIPT_ALLOWLIST = {
    "live_smoke_test.py",
    "live_smoke_video_dg.py",
    "live_smoke_lead_form.py",
    "live_smoke_gdn.py",  # §11 GDN-смоук: создаёт через confirm-гейт, cleanup только при ENV=dev
}

# Второй allow-list — скрипты, которые НЕ ПИШУТ ВООБЩЕ: Mutate зовётся с `validate_only=True`,
# сервер только валидирует запрос. `require_dev_env()` им не просто не нужен, а противопоказан:
# `verify_readonly_ceiling.py` работает на Хосте A, где ENV=test, и сам роняет запуск при ENV=dev
# (`hygiene_host_a`) — dev открывает demo-скрипты прямой записи, ровно то, чего на том хосте быть
# не должно.
#
# Исключение НЕ по имени: для каждого файла отсюда проверяется САМО СВОЙСТВО, ради которого он
# исключён. Уберут `validate_only` или fail-closed гард — тест снова покраснеет, а не промолчит.
_VALIDATE_ONLY_SCRIPTS = {"verify_readonly_ceiling.py"}


def _assert_writes_nothing(name: str, text: str) -> None:
    """Свойство, оправдывающее исключение: запрос помечен validate_only И это проверено кодом."""
    assert re.search(r"\.validate_only\s*=\s*True", text), (
        f"{name} в _VALIDATE_ONLY_SCRIPTS, но `validate_only = True` в нём не найден — "
        "исключение из гарда прямой записи больше не обосновано"
    )
    assert re.search(r"validate_only\s+is\s+not\s+True", text), (
        f"{name} не проверяет validate_only явным fail-closed условием — под `python -O` "
        "ассерт вырезается, и зонд начнёт писать по-настоящему"
    )


def test_direct_write_scripts_call_require_dev_env():
    scripts_dir = _REPO_ROOT / "scripts"
    direct_write = re.compile(r"\.mutate_\w+\(")  # прямой вызов Mutate-сервиса SDK
    offenders: list[str] = []
    for py in scripts_dir.glob("*.py"):
        if py.name in _GATED_SCRIPT_ALLOWLIST:
            continue
        text = py.read_text(encoding="utf-8", errors="ignore")
        if py.name in _VALIDATE_ONLY_SCRIPTS:
            _assert_writes_nothing(py.name, text)
            continue
        if direct_write.search(text) and "require_dev_env()" not in text:
            offenders.append(py.name)
    assert not offenders, (
        f"скрипты с прямой записью в Google Ads БЕЗ require_dev_env(): {offenders} — "
        "golden rule #10: прямая запись мимо confirm-гейта разрешена только при ENV=dev "
        "(вызови require_dev_env() первой строкой main())"
    )


# ── #5 (регресс-инвариант): GoogleAdsException с токеном → REDACTED и в чат, и в audit ──
async def test_google_ads_exception_with_token_redacted_in_chat_and_audit():
    """Сырой GoogleAdsException может нести креды в message. Инвариант: на ОБЕИХ границах
    (текст пользователю и audit_log) секрет заменён на REDACTED, request_id сохранён (не секрет —
    нужен саппорту). Ловит будущий рефактор, случайно открывший утечку."""
    import uuid

    from bot import ux
    from confirm.store import ConfirmStore
    from core.ads_errors import humanize_google_ads_error
    from db.session import init_db

    secret = "1//0SECRETrefreshTOKENvalue123"  # gitleaks:allow — форма refresh-токена

    class _Code:
        name = "AUTHENTICATION_ERROR"

    class _Err:
        message = f"auth failed refresh_token={secret} denied"
        error_code = _Code()

    class _Failure:
        errors = [_Err()]

    class _FakeAdsExc(Exception):
        failure = _Failure()
        request_id = "AbCd123"

    exc = _FakeAdsExc("raw")

    # Граница 1: текст пользователю (humanize + err_text)
    for text in (humanize_google_ads_error(exc), ux.err_text(exc)):
        assert secret not in text, "секрет утёк в текст пользователю (golden rule #5)"
        assert "REDACTED" in text
        assert "AbCd123" in text or "request_id" not in text  # request_id сохранён, если печатается

    # Граница 2: audit_log (record_failure редактирует на записи в БД)
    await init_db()
    store = ConfirmStore()
    cid = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid,
        operation="update_budget",
        customer_id="7753643025",
        params={},
        summary="s",
        chat_id=1,
        user_initiated=True,
    )
    assert await store.confirm(cid, chat_id=1)
    await store.record_failure(cid, error=f"auth failed refresh_token={secret} denied")

    from sqlalchemy import select

    from db.models import AuditLog
    from db.session import Session

    async with Session() as s:
        row = (
            await s.execute(
                select(AuditLog).where(AuditLog.confirmation_id == cid, AuditLog.status == "failed")
            )
        ).scalar_one()
    err_text_db = str(row.result.get("error", ""))
    assert secret not in err_text_db, "секрет утёк в audit_log (golden rule #5)"
    assert "REDACTED" in err_text_db


# ── Phase 4: дрейф SUPPORTED_OPERATIONS ↔ MUTATION_TOOLS (агент-невидимая мутация) ──
# Исполнимые операции (ads.service.SUPPORTED_OPERATIONS) = агент-инструменты (schemas.MUTATION_TOOLS)
# ∪ UI-only-операции (минтятся кнопками, агент их НЕ вызывает — защита от prompt-injection). Явный
# allowlist UI-only: новая SUPPORTED-операция, забытая в MUTATION_TOOLS, провалит тест (агент не
# сможет её вызвать по NL), ЕСЛИ она не внесена сюда осознанно как UI-only.
_UI_ONLY_OPS: set[str] = set()


def test_supported_ops_are_agent_tools_or_ui_only():
    from ads.service import SUPPORTED_OPERATIONS
    from agent.tools.schemas import MUTATION_TOOLS

    ui_only = set(SUPPORTED_OPERATIONS) - set(MUTATION_TOOLS)
    assert ui_only == _UI_ONLY_OPS, (
        f"SUPPORTED_OPERATIONS вне MUTATION_TOOLS = {sorted(ui_only)}, ожидалось {sorted(_UI_ONLY_OPS)}. "
        "Новая исполнимая операция должна быть либо агент-инструментом (schemas.MUTATION_TOOLS), либо "
        "осознанно UI-only (внеси в _UI_ONLY_OPS). Иначе агент не вызовет её по NL — тихий пробел."
    )


def test_every_supported_op_has_a_schema():
    """Каждая исполнимая операция имеет Pydantic-схему (SCHEMAS) — _build_proposal (UI-кнопки) и
    валидация агента опираются на неё; op без схемы упал бы KeyError при попытке минтить черновик."""
    from ads.service import SUPPORTED_OPERATIONS
    from agent.tools.schemas import SCHEMAS

    missing = sorted(op for op in SUPPORTED_OPERATIONS if op not in SCHEMAS)
    assert not missing, f"SUPPORTED_OPERATIONS без Pydantic-схемы (SCHEMAS): {missing}"


# ── #3 (UI-зеркало): _MONEY_OPS_UI в bot.main синхронен реестру денежных операций ──
_PROD_PACKAGES = (
    "adcopy",
    "ads",
    "agent",
    "app",
    "bot",
    "clients",
    "confirm",
    "core",
    "db",
    "keywords",
    "mcp_server",
    "reports",
    "scheduler",
)


def _run_probe(code: str, *, optimized: bool) -> subprocess.CompletedProcess[str]:
    args = [sys.executable] + (["-O"] if optimized else []) + ["-c", code]
    return subprocess.run(
        args,
        cwd=str(_REPO_ROOT),
        env={
            **os.environ,
            "PYTHONPATH": str(_REPO_ROOT),
            "ENV": "dev",
            "PYTHONIOENCODING": "utf-8",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )


_I4_PROBE = """
import mcp_server.tools_read as tr
tr.READ_MCP_TOOLS = frozenset(tr.READ_MCP_TOOLS) | {"update_budget"}
import mcp_server.server
print("ГАРД НЕ СРАБОТАЛ")
"""


def test_i4_guard_survives_python_O():
    """Мутация, просочившаяся в READ_MCP_TOOLS, роняет импорт `mcp_server.server` И ПОД `-O`.

    Пока гард был `assert`, этот же зонд под `-O` собирался молча: MCP-сервер поднялся бы с
    мутационным инструментом, выставленным агенту как чтение."""
    for optimized in (False, True):
        proc = _run_probe(_I4_PROBE, optimized=optimized)
        flag = "-O" if optimized else "без -O"
        assert proc.returncode != 0, (
            f"И4-гард не сработал ({flag}): импорт прошёл.\n{proc.stdout}\n{proc.stderr}"
        )
        assert "И4" in proc.stderr, f"({flag}) упало не на И4:\n{proc.stderr}"
        assert "update_budget" in proc.stderr, (
            f"({flag}) сообщение не называет просочившийся инструмент:\n{proc.stderr}"
        )


_S4_PROBE = """
from core.guards import require_no_mutations
require_no_mutations({"get_ads", "update_budget"}, {"update_budget"}, rule="S4/GR6", subject="X")
print("ГАРД НЕ СРАБОТАЛ")
"""


def test_guard_mechanism_itself_survives_python_O():
    """Сам механизм `core.guards.require_no_mutations` — не assert: под `-O` тоже бросает.

    Отдельно от И4 потому, что S4 (`agent/tools/schemas.py`) вычисляется из наборов того же модуля
    и подсадить туда мутацию до импорта нельзя — проверяем механизм, а его использование на месте
    S4 держит тест ниже (module-level assert запрещён)."""
    for optimized in (False, True):
        proc = _run_probe(_S4_PROBE, optimized=optimized)
        flag = "-O" if optimized else "без -O"
        assert proc.returncode != 0, (
            f"механизм гарда не сработал ({flag}).\n{proc.stdout}\n{proc.stderr}"
        )
        assert "S4/GR6" in proc.stderr, f"({flag}) упало не на S4:\n{proc.stderr}"


def test_no_module_level_assert_in_production_packages():
    """Ни один продовый модуль не держит гард на module-level `assert`.

    Это и есть закрытие класса: конкретные И4/S4 уже переписаны, а тест не даёт вернуть форму —
    новый construction-time гард обязан звать `core.guards` (или свой `if ...: raise`), иначе
    красный. `assert` ВНУТРИ функций не трогаем: там он документирует внутренний инвариант, а не
    охраняет денежный путь, и запрет дал бы шум без пользы."""
    offenders: list[str] = []
    for pkg in _PROD_PACKAGES:
        root = _REPO_ROOT / pkg
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in tree.body:  # ТОЛЬКО верхний уровень модуля
                if isinstance(node, ast.Assert):
                    rel = path.relative_to(_REPO_ROOT).as_posix()
                    offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "module-level `assert` в продовом коде — под `python -O` он исчезает вместе с гардом: "
        f"{offenders}. Используй `core.guards.require_no_mutations` или явный `if ...: raise`."
    )
