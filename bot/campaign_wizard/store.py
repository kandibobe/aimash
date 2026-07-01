"""Персист состояния визарда «Создание кампании» (§19) поверх таблицы campaign_drafts.

Накопленный черновик (wizard_state JSON) переживает рестарт бота и Этап-2 round-trip с Google
Sheets. Это НЕ proposal: в Google Ads ничего не меняется, пока финальное «Создать черновик» не
выпустит отдельный proposal create_search_campaign (confirm-гейт, см. bot.main / ads.service).

Паттерн мутации JSON-колонки скопирован с adcopy.session.SessionStore: deepcopy → mutate →
reassign → flag_modified (JSON-колонка не отслеживает изменения вложенных структур). Все мутации
имеют гард владения expected_chat_id (мультиоператор: чужой session_id не подойдёт по WHERE).
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from db.models import CampaignDraft
from db.session import Session


def empty_wizard_state() -> dict:
    """Скелет wizard_state со всеми ключами всех этапов — хендлеры рассчитывают на их наличие."""
    return {
        "settings": {},  # campaign_name/geo/languages/budget_daily_micros/bidding/.../by_analogy
        "keywords": {
            "list": [],
            "match_type": "phrase",
            "source": None,  # sheet|file|text|generated
            "sheet_id": None,
            "verified": False,
        },
        "ad": {
            "final_url": None,
            "path1": None,
            "path2": None,
            "rsa_session_id": None,  # ссылка на adcopy.session SessionStore (Этап 3)
            "headlines": [],
            "descriptions": [],
        },
        "images": {"media_ids": [], "skipped": False, "eligible": None},
        "assets": {"reuse_links": [], "new": []},  # new: [{"family":..., "params":{...}}]
        "url_options": {
            "tracking_url_template": None,
            "final_url_suffix": None,
            "custom_parameters": {},
        },
    }


@dataclass
class DraftSnapshot:
    """Снимок черновика для бота (отвязан от ORM-сессии)."""

    session_id: str
    chat_id: int
    customer_id: str
    preview_customer_id: str | None
    current_step: int
    status: str
    wizard_state: dict


def _snap(p: CampaignDraft) -> DraftSnapshot:
    return DraftSnapshot(
        session_id=p.session_id,
        chat_id=p.chat_id,
        customer_id=p.customer_id,
        preview_customer_id=p.preview_customer_id,
        current_step=p.current_step,
        status=p.status,
        wizard_state=dict(p.wizard_state or {}),
    )


class CampaignDraftStore:
    """Хранилище черновиков визарда «Создание кампании»."""

    async def create(
        self, *, chat_id: int, customer_id: str, preview_customer_id: str | None = None
    ) -> str:
        """Создать НОВЫЙ активный черновик (предыдущие активные этого чата — в abandoned, чтобы
        активный был один). Возвращает session_id (hex uuid)."""
        session_id = uuid.uuid4().hex
        async with Session() as s:
            # один активный на чат: гасим прежние активные (start-over)
            prev = (
                (
                    await s.execute(
                        select(CampaignDraft).where(
                            CampaignDraft.chat_id == chat_id, CampaignDraft.status == "active"
                        )
                    )
                )
                .scalars()
                .all()
            )
            for row in prev:
                row.status = "abandoned"
            s.add(
                CampaignDraft(
                    session_id=session_id,
                    chat_id=chat_id,
                    customer_id=customer_id,
                    preview_customer_id=preview_customer_id,
                    current_step=0,
                    wizard_state=empty_wizard_state(),
                    status="active",
                )
            )
            await s.commit()
        return session_id

    async def get(
        self, session_id: str, *, expected_chat_id: int | None = None
    ) -> DraftSnapshot | None:
        async with Session() as s:
            p = await self._load(s, session_id, expected_chat_id)
            return _snap(p) if p is not None else None

    async def get_active(self, chat_id: int) -> DraftSnapshot | None:
        """Текущий активный черновик чата (для возобновления после рестарта), либо None."""
        async with Session() as s:
            p = (
                await s.execute(
                    select(CampaignDraft)
                    .where(CampaignDraft.chat_id == chat_id, CampaignDraft.status == "active")
                    .order_by(CampaignDraft.updated_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            return _snap(p) if p is not None else None

    async def patch(
        self,
        session_id: str,
        fn: Callable[[dict], None],
        *,
        expected_chat_id: int | None = None,
    ) -> DraftSnapshot | None:
        """Применить fn(wizard_state) к накопленному состоянию и переприсвоить JSON. fn мутирует
        словарь wizard_state на месте. Возвращает обновлённый снимок или None (нет/чужой/неактивный).
        active_only: правки идут ТОЛЬКО на active-черновик (поздний колбэк после abandon/done —
        None, как обещает контракт; иначе scheduler-abandon с живым FSM «воскресил» бы черновик)."""
        async with Session() as s:
            p = await self._load(s, session_id, expected_chat_id, active_only=True)
            if p is None:
                return None
            # deepcopy + flag_modified: JSON-колонка не помечается dirty при мутации вложенного.
            state = copy.deepcopy(dict(p.wizard_state or {}))
            for k, v in empty_wizard_state().items():
                state.setdefault(k, v)
            fn(state)
            p.wizard_state = state
            flag_modified(p, "wizard_state")
            await s.commit()
            return _snap(p)

    async def set_preview(
        self,
        session_id: str,
        preview_customer_id: str | None,
        *,
        expected_chat_id: int | None = None,
    ) -> DraftSnapshot | None:
        """Зафиксировать выбранный на Этапе-0 дочерний аккаунт (read-only превью). Аккаунт МУТАЦИИ
        (customer_id) НЕ трогаем — он всегда Draft."""
        async with Session() as s:
            p = await self._load(s, session_id, expected_chat_id, active_only=True)
            if p is None:
                return None
            p.preview_customer_id = preview_customer_id
            await s.commit()
            return _snap(p)

    async def set_step(
        self, session_id: str, step: int, *, expected_chat_id: int | None = None
    ) -> DraftSnapshot | None:
        async with Session() as s:
            p = await self._load(s, session_id, expected_chat_id, active_only=True)
            if p is None:
                return None
            p.current_step = int(step)
            await s.commit()
            return _snap(p)

    async def finish(self, session_id: str, *, expected_chat_id: int | None = None) -> None:
        await self._set_status(session_id, "done", expected_chat_id)

    async def abandon(self, session_id: str, *, expected_chat_id: int | None = None) -> None:
        await self._set_status(session_id, "abandoned", expected_chat_id)

    # ── внутреннее ──────────────────────────────────────────────────────────────
    async def _set_status(self, session_id: str, status: str, expected_chat_id: int | None) -> None:
        async with Session() as s:
            p = await self._load(s, session_id, expected_chat_id)
            if p is None:
                return
            p.status = status
            await s.commit()

    @staticmethod
    async def _load(
        s, session_id: str, expected_chat_id: int | None, *, active_only: bool = False
    ) -> CampaignDraft | None:
        conds = [CampaignDraft.session_id == session_id]
        if expected_chat_id is not None:
            conds.append(CampaignDraft.chat_id == expected_chat_id)
        if active_only:  # правки — только на активный черновик (done/abandoned не воскрешаем)
            conds.append(CampaignDraft.status == "active")
        return (await s.execute(select(CampaignDraft).where(*conds))).scalar_one_or_none()
