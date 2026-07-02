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
