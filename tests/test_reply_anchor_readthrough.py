"""Волна 2.6, шаг 1 (reply-anchor): поля `author_user_id` и `tg_message_id` прорастают через ВСЕ
три снимкобилдера `ConfirmStore` — `get_confirmed`, `load_proposals`, `claim` — как read-through.

Критик money-пути поставил жёсткое условие к первому инкременту: поля добавлены во все три пути
снятия снимка (не в два, которые называл черновой план), зеркалены на Protocol в `ads/mutations.py`,
и NULL `tg_message_id` прорастает как `None` — не отброшен и не подменён на `0`. На этом шаге ни один
гейт на новые поля НЕ смотрит: это чистый проброс с нулевым изменением поведения, он лишь
разблокирует identity-проверки шагов 2–3 (actor==author, reply==tg_message_id).

Работаем на НАСТОЯЩЕМ `ConfirmStore` (temp SQLite из conftest), а не на фейке: смысл теста в том,
что значение переживает круговой рейс через БД и одинаково видно во всех трёх снимках.

`tg_message_id` сегодня НЕ пишет `save_proposal` (его заполнит транспорт подтверждения, шаг 2), и
это правильно: read-through-тесту достаточно проставить колонку напрямую, имитируя будущую запись
транспорта, — так проверяется именно чтение снимка, а не ещё не существующий писатель.
"""

from __future__ import annotations

import pathlib
import sys
import uuid

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import select, update  # noqa: E402

from confirm.store import ConfirmStore  # noqa: E402
from conftest import attested  # noqa: E402
from core.config import settings  # noqa: E402
from core.provenance import human_turn  # noqa: E402
from db.models import Proposal  # noqa: E402
from db.session import Session, init_db  # noqa: E402

DRAFT = "7753643025"
OWNER = 100
ACTOR = 4242
CARD_MSG = 987654


@pytest.fixture(autouse=True)
async def _db():
    """Схема + разрешённый Draft — тот же каркас, что у денежных тестов провенанса."""
    await init_db()
    prev = settings.google_ads_allowed_customer_ids
    settings.google_ads_allowed_customer_ids = DRAFT
    try:
        yield
    finally:
        settings.google_ads_allowed_customer_ids = prev


async def _mk(
    store: ConfirmStore,
    *,
    actor: int | None = ACTOR,
    tg_message_id: int | None = None,
    operation: str = "update_budget",
) -> str:
    """Создать confirmed-черновик человеческим ходом; при заданном `tg_message_id` проставить колонку
    напрямую (писатель транспорта — шаг 2, здесь его ещё нет)."""
    cid = uuid.uuid4().hex
    with human_turn(actor_user_id=actor):
        await store.save_proposal(
            confirmation_id=cid,
            operation=operation,
            customer_id=DRAFT,
            params=attested({"campaign": "X"}, {"kind": "budget"}),
            summary="s",
            chat_id=OWNER,
            user_initiated=True,
        )
    if tg_message_id is not None:
        async with Session() as s:
            await s.execute(
                update(Proposal)
                .where(Proposal.confirmation_id == cid)
                .values(tg_message_id=tg_message_id)
            )
            await s.commit()
    return cid


# ── Проброс через все три снимка ──────────────────────────────────────────────────
async def test_get_confirmed_carries_reply_anchor():
    store = ConfirmStore()
    cid = await _mk(store, actor=ACTOR, tg_message_id=CARD_MSG)

    snap = await store.get_confirmed(cid)
    assert snap is not None
    assert snap.author_user_id == ACTOR
    assert snap.tg_message_id == CARD_MSG


async def test_load_proposals_carries_reply_anchor():
    store = ConfirmStore()
    cid = await _mk(store, actor=ACTOR, tg_message_id=CARD_MSG)

    got = await store.load_proposals([cid])
    assert cid in got
    assert got[cid].author_user_id == ACTOR
    assert got[cid].tg_message_id == CARD_MSG


async def test_claim_carries_reply_anchor():
    """`claim` столбит confirmed→executing; его снимок — тот, что доедет до `execute_confirmed`,
    поэтому якорь обязан быть виден именно здесь (иначе шаг 3 гейтить нечем)."""
    store = ConfirmStore()
    cid = await _mk(store, actor=ACTOR, tg_message_id=CARD_MSG)
    assert await store.confirm(cid, chat_id=OWNER, actor_user_id=ACTOR) is True

    snap = await store.claim(cid, operation="update_budget")
    assert snap is not None
    assert snap.author_user_id == ACTOR
    assert snap.tg_message_id == CARD_MSG


# ── NULL прорастает как None, не как 0 и не «потерялся» ────────────────────────────
async def test_null_tg_message_id_surfaces_as_none_in_all_snapshots():
    """Сегодня `tg_message_id` в БД NULL (транспорт его ещё не пишет). Все три снимка обязаны отдать
    именно `None` — не `0` и не «ключ отсутствует». Это load-bearing свойство: будущая проверка
    «реплай именно на эту карточку» считает `None` ОТКАЗОМ, и она обязана отличать NULL от валидного
    message_id 0 (fail-closed на старых строках)."""
    store = ConfirmStore()
    cid = await _mk(store, actor=ACTOR, tg_message_id=None)  # NULL, как сегодня в проде

    gc = await store.get_confirmed(cid)
    assert gc is not None and gc.tg_message_id is None

    lp = await store.load_proposals([cid])
    assert lp[cid].tg_message_id is None

    assert await store.confirm(cid, chat_id=OWNER, actor_user_id=ACTOR) is True
    cl = await store.claim(cid, operation="update_budget")
    assert cl is not None and cl.tg_message_id is None


# ── Писатель якоря (set_card_message_id): одноразовый CAS, fail-closed ─────────────
async def test_set_card_message_id_stamps_once_and_reads_back():
    """Транспорт штампует message_id карточки после публикации; снимок затем его отдаёт."""
    store = ConfirmStore()
    cid = await _mk(store, actor=ACTOR, tg_message_id=None)  # ещё не проштампован

    assert await store.set_card_message_id(cid, CARD_MSG) is True
    snap = await store.get_confirmed(cid)
    assert snap is not None and snap.tg_message_id == CARD_MSG


async def test_set_card_message_id_is_one_shot():
    """Одноразовость якоря: второй штамп не совпадёт по WHERE (tg_message_id IS NULL) → False, и
    исходное значение НЕ перезаписано. Это защита от перенаправления якоря на чужую карточку и от
    двойной доставки апдейта."""
    store = ConfirmStore()
    cid = await _mk(store, actor=ACTOR, tg_message_id=None)
    assert await store.set_card_message_id(cid, CARD_MSG) is True

    assert await store.set_card_message_id(cid, 111222) is False
    snap = await store.get_confirmed(cid)
    assert snap is not None and snap.tg_message_id == CARD_MSG  # первый выиграл, подмены нет


async def test_set_card_message_id_refuses_non_pending():
    """Проштамповать якорь уже подтверждённого/исполняемого черновика нельзя (status='pending' в
    WHERE): якорь фиксируется до подтверждения, не переставляется после."""
    store = ConfirmStore()
    cid = await _mk(store, actor=ACTOR, tg_message_id=None)
    assert await store.confirm(cid, chat_id=OWNER, actor_user_id=ACTOR) is True  # уже confirmed

    assert await store.set_card_message_id(cid, CARD_MSG) is False
    snap = await store.get_confirmed(cid)
    assert snap is not None and snap.tg_message_id is None  # остался NULL — штамп отбит


async def test_set_card_message_id_unknown_cid_is_false():
    """Несуществующий confirmation_id → False (нечего штамповать), без создания строки."""
    store = ConfirmStore()
    assert await store.set_card_message_id("нет-такого", CARD_MSG) is False


async def test_null_author_user_id_surfaces_as_none():
    """Симметрично `author_user_id`: ход без известного актора (`human_turn(actor_user_id=None)`)
    даёт NULL в БД, а снимок — `None`, не `0`. Актор-часть якоря (actor==author, шаг 3) на таком
    черновике обязана отказать, а не сравнивать с нулём."""
    store = ConfirmStore()
    cid = await _mk(store, actor=None, tg_message_id=CARD_MSG)

    snap = await store.get_confirmed(cid)
    assert snap is not None and snap.author_user_id is None

    async with Session() as s:  # и в самой строке БД тоже NULL, а не 0
        row = (
            await s.execute(select(Proposal).where(Proposal.confirmation_id == cid))
        ).scalar_one()
    assert row.author_user_id is None
