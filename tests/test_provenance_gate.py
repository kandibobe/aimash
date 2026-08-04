"""Волна 1.4: денежная операция требует ДВУХ независимых битов провенанса.

`user_initiated` — аргумент `save_proposal`. Сегодня он верен по построению (черновик рождается
внутри aiogram-хендлера за whitelist), но в headless-контуре создание становится вызываемым из
MCP-инструмента, cron-джобы и self-improvement-форка, где значение аргумента пишет вызывающий.
Бит, который можно передать аргументом, охраняет только аккуратных.

Второй бит (`origin_human_turn`) аргументом не задаётся вовсе: стор берёт его из `core.provenance`,
поднять который может только `human_turn(...)` доверенного слоя. Главный тест здесь — выпускной
гейт (И3): черновик, СОЗДАННЫЙ машинным ходом и подтверждённый живым человеком, всё равно даёт
`PermissionError`. Прежняя формулировка И3 выводила провенанс из факта подтверждения и делала
проверку тождественно истинной — cron предлагает поднять бюджет, человек жмёт ✅, гард молчит.

Работаем на НАСТОЯЩЕМ `ConfirmStore` (temp SQLite из conftest), а не на фейке: смысл теста именно
в том, что бит берётся из контекста хода и переживает запись в БД.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import os
import pathlib
import sys
import uuid

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

import ads.mutations as mut  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
from conftest import attested  # noqa: E402
from core.config import settings  # noqa: E402
from core.context import request_scope  # noqa: E402
from core.provenance import get_provenance, human_turn, machine_turn  # noqa: E402
from db.models import Proposal  # noqa: E402
from db.session import Session, init_db  # noqa: E402

DRAFT = "7753643025"
OWNER = 100
ACTOR = 4242


@pytest.fixture(autouse=True)
async def _db_and_lock():
    """Схема + разрешённый Draft: замок аккаунта (правило 9) стоит ПЕРЕД confirm-гейтом, и без
    разрешения тест упёрся бы в него, не дойдя до проверяемого гарда провенанса."""
    await init_db()
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = DRAFT
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


async def _mk(store: ConfirmStore, *, operation: str = "update_budget") -> str:
    """Создать черновик, солгав про `user_initiated`: аргумент под контролем вызывающего, и все
    тесты ниже проверяют, что одного его недостаточно."""
    cid = uuid.uuid4().hex
    await store.save_proposal(
        confirmation_id=cid,
        operation=operation,
        customer_id=DRAFT,
        # Аттестация свежести — как её кладёт карточка бота: предмет этих тестов провенанс, а не
        # свежесть, и черновик без снимка отбил бы гейт A (`update_budget` — STRICT) раньше, чем
        # дело дошло бы до проверки битов.
        params=attested({"campaign": "X"}, {"kind": "budget"}),
        summary="s",
        chat_id=OWNER,
        user_initiated=True,
    )
    return cid


async def _row(cid: str) -> Proposal:
    async with Session() as s:
        return (
            await s.execute(select(Proposal).where(Proposal.confirmation_id == cid))
        ).scalar_one()


class _SpySdk:
    """Подмена SDK-вызова: считает, доехали ли до денег. Ноль вызовов = гард сработал ДО Google."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *a, **kw):
        self.calls += 1
        return {"ok": True}


# ── Выпускной гейт (И3): подтверждение человеком НЕ повышает провенанс ────────────
async def test_machine_draft_confirmed_by_human_still_refused(monkeypatch):
    """Черновик создан вне человеческого хода (cron/агент/скрипт), но подтверждён живым человеком.
    Это ровно тот сценарий, который прежняя формулировка И3 пропускала: `user_initiated` выводился
    из факта подтверждения. Ожидание — PermissionError и НОЛЬ обращений к SDK."""
    store = ConfirmStore()
    with machine_turn():  # ход cron-джобы: бит не поднят
        cid = await _mk(store)
    assert (await _row(cid)).origin_human_turn is False

    assert await store.confirm(cid, chat_id=OWNER, actor_user_id=ACTOR) is True  # человек нажал ✅

    spy = _SpySdk()
    monkeypatch.setattr(mut, "_apply_budget_via_sdk", spy)
    with pytest.raises(PermissionError):
        await mut.apply_update_budget(
            customer_id=DRAFT,
            campaign_id="1",
            new_budget_micros=5_000_000,
            confirmation_id=cid,
            confirm_store=store,
            ads_client=object(),
        )
    assert spy.calls == 0, "гард пропустил машинный черновик до SDK — деньги в руках у cron"


async def test_human_draft_passes_and_records_author(monkeypatch):
    """Обратная половина: черновик, созданный человеческим ходом, проходит как раньше — иначе
    «отказывать всем» удовлетворило бы тест выше. Заодно фиксируем, что стор записал автора и
    корреляцию хода, не получив их аргументом."""
    store = ConfirmStore()
    with request_scope("tg:update_budget"):  # даёт run_id, но объявляет ход машинным…
        with human_turn(actor_user_id=ACTOR):  # …а доверенный слой поднимает бит поверх
            cid = await _mk(store)
            run_id = get_provenance().run_id

    row = await _row(cid)
    assert row.origin_human_turn is True
    assert row.author_user_id == ACTOR
    assert row.run_id == run_id and row.run_id != "-"

    assert await store.confirm(cid, chat_id=OWNER, actor_user_id=ACTOR) is True
    spy = _SpySdk()
    monkeypatch.setattr(mut, "_apply_budget_via_sdk", spy)
    await mut.apply_update_budget(
        customer_id=DRAFT,
        campaign_id="1",
        new_budget_micros=5_000_000,
        confirmation_id=cid,
        confirm_store=store,
        ads_client=object(),
    )
    assert spy.calls == 1


# ── Оба бита обязательны, ни один не достаточен ───────────────────────────────────
@pytest.mark.parametrize(
    "user_initiated, origin_human_turn",
    [(False, False), (True, False), (False, True)],
)
def test_gate_refuses_unless_both_bits(user_initiated, origin_human_turn):
    """Один поднятый бит не открывает деньги ни в какой комбинации. (True, True) проверяется
    сквозным тестом выше — здесь важны именно частичные наборы."""

    class P:
        pass

    p = P()
    p.user_initiated = user_initiated
    p.origin_human_turn = origin_human_turn
    with pytest.raises(PermissionError):
        mut._require_user_command(p, "изменение бюджета")


def test_gate_refuses_proposal_without_the_bit_at_all():
    """Сторонний стор, забывший поле, обязан упасть на денежном пути, а не получить трактовку по
    умолчанию: `getattr(p, 'origin_human_turn', ...)` с любым дефолтом — тихая подмена решения."""

    class P:
        user_initiated = True

    with pytest.raises((PermissionError, AttributeError)):
        mut._require_user_command(P(), "изменение бюджета")


# ── Провенанс нельзя задать аргументом ────────────────────────────────────────────
def test_save_proposal_has_no_provenance_kwargs():
    """Появление `origin_human_turn=` в сигнатуре вернуло бы дыру целиком: бит снова стал бы тем,
    что напишет вызывающий. `author_user_id`/`run_id` — там же и по той же причине."""
    params = set(inspect.signature(ConfirmStore.save_proposal).parameters)
    forbidden = params & {"origin_human_turn", "author_user_id", "run_id", "provenance"}
    assert not forbidden, (
        f"save_proposal принимает провенанс аргументом: {sorted(forbidden)}. Бит обязан приходить "
        "из core.provenance — иначе его подделает любой вызывающий (И3)."
    )


def test_human_turn_call_sites_are_allow_listed():
    """`human_turn(` в проде — только доверенные Telegram-входы и operator-only scripts.

    Legacy middleware поднимает бит после whitelist. Hermes-вход поднимает его только после HMAC
    exact-tool/args verification и повторной проверки whitelist в `trusted_tool`; сам plugin этого
    сделать не может. Новый MCP-инструмент, скрипт или self-improvement-форк ломает тест и требует
    осознанного решения, а не проходит ревью незамеченным.

    `scripts/` из `skip` УБРАН намеренно: там была слепая зона, и не гипотетическая — девять
    скриптов создают черновики (`exec_demo*`, `live_smoke_*`), в том числе денежные, и ни один не
    поднимал бит, то есть с Волны 1.4 молча упирался в `_require_user_command`."""
    root = pathlib.Path(__file__).resolve().parents[1]
    # определение + два доверенных Telegram-входа + точка для запуска руками из консоли
    allowed = {
        "core/provenance.py",
        "bot/main.py",
        "mcp_server/trusted_transport.py",
        "scripts/_operator_turn.py",
    }
    skip_dirs = {
        "tests",
        "migrations",
        ".git",
        "__pycache__",
        "deploy",
        "docs",
        "venv",
        ".venv",
        ".agents",
        ".claude",
        ".codex",
        ".hypothesis",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
    found: set[str] = set()
    project_python: list[pathlib.Path] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in skip_dirs]
        project_python.extend(
            pathlib.Path(current) / name for name in files if name.endswith(".py")
        )
    for py in project_python:
        rel = py.relative_to(root).as_posix()
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except SyntaxError:  # чужой/битый файл в дереве не должен ронять мета-гард
            continue
        # Именно ВЫЗОВ, а не вхождение подстроки: про human_turn пишут докстринги ads/mutations.py
        # и confirm/store.py — грепу они неотличимы от call-site, и гард стал бы шумным.
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                name = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
                if name == "human_turn":
                    found.add(rel)
    assert found <= allowed, (
        f"human_turn(...) открывают вне allow-list: {sorted(found - allowed)}. Человеческий бит "
        "поднимает ТОЛЬКО слой, который сам установил, что апдейт пришёл от живого человека из "
        "whitelist по доверенному каналу."
    )


def test_scripts_declare_provenance_where_they_create_proposals():
    """Каждый `save_proposal` в `scripts/` обязан лежать внутри `with operator_turn()`.

    Тест выше сторожит, КТО вправе поднять бит; этот — что скрипт, создающий черновик, его вообще
    поднимает. Без второго ассерта класс не закрыт: allow-list остаётся зелёным ровно тогда, когда
    новый скрипт бит не трогает, — то есть в точности в сломанном состоянии, из которого мы вышли
    (девять скриптов молча отказывали на денежном пути с Волны 1.4)."""
    scripts = pathlib.Path(__file__).resolve().parents[1] / "scripts"
    offenders: list[str] = []
    for py in sorted(scripts.glob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except SyntaxError:
            continue
        # Диапазоны строк всех `with ... operator_turn(...) ...` в файле.
        guarded: list[tuple[int, int]] = []
        for n in ast.walk(tree):
            if isinstance(n, (ast.With, ast.AsyncWith)) and any(
                isinstance(it.context_expr, ast.Call)
                and getattr(it.context_expr.func, "id", None) == "operator_turn"
                for it in n.items
            ):
                guarded.append((n.lineno, n.end_lineno or n.lineno))
        for n in ast.walk(tree):
            if (
                isinstance(n, ast.Call)
                and getattr(n.func, "attr", None) == "save_proposal"
                and not any(lo <= n.lineno <= hi for lo, hi in guarded)
            ):
                offenders.append(f"{py.name}:{n.lineno}")
    assert not offenders, (
        f"save_proposal вне operator_turn: {offenders}. Черновик без второго бита провенанса "
        "исполнится только для не-денежных операций, и обнаружится это на живом аккаунте."
    )


def test_operator_turn_refuses_outside_scripts():
    """Тот же контекст-менеджер, вызванный ИЗ СЕРВЕРА (хендлер, MCP-инструмент, джоба), обязан
    отказать: точка входа процесса лежит не в `scripts/`. Под pytest это выполняется само —
    `sys.argv[0]` указывает на раннер, — поэтому проверка бесплатна и не требует подделки."""
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
    from _operator_turn import operator_turn  # noqa: PLC0415 — модуль вне пакета, импорт по месту

    with pytest.raises(RuntimeError, match="scripts"):
        with operator_turn():
            pass  # pragma: no cover — сюда не доходим


def test_operator_turn_raises_the_bit_for_a_real_script(monkeypatch):
    """Обратная половина: из скрипта бит поднимается — иначе «отказывать всем» удовлетворило бы
    тест выше, и смоуки остались бы сломанными при зелёном прогоне."""
    root = pathlib.Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    from _operator_turn import operator_turn  # noqa: PLC0415

    monkeypatch.setattr(sys, "argv", [str(root / "scripts" / "live_smoke_test.py")])
    with operator_turn(actor_user_id=ACTOR):
        prov = get_provenance()
    assert prov.human_turn is True and prov.actor_user_id == ACTOR
    assert get_provenance().human_turn is False, "бит не опущен на выходе из scope"


# ── Фон не наследует человеческий бит ─────────────────────────────────────────────
async def test_request_scope_downgrades_to_machine_turn():
    """`request_scope` открывают только фоновые входы (scheduler-джобы, краул, dev-скрипты), и он
    обязан опускать бит. Иначе задача, порождённая из человеческого хода (`asyncio.create_task`
    копирует contextvars), создавала бы денежные черновики «от имени человека»."""
    with human_turn(actor_user_id=ACTOR):
        assert get_provenance().human_turn is True
        with request_scope("scheduler:cleanup"):
            assert get_provenance().human_turn is False
        assert get_provenance().human_turn is True, "request_scope не вернул контекст на выходе"


async def test_child_task_inherits_but_job_scope_resets():
    """Задача, запущенная из человеческого хода, наследует бит (contextvars копируются) — это не
    баг: работу заказал человек. Опасен только фоновый вход, и он закрыт `request_scope`."""

    async def child_raw() -> bool:
        return get_provenance().human_turn

    async def child_job() -> bool:
        with request_scope("job"):
            return get_provenance().human_turn

    with human_turn(actor_user_id=ACTOR):
        assert await asyncio.create_task(child_raw()) is True
        assert await asyncio.create_task(child_job()) is False


async def test_provenance_defaults_to_machine_outside_any_scope():
    """Вне всякого scope — машинный ход (правило 10): забыть поднять бит = отказ, забыть
    опустить — невозможно."""
    prov = get_provenance()
    assert prov.human_turn is False and prov.actor_user_id is None

    store = ConfirmStore()
    cid = await _mk(store)  # headless-вызывающий, солгавший про user_initiated
    row = await _row(cid)
    assert row.user_initiated is True and row.origin_human_turn is False
