"""§20: хранилище досье (client_dossiers). Чистый слой БД — оркестрацию держат bot.main/clients.execute.

Жизненный цикл строки: `save_draft` (собрали по краулу, ждём ✅) → `promote` (подтвердили). В
'current' одновременно ровно одна строка на аккаунт — прошлая при повышении УДАЛЯЕТСЯ, а не
архивируется: в досье лежат имена сотрудников клиента (чужая PII), копить их версиями незачем
(правило 5; тот же аргумент, почему досье не живёт в client_profiles — history переживает clear).

Правила 1–2: `promote` вызывается ТОЛЬКО из clients.execute.execute_confirmed_memory ПОСЛЕ атомарного
`ConfirmStore.claim` — либо напрямую при auto-save свежего профиля (гейта нет и у самого профиля:
затирать нечего). Гард монотонности: черновик старее текущего досье не повышается (подтвердили
устаревшую карточку после нового краула → тихо отбрасываем, current не откатывается).
"""

from __future__ import annotations

from sqlalchemy import delete, func, select

from clients.dossier_render import CONTEXT_MAX_CHARS
from db.models import ClientDossier, ClientProfile
from db.session import Session


class ClientDossierStore:
    async def save_draft(
        self,
        customer_id: str,
        *,
        markdown: str,
        llm_context: str,
        data: dict | None = None,
    ) -> int:
        """Записать черновик досье. Возвращает id строки — он кладётся в proposal.params, и по нему
        (а не «по последнему») досье повышается на ✅: два краула подряд не перепутают черновики.
        Прежние ЧЕРНОВИКИ этого аккаунта удаляются — неподтверждённый мусор не копится."""
        async with Session() as s:
            pid = (
                await s.execute(
                    select(ClientProfile.id).where(ClientProfile.customer_id == customer_id)
                )
            ).scalar_one_or_none()
            ver = (
                await s.execute(
                    select(func.coalesce(func.max(ClientDossier.version), 0)).where(
                        ClientDossier.customer_id == customer_id
                    )
                )
            ).scalar_one() or 0
            await s.execute(
                delete(ClientDossier).where(
                    ClientDossier.customer_id == customer_id, ClientDossier.status == "draft"
                )
            )
            row = ClientDossier(
                customer_id=customer_id,
                profile_id=pid,
                version=int(ver) + 1,
                status="draft",
                markdown=markdown,
                llm_context=llm_context,
                data=data,
            )
            s.add(row)
            await s.commit()
            return int(row.id)

    async def promote(
        self, dossier_id: int, *, customer_id: str, confirmation_id: str | None = None
    ) -> bool:
        """Черновик → current (вызывать ТОЛЬКО после claim подтверждения). True, если повысили.

        False (и черновик удаляется) если: строки нет / она чужая / уже есть более СВЕЖЕЕ current —
        гард монотонности, иначе подтверждение старой карточки откатило бы досье назад."""
        async with Session() as s:
            row = (
                await s.execute(select(ClientDossier).where(ClientDossier.id == dossier_id))
            ).scalar_one_or_none()
            if row is None or row.customer_id != customer_id:
                return False
            if row.status == "current":
                return True  # идемпотентность: повторный ✅ по тому же черновику
            cur = (
                (
                    await s.execute(
                        select(ClientDossier).where(
                            ClientDossier.customer_id == customer_id,
                            ClientDossier.status == "current",
                        )
                    )
                )
                .scalars()
                .all()
            )
            if any(c.version > row.version for c in cur):
                await s.delete(row)  # устаревший черновик: current новее — назад не откатываем
                await s.commit()
                return False
            for c in cur:  # прошлое досье не архивируем — в нём PII (см. докстринг модуля)
                await s.delete(c)
            pid = (
                await s.execute(
                    select(ClientProfile.id).where(ClientProfile.customer_id == customer_id)
                )
            ).scalar_one_or_none()
            row.status = "current"
            row.profile_id = pid or row.profile_id
            row.confirmation_id = confirmation_id
            await s.commit()
            return True

    async def get_current(self, customer_id: str) -> dict | None:
        """Текущее досье аккаунта (для кнопки «📄 Досье» и контекста генераторов). Нет — None."""
        async with Session() as s:
            row = (
                (
                    await s.execute(
                        select(ClientDossier).where(
                            ClientDossier.customer_id == customer_id,
                            ClientDossier.status == "current",
                        )
                    )
                )
                .scalars()
                .first()
            )
        if row is None:
            return None
        return {
            "id": row.id,
            "version": row.version,
            "markdown": row.markdown or "",
            "llm_context": row.llm_context or "",
            "data": row.data or {},
            "created_at": row.created_at,
        }

    async def context_text(self, customer_id: str, *, max_chars: int = CONTEXT_MAX_CHARS) -> str:
        """PII-free контекст текущего досье для генераторов RSA/ключей. Нет досье → ''."""
        async with Session() as s:
            txt = (
                (
                    await s.execute(
                        select(ClientDossier.llm_context).where(
                            ClientDossier.customer_id == customer_id,
                            ClientDossier.status == "current",
                        )
                    )
                )
                .scalars()
                .first()
            )
        return (txt or "")[: max(0, max_chars)]
