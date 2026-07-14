"""Резолв кампании по имени → id/budget + пересчёт суммы бюджета. READ-ONLY (для оркестрации).

Модель даёт ИМЯ кампании, а SDK-мутации нужен id (и для процентов — текущий бюджет).
Здесь только чтение; запись — в ads/mutations за confirm-гейтом.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from google.ads.googleads.client import GoogleAdsClient

from ads.client import ensure_allowed
from core.limits import round_micros


@dataclass
class CampaignRef:
    id: str
    resource_name: str
    name: str
    status: str
    budget_resource: str
    budget_micros: int


@dataclass
class AdGroupRef:
    id: str
    resource_name: str
    name: str
    status: str
    cpc_bid_micros: int
    campaign_id: str
    # B2: тип группы (SEARCH_STANDARD/SEARCH_DYNAMIC_ADS/DISPLAY_STANDARD/…) и канал кампании
    # (SEARCH/DISPLAY/VIDEO/…). RSA валиден ТОЛЬКО в SEARCH_STANDARD внутри SEARCH-кампании —
    # иначе Google отвергает создание. Дефолты пустые: прочие вызовы (pause/bid) поля игнорируют.
    ad_group_type: str = ""
    channel_type: str = ""

    def accepts_rsa(self) -> bool:
        """True, если в эту группу можно добавить Responsive Search Ad (Search-стандарт)."""
        return (
            self.channel_type.upper() == "SEARCH"
            and self.ad_group_type.upper() == "SEARCH_STANDARD"
        )


def gaql_escape(value: str) -> str:
    """Экранирование строкового литерала для GAQL (предотвращает инъекцию в WHERE name = '...').
    ЕДИНЫЙ источник: используют и резолв по имени (user/model-вход), и mutations для literal-WHERE
    с resource_name (источник — Google API, но эскейпим единообразно, чтобы не было дыр-исключений)."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


_gaql_escape = gaql_escape  # обратная совместимость (имя могло использоваться извне)


def find_campaign_by_name(
    client: GoogleAdsClient, customer_id: str, name: str
) -> CampaignRef | None:
    ensure_allowed(customer_id)
    ga = client.get_service("GoogleAdsService")
    safe = gaql_escape(name)
    # status != REMOVED (A3): Google освобождает имя удалённой кампании — при тёзке-дубле
    # (удалили «Sale», создали новую «Sale») запрос без фильтра и без ORDER BY обычно вернул бы
    # СТАРШУЮ (удалённую) строку, и денежная команда «увеличь бюджет Sale» ушла бы в мёртвую сущность.
    q = (
        "SELECT campaign.id, campaign.name, campaign.status, campaign.campaign_budget, "
        "campaign_budget.amount_micros FROM campaign "
        f"WHERE campaign.name = '{safe}' AND campaign.status != 'REMOVED' LIMIT 1"
    )
    for row in ga.search(customer_id=str(customer_id), query=q):
        return CampaignRef(
            id=str(row.campaign.id),
            resource_name=row.campaign.resource_name,
            name=row.campaign.name,
            status=row.campaign.status.name,
            budget_resource=row.campaign.campaign_budget,
            budget_micros=row.campaign_budget.amount_micros,
        )
    return None


def campaign_network_settings(client: GoogleAdsClient, customer_id: str, name: str) -> dict | None:
    """Текущий тумблер поисковых партнёров кампании — для «было→станет» set_campaign_network
    (§19.3). READ-ONLY. None = кампания не найдена."""
    ensure_allowed(customer_id)
    ga = client.get_service("GoogleAdsService")
    safe = gaql_escape(name)
    q = (
        "SELECT campaign.id, campaign.network_settings.target_search_network FROM campaign "
        f"WHERE campaign.name = '{safe}' AND campaign.status != 'REMOVED' LIMIT 1"
    )
    for row in ga.search(customer_id=str(customer_id), query=q):
        return {
            "id": str(row.campaign.id),
            "search_partners": bool(row.campaign.network_settings.target_search_network),
        }
    return None


def campaign_display_network(client: GoogleAdsClient, customer_id: str, name: str) -> dict | None:
    """G12: включена ли КМС у кампании — для «было→станет» set_campaign_display_network.
    READ-ONLY. None = кампания не найдена."""
    ensure_allowed(customer_id)
    ga = client.get_service("GoogleAdsService")
    safe = gaql_escape(name)
    q = (
        "SELECT campaign.id, campaign.network_settings.target_content_network FROM campaign "
        f"WHERE campaign.name = '{safe}' AND campaign.status != 'REMOVED' LIMIT 1"
    )
    for row in ga.search(customer_id=str(customer_id), query=q):
        return {
            "id": str(row.campaign.id),
            "display_network": bool(row.campaign.network_settings.target_content_network),
        }
    return None


def campaign_geo_target_type(client: GoogleAdsClient, customer_id: str, name: str) -> dict | None:
    """G11: текущий тип гео-таргетинга — для «было→станет» set_campaign_geo_target_type.
    READ-ONLY. None = кампания не найдена. Пустая строка в geo_target_type = Google вернул
    UNSPECIFIED/UNKNOWN: «было» неизвестно, и врать о нём мы не будем (карточка покажет только
    «станет»)."""
    ensure_allowed(customer_id)
    ga = client.get_service("GoogleAdsService")
    safe = gaql_escape(name)
    q = (
        "SELECT campaign.id, campaign.geo_target_type_setting.positive_geo_target_type "
        f"FROM campaign WHERE campaign.name = '{safe}' AND campaign.status != 'REMOVED' LIMIT 1"
    )
    for row in ga.search(customer_id=str(customer_id), query=q):
        geo = row.campaign.geo_target_type_setting.positive_geo_target_type.name
        return {
            "id": str(row.campaign.id),
            "geo_target_type": "" if geo in ("UNSPECIFIED", "UNKNOWN") else geo,
        }
    return None


def campaign_bidding_strategy(client: GoogleAdsClient, customer_id: str, name: str) -> dict | None:
    """D6: текущий тип стратегии ставок кампании — для «было→станет» set_bidding_strategy. READ-ONLY.
    Возврат {"id", "strategy": <ENUM-имя, напр. MAXIMIZE_CONVERSIONS>}. None = кампания не найдена."""
    ensure_allowed(customer_id)
    ga = client.get_service("GoogleAdsService")
    safe = gaql_escape(name)
    q = (
        "SELECT campaign.id, campaign.bidding_strategy_type FROM campaign "
        f"WHERE campaign.name = '{safe}' AND campaign.status != 'REMOVED' LIMIT 1"
    )
    for row in ga.search(customer_id=str(customer_id), query=q):
        bst = getattr(row.campaign, "bidding_strategy_type", None)
        return {"id": str(row.campaign.id), "strategy": getattr(bst, "name", "") or str(bst or "")}
    return None


def find_ad_groups(
    client: GoogleAdsClient, customer_id: str, campaign_name: str
) -> list[AdGroupRef]:
    """Группы объявлений кампании (по имени кампании). Для bid (ставка) и add_keywords —
    обе операции живут на уровне ad group. READ-ONLY. Пустой список = у кампании нет групп
    (или кампания не найдена) — вызывающий код должен это обработать ДО любой записи."""
    ensure_allowed(customer_id)
    ga = client.get_service("GoogleAdsService")
    safe = gaql_escape(campaign_name)
    # status != REMOVED (A3): исключаем и удалённую кампанию-тёзку, и удалённые группы — иначе
    # bid/add_keywords могли бы адресовать мёртвую сущность (имена Google переиспользует).
    q = (
        "SELECT ad_group.id, ad_group.name, ad_group.status, ad_group.cpc_bid_micros, "
        "ad_group.resource_name, ad_group.type, campaign.id, "
        "campaign.advertising_channel_type FROM ad_group "
        f"WHERE campaign.name = '{safe}' AND campaign.status != 'REMOVED' "
        "AND ad_group.status != 'REMOVED' ORDER BY ad_group.id"
    )
    out: list[AdGroupRef] = []
    for row in ga.search(customer_id=str(customer_id), query=q):
        out.append(
            AdGroupRef(
                id=str(row.ad_group.id),
                resource_name=row.ad_group.resource_name,
                name=row.ad_group.name,
                status=row.ad_group.status.name,
                cpc_bid_micros=row.ad_group.cpc_bid_micros,
                campaign_id=str(row.campaign.id),
                # getattr с дефолтом: реальный SDK-enum несёт .name; тест-фейки/старые строки без
                # этих полей → пусто (accepts_rsa() тогда False, вызывающий покажет rsa_not_search).
                ad_group_type=getattr(getattr(row.ad_group, "type_", None), "name", "") or "",
                channel_type=getattr(
                    getattr(row.campaign, "advertising_channel_type", None), "name", ""
                )
                or "",
            )
        )
    return out


def find_ad_group_by_name(
    client: GoogleAdsClient, customer_id: str, campaign_name: str, ad_group_name: str
) -> AdGroupRef | None:
    """Конкретная группа объявлений по имени ВНУТРИ кампании (для pause/resume на уровне группы, §16).
    Реюз find_ad_groups + точное регистронезависимое совпадение имени. None — группа не найдена
    (вызывающий честно отвергает ДО любой записи). Имена групп в одной кампании обычно уникальны;
    при дубле берём первую по ORDER BY ad_group.id (детерминированно)."""
    target = (ad_group_name or "").strip().casefold()
    if not target:
        return None
    for ag in find_ad_groups(client, customer_id, campaign_name):
        if ag.name.casefold() == target:
            return ag
    return None


@dataclass
class KeywordRef:
    """Ключевое слово как КРИТЕРИЙ группы (для ставки на уровне ключа, Ф1). bid_micros — та ставка,
    от которой считается «было→станет»: собственная ставка критерия, если задана, иначе
    унаследованная от группы (effective_cpc_bid_micros — ровно то, что реально играет на аукционе)."""

    criterion_id: str
    ad_group_id: str
    ad_group: str
    campaign_id: str
    text: str
    match_type: str
    bid_micros: int
    own_bid: bool  # False = ставка унаследована от группы (у критерия своей нет)


def find_keywords(
    client: GoogleAdsClient,
    customer_id: str,
    campaign_name: str,
    keyword: str,
    ad_group_name: str | None = None,
    match_type: str | None = None,
) -> list[KeywordRef]:
    """Критерии-ключи кампании по ТЕКСТУ (для update_keyword_bid). READ-ONLY.

    Текст и тип матчим в Python (casefold), а не в GAQL: интерполировать пользовательский текст в
    WHERE — лишняя поверхность (в _remove_keywords_via_sdk сделано так же). negative = FALSE:
    минус-слово тоже type=KEYWORD, ставки у него нет — адресовать его ставкой нельзя.
    Порядок детерминирован (ORDER BY ad_group.id, criterion_id) — от него зависит сверка дрейфа.
    Пустой список = ключ не найден: вызывающий обязан отказать ДО любой записи."""
    ensure_allowed(customer_id)
    ga = client.get_service("GoogleAdsService")
    safe_c = gaql_escape(campaign_name)
    q = (
        "SELECT ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, "
        "ad_group_criterion.keyword.match_type, ad_group_criterion.cpc_bid_micros, "
        "ad_group_criterion.effective_cpc_bid_micros, ad_group.id, ad_group.name, campaign.id "
        "FROM ad_group_criterion "
        f"WHERE campaign.name = '{safe_c}' AND campaign.status != 'REMOVED' "
        "AND ad_group.status != 'REMOVED' AND ad_group_criterion.type = KEYWORD "
        "AND ad_group_criterion.negative = FALSE AND ad_group_criterion.status != 'REMOVED' "
        "ORDER BY ad_group.id, ad_group_criterion.criterion_id"
    )
    want_kw = (keyword or "").strip().casefold()
    want_ag = (ad_group_name or "").strip().casefold()
    want_mt = (match_type or "").strip().upper()
    out: list[KeywordRef] = []
    for row in ga.search(customer_id=str(customer_id), query=q):
        crit = row.ad_group_criterion
        if crit.keyword.text.casefold() != want_kw:
            continue
        mt = getattr(crit.keyword.match_type, "name", "") or str(crit.keyword.match_type or "")
        if want_mt and mt.upper() != want_mt:
            continue
        if want_ag and row.ad_group.name.casefold() != want_ag:
            continue
        own = int(getattr(crit, "cpc_bid_micros", 0) or 0)
        eff = int(getattr(crit, "effective_cpc_bid_micros", 0) or 0)
        out.append(
            KeywordRef(
                criterion_id=str(crit.criterion_id),
                ad_group_id=str(row.ad_group.id),
                ad_group=row.ad_group.name,
                campaign_id=str(row.campaign.id),
                text=crit.keyword.text,
                match_type=mt,
                bid_micros=own or eff,
                own_bid=bool(own),
            )
        )
    return out


@dataclass
class AdRef:
    """Ссылка на отдельное объявление (C6: pause/resume/remove_ad). headline — первый заголовок
    RSA (человекочитаемая метка; у не-RSA пусто)."""

    ad_id: str
    ad_group_id: str
    status: str
    headline: str


def find_ads_in_group(
    client: GoogleAdsClient, customer_id: str, campaign_name: str, ad_group_name: str, needle: str
) -> list[AdRef]:
    """Объявления группы, матчащие needle: только цифры → точный ad.id; иначе — подстрока
    первого заголовка RSA (casefold). READ-ONLY. Пустой needle → ВСЕ объявления группы
    (вызывающий покажет список с id). Несколько совпадений — вернём все (вызывающий просит
    уточнить id, ничего не угадываем — денежная поверхность)."""
    ensure_allowed(customer_id)
    ga = client.get_service("GoogleAdsService")
    safe_c, safe_g = gaql_escape(campaign_name), gaql_escape(ad_group_name)
    q = (
        "SELECT ad_group.id, ad_group_ad.ad.id, ad_group_ad.status, "
        "ad_group_ad.ad.responsive_search_ad.headlines FROM ad_group_ad "
        f"WHERE campaign.name = '{safe_c}' AND ad_group.name = '{safe_g}' "
        "AND ad_group_ad.status != 'REMOVED' ORDER BY ad_group_ad.ad.id"
    )
    want = (needle or "").strip()
    by_id = want.isdigit()
    out: list[AdRef] = []
    for row in ga.search(customer_id=str(customer_id), query=q):
        ad_id = str(row.ad_group_ad.ad.id)
        heads = getattr(row.ad_group_ad.ad.responsive_search_ad, "headlines", []) or []
        first = next((getattr(h, "text", "") for h in heads if getattr(h, "text", "")), "")
        if by_id:
            if ad_id != want:
                continue
        elif want and want.casefold() not in first.casefold():
            continue
        out.append(
            AdRef(
                ad_id=ad_id,
                ad_group_id=str(row.ad_group.id),
                status=row.ad_group_ad.status.name,
                headline=first,
            )
        )
    return out


_CURRENCY_HUMAN = {
    "USD": "USD",
    "UAH": "грн",
    "EUR": "EUR",
    "AUD": "AUD",
    "CZK": "Kč",
    "PLN": "zł",
    "GBP": "GBP",
}


# Валютные маркеры в СВОБОДНОМ тексте пользователя (коды/символы/слова RU+EN). Если пользователь
# валюту НЕ писал, а модель всё равно проставила currency (обычно 'USD') — код снимает её, чтобы
# голая цифра трактовалась в валюте аккаунта, а не рождала ложный currency_mismatch (петля
# «переформулируй в AUD»). Границы слова, чтобы 'usd' в 'thousand' не сматчилось случайно.
_CURRENCY_TOKEN_RE = re.compile(
    r"(?:(?<![a-zа-я])(?:usd|uah|eur|aud|czk|pln|gbp|cad|nzd|chf|jpy|"
    r"грн|гривн\w*|гривень|евро|долл\w*|долар\w*|бакс\w*|злот\w*|крон\w*|"
    r"dollars?|euros?|hryvnia|zloty|koruna)(?![a-zа-я]))"
    r"|[$€£¥₴]|zł|kč",
    re.IGNORECASE,
)


def detect_currency_token(text: str) -> bool:
    """True, если в тексте пользователя есть явный валютный маркер (код/символ/слово). Голую цифру
    без маркера трактуем в валюте аккаунта (golden rule #4: FX нет, «было→станет» правдив)."""
    return bool(_CURRENCY_TOKEN_RE.search(text or ""))


PERCENT_MONEY_MODES = ("increase_by_percent", "decrease_by_percent")

# Операции, у которых value — СУММА в валюте (а не процент/штука) ⇒ подлежат валютной сверке.
# Единый источник: currency_mismatch (ниже) и валютный гард на входе бота (bot.main._present_proposal).
# Раньше кортеж дублировался литералом в обоих местах — новая денежная операция молча теряла сверку.
MONEY_OPS = ("update_budget", "update_bid", "update_keyword_bid")


class DecreaseBelowZero(ValueError):
    """«Снизь бюджет на 200» при бюджете 100: результат ≤ 0 — не «обнуление», а невыполнимая команда.
    Отдельный тип, чтобы путь предпросмотра (ads.service.read_before) отличил её от сбоя чтения и
    ОТКАЗАЛ пользователю ДО кнопок ✅ (иначе падение случилось бы после подтверждения — claim сожжён,
    операция не применена). Текст сообщения формирует КОД (golden rule #4), не модель."""

    def __init__(self, mode: str, value: float, current_micros: int) -> None:
        self.mode, self.value, self.current_micros = mode, float(value), int(current_micros)
        cur = f"{int(current_micros) / 1_000_000:g}"
        how = f"на {value:g}%" if mode == "decrease_by_percent" else f"на {value:g}"
        super().__init__(
            f"Текущее значение — {cur} (в валюте аккаунта). Уменьшить {how} нельзя: получился бы "
            "ноль или минус. Уменьши на меньшую величину, задай новое значение («поставь бюджет N») "
            "или останови кампанию («пауза»)."
        )


def currency_mismatch(operation: str, params: dict, account_currency: str) -> str | None:
    """Текст-уточнение, если абсолютная денежная команда (set_to/increase_by_amount/
    decrease_by_amount) задана в валюте, отличной от валюты аккаунта. FX НЕ делаем — суммы считаются
    в валюте аккаунта (golden rule #4: «было→станет» обязан быть правдивым). None — расхождения нет /
    неприменимо.

    - Только update_budget/update_bid/update_keyword_bid; процентные режимы — без валюты.
    - currency не указана или 'percent' → трактуем как валюту аккаунта (расхождения нет).
    - валюта аккаунта неизвестна (read не удался, '') → не блокируем (деградация, не показываем чужую).
    """
    if operation not in MONEY_OPS:
        return None
    if params.get("mode") in PERCENT_MONEY_MODES:
        return None
    claimed = params.get("currency")
    if not claimed or claimed == "percent":
        return None
    acct = (account_currency or "").strip().upper()
    if not acct:
        return None
    if str(claimed).strip().upper() != acct:
        human = _CURRENCY_HUMAN.get(acct, acct)
        return (
            f"Вы указали сумму в {claimed}, а аккаунт ведётся в {human}. "
            f"Конвертацию валют я не делаю. Повторите сумму в {human} "
            f"(например «бюджет 100 {human}») — или пришлите просто число, и я приму его в {human}."
        )
    return None


def compute_new_micros(
    current_micros: int, mode: str, value: float, *, currency: str | None
) -> int:
    """Пересчёт бюджета/ставки в micros по режиму команды. Валюта не конвертируется (значение в
    валюте аккаунта).

    Результат ОКРУГЛЯЕТСЯ до кратного биллинг-единице ВАЛЮТЫ АККАУНТА (core.limits.round_micros):
    суммы, не кратные ей, Google Ads отклоняет (NON_MULTIPLE_OF_MINIMUM_CURRENCY_UNIT), а
    increase_by_percent почти всегда даёт не-кратное значение. Округление ЗДЕСЬ — единый источник:
    превью «станет» (ads.service.read_before) считается тем же вызовом, что и применяемое значение
    (execute_confirmed), поэтому карточка «было→станет» == реально применённому (как в create-путях).

    `currency` обязателен (может быть None = «не прочитали»): единица зависит от валюты (UGX/JPY —
    1 000 000, USD/EUR — 10 000), и валютно-слепое округление отвергалось API уже ПОСЛЕ «да».

    Направление несёт mode, value всегда > 0 (схема). decrease_* с результатом ≤ 0 →
    DecreaseBelowZero: команду НЕ выполняем и НЕ «клампим к минимуму» — 100 → 0.01 никто не просил."""
    if mode == "increase_by_percent":
        raw = int(round(current_micros * (1 + value / 100)))
    elif mode == "decrease_by_percent":
        raw = int(round(current_micros * (1 - value / 100)))
    elif mode == "increase_by_amount":
        raw = int(round(current_micros + value * 1_000_000))
    elif mode == "decrease_by_amount":
        raw = int(round(current_micros - value * 1_000_000))
    elif mode == "set_to":
        raw = int(round(value * 1_000_000))
    else:
        raise ValueError(f"неизвестный mode бюджета: {mode}")
    if raw <= 0:  # только decrease_*: «снизь бюджет на 200» при бюджете 100 — не 0, а бессмыслица
        raise DecreaseBelowZero(mode, value, int(current_micros))
    return round_micros(raw, currency=currency)
