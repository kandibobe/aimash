"""Волна 2.6, шаги 2–3 (reply-anchor GATE): `ConfirmStore.confirm_by_reply` подтверждает черновик
РЕПЛАЕМ автора на его карточку, а не владением чатом. Плюс §6.4-оркестратор
`ads.service.confirm_and_execute_by_reply`, композирующий девять проверок §2.2 без переписывания
существующего пути исполнения.

Шаг 1 (read-through, tests/test_reply_anchor_readthrough.py) уже доказал, что `author_user_id` и
`tg_message_id` доезжают до снимков. Здесь на них наконец СМОТРИТ гейт:
  • §2.2 №2 — `reply_to_message_id == proposal.tg_message_id` (реплай именно на карточку), причём в
    неймспейсе чата: `actor_chat_id == proposal.chat_id` (message_id уникален лишь ВНУТРИ чата);
  • §2.2 №3 — `actor_user_id == proposal.author_user_id` (подтвердил автор, личность по user_id).

Ключевое свойство — fail-closed по КОНСТРУКЦИИ (правило 10) для якоря+автора+TTL: черновик без якоря
(tg_message_id или author_user_id == NULL — прод-реальность, транспорт якоря ещё не проведён) НЕ
подтверждается никогда, потому что в SQL `NULL = :x` даёт UNKNOWN и строка выпадает из WHERE. Отдельно
ловим ловушку SQLAlchemy `col == None` → `col IS NULL`, которая без None-guard превратила бы «нет якоря»
в дыру fail-OPEN.

Реальный ConfirmStore на temp SQLite (conftest), как соседние CAS-тесты. Оркестратор тестируем на
реальном store + подменённом (monkeypatch) `execute_confirmed` — исполняющую половину (SDK) стубим,
чтобы проверять именно КОМПОЗИЦИЮ (якорь → исполнение / отказ до исполнения), а не её потроха. Границу
покрытия держим честной: совместимость reply-confirmed → `claim` (тот самый шов composed-пути) проверена
в test_matching_anchor напрямую (claim после confirm_by_reply даёт status='executing'); а что apply_*
реально стреляет — территория самого execute_confirmed (tests/test_execute_account_binding.py и др.).
"""

from __future__ import annotations

import inspect
import pathlib
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import select, update  # noqa: E402

from ads import service  # noqa: E402
from confirm.store import ConfirmStore  # noqa: E402
from conftest import attested  # noqa: E402
from core.config import settings  # noqa: E402
from core.provenance import human_turn  # noqa: E402
from db.models import AuditLog, Proposal  # noqa: E402
from db.session import Session, db_dt, init_db  # noqa: E402

DRAFT = "7753643025"
CHAT = (
    100  # чат публикации карточки == proposal.chat_id (в счастливом пути реплай приходит сюда же)
)
OTHER_CHAT = 200  # другая супергруппа — для теста неймспейса message_id
ACTOR = 4242
OTHER_ACTOR = 9999
CARD_MSG = 987654
OTHER_MSG = 111222
OP = "update_budget"


@pytest.fixture(autouse=True)
async def _db():
    """Схема + разрешённый Draft — тот же каркас, что у денежных тестов провенанса/якоря."""
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
    chat: int = CHAT,
    tg_message_id: int | None = CARD_MSG,
) -> str:
    """Создать pending-черновик человеческим ходом; при заданном `tg_message_id` проставить якорь
    напрямую (имитируя писатель транспорта `set_card_message_id`, шаг публикации карточки)."""
    cid = uuid.uuid4().hex
    with human_turn(actor_user_id=actor):
        await store.save_proposal(
            confirmation_id=cid,
            operation=OP,
            customer_id=DRAFT,
            params=attested({"campaign": "X"}, {"kind": "budget"}),
            summary="s",
            chat_id=chat,
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


async def _backdate(cid: str, *, hours_over_ttl: float = 1.0) -> None:
    """Состарить черновик на TTL + запас (двигаем возраст в БД — границу считает `_ttl_boundary()`
    по живому settings.proposal_ttl_hours, тест не знает её числом). Зеркало test_confirm_ttl_cas."""
    old = datetime.now(timezone.utc) - timedelta(
        hours=float(settings.proposal_ttl_hours) + hours_over_ttl
    )
    async with Session() as s:
        await s.execute(
            update(Proposal).where(Proposal.confirmation_id == cid).values(created_at=db_dt(old))
        )
        await s.commit()


async def _status(cid: str) -> str:
    async with Session() as s:
        p = (await s.execute(select(Proposal).where(Proposal.confirmation_id == cid))).scalar_one()
        return p.status


async def _audit_count(cid: str, status: str) -> int:
    async with Session() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.confirmation_id == cid, AuditLog.status == status
                    )
                )
            )
            .scalars()
            .all()
        )
    return len(rows)


# ── Позитив: якорь совпал → confirmed, дальше путь исполнения открыт ────────────────
async def test_matching_anchor_confirms_and_writes_audit():
    """Реплай автора (actor==author) на карточку (reply==tg_message_id) в своём чате подтверждает
    черновик, и он столбится claim'ом — то есть доезжает до исполняющей половины (шов composed-пути)."""
    store = ConfirmStore()
    cid = await _mk(store, actor=ACTOR, chat=CHAT, tg_message_id=CARD_MSG)

    assert (
        await store.confirm_by_reply(
            cid, actor_user_id=ACTOR, actor_chat_id=CHAT, reply_to_message_id=CARD_MSG
        )
        is True
    )
    assert await _status(cid) == "confirmed"
    assert await _audit_count(cid, "confirmed") == 1  # решение зафиксировано (актор в строке)
    snap = await store.claim(cid, operation=OP)
    assert snap is not None and snap.status == "executing"


# ── §2.2 №2: реплай НЕ на ту карточку → отказ ──────────────────────────────────────
async def test_wrong_reply_message_id_refuses():
    """Верный автор, но реплай на ЧУЖОЕ сообщение (не карточку черновика) → False, статус не тронут."""
    store = ConfirmStore()
    cid = await _mk(store, actor=ACTOR, tg_message_id=CARD_MSG)

    assert (
        await store.confirm_by_reply(
            cid, actor_user_id=ACTOR, actor_chat_id=CHAT, reply_to_message_id=OTHER_MSG
        )
        is False
    )
    assert await _status(cid) == "pending"
    assert await _audit_count(cid, "confirmed") == 0


# ── §2.2 №3: подтвердил НЕ автор → отказ (личность по user_id) ──────────────────────
async def test_wrong_actor_user_id_refuses():
    """Реплай на верную карточку, но от ДРУГОГО user_id (не автор черновика) → False. Это и есть
    защита, которую chat_id в форум-топике дать не мог: там все сообщения делят chat_id."""
    store = ConfirmStore()
    cid = await _mk(store, actor=ACTOR, tg_message_id=CARD_MSG)

    assert (
        await store.confirm_by_reply(
            cid, actor_user_id=OTHER_ACTOR, actor_chat_id=CHAT, reply_to_message_id=CARD_MSG
        )
        is False
    )
    assert await _status(cid) == "pending"


# ── Неймспейс message_id чатом: реплай из чужого чата не подтверждает чужой черновик ─
async def test_reply_from_other_chat_does_not_confirm_cross_chat_draft():
    """Defense-in-depth: Telegram message_id уникален лишь ВНУТРИ чата. Два черновика ОДНОГО автора в
    РАЗНЫХ супергруппах с СОВПАВШИМ tg_message_id не должны спутаться. Реплай из OTHER_CHAT (верный
    автор, верный message_id) НЕ подтверждает черновик D_A из CHAT — а свой D_B подтверждает ровно
    потому, что namespace (chat_id) совпал. Личность при этом по-прежнему по user_id — вырожденность
    форум-топика не возвращается."""
    store = ConfirmStore()
    cid_a = await _mk(store, actor=ACTOR, chat=CHAT, tg_message_id=CARD_MSG)
    cid_b = await _mk(store, actor=ACTOR, chat=OTHER_CHAT, tg_message_id=CARD_MSG)

    # Реплай пришёл из OTHER_CHAT: чужому чату (D_A) отказ, несмотря на верный автора и message_id.
    assert (
        await store.confirm_by_reply(
            cid_a, actor_user_id=ACTOR, actor_chat_id=OTHER_CHAT, reply_to_message_id=CARD_MSG
        )
        is False
    )
    assert await _status(cid_a) == "pending"
    # Тот же реплай для своего чата (D_B) — подтверждает: namespace совпал.
    assert (
        await store.confirm_by_reply(
            cid_b, actor_user_id=ACTOR, actor_chat_id=OTHER_CHAT, reply_to_message_id=CARD_MSG
        )
        is True
    )
    assert await _status(cid_b) == "confirmed"


async def test_none_chat_param_refuses():
    """None-guard для неймспейса: `actor_chat_id=None` компилировался бы в `chat_id IS NULL` (ловушка
    SQLAlchemy `col == None`). chat_id NOT NULL, так что совпадения нет, но guard режет ДО запроса —
    без доверенного пространства имён подтверждать нечего (fail-closed)."""
    store = ConfirmStore()
    cid = await _mk(store, actor=ACTOR, tg_message_id=CARD_MSG)

    assert (
        await store.confirm_by_reply(
            cid,
            actor_user_id=ACTOR,
            actor_chat_id=None,  # type: ignore[arg-type]
            reply_to_message_id=CARD_MSG,
        )
        is False
    )
    assert await _status(cid) == "pending"


# ── Fail-closed по КОНСТРУКЦИИ: NULL-якорь не подтверждается никогда ────────────────
async def test_null_tg_message_id_never_confirms():
    """Прод-реальность: транспорт якоря не проведён → tg_message_id == NULL. Даже с верным автором
    подтверждения быть не должно (нет доверенной записи о карточке). В SQL `NULL = :reply` — UNKNOWN,
    строка вне WHERE."""
    store = ConfirmStore()
    cid = await _mk(store, actor=ACTOR, tg_message_id=None)  # автор есть, якоря НЕТ

    assert (
        await store.confirm_by_reply(
            cid, actor_user_id=ACTOR, actor_chat_id=CHAT, reply_to_message_id=CARD_MSG
        )
        is False
    )
    assert await _status(cid) == "pending"


async def test_null_author_user_id_never_confirms():
    """Симметрично: машинный черновик (human_turn без актора) → author_user_id == NULL. Даже с верным
    реплаем на карточку подтверждать нечем — некому приписать авторство (`NULL = :actor` UNKNOWN)."""
    store = ConfirmStore()
    cid = await _mk(store, actor=None, tg_message_id=CARD_MSG)  # якорь есть, автора НЕТ

    assert (
        await store.confirm_by_reply(
            cid, actor_user_id=ACTOR, actor_chat_id=CHAT, reply_to_message_id=CARD_MSG
        )
        is False
    )
    assert await _status(cid) == "pending"


# ── Ловушка SQLAlchemy `col == None` → `IS NULL`: None-параметр НЕ должен матчить NULL-якорь ──
async def test_none_reply_param_does_not_match_null_anchor_fail_open_trap():
    """Регресс на дыру fail-OPEN. `Proposal.tg_message_id == None` SQLAlchemy компилирует в
    `tg_message_id IS NULL` — и без None-guard реплай с `reply_to_message_id=None` СОВПАЛ бы с
    непроштампованным (NULL-якорь) черновиком и подтвердил его. Guard обязан вернуть False ДО запроса."""
    store = ConfirmStore()
    cid = await _mk(store, actor=ACTOR, tg_message_id=None)  # NULL-якорь, как в проде

    assert (
        await store.confirm_by_reply(
            cid,
            actor_user_id=ACTOR,
            actor_chat_id=CHAT,
            reply_to_message_id=None,  # type: ignore[arg-type]
        )
        is False
    )
    assert await _status(cid) == "pending"


async def test_none_actor_param_does_not_match_null_author():
    """Зеркальная половина ловушки для актора: `author_user_id == None` → `IS NULL`. Машинный
    черновик (author NULL) + `actor_user_id=None` не должен схлопнуться в совпадение (в SQL и
    `NULL = NULL` — UNKNOWN, но полагаться на это нельзя: None-guard режет раньше)."""
    store = ConfirmStore()
    cid = await _mk(store, actor=None, tg_message_id=None)  # оба поля NULL

    assert (
        await store.confirm_by_reply(
            cid,
            actor_user_id=None,  # type: ignore[arg-type]
            actor_chat_id=CHAT,
            reply_to_message_id=None,  # type: ignore[arg-type]
        )
        is False
    )
    assert await _status(cid) == "pending"


# ── Одноразовость (CAS): второй реплай не переисполняет ─────────────────────────────
async def test_confirm_by_reply_is_one_shot():
    """Двойная доставка апдейта / повторный реплай: первый перевод pending→confirmed выигрывает,
    второй не совпадёт по WHERE (status уже не 'pending') → False. Защита от TOCTOU/replay."""
    store = ConfirmStore()
    cid = await _mk(store, actor=ACTOR, tg_message_id=CARD_MSG)

    assert (
        await store.confirm_by_reply(
            cid, actor_user_id=ACTOR, actor_chat_id=CHAT, reply_to_message_id=CARD_MSG
        )
        is True
    )
    assert (
        await store.confirm_by_reply(
            cid, actor_user_id=ACTOR, actor_chat_id=CHAT, reply_to_message_id=CARD_MSG
        )
        is False
    )
    assert await _audit_count(cid, "confirmed") == 1  # ровно одна audit-строка решения


# ── §2.2 №5: просроченный черновик не подтверждается (TTL в CAS) ────────────────────
async def test_expired_anchor_refuses():
    """Возраст стоит в том же WHERE (зеркало confirm/claim): просроченный черновик с верным якорем
    и автором всё равно даёт False — TTL энфорсит CAS, а не джоба-уборщик."""
    store = ConfirmStore()
    cid = await _mk(store, actor=ACTOR, tg_message_id=CARD_MSG)
    await _backdate(cid)

    assert (
        await store.confirm_by_reply(
            cid, actor_user_id=ACTOR, actor_chat_id=CHAT, reply_to_message_id=CARD_MSG
        )
        is False
    )
    assert await _status(cid) == "pending"


async def test_unknown_cid_refuses():
    """Несуществующий/утёкший confirmation_id → False, без создания строки."""
    store = ConfirmStore()
    assert (
        await store.confirm_by_reply(
            "нет-такого", actor_user_id=ACTOR, actor_chat_id=CHAT, reply_to_message_id=CARD_MSG
        )
        is False
    )


async def test_non_pending_refuses():
    """Уже подтверждённый черновик повторно по реплаю не подтверждается (status='pending' в WHERE)."""
    store = ConfirmStore()
    cid = await _mk(store, actor=ACTOR, tg_message_id=CARD_MSG)
    assert await store.confirm(cid, chat_id=CHAT, actor_user_id=ACTOR) is True  # уже confirmed

    assert (
        await store.confirm_by_reply(
            cid, actor_user_id=ACTOR, actor_chat_id=CHAT, reply_to_message_id=CARD_MSG
        )
        is False
    )


# ── §6.4-оркестратор: якорь → исполнение / отказ ДО исполнения ──────────────────────
async def test_orchestrator_bad_anchor_refuses_before_execute(monkeypatch):
    """Fail-closed композиции: якорь не совпал → PermissionError, а исполняющая половина
    (`execute_confirmed`) НЕ вызвана — claim не съеден, Google Ads не тронут."""
    store = ConfirmStore()
    cid = await _mk(store, actor=ACTOR, tg_message_id=None)  # NULL-якорь ⇒ confirm_by_reply=False

    called: list[tuple] = []

    async def _spy(st, confirmation_id):
        called.append((st, confirmation_id))
        return {"ok": True}

    monkeypatch.setattr(service, "execute_confirmed", _spy)

    with pytest.raises(PermissionError):
        await service.confirm_and_execute_by_reply(
            store,
            confirmation_id=cid,
            actor_user_id=ACTOR,
            actor_chat_id=CHAT,
            reply_to_message_id=CARD_MSG,
        )
    assert called == []  # исполнение не запускалось
    assert await _status(cid) == "pending"


async def test_orchestrator_good_anchor_confirms_then_executes(monkeypatch):
    """Позитив композиции: верный якорь → черновик подтверждён (confirm_by_reply) → вызвана
    исполняющая половина ровно с (store, cid), её результат возвращается наверх."""
    store = ConfirmStore()
    cid = await _mk(store, actor=ACTOR, tg_message_id=CARD_MSG)

    called: list[tuple] = []

    async def _spy(st, confirmation_id):
        called.append((st, confirmation_id))
        return {"applied": True}

    monkeypatch.setattr(service, "execute_confirmed", _spy)

    out = await service.confirm_and_execute_by_reply(
        store,
        confirmation_id=cid,
        actor_user_id=ACTOR,
        actor_chat_id=CHAT,
        reply_to_message_id=CARD_MSG,
    )
    assert out == {"applied": True}
    assert called == [(store, cid)]
    assert await _status(cid) == "confirmed"  # confirm_by_reply перевёл; исполнение застубено


# ── Мета-гард: fail-closed-условия живут в WHERE, а не в проверке после выборки ─────
def test_anchor_conditions_are_part_of_the_cas_where():
    """Зеркало test_ttl_condition_is_part_of_the_cas_where. Рефактор, вынесший якорь/чат/возраст/NULL-
    guard из CAS (или снявший `.isnot(None)`), валит тест: проверка ПОСЛЕ выборки — это TOCTOU, а
    потеря `.isnot(None)`/None-guard — дыра fail-OPEN на NULL-якорях; потеря неймспейса чата —
    межчатовая путаница message_id."""
    src = inspect.getsource(ConfirmStore.confirm_by_reply)
    assert "Proposal.chat_id == actor_chat_id" in src  # неймспейс message_id чатом
    assert "Proposal.tg_message_id == reply_to_message_id" in src
    assert "Proposal.author_user_id == actor_user_id" in src
    assert "Proposal.tg_message_id.isnot(None)" in src
    assert "Proposal.author_user_id.isnot(None)" in src
    assert "_ttl_boundary()" in src
    # None-guard до запроса включает все три доверенных поля (иначе `col == None` → `IS NULL`).
    assert "actor_user_id is None or actor_chat_id is None or reply_to_message_id is None" in src
