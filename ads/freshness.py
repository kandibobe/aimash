"""Freshness-контракт денежного пути (Волна 1.1): чем операция обязана доказать, что состояние
аккаунта читали ЖИВЬЁМ при показе карточки и что оно не разошлось к моменту исполнения.

Зачем отдельный модуль. Сегодня сверка дрейфа живёт одной функцией `ads.service._assert_no_drift`
и зовётся ровно из трёх мест (`ads/service.py:355`, `:583`, `:620`) — то есть 38 операций из 41
исполняются вообще без сверки. Хуже: сама функция самораспускается тремя `return` (`mode == "set_to"`,
нет `_before`, нет `before_micros`), а `read_before` глушит любое исключение и возвращает `None`
(`ads/service.py:286-287`) — «не смогли прочитать» неотличимо от «сравнивать нечего». Fail-open в трёх
слоях сразу, при том что правило 10 требует ровно обратного.

Конструкция — реестр, а не флаги. Каждая из 41 операции `ads.service.SUPPORTED_OPERATIONS` обязана
иметь тир; операция без тира — ОТКАЗ (deny-by-default), а не «наверное, ничего страшного». Постепенность
даётся тирами и храповиком `ADVISORY_DEBT`, который умеет только сокращаться. Env-флага отключения
гейта нет и не будет: флаг — это fail-open по конфигу.

Модуль намеренно ЧИСТЫЙ: без импортов из `ads`/`bot`/`db`. Его импортируют и `ads.service`, и
`ads.mutations` — любая зависимость обратно даст цикл.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# ── Тиры ──────────────────────────────────────────────────────────────────────────


class Tier(str, Enum):
    """Чего требует операция от снимка состояния.

    STRICT — снимок ОБЯЗАТЕЛЕН и сверяется; нет снимка или живьём не прочитали ⇒ отказ.
    ADVISORY — сверяем, если снимок есть И живое чтение удалось; иначе пропускаем с WARN.
               Это признанный долг (`ADVISORY_DEBT`), а не проектное «здесь можно без сверки».
    NO_DIFF — результат не зависит от прежнего состояния (создание сущности с нуля): сверять нечего
              ПО ПРИРОДЕ операции, а не потому что не смогли прочитать. Эти два случая обязаны
              различаться — их слипание и есть сегодняшняя дыра.
    """

    STRICT = "strict"
    ADVISORY = "advisory"
    NO_DIFF = "no_diff"


# ── Реестр: РОВНО 41 ключ = ads.service.SUPPORTED_OPERATIONS (инвариант в тестах) ─────
#
# STRICT сейчас: четыре денежные и шесть pause/resume.
#  · update_budget/update_bid/update_keyword_bid уже сверяются через `_assert_no_drift` — регрессии
#    в поведении нет, меняется только то, что «не прочитали» перестаёт быть молчаливым пропуском.
#  · set_bidding_strategy — отклонение от исходного проекта, где он оставался ADVISORY. Это одна из
#    восьми ДЕНЕЖНЫХ операций (`_require_user_command`), а деньги — ровно тот класс, где «не
#    прочитали ⇒ отказ» обязателен. Снимок (`kind="bidding"`) у него уже есть, стоимость нулевая.
#  · pause/resume — дрейф здесь значит «состояние изменил кто-то другой между показом и ✅»;
#    подтверждать человек имеет право только то, что видел.
#
# launch_campaign и add_keywords держим в ADVISORY ОСОЗНАННО до углубления снимков (шаг 6):
# у launch_campaign снимок сегодня — только статус кампании (`ads/service.py:151-155`), а исполнение
# трогает каждую не-REMOVED группу и каждое объявление; у add_keywords без `params["ad_group"]`
# группы резолвятся В МОМЕНТ ИСПОЛНЕНИЯ. Пометить их STRICT сейчас — выдать ложную аттестацию
# свежести, а это хуже отсутствия проверки: гард отрапортует «свежо», сверив не то.
FRESHNESS_TIERS: dict[str, Tier] = {
    # ── деньги: сверка обязательна ──
    "update_budget": Tier.STRICT,
    "update_bid": Tier.STRICT,
    "update_keyword_bid": Tier.STRICT,
    "set_bidding_strategy": Tier.STRICT,
    # ── статусы: подтверждается ровно то состояние, которое человек видел на карточке ──
    "pause_campaign": Tier.STRICT,
    "resume_campaign": Tier.STRICT,
    "pause_ad_group": Tier.STRICT,
    "resume_ad_group": Tier.STRICT,
    "pause_ad": Tier.STRICT,
    "resume_ad": Tier.STRICT,
    # ── долг: сверяем при возможности, но отсутствие снимка не блокирует (см. ADVISORY_DEBT) ──
    "launch_campaign": Tier.ADVISORY,
    "add_keywords": Tier.ADVISORY,
    "remove_keywords": Tier.ADVISORY,
    "add_negative_keywords": Tier.ADVISORY,
    "remove_negative_keywords": Tier.ADVISORY,
    "update_campaign": Tier.ADVISORY,
    "set_campaign_network": Tier.ADVISORY,
    "set_campaign_display_network": Tier.ADVISORY,
    "set_campaign_geo_target_type": Tier.ADVISORY,
    "remove_campaign": Tier.ADVISORY,
    "remove_ad_group": Tier.ADVISORY,
    "remove_ad": Tier.ADVISORY,
    "set_geo_location": Tier.ADVISORY,
    "set_geo_proximity": Tier.ADVISORY,
    "attach_audience": Tier.ADVISORY,
    "detach_audience": Tier.ADVISORY,
    "remove_asset_link": Tier.ADVISORY,
    # ── создание с нуля: прежнего состояния не существует ──
    "create_rsa": Tier.NO_DIFF,
    "create_gdn_campaign": Tier.NO_DIFF,
    "create_search_campaign": Tier.NO_DIFF,
    "create_demand_gen_campaign": Tier.NO_DIFF,
    "create_video_campaign": Tier.NO_DIFF,
    "add_sitelinks": Tier.NO_DIFF,
    "add_callouts": Tier.NO_DIFF,
    "add_structured_snippets": Tier.NO_DIFF,
    "attach_image_asset": Tier.NO_DIFF,
    "add_call_asset": Tier.NO_DIFF,
    "add_promotion": Tier.NO_DIFF,
    "add_price_asset": Tier.NO_DIFF,
    "add_negatives_to_shared_set": Tier.NO_DIFF,
    "attach_shared_set": Tier.NO_DIFF,
}


# Храповик. Множество ADVISORY обязано быть ПОДМНОЖЕСТВОМ этого набора (инвариант в тестах): операцию
# можно поднять из долга в STRICT, но нельзя тихо опустить в долг новую — для этого придётся руками
# расширить замороженный список, и это будет видно в диффе ревьюеру.
ADVISORY_DEBT: frozenset[str] = frozenset(
    {
        "launch_campaign",
        "add_keywords",
        "remove_keywords",
        "add_negative_keywords",
        "remove_negative_keywords",
        "update_campaign",
        "set_campaign_network",
        "set_campaign_display_network",
        "set_campaign_geo_target_type",
        "remove_campaign",
        "remove_ad_group",
        "remove_ad",
        "set_geo_location",
        "set_geo_proximity",
        "attach_audience",
        "detach_audience",
        "remove_asset_link",
    }
)


# ── Исключения ────────────────────────────────────────────────────────────────────
#
# Наследники RuntimeError, и это существенно: `core.ads_errors.is_outcome_unknown_after_mutate`
# уводит черновик в `needs_review` («результат неизвестен, сверьте в Google Ads») для TimeoutError и
# gapi-ошибок. Наш отказ происходит ДО вызова SDK — мутации не было. Увести его в needs_review значит
# сказать пользователю неправду о состоянии его аккаунта.
class FreshnessError(RuntimeError):
    """База: исполнение остановлено freshness-контрактом. SDK НЕ вызывался, аккаунт не изменён."""


class FreshnessMissing(FreshnessError):
    """Снимка нет, он не читался живьём, или тир операции неизвестен. Отсутствие данных = ОТКАЗ."""


class FreshnessDrift(FreshnessError):
    """Снимок есть, но состояние аккаунта разошлось с тем, что человек видел на карточке."""


# ── Сравнение ─────────────────────────────────────────────────────────────────────

# Ключи снимка, участвующие в сверке помимо всех `before_*`. `shared`/`shared_campaigns` — радиус
# общего бюджета (П1, `ads/service.py:138-149`): именно его человек видел на карточке и именно на него
# давал информированное согласие. Радиус расширился между показом и ✅ — согласие больше не покрывает
# операцию, даже если сама сумма не менялась.
_EXTRA_COMPARED = ("kind", "shared", "shared_campaigns")

# Списки, у которых порядок не несёт смысла. Гео-критерии читаются GAQL БЕЗ `ORDER BY`
# (`ads/read.py:335-348`), кампании общего бюджета — тоже; сверять их как последовательности значит
# ловить ложный дрейф на перестановке. У `before_micros` ровно наоборот: порядок идёт `ORDER BY id`
# и значим (см. `ads/service.py:303-305`) — сортировать его НЕЛЬЗЯ.
_ORDER_INSENSITIVE = ("shared_campaigns", "before_locations", "before_proximity")

# Человеческие ярлыки для текста отказа. Значения в сообщение НЕ подставляются: они содержат имена
# кампаний, то есть текст, который пишет клиент (правило 5 — наружу только редактированное).
_FIELD_LABELS = {
    "before_micros": "значение ставки/бюджета",
    "before_status": "статус",
    "before_name": "имя кампании",
    "before_search_partners": "поисковые партнёры",
    "before_display_network": "КМС",
    "before_geo_target_type": "тип гео-таргетинга",
    "before_locations": "гео-локации",
    "before_proximity": "радиусы",
    "before_strategy": "стратегия ставок",
    "shared": "общий бюджет",
    "shared_campaigns": "состав кампаний на общем бюджете",
    "kind": "тип снимка",
}


# ── Снимок и аттестация ───────────────────────────────────────────────────────────
#
# Словарь снимка живёт ЗДЕСЬ, а не рядом с ридером (`ads.service.read_state`), потому что читает
# маркер не только ридер: гейт A (`ads.mutations._require_freshness`) обязан понимать форму `_freshness`
# без импорта `ads.service` — тот сам импортирует `ads.mutations`, и обратная зависимость дала бы цикл.


class SnapshotKind(str, Enum):
    """Почему снимка нет — Волна 1.1. Три разных причины, которые `read_before` схлопывал в один
    `None`: «читать нечего по природе операции», «прочитали» и «не смогли прочитать». Первое и
    третье обязаны различаться — их слипание и есть fail-open: freshness-гард не может отличить
    «сверять нечего» от «сверка не состоялась»."""

    OK = "ok"
    NO_DIFF = "no_diff"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Результат чтения состояния. `data` заполнен только при `kind is OK`."""

    kind: SnapshotKind
    data: dict | None = None
    reason: str = ""


# Снимок и его аттестация. Пара служебная и ХОД-СПЕЦИФИЧНАЯ: она описывает состояние аккаунта в
# момент показа ЭТОЙ карточки. Любой путь, который переиспользует params позже (шаблон, повтор из
# /recent, клон), обязан её снять — иначе новый черновик унаследует чужое «прочитано» и проедет гейт
# без единого чтения. Это ровно тот класс дыр, который гейт и закрывает, поэтому снятие вынесено
# в общий хелпер, а не повторяется списками ключей по местам.
ATTESTATION_KEYS: frozenset[str] = frozenset({"_before", "_freshness"})


def attach_freshness(params: dict, snap: Snapshot) -> dict:
    """Прикрепить к черновику снимок И аттестацию того, ЧЕМ он является.

    `_before` кладётся как раньше — только при успешном чтении. Новое здесь `_freshness`: он есть
    ВСЕГДА, в том числе когда снимка нет. Черновик без маркера freshness-гейт обязан считать
    непрочитанным (fail-closed), поэтому «забыли прикрепить» и «не смогли прочитать» дают один и тот
    же исход — отказ на STRICT-операции, а не тихий пропуск.

    Оба ключа инертны для исполнения: `apply_*` читают свои ключи, а `db/history.py:_INERT_KEYS`
    вычищает служебные из повтора `/recent`."""
    out = {**params, "_freshness": {"kind": snap.kind.value, "reason": snap.reason}}
    if snap.kind is SnapshotKind.OK and snap.data is not None:
        out["_before"] = snap.data  # инертно для execute (apply_* читают свои ключи)
    return out


def strip_attestation(params: dict) -> dict:
    """params без снимка и аттестации свежести — для переиспользования в ДРУГОМ ходе."""
    return {k: v for k, v in (params or {}).items() if k not in ATTESTATION_KEYS}


def attested_snapshot(params: dict) -> dict | None:
    """Снимок карточки, если он ДОКАЗАН аттестацией; иначе None — единственный способ прочитать
    `_before`, которым пользуются оба гейта.

    Раздельное чтение `_before` и `_freshness` — это две трактовки одних данных в двух местах, и
    расходятся они молча: `_before` без маркера (или с маркером `unreadable`) выглядит как валидный
    снимок ровно до тех пор, пока кто-то не забудет проверить второй ключ. Здесь пара читается
    атомарно: снимок засчитывается, только если аттестация говорит `ok` и данные действительно есть."""
    if not isinstance(params, dict):
        return None
    marker = params.get("_freshness")
    before = params.get("_before")
    if not isinstance(marker, dict) or marker.get("kind") != SnapshotKind.OK.value:
        return None
    return before if isinstance(before, dict) else None


def _marker(params: dict) -> dict:
    m = params.get("_freshness") if isinstance(params, dict) else None
    return m if isinstance(m, dict) else {}


def marker_kind(params: dict) -> str:
    """`kind` аттестации (`ok`/`no_diff`/`unreadable`) или `""`, если маркера нет вовсе. Нужен там,
    где важно ОТЛИЧИТЬ сорвавшееся чтение от структурного «сверять нечего»: первое — сигнал (WARN),
    второе — норма (INFO)."""
    return str(_marker(params).get("kind") or "")


def absence_reason(params: dict) -> str:
    """Почему снимка нет — для текста отказа и лога. Строка ТЕХНИЧЕСКАЯ (`no_marker`, `not_diffable`,
    имя исключения ридера): значений снимка в ней нет, наружу идёт только она (правило 5)."""
    m = _marker(params)
    return str(m.get("reason") or m.get("kind") or "no_marker")


def tier_of(operation: str) -> Tier:
    """Тир операции. Неизвестная операция — ОТКАЗ (deny-by-default): новая мутация, забытая в реестре,
    обязана упереться в гейт, а не проехать как «сверять нечего»."""
    tier = FRESHNESS_TIERS.get(operation)
    if tier is None:
        raise FreshnessMissing(
            f"операция '{operation}' не объявлена в реестре freshness — выполнение отклонено"
        )
    return tier


def _compared_keys(params: dict, stored: dict, live: dict) -> list[str]:
    """Ключи сверки: объединение ОБОИХ снимков, а не только сохранённого. Иначе появление ключа
    (например, бюджет СТАЛ общим уже после показа карточки) прошло бы незамеченным."""
    keys = {k for k in (*stored, *live) if k.startswith("before_") or k in _EXTRA_COMPARED}
    if params.get("mode") == "set_to" and stored.get("kind") in ("budget", "bid", "keyword_bid"):
        # Абсолютный режим: итог не зависит от базы (`resolve.compute_new_micros`), а ВСЕ откаты
        # `_reverse_spec` (`bot/main.py:1509-1548`) строятся именно как set_to. Блокировать их при
        # дрейфе — сломать ровно тот путь, которым человек восстанавливается ПОСЛЕ дрейфа.
        keys.discard("before_micros")
    return sorted(keys)


def _norm(key: str, value: object) -> object:
    """Канонизация перед сравнением. Все снимки — плоские: скаляры и списки строк/чисел (JSON-round-trip
    через `proposals.params` их не меняет), поэтому канонизация нужна ровно одна — порядок."""
    if key in _ORDER_INSENSITIVE and isinstance(value, list):
        return sorted(str(x) for x in value)
    return value


def compare(operation: str, params: dict, stored: dict, live: dict) -> None:
    """Сверить снимок карточки с живым состоянием. Расхождение ⇒ `FreshnessDrift`.

    Генерик, без рукописного компаратора на операцию: 41 операция × рукописный компаратор — это 41
    место, где можно ошибиться, и ни одного структурного гарда, что все написаны. Сверяются `kind`,
    радиус общего бюджета и ВСЕ ключи с префиксом `before_`; `after_*` не сверяются никогда — это
    вычисленное из `params` «станет», а не состояние аккаунта. Новый ключ снимка попадает под сверку
    автоматически, как только ридер начинает его отдавать."""
    diffs = [
        _FIELD_LABELS.get(key, key)
        for key in _compared_keys(params, stored, live)
        if _norm(key, stored.get(key)) != _norm(key, live.get(key))
    ]
    if diffs:
        raise FreshnessDrift(
            f"состояние изменилось с момента показа черновика ({', '.join(diffs)}) — операция "
            f"'{operation}' отклонена, создай черновик заново, чтобы увидеть актуальное «было → станет»"
        )
