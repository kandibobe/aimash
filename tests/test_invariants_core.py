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
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import ads.mutations as mut  # noqa: E402

# ── Каркасные knobs (правятся при переносе) ──────────────────────────────────────
_ACCOUNT_LOCK_GATE = "ensure_allowed"  # золотое правило #9 — замок аккаунта
_CONFIRM_GATE = "_require_confirmation"  # золотое правило #1 — confirm-гейт (одноразовый claim)
_MONEY_MARKER = "user_initiated"  # золотое правило #3 — деньги только по прямой команде
# Денежные операции: те apply_*, что ОБЯЗАНЫ проверять user_initiated. Добавил денежную мутацию —
# внеси сюда И поставь гард (иначе тест ниже красный). Убрал гард — тоже красный.
# create_*_campaign — тоже деньги: создание кампании задаёт дневной бюджет.
_EXPECTED_MONEY_OPS = {
    "apply_update_budget",
    "apply_update_bid",
    "apply_set_bidding_strategy",
    "apply_create_search_campaign",
    "apply_create_gdn_campaign",
    "apply_create_demand_gen_campaign",  # §11: кампания из видео (Demand Gen) — задаёт бюджет
    "apply_create_video_campaign",  # §11: видеокампания — задаёт бюджет
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
    """Денежные мутации определяем структурно — по ссылке на `user_initiated` (тот самый гард).
    Набор ДОЛЖЕН совпадать с _EXPECTED_MONEY_OPS. Расхождение ловит два класса ошибок:
    - гард сняли с денежной операции → она выпадает из набора → mismatch;
    - гард появился там, где его быть не должно → лишний элемент → mismatch.
    (Новую денежную операцию БЕЗ гарда этот тест не увидит как «денежную» — поэтому реестр ведём
    вручную: добавляя её сюда, ты обязан поставить гард, и тест это подтвердит.)"""
    guarded = {f.name for f in _apply_functions() if _references_attr(f, _MONEY_MARKER)}
    assert guarded == _EXPECTED_MONEY_OPS, (
        f"набор денежных apply_* (ссылаются на {_MONEY_MARKER}) = {sorted(guarded)}, "
        f"ожидалось {sorted(_EXPECTED_MONEY_OPS)}. Golden rule #3: деньги — только прямой командой. "
        "Обнови реестр _EXPECTED_MONEY_OPS осознанно вместе с гардом."
    )


# ── #5: глобальный @dp.errors() не утекает сырой текст исключения в Telegram ──────
async def test_on_error_handler_does_not_leak_secret_to_telegram(monkeypatch):
    """Необработанное исключение с секрето-подобным текстом → пользователю уходит НЕЙТРАЛЬНОЕ
    сообщение с кодом инцидента, а не str(e). capture_exception (БД/Sentry) подменяем — тестируем
    именно путь уведомления."""
    import bot.main as bm

    secret = "1//0SECRETrefreshTOKENvalue123"  # gitleaks:allow — форма refresh-токена
    incident_code = "INC-TEST-42"

    async def _fake_capture(exc, where="handler"):
        return incident_code

    monkeypatch.setattr(bm, "capture_exception", _fake_capture)

    sent: dict[str, str] = {}

    class _Msg:
        async def answer(self, text, **kw):
            sent["text"] = text

    class _Update:
        message = _Msg()
        callback_query = None

    class _Event:
        exception = RuntimeError(f"google-ads auth failed refresh_token={secret} denied")
        update = _Update()

    handled = await bm.on_error(_Event())

    assert handled is True  # aiogram: ошибка «обработана», не падает в stderr
    text = sent.get("text", "")
    assert text, "пользователю ничего не отправлено — уведомление о сбое пропало"
    assert secret not in text, "СЕКРЕТ утёк в Telegram (golden rule #5)"
    assert "refresh_token" not in text, "сырой текст исключения (str(e)) утёк в Telegram"
    assert incident_code in text, "нет кода инцидента — пользователь не сможет сослаться на /diag"


# ── Раскладка пакета `adcopy` — top-level, НЕ под ads/ (гард на IDE-дрейф) ─────────
# Pylance периодически «услужливо» переносит adcopy/ внутрь ads/ (ads↔adcopy — общий
# префикс) и переписывает импорты на `ads.adcopy.*`, что рушит рантайм (adcopy — плоский
# top-level пакет, см. pyproject setuptools include). Раньше ловилось только глазами по
# ModuleNotFoundError. Теперь — красный тест до отгрузки. Правится в одном месте при
# осознанном переезде пакета.
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
_GATED_SCRIPT_ALLOWLIST = {"live_smoke_test.py", "live_smoke_video_dg.py"}


def test_direct_write_scripts_call_require_dev_env():
    scripts_dir = _REPO_ROOT / "scripts"
    direct_write = re.compile(r"\.mutate_\w+\(")  # прямой вызов Mutate-сервиса SDK
    offenders: list[str] = []
    for py in scripts_dir.glob("*.py"):
        if py.name in _GATED_SCRIPT_ALLOWLIST:
            continue
        text = py.read_text(encoding="utf-8", errors="ignore")
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


# ── #3 (UI-зеркало): _MONEY_OPS_UI в bot.main синхронен реестру денежных операций ──
def test_money_ops_ui_mirror_matches_registry():
    """bot.main._MONEY_OPS_UI (предупреждение о внешнем контенте) обязан зеркалить
    _EXPECTED_MONEY_OPS (без префикса apply_) — дрейф реестров ловится здесь."""
    import bot.main as bm

    expected = {name.removeprefix("apply_") for name in _EXPECTED_MONEY_OPS}
    assert set(bm._MONEY_OPS_UI) == expected, (
        f"_MONEY_OPS_UI={sorted(bm._MONEY_OPS_UI)} разошёлся с реестром денежных операций "
        f"{sorted(expected)} — обнови оба осознанно"
    )


# ── 1F7: офлайн-бэклог Telegram не переигрывается после рестарта ───────────────────
def test_polling_drops_pending_updates():
    """bot/main.py обязан вызывать delete_webhook(drop_pending_updates=True) ДО start_polling:
    NL-команды многочасовой давности на денежном пути опасны. Слабая (текстовая), но
    класс-фиксирующая проверка — снятие вызова ломает тест."""
    src = (_REPO_ROOT / "bot" / "main.py").read_text(encoding="utf-8")
    drop_pos = src.find("delete_webhook(drop_pending_updates=True)")
    poll_pos = src.find("await dp.start_polling(bot)")
    assert drop_pos != -1, "drop_pending_updates(True) пропал из bot/main.py (1F7)"
    assert poll_pos != -1 and drop_pos < poll_pos, (
        "drop_pending_updates должен идти ДО start_polling"
    )
