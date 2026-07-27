"""Сборка черновика мутации БЕЗ транспорта: замок аккаунта → снимок «было» → валютная сверка →
аттестация свежести → человеческий diff → строка в БД. Всё, что обязано случиться ДО кнопок, и
ничего о том, КАК кнопки показаны.

Зачем отдельный модуль (Ш3): до разреза эти восемь десятков строк умел выполнить только
aiogram-хендлер — `bot.main._present_proposal` принимал `Message` и отвечал прямо в чат. Любой
второй контур подтверждения (MCP-WRITE, свой поллер Telegram) либо тащил бы за собой aiogram,
либо переписывал бы гейты заново — а гейт, продублированный руками, расходится с оригиналом
МОЛЧА. Модуль не импортирует aiogram: приколочено
`tests/test_proposal_build.py::test_build_is_transport_free`.

Переехал `bot/proposal.py` → `mcp_server/propose.py` (Волна 2, propose-only WRITE-MCP): это
единственный bot-файл, которому CLAUDE.md предписан переезд в тул-слой, а не архивация на месте.
Причина переезда именно сейчас — headless-развязка: `mcp_server.tools_write` зовёт `build_proposal`
как свой primitive, а импорт `bot.*` из MCP-процесса протащил бы весь Telegram-слой в `sys.modules`
(гард `tests/test_headless_bootstrap.py`). Первый и единственный реальный потребитель `build_proposal`
— MCP-инструмент `propose_*`; кнопочный бот идёт своим синхронным `bot.main._build_proposal`.

Отказы — исключение `ProposalRefused` с ГОТОВЫМ текстом: его формирует КОД (не SDK и не модель),
поэтому редактировать на выходе нечего, а транспорт только показывает `.text`.

`user_initiated` — параметр с fail-closed дефолтом False (golden rule #3): бит «прямая команда
человека» ставит доверенный вход, а не тот, кто просто вызвал сборку. Второй бит провенанса
(`origin_human_turn`) сюда не пробрасывается ВОВСЕ — его store читает из `core.provenance`,
поднять которую может только доверенный слой.
"""

from __future__ import annotations

from dataclasses import dataclass

from ads.client import DRAFT_ACCOUNT_ID, ensure_allowed
from ads.freshness import attach_freshness
from ads.resolve import (
    MONEY_OPS,
    DecreaseBelowZero,
    currency_mismatch,
    detect_currency_token,
)
from ads.service import read_state
from core import i18n
from confirm import render
from confirm.attachment import plan_attachment
from confirm.store import ConfirmStore
from core.config import normalize_customer_id
from core.resilience import run_ads_read_call

# Денежные операции (UI-слой): для них при внешнем контенте (файл/ссылка) в сводку добавляется
# предупреждение. Зеркалит реестр _EXPECTED_MONEY_OPS в
# tests/test_invariants_core.py (имена op без префикса apply_) — дрейф ловит тест.
MONEY_OPS_UI: frozenset[str] = frozenset(
    {
        "update_budget",
        "update_bid",
        "update_keyword_bid",
        "set_bidding_strategy",
        "create_search_campaign",
        "create_gdn_campaign",
        "create_demand_gen_campaign",
        "create_video_campaign",
    }
)


class ProposalRefused(Exception):
    """Гейт отказал ДО показа кнопок. `text` — готовое сообщение человеку (сформировано кодом)."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


@dataclass(slots=True)
class BuiltProposal:
    """Черновик уже в БД, текст уже человеческий — транспорту осталось только показать."""

    cid: str
    operation: str
    customer_id: str
    params: dict  # с аттестацией свежести: `_freshness` всегда, `_before` — только при успехе
    display: str  # баннер аккаунта + «было → станет» (+ предупреждение о внешнем контенте)


def account_label(cid: str) -> str:
    """Человекочитаемая метка аккаунта для карточки/сообщений: «Имя · id» или id. Draft — явно."""
    cid = normalize_customer_id(cid)
    if cid == DRAFT_ACCOUNT_ID:
        return f"Aimash (Draft) · {cid}"
    from ads.client import discovered_read_children_meta

    meta = discovered_read_children_meta().get(cid)
    name = (getattr(meta, "name", "") or "").strip() if meta else ""
    return f"{name} · {cid}" if name else cid


async def read_account_currency(client, customer_id: str | None = None) -> str:
    """Код валюты аккаунта (§9) для денежных метрик. '' при сбое чтения — отчёт/статистику
    показываем и без валюты (не блокируем). Кэш — в ads.read.account_currency.
    customer_id — активный аккаунт чтения (§6 /account); None ⇒ Draft (прежнее поведение)."""
    from ads.read import account_currency

    try:
        return await run_ads_read_call(
            account_currency, client, customer_id or DRAFT_ACCOUNT_ID, label="account_currency"
        )
    except Exception:  # noqa: BLE001 — валюта необязательна, не роняем отчёт
        return ""


async def build_proposal(
    *,
    store: ConfirmStore,
    operation: str,
    params: dict,
    cid: str,
    chat_id: int,
    summary: str = "",
    customer_id: str | None = None,
    external_context: bool = False,
    user_text: str = "",
    lang: str = "ru",
    user_initiated: bool = False,
) -> BuiltProposal:
    """Собрать и СОХРАНИТЬ черновик мутации; вернуть готовый к показу текст. Транспорта не знает.

    customer_id — аккаунт МУТАЦИИ, штампуемый в черновик (authoritative: execute_confirmed исполняет
    именно его, с повторным ensure_allowed). G2/G3: не задан → DRAFT (базовый).

    external_context=True — предложение родилось при наличии СПРАВОЧНОГО контента из файла/ссылки
    (prompt-injection поверхность): для ДЕНЕЖНЫХ операций префиксуем сводку предупреждением
    «сумма могла быть предложена внешним контентом» (попадает и в audit-summary). Механику
    user_initiated это НЕ меняет — последний гейт всё равно человек с diff и ✅.

    user_text — исходная реплика человека; нужна ровно для одного решения (писал ли он валюту),
    поэтому передаётся строкой, а не объектом сообщения.

    Бросает ProposalRefused: замок аккаунта, невыполнимое снижение, валютный mismatch."""
    # G2/G3: дефолт — Draft (базовый). Не-Draft приходит ЯВНО (agent-loop/меню/визард на активном
    # аккаунте, §8). Не-Draft обязан быть включён на мутации (allowed_customer_ids), иначе отказ ДО карточки.
    if customer_id is None:
        customer_id = DRAFT_ACCOUNT_ID
    if customer_id != DRAFT_ACCOUNT_ID:
        try:
            ensure_allowed(customer_id)
        except PermissionError:
            raise ProposalRefused(
                i18n.t("mutation_account_read_only", lang, acct=account_label(customer_id))
            ) from None
    # §5: читаем ТЕКУЩЕЕ значение (бюджет/ставку/статус) ДО показа → реальный diff «было → станет»
    # и снимок-база для оптимистичной сверки при исполнении (TOCTOU). Волна 1.1: берём Snapshot, а не
    # голый dict — исполнению нужна не только сама база, но и КЛАССИФИКАЦИЯ («не прочитали» ≠ «нечего
    # сверять»), иначе freshness-гейт на исполнении не отличит одно от другого.
    try:
        snap = await read_state(operation, params, customer_id=customer_id)
    except DecreaseBelowZero as e:
        # §5: «снизь бюджет на 200» при бюджете 100 — невыполнимо. Отказ ДО кнопок; текст
        # сформирован КОДОМ (не SDK/не модель) → редактировать нечего.
        raise ProposalRefused("⚠️ " + str(e)) from None
    # P0 (golden rule #4): денежная команда в валюте ≠ валюте аккаунта → отказ с уточнением ДО
    # показа кнопок (FX не делаем; иначе «было→станет» соврал бы про сумму). Валюта — best-effort:
    # неизвестна (нет клиента/сбой read) ⇒ не блокируем (и чужую валюту на показе не печатаем).
    if operation in MONEY_OPS:
        # P0-1: голая цифра без валютного слова. Модель порой всё равно ставит currency (обычно
        # 'USD') — детерминированно снимаем её, если пользователь валюту НЕ писал, чтобы сумма
        # трактовалась в валюте аккаунта, а не рождала ложный mismatch (петля «переформулируй в
        # AUD»). Источник текста — сообщение пользователя (NL-путь). Символ/код валюты в тексте →
        # уважаем выбор модели (честный mismatch, если валюта ≠ аккаунтной; FX не делаем).
        claimed_cur = params.get("currency")
        if claimed_cur and claimed_cur != "percent" and not detect_currency_token(user_text):
            params = {**params, "currency": None}
        acct_cur = ""
        try:
            from ads.client import build_client_async

            # Валюта именно аккаунта МУТАЦИИ (customer_id), а не всегда Draft: при включённом
            # не-Draft аккаунте (управляемый список) команда «бюджет 100 UAH» на UAH-child не
            # должна ложно отклоняться по валюте USD-Draft, а «было→станет» — врать про сумму.
            acct_cur = await read_account_currency(
                await build_client_async(customer_id), customer_id
            )
        except Exception:  # noqa: BLE001 — валюту не определить → без FX-сверки, не роняем показ
            acct_cur = ""
        mismatch = currency_mismatch(operation, params, acct_cur)
        if mismatch:
            raise ProposalRefused("⚠️ " + mismatch)
    # Снимок + аттестация свежести в одном месте: `_before` как раньше (только при успехе),
    # `_freshness` — всегда. Без маркера черновик считается непрочитанным (fail-closed).
    params = attach_freshness(params, snap)
    # Человекочитаемая сводка по operation+params (деньги — реальное «40.00 → 48.00 (+20%)»).
    # Для create_rsa/create_gdn у вызывающего свой богатый summary → рендер вернёт "".
    # Волна 1b: обещание вложения и обязательство его доставить рождаются ИЗ ОДНОГО решения. `spec`
    # решает оба: он же включает фразу «полный список во вложении» в тексте, он же ставит
    # attachment_state='pending' в той же вставке ниже. Разъехаться им негде — раньше первое жило в
    # confirm/render.py (6 операций), второе в bot/main.py::_KEYWORD_OPS (4), и обещание уезжало в
    # summary → audit-row, из которого правило 15 репортит «выполнено».
    spec = plan_attachment(operation, params, cid=cid, lang=lang)
    display = (
        render.fmt_mutation_summary(operation, params, lang, attachment=spec is not None) or summary
    )
    # AD.2: баннер аккаунта — на КАЖДОЙ карточке (и в audit), включая Draft. Раз выбрано «одно
    # подтверждение везде», всегда-видимый ярлык — единственная страховка от мутации не того
    # аккаунта: менеджер видит, на ЧЬИ деньги идёт правка, до ✅. Боевой — ⚠️, Draft — 🧪 (спокойнее),
    # так что «реальные деньги» остаётся отличимым сигналом; тихий фолбэк на Draft больше не невидим.
    banner_key = (
        "mutation_account_banner_draft"
        if customer_id == DRAFT_ACCOUNT_ID
        else "mutation_account_banner"
    )
    display = i18n.t(banner_key, lang, acct=account_label(customer_id)) + "\n\n" + display
    if external_context and operation in MONEY_OPS_UI:
        # Денежное предложение при внешнем контенте — усиленное предупреждение В СВОДКЕ (и в audit).
        display = i18n.t("external_context_money_warn", lang) + "\n\n" + display
    await store.save_proposal(
        confirmation_id=cid,
        operation=operation,
        customer_id=customer_id,  # штамп аккаунта мутации (authoritative для execute_confirmed)
        params=params,
        summary=display,
        chat_id=chat_id,
        user_initiated=user_initiated,
        attachment_state="pending" if spec is not None else None,
    )
    return BuiltProposal(
        cid=cid,
        operation=operation,
        customer_id=customer_id,
        params=params,
        display=display,
    )
