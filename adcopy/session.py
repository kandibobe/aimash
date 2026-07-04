"""Сессия курации RSA-текстов (фаза 2.C): поэлементное одобрение/отклонение/доработка
сгенерированных заголовков и описаний ПЕРЕД созданием объявления.

Курация — это «просто текст»: SDK НЕ трогается, confirmation_id НЕ тратится. Реальная мутация
(create_rsa) минтуется отдельным черновиком в конце (см. bot.main / ads.service). Поэтому сессия
лежит в таблице proposals под операцией `rsa_curation`, которой НЕТ в SUPPORTED_OPERATIONS →
она физически неисполнима (execute_confirmed её отвергает). Реюз proposals.params (JSON) даёт
персист сессии через рестарт бота БЕЗ новой Alembic-миграции.

Состояние элемента: pending | approved | rejected. «Доработать» (refine) заменяет текст и
возвращает элемент в pending (нужно одобрить заново). В объявление идут только approved.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from adcopy.validate import (
    RSA_MIN_DESCRIPTIONS,
    RSA_MIN_HEADLINES,
    validate,
)
from db.models import Proposal
from db.session import Session

SESSION_OP = "rsa_curation"  # НЕ в SUPPORTED_OPERATIONS → неисполнимо

_KIND_FIELD = {"h": "headlines", "d": "descriptions"}
_KIND_VALIDATE = {"h": "headline", "d": "description"}


@dataclass
class CurationSession:
    """Снимок сессии курации для бота. headlines/descriptions — list[dict]:
    {"text": str, "len": int, "state": "pending|approved|rejected"}."""

    session_id: str
    chat_id: int
    campaign: str
    ad_group_id: str
    ad_group_name: str
    final_url: str
    brief: dict
    headlines: list[dict]
    descriptions: list[dict]

    def items(self, kind: str) -> list[dict]:
        return self.headlines if kind == "h" else self.descriptions

    def approved_texts(self, kind: str) -> list[str]:
        return [e["text"] for e in self.items(kind) if e["state"] == "approved"]

    def counts(self) -> tuple[int, int]:
        """(одобрено заголовков, одобрено описаний)."""
        return len(self.approved_texts("h")), len(self.approved_texts("d"))

    def can_finalize(self) -> bool:
        h, d = self.counts()
        return h >= RSA_MIN_HEADLINES and d >= RSA_MIN_DESCRIPTIONS

    def next_pending(self) -> tuple[str, int] | None:
        """Следующий элемент в статусе pending (kind, idx) для пошаговой курации, или None."""
        for kind in ("h", "d"):
            for i, e in enumerate(self.items(kind)):
                if e["state"] == "pending":
                    return kind, i
        return None


def _to_params(sess: dict) -> dict:
    return sess  # сессия хранится «как есть» в proposals.params


def _from_row(p: Proposal) -> CurationSession:
    d = dict(p.params or {})
    return CurationSession(
        session_id=p.confirmation_id,
        chat_id=p.chat_id,
        campaign=d.get("campaign", ""),
        ad_group_id=d.get("ad_group_id", ""),
        ad_group_name=d.get("ad_group_name", ""),
        final_url=d.get("final_url", ""),
        brief=d.get("brief", {}),
        headlines=list(d.get("headlines", [])),
        descriptions=list(d.get("descriptions", [])),
    )


class SessionStore:
    """Персист сессии курации поверх таблицы proposals (operation=rsa_curation)."""

    async def create(
        self,
        *,
        chat_id: int,
        customer_id: str,
        campaign: str,
        ad_group_id: str,
        ad_group_name: str,
        final_url: str,
        headlines: list[str],
        descriptions: list[str],
        brief: dict | None = None,
    ) -> str:
        """Создать сессию из сгенерированных текстов. Возвращает session_id (hex uuid)."""
        session_id = uuid.uuid4().hex

        def _mk(texts: list[str], kind: str) -> list[dict]:
            out = []
            for t in texts:
                _ok, n = validate(t, _KIND_VALIDATE[kind])
                out.append({"text": t, "len": n, "state": "pending"})
            return out

        params = {
            "campaign": campaign,
            "ad_group_id": ad_group_id,
            "ad_group_name": ad_group_name,
            "final_url": final_url,
            "brief": brief or {},
            "headlines": _mk(headlines, "h"),
            "descriptions": _mk(descriptions, "d"),
        }
        async with Session() as s:
            s.add(
                Proposal(
                    confirmation_id=session_id,
                    operation=SESSION_OP,
                    customer_id=customer_id,
                    summary=f"RSA-курация: {campaign} / {ad_group_name}",
                    params=_to_params(params),
                    chat_id=chat_id,
                    user_initiated=False,  # сессия неисполнима, флаг неважен
                    status="pending",
                )
            )
            await s.commit()
        return session_id

    async def get(
        self, session_id: str, *, expected_chat_id: int | None = None
    ) -> CurationSession | None:
        """Снимок сессии. expected_chat_id (гард владения, мультиоператор): если задан — сессия
        видна ТОЛЬКО своему чату (чужой session_id не подойдёт по WHERE → None). None → без проверки
        (обратная совместимость)."""
        async with Session() as s:
            conds = [
                Proposal.confirmation_id == session_id,
                Proposal.operation == SESSION_OP,
            ]
            if expected_chat_id is not None:
                conds.append(Proposal.chat_id == expected_chat_id)
            p = (await s.execute(select(Proposal).where(*conds))).scalar_one_or_none()
            return _from_row(p) if p is not None else None

    async def _mutate(
        self, session_id: str, fn, *, expected_chat_id: int | None = None
    ) -> CurationSession | None:
        """Загрузить сессию, применить fn(params) и переприсвоить JSON (для детекта изменения).
        expected_chat_id — гард владения (как get): чужой чат не мутирует чужую сессию.

        FOR UPDATE (row-lock): курация RSA — read-modify-write JSON params; aiogram диспатчит
        callback-и параллельными тасками, поэтому два почти одновременных тапа (✅ approve + 🔁
        refine на одну сессию) без блокировки на Postgres дали бы lost-update — второй коммит затирал
        бы первый (потеря подтверждения/правки). with_for_update сериализует патчи (SQLite молча
        игнорирует — single-writer). Зеркалит проверенный фикс bot/campaign_wizard/store.py."""
        async with Session() as s:
            conds = [
                Proposal.confirmation_id == session_id,
                Proposal.operation == SESSION_OP,
            ]
            if expected_chat_id is not None:
                conds.append(Proposal.chat_id == expected_chat_id)
            p = (
                await s.execute(select(Proposal).where(*conds).with_for_update())
            ).scalar_one_or_none()
            if p is None:
                return None
            # deepcopy + flag_modified: JSON-колонка не отслеживает мутации вложенных структур;
            # без явного флага reassignment с общими вложенными ссылками не помечается dirty.
            d = copy.deepcopy(dict(p.params or {}))
            d.setdefault("headlines", [])
            d.setdefault("descriptions", [])
            fn(d)
            p.params = d
            flag_modified(p, "params")
            await s.commit()
            return _from_row(p)

    async def set_state(
        self,
        session_id: str,
        kind: str,
        idx: int,
        state: str,
        *,
        expected_chat_id: int | None = None,
    ) -> CurationSession | None:
        """Одобрить/отклонить элемент (state: approved|rejected|pending)."""
        field = _KIND_FIELD[kind]

        def _fn(d: dict) -> None:
            items = d[field]
            if 0 <= idx < len(items):
                items[idx]["state"] = state

        return await self._mutate(session_id, _fn, expected_chat_id=expected_chat_id)

    async def approve_all_valid(
        self, session_id: str, *, expected_chat_id: int | None = None
    ) -> CurationSession | None:
        """Одобрить все pending-элементы, укладывающиеся в лимит (длину считает КОД)."""

        def _fn(d: dict) -> None:
            for kind in ("h", "d"):
                for e in d[_KIND_FIELD[kind]]:
                    if e["state"] == "pending":
                        ok, _ = validate(e["text"], _KIND_VALIDATE[kind])
                        if ok:
                            e["state"] = "approved"

        return await self._mutate(session_id, _fn, expected_chat_id=expected_chat_id)

    async def replace_all(
        self,
        session_id: str,
        headlines: list[str],
        descriptions: list[str],
        *,
        expected_chat_id: int | None = None,
        mark: str = "approved",
    ) -> CurationSession | None:
        """List-UX (§10): заменить ВЕСЬ набор заголовков/описаний присланным менеджером списком —
        по умолчанию все `approved` (менеджер уже отредактировал и прислал финальный список).
        mark="pending" — регенерация набора (🔁 в визарде §19.5.2): новый набор снова проходит
        поэлементную курацию. Длину каждого элемента пересчитывает КОД (кириллица=1). Валидация
        мин/макс/длины — на стороне вызывающего (bot.main) ДО вызова."""

        def _mk(items: list[str], kind: str) -> list[dict]:
            out = []
            for t in items:
                _ok, n = validate(t, _KIND_VALIDATE[kind])
                out.append({"text": t, "len": n, "state": mark})
            return out

        def _fn(d: dict) -> None:
            d["headlines"] = _mk(headlines, "h")
            d["descriptions"] = _mk(descriptions, "d")

        return await self._mutate(session_id, _fn, expected_chat_id=expected_chat_id)

    async def replace_element(
        self,
        session_id: str,
        kind: str,
        idx: int,
        new_text: str,
        *,
        expected_chat_id: int | None = None,
    ) -> CurationSession | None:
        """Заменить текст элемента (после доработки) и вернуть его в pending. Длину пересчитывает КОД."""
        field = _KIND_FIELD[kind]
        _ok, n = validate(new_text, _KIND_VALIDATE[kind])

        def _fn(d: dict) -> None:
            items = d[field]
            if 0 <= idx < len(items):
                items[idx] = {"text": new_text, "len": n, "state": "pending"}

        return await self._mutate(session_id, _fn, expected_chat_id=expected_chat_id)
