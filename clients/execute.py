"""§20: исполнитель подтверждённых memory-операций (профиль клиента) за confirm-гейтом.

Домен «memory» отделён от домена «google-ads» (ads.service.execute_confirmed): операции профиля
(save/update/clear) — это ЛОКАЛЬНАЯ БД, а не Google Ads. Замок аккаунта (ads.client.ensure_allowed)
и SDK к ним НЕ применяются. Форма зеркалит ads.service.execute_confirmed:
  get_confirmed → status=='confirmed' → op ∈ MEMORY_OPERATIONS (defense-in-depth) →
  claim (АТОМАРНО, одноразово — защита от replay) → writer (clients.store) → finalize (+audit).

bot.main._do_confirm маршрутизирует ✅ сюда, если proposal.operation ∈ MEMORY_OPERATIONS, иначе в
ads.service.execute_confirmed. Ошибка внутри пробрасывается — _do_confirm пишет record_failure.

Cross-domain инвариант: memory-op, случайно дошедший до ads.service.execute_confirmed, отвергается
там (op вне SUPPORTED_OPERATIONS, fail-closed); ads-op здесь — вне MEMORY_OPERATIONS (тоже отказ).
"""

from __future__ import annotations

from clients.store import ClientProfileStore

# Единый источник истины: какие операции исполняет ЭТОТ (memory) домен. Всё вне списка — не наше.
MEMORY_OPERATIONS: frozenset[str] = frozenset({"profile_save", "profile_update", "profile_clear"})


async def execute_confirmed_memory(store, confirmation_id: str) -> dict:
    """store — ConfirmStore. Исполнить подтверждённую memory-операцию профиля. Возвращает result
    или бросает (ValueError/PermissionError) — вызывающий (_do_confirm) запишет record_failure."""
    p = await store.get_confirmed(confirmation_id)
    if p is None:
        raise ValueError("черновик не найден")
    if p.status != "confirmed":
        raise PermissionError(f"черновик не подтверждён (status={p.status})")

    op = p.operation
    if op not in MEMORY_OPERATIONS:  # defense-in-depth: не наша операция (зеркало ads.service:161)
        raise PermissionError(
            f"операция '{op}' не относится к домену профиля (memory) — выполнение отклонено"
        )

    # Атомарно застолбить (confirmed→executing, одноразово). Второй вызов (повтор/гонка) → None.
    claimed = await store.claim(confirmation_id, operation=op)
    if claimed is None:
        raise PermissionError(
            "черновик уже исполнен или недействителен (повтор/гонка) — переподтверди команду"
        )

    params = claimed.params or {}
    customer_id = params.get("customer_id") or claimed.customer_id
    profile_store = ClientProfileStore()

    if op in ("profile_save", "profile_update"):
        patch = params.get("patch") or {}
        result = await profile_store.apply_upsert(
            customer_id,
            patch,
            operation=op,
            confirmation_id=confirmation_id,
            source=params.get("source", "text"),
            crawl_extra=params.get(
                "crawl_extra"
            ),  # §20.4: сайт/карта страниц (при краул-обновлении)
        )
    elif op == "profile_clear":
        result = await profile_store.apply_clear(
            customer_id, operation=op, confirmation_id=confirmation_id
        )
    else:  # не должно случиться: op ∈ MEMORY_OPERATIONS, но ветки нет (рассинхрон). Fail-closed.
        raise ValueError(f"memory-операция '{op}' заявлена, но не имеет обработчика (баг)")

    await store.finalize(confirmation_id, result=result)
    return result
