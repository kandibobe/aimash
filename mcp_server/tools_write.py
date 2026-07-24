"""PROPOSE-инструменты MCP-слоя (Волна 2, propose-only WRITE-MCP): агент СОЗДАЁТ черновик мутации и
показывает «было → станет», но Google Ads НЕ трогает. Мутация и подтверждение разделены (золотое
правило #1): здесь только левая половина — черновик; исполнение (execute_confirmed) — отдельный,
ещё не выпущенный контур за реплай-якорем (§6.4, блокирован пробой V7–V9 на VPS).

Почему это безопасно выпускать первым: propose ничего не исполняет. `build_proposal` лишь ЧИТАЕТ
текущее значение («было») и пишет строку в НАШУ БД (`ConfirmStore.save_proposal`) — ни один
`ads/mutations.py::apply_*` отсюда не достижим. Значит гейт подтверждения propose не нужен, а
prompt-injection через внешний контент в худшем случае создаёт БЕЗвредный черновик, который человек
всё равно увидит и не подтвердит.

Три вещи этот слой делает КОДОМ, не доверием к модели:
  • **Провенанс (правило 3).** Денежный черновик (бюджет/ставка) создаётся только когда бит
    `human_turn` поднят доверенным входом (`core.provenance`) — не из cron/anomaly/self-improve и не
    по аргументу инструмента (агент его подделать не может). `user_initiated` черновика берётся из
    того же провенанса, а не из параметра.
  • **И8 (правило 13).** Не более ОДНОГО черновика на ассистентский ход. Счёт — свойство ХРАНИЛИЩА
    (`ConfirmStore.count_run_proposals` по run-корреляции), а не in-memory-счётчик: агентский цикл
    делает много последовательных итераций, и счётчик в процессе пережил бы не каждую.
  • **Валидация входа (правило 4).** Диапазоны/режимы/валюту проверяет Pydantic (`UpdateBudget`/
    `UpdateBid`), кривой вход → редактированный отказ, а не «доверие к модели».

⛔ **Реестр `PROPOSE_TOOL_FUNCS` НАМЕРЕННО не регистрируется в `mcp_server.server.build_server`**
(§15.2: WRITE-слой не выходит в прод, пока не подтверждён хотя бы один канал доставки/якоря; И7
taint-гейт ещё не собран). Слой существует, импортируется и тестируется офлайн — но на живой
MCP-поверхности его нет. Регистрацию добавит шаг проводки WRITE, вместе с execute-контуром.

Границы слоя (правило 6, тонкий тул-слой): здесь ровно валидация входа + вызов существующего
`build_proposal` + сериализация конверта. Вся логика гейтов до кнопок — в `mcp_server.propose`;
счёт И8 — в `confirm.store`; провенанс — в `core.provenance`. Ни строки бизнес-логики тут.
"""

from __future__ import annotations

import uuid
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ValidationError

from agent.tools.schemas import MUTATION_TOOLS, UpdateBid, UpdateBudget
from ads.resolve import MONEY_OPS
from confirm.store import ConfirmStore
from core import i18n
from core.context import get_context
from core.guards import require_no_mutations
from core.logging import log
from core.provenance import get_provenance
from mcp_server.envelope import classify_error, proposed, refused
from mcp_server.propose import ProposalRefused, build_proposal
from mcp_server.redact import redact_error


def _validation_text(exc: ValidationError) -> str:
    """Компактный (loc: msg) первых ошибок Pydantic — чтобы агент понял, ЧТО переформулировать. Не
    сырой repr исключения: отдаём только (поле, сообщение), а весь текст оборачиваем i18n-ключом.
    Сообщения валидаторов схем про диапазоны/режимы (не секреты), но полный repr всё равно не льём."""
    parts = []
    for err in exc.errors()[:4]:
        loc = ".".join(str(x) for x in err.get("loc", ())) or "?"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return i18n.t("propose_bad_params", details="; ".join(parts))


async def _propose(
    operation: str,
    model_cls: type[BaseModel],
    *,
    account: str,
    **fields: Any,
) -> dict[str, Any]:
    """Общая механика propose-инструмента. ЛЮБОЙ отказ — редактированный `refused()`-конверт (правило
    5: сырой str(e) наружу не идёт). Успех — `proposed()`-конверт с `preview` «было → станет».

    Порядок гейтов fail-closed и значим:
      1) валидация входа моделью (диапазоны/режимы/валюта — КОД, не доверие);
      2) провенанс: денежный черновик только человеческим ходом (правило 3);
      3) контекст хода: черновику нужен чат доставки/подтверждения (fail-closed);
      4) И8: не более одного черновика на ход (счёт из хранилища по run-корреляции);
      5) сборка+сохранение черновика (Google Ads не тронут).
    """
    lang = i18n.current_lang()
    # 1) Диапазоны/режимы/валюту считает КОД (Pydantic), не доверие к модели (правило 4). Кривой
    #    вход — вина ВЫЗЫВАЮЩЕГО (агент собрал плохие params) → invalid_argument, черновик не создан.
    try:
        model = model_cls(**fields)
    except ValidationError as e:
        return refused(_validation_text(e), error_code="invalid_argument")

    prov = get_provenance()
    # 2) Правило 3: денежный черновик — только прямой командой ЧЕЛОВЕКА в этом ходе. Бит human_turn
    #    поднимает ТОЛЬКО доверенный вход (core.provenance.human_turn), агент/аргумент его не
    #    подделают. Машинный ход (cron/anomaly/self-improve) → отказ ДО создания черновика.
    if operation in MONEY_OPS and not prov.human_turn:
        return refused(i18n.t("propose_requires_human", lang), error_code="refused")
    # 3) Черновику нужен чат доставки/подтверждения. Его несёт доверенный КОНТЕКСТ хода
    #    (core.context, ставит транспорт), а НЕ аргумент инструмента. Нет — отказ (fail-closed):
    #    безадресный денежный черновик — это черновик, который некому показать и подтвердить.
    chat_id = get_context().chat_id
    if chat_id is None:
        return refused(i18n.t("propose_no_turn_context", lang), error_code="refused")
    # 4) И8 (правило 13): не более одного черновика на ассистентский ход. Счёт — из ХРАНИЛИЩА по
    #    run-корреляции (не in-memory), fail-closed: уже есть черновик в этом run → отказ.
    store = ConfirmStore()
    if await store.count_run_proposals(prov.run_id) >= 1:
        return refused(i18n.t("propose_draft_limit", lang), error_code="refused")
    # 5) Сборка + СОХРАНЕНИЕ черновика. build_proposal только ЧИТАЕТ «было» и пишет строку в нашу БД
    #    — mutations.apply_* не вызывается, Google Ads не тронут. user_initiated — из доверенного
    #    провенанса (не из аргумента): к этой точке human_turn для денежной операции уже True.
    cid = uuid.uuid4().hex
    try:
        built = await build_proposal(
            store=store,
            operation=operation,
            params=model.model_dump(exclude_none=True),
            cid=cid,
            chat_id=chat_id,
            customer_id=str(account),
            # Валюту, если модель её проставила, трактуем как ОСОЗНАННЫЙ выбор (структурный вход, не
            # NL): отдаём как user_text, чтобы build_proposal не снял её эвристикой NL-пути, а честно
            # сверил с валютой аккаунта (currency_mismatch) — иначе «было→станет» соврал бы про сумму.
            user_text=str(getattr(model, "currency", "") or ""),
            lang=lang,
            user_initiated=prov.human_turn,
        )
    except ProposalRefused as e:
        # Штатный отказ гейта до кнопок (замок аккаунта / валюта ≠ аккаунтной / невыполнимое снижение).
        # Текст сформирован КОДОМ (i18n в build_proposal) — редактировать нечего.
        return refused(e.text, error_code="refused")
    except Exception as e:  # noqa: BLE001 — граница слоя: наружу только редактированное (правило 5)
        log.warning("mcp propose tool failed: %s", type(e).__name__)
        return refused(redact_error(e), error_code=classify_error(e))
    return proposed(
        confirmation_id=built.cid,
        operation=built.operation,
        customer_id=built.customer_id,
        preview=built.display,
    )


# ── Инструменты ───────────────────────────────────────────────────────────────────


async def propose_budget_change(
    account: str,
    campaign: str,
    mode: str,
    value: float,
    currency: str | None = None,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК изменения дневного бюджета кампании и показать «было → станет». Google Ads НЕ
    изменяется: читаю текущий бюджет и сохраняю черновик — применит его человек отдельным
    подтверждением. Денежная операция: черновик создаётся только по прямой команде человека в этом ходе.

    account — id аккаунта мутации (10 цифр). campaign — точное имя кампании.
    mode — как менять: increase_by_percent | increase_by_amount | decrease_by_percent |
    decrease_by_amount | set_to. value — всегда положительное число (направление несёт mode; процент
    ≤1000, снижение <100%). currency — код валюты (напр. USD) ТОЛЬКО если её явно назвал человек;
    иначе опусти — сумма трактуется в валюте аккаунта.

    Успех: конверт `status='pending'` + `confirmation_id` + `preview` («было → станет»). Отказ:
    `status='refused'`, причина в `error` (замок аккаунта, валюта ≠ аккаунтной, невыполнимое снижение,
    лимит И8 «один черновик на ход», кривой вход)."""
    return await _propose(
        "update_budget",
        UpdateBudget,
        account=account,
        campaign=campaign,
        mode=mode,
        value=value,
        currency=currency,
    )


async def propose_bid_change(
    account: str,
    campaign: str,
    mode: str,
    value: float,
    currency: str | None = None,
) -> dict[str, Any]:
    """Создать ЧЕРНОВИК изменения ставки CPC кампании (уровень групп объявлений) и показать «было →
    станет». Google Ads НЕ изменяется: читаю текущую ставку и сохраняю черновик — применит его человек
    отдельным подтверждением. Ставка осмысленна только на ручной стратегии (MANUAL_CPC/ECPC); на Smart
    Bidding ставку решает Google. Денежная операция: только по прямой команде человека в этом ходе.

    account — id аккаунта мутации (10 цифр). campaign — точное имя кампании.
    mode — как менять: increase_by_percent | decrease_by_percent | set_to. value — всегда
    положительное число (направление несёт mode; процент ≤1000, снижение <100%). currency — код
    валюты ТОЛЬКО если её явно назвал человек; иначе опусти (в валюте аккаунта).

    Успех: конверт `status='pending'` + `confirmation_id` + `preview`. Отказ: `status='refused'`,
    причина в `error` (замок аккаунта, валюта ≠ аккаунтной, лимит И8, кривой вход)."""
    return await _propose(
        "update_bid",
        UpdateBid,
        account=account,
        campaign=campaign,
        mode=mode,
        value=value,
        currency=currency,
    )


# Реестр: имя propose-инструмента → функция. Существует для тестов и БУДУЩЕЙ проводки WRITE; в
# build_server НЕ регистрируется (см. шапку модуля, §15.2).
PROPOSE_TOOL_FUNCS: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    "propose_budget_change": propose_budget_change,
    "propose_bid_change": propose_bid_change,
}

# И4 / construction-time (не комментарий — барьер, роняет ИМПОРТ модуля): имена propose-инструментов
# НЕ пересекаются с мутационными (update_budget/update_bid/create_*…). propose создаёт ЧЕРНОВИК —
# строку в нашей БД, а не мутацию Google Ads; коллизия имени означала бы, что настоящая мутация
# выставлена под видом propose. `require_no_mutations` бросит RuntimeError на импорте (не assert:
# под -O он бы исчез — core.guards).
require_no_mutations(
    PROPOSE_TOOL_FUNCS,
    MUTATION_TOOLS,
    rule="И4",
    subject="mcp_server.tools_write.PROPOSE_TOOL_FUNCS (propose-инструменты)",
)

PROPOSE_MCP_TOOLS: frozenset[str] = frozenset(PROPOSE_TOOL_FUNCS)
