"""Офлайн-smoke PROPOSE-слоя MCP (`mcp_server.tools_write`): черновик создаётся, Google Ads НЕ трогается,
три гейта, которые слой держит КОДОМ, а не доверием к модели, — провенанс (правило 3), И8 (правило 13),
валидация входа (правило 4).

Работаем на НАСТОЯЩЕМ `ConfirmStore` (temp SQLite из conftest), а не на фейке: смысл propose именно в
том, что строка черновика реально ложится в нашу БД со штампом провенанса из контекста хода, а счёт И8
берётся из ХРАНИЛИЩА (`count_run_proposals`), а не из памяти процесса. SDK-путь внутри `build_proposal`
(снимок «было» через `read_state`, валюта аккаунта) подменён — как `read_state`/`build_client_async` в
соседних офлайн-смоуках; Google Ads в этом тесте не дёргается ни разу (что и есть свойство propose-only:
`ads.mutations.apply_*` отсюда физически недостижим — гард `require_no_mutations` в самом модуле).

Стиль — как tests/test_provenance_gate.py: `init_db()` в autouse-фикстуре, `human_turn`/`set_context`
открывают доверенный ход, `async def test_*` (asyncio_mode=auto).
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

import ads.client as ac  # noqa: E402
import mcp_server.propose as propose  # noqa: E402
import mcp_server.tools_write as tw  # noqa: E402
from ads.client import DRAFT_ACCOUNT_ID  # noqa: E402
from ads.freshness import Snapshot, SnapshotKind  # noqa: E402
from core import i18n  # noqa: E402
from core.context import reset_context, set_context  # noqa: E402
from core.provenance import human_turn  # noqa: E402
from db.models import Proposal  # noqa: E402
from db.session import Session, init_db  # noqa: E402

OWNER = 100
ACTOR = 4242


class _ReadStateSpy:
    """Подмена `mcp_server.propose.read_state` (единственный SDK-путь снимка «было»): считает вызовы —
    ноль вызовов доказывает, что гейт отказал ДО обращения к Google Ads. Отдаёт валидный OK-Snapshot,
    чтобы `attach_freshness`+`fmt_mutation_summary` собрали настоящий diff «было → станет»."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(
        self, operation: str, params: dict, customer_id: str | None = None
    ) -> Snapshot:
        self.calls += 1
        if operation == "update_bid":
            return Snapshot(
                SnapshotKind.OK,
                {
                    "kind": "bid",
                    "before_micros": [1_000_000],
                    "after_micros": [1_200_000],
                    "currency": "",
                    "n_groups": 1,
                },
            )
        if operation == "launch_campaign":
            return Snapshot(
                SnapshotKind.OK,
                {"kind": "status", "before_status": "PAUSED"},
            )
        return Snapshot(
            SnapshotKind.OK,
            {
                "kind": "budget",
                "before_micros": 40_000_000,
                "after_micros": 48_000_000,
                "currency": "",
            },
        )


@pytest.fixture(autouse=True)
async def propose_env():
    """Схема БД + подмена SDK-путей внутри `build_proposal`. Патчим ВРУЧНУЮ с восстановлением в finally
    (как settings в test_provenance_gate), чтобы не зависеть от инъекции monkeypatch в async-фикстуру.

    `read_state` — спай (см. класс). `read_account_currency`/`build_client_async` — заглушки: валюта
    аккаунта в happy-path не задаётся, currency_mismatch тождественно ложен, но обе функции всё равно
    вызываются в денежной ветке — уводим их от реального SDK. Возвращаем спай, чтобы тест «машинный ход
    отказал ДО SDK» мог проверить `calls == 0`."""
    await init_db()
    spy = _ReadStateSpy()
    orig_rs, orig_cur, orig_bc = (
        propose.read_state,
        propose.read_account_currency,
        ac.build_client_async,
    )

    async def _cur(client, customer_id=None):  # валюта аккаунта не нужна — happy-path без currency
        return ""

    async def _bc(customer_id=None):  # клиент-заглушка: до реального SDK не доходим
        return object()

    propose.read_state = spy
    propose.read_account_currency = _cur
    ac.build_client_async = _bc
    try:
        yield spy
    finally:
        propose.read_state = orig_rs
        propose.read_account_currency = orig_cur
        ac.build_client_async = orig_bc


@contextmanager
def _human_turn(*, run_id: str, chat_id: int | None):
    """Доверенный человеческий ход: `human_turn` поднимает бит провенанса и фиксирует run_id (счёт И8);
    `set_context` кладёт chat_id доставки, который `_propose` читает из КОНТЕКСТА, а не из аргумента.
    chat_id=None ⇒ контекст без чата (проверка fail-closed)."""
    tok = set_context(request_id=run_id, chat_id=chat_id)
    try:
        with human_turn(actor_user_id=ACTOR, run_id=run_id):
            yield
    finally:
        reset_context(tok)


async def _row(cid: str) -> Proposal:
    async with Session() as s:
        return (
            await s.execute(select(Proposal).where(Proposal.confirmation_id == cid))
        ).scalar_one()


async def _count(run_id: str) -> int:
    from confirm.store import ConfirmStore

    return await ConfirmStore().count_run_proposals(run_id)


# ── Happy-path: черновик создан, Google Ads не тронут, провенанс проштампован из контекста ──────────
async def test_propose_budget_creates_pending_draft_without_touching_ads(propose_env):
    """Прямая команда человека в этом ходе → черновик `update_budget` на Draft. Конверт `pending` +
    confirmation_id + preview с «было → станет». Строка реально в БД, статус pending (не applied), и оба
    бита провенанса подняты из контекста хода, а не переданы аргументом инструмента."""
    with _human_turn(run_id="wp_happy_budget", chat_id=OWNER):
        env = await tw.propose_budget_change(
            account=DRAFT_ACCOUNT_ID, campaign="Camp A", mode="increase_by_percent", value=20
        )

    assert env["status"] == "pending"
    assert env["error"] is None and env["error_code"] is None
    assert isinstance(env["confirmation_id"], str) and env["confirmation_id"]
    assert env["operation"] == "update_budget"
    assert env["customer_id"] == DRAFT_ACCOUNT_ID
    assert "→" in env["preview"]  # реальный diff «было → станет», собранный confirm.render (КОД)
    assert propose_env.calls == 1  # снимок «было» прочитан ровно раз; мутаций — ноль (propose-only)

    row = await _row(env["confirmation_id"])
    assert row.status == "pending"  # черновик, не примёнён — Google Ads не тронут
    # Провенанс: оба бита пришли из доверенного контекста хода, не из аргумента (правило 3, И3).
    assert row.origin_human_turn is True and row.user_initiated is True
    assert row.author_user_id == ACTOR and row.run_id == "wp_happy_budget"


async def test_propose_bid_creates_pending_draft(propose_env):
    """Симметрично для ставки CPC (`update_bid`) — второй денежный propose-инструмент собирает черновик
    тем же путём (общий `_propose`), preview несёт «было → станет» по группам."""
    with _human_turn(run_id="wp_happy_bid", chat_id=OWNER):
        env = await tw.propose_bid_change(
            account=DRAFT_ACCOUNT_ID, campaign="Camp A", mode="increase_by_percent", value=20
        )
    assert env["status"] == "pending" and env["operation"] == "update_bid"
    assert "→" in env["preview"]
    assert (await _row(env["confirmation_id"])).status == "pending"


async def test_propose_launch_creates_pending_draft_from_human_turn(propose_env):
    with _human_turn(run_id="wp_happy_launch", chat_id=OWNER):
        env = await tw.propose_launch_campaign(
            account=DRAFT_ACCOUNT_ID,
            campaign="Draft Search",
        )

    assert env["status"] == "pending" and env["operation"] == "launch_campaign"
    row = await _row(env["confirmation_id"])
    assert row.status == "pending"
    assert row.user_initiated is True and row.origin_human_turn is True


async def test_machine_turn_launch_propose_refused_before_ads(propose_env):
    tok = set_context(request_id="wp_machine_launch", chat_id=OWNER)
    try:
        env = await tw.propose_launch_campaign(
            account=DRAFT_ACCOUNT_ID,
            campaign="Draft Search",
        )
    finally:
        reset_context(tok)

    assert env["status"] == "refused" and env["error_code"] == "refused"
    assert env["error"] == i18n.t("propose_requires_human", "ru")
    assert propose_env.calls == 0


# ── И8 (правило 13): не более одного черновика на ассистентский ход ─────────────────────────────────
async def test_i8_second_draft_in_same_run_is_refused(propose_env):
    """Второй propose в ТОМ ЖЕ run → отказ по счёту из хранилища, ДО сборки/сохранения (read_state
    больше не вызывается). В БД остаётся ровно один черновик этого run — счётчик не в памяти процесса."""
    with _human_turn(run_id="wp_i8", chat_id=OWNER):
        first = await tw.propose_budget_change(
            account=DRAFT_ACCOUNT_ID, campaign="Camp A", mode="increase_by_percent", value=10
        )
        assert first["status"] == "pending"
        calls_after_first = propose_env.calls

        second = await tw.propose_budget_change(
            account=DRAFT_ACCOUNT_ID, campaign="Camp B", mode="increase_by_percent", value=10
        )

    assert second["status"] == "refused" and second["confirmation_id"] is None
    assert second["error_code"] == "refused"
    assert second["error"] == i18n.t("propose_draft_limit", "ru")
    assert (
        propose_env.calls == calls_after_first
    )  # второй отказан ДО read_state (build не достигнут)
    assert await _count("wp_i8") == 1  # ровно один черновик в этом ходе — лимит держит хранилище


async def test_i8_new_run_allows_a_fresh_draft(propose_env):
    """Обратный контроль: НОВЫЙ ход (другой run_id) снова разрешает черновик — иначе лимит был бы
    глобальным «один на процесс», а не «один на ход». Доказывает, что ключ счёта — именно run."""
    with _human_turn(run_id="wp_run_one", chat_id=OWNER):
        a = await tw.propose_budget_change(
            account=DRAFT_ACCOUNT_ID, campaign="Camp A", mode="increase_by_percent", value=10
        )
    with _human_turn(run_id="wp_run_two", chat_id=OWNER):
        b = await tw.propose_budget_change(
            account=DRAFT_ACCOUNT_ID, campaign="Camp A", mode="increase_by_percent", value=10
        )
    assert a["status"] == "pending" and b["status"] == "pending"
    assert a["confirmation_id"] != b["confirmation_id"]


# ── Провенанс (правило 3): денежный черновик только человеческим ходом ──────────────────────────────
async def test_machine_turn_money_propose_refused_before_ads(propose_env):
    """Машинный ход (нет `human_turn` — cron/anomaly/self-improve): денежный propose отказан по правилу
    3 ДО любого чтения Google Ads. Ноль обращений к read_state — гейт стоит раньше сборки черновика."""
    tok = set_context(
        request_id="wp_machine", chat_id=OWNER
    )  # чат есть, а человеческого бита — нет
    try:
        env = await tw.propose_budget_change(
            account=DRAFT_ACCOUNT_ID, campaign="Camp A", mode="increase_by_percent", value=20
        )
    finally:
        reset_context(tok)
    assert env["status"] == "refused" and env["error_code"] == "refused"
    assert env["error"] == i18n.t("propose_requires_human", "ru")
    assert propose_env.calls == 0  # деньги не дошли до SDK — отказ раньше build_proposal


# ── Fail-closed: черновику нужен чат доставки/подтверждения ─────────────────────────────────────────
async def test_missing_chat_context_refused(propose_env):
    """Человеческий ход, но контекст без chat_id (транспорт не проставил): безадресный денежный
    черновик показать/подтвердить некому → отказ (fail-closed), ДО чтения Google Ads."""
    with human_turn(actor_user_id=ACTOR, run_id="wp_nochat"):  # без set_context ⇒ chat_id=None
        env = await tw.propose_budget_change(
            account=DRAFT_ACCOUNT_ID, campaign="Camp A", mode="increase_by_percent", value=20
        )
    assert env["status"] == "refused" and env["error_code"] == "refused"
    assert env["error"] == i18n.t("propose_no_turn_context", "ru")
    assert propose_env.calls == 0


# ── Валидация входа (правило 4): диапазоны/режимы считает КОД ───────────────────────────────────────
async def test_invalid_params_refused_as_invalid_argument(propose_env):
    """Кривой вход (снижение на ≥100% — бюджет стал бы нулевым) отбивается Pydantic-валидатором ДО
    всех прочих гейтов → `invalid_argument`, черновик не создан, Google Ads не тронут. Вина вызывающего
    (агент собрал плохие params), поэтому код отличается от штатного `refused`."""
    with _human_turn(run_id="wp_badparams", chat_id=OWNER):
        env = await tw.propose_budget_change(
            account=DRAFT_ACCOUNT_ID, campaign="Camp A", mode="decrease_by_percent", value=150
        )
    assert env["status"] == "refused" and env["confirmation_id"] is None
    assert env["error_code"] == "invalid_argument"
    assert propose_env.calls == 0  # валидатор отбил вход раньше сборки черновика
    assert await _count("wp_badparams") == 0  # ни строки в БД


async def test_negative_value_refused(propose_env):
    """`value` всегда положительное (Field(gt=0)); направление несёт mode. Отрицательное — ValidationError
    → `invalid_argument` (а не молчаливое принятие «минус-бюджета»)."""
    with _human_turn(run_id="wp_negval", chat_id=OWNER):
        env = await tw.propose_bid_change(
            account=DRAFT_ACCOUNT_ID, campaign="Camp A", mode="set_to", value=-5
        )
    assert env["status"] == "refused" and env["error_code"] == "invalid_argument"
