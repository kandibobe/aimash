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
    q = (
        "SELECT campaign.id, campaign.name, campaign.status, campaign.campaign_budget, "
        "campaign_budget.amount_micros FROM campaign "
        f"WHERE campaign.name = '{safe}' LIMIT 1"
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
        f"WHERE campaign.name = '{safe}' LIMIT 1"
    )
    for row in ga.search(customer_id=str(customer_id), query=q):
        return {
            "id": str(row.campaign.id),
            "search_partners": bool(row.campaign.network_settings.target_search_network),
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
        f"WHERE campaign.name = '{safe}' LIMIT 1"
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
    q = (
        "SELECT ad_group.id, ad_group.name, ad_group.status, ad_group.cpc_bid_micros, "
        "ad_group.resource_name, ad_group.type, campaign.id, "
        "campaign.advertising_channel_type FROM ad_group "
        f"WHERE campaign.name = '{safe}' ORDER BY ad_group.id"
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


def currency_mismatch(operation: str, params: dict, account_currency: str) -> str | None:
    """Текст-уточнение, если абсолютная денежная команда (set_to/increase_by_amount) задана в валюте,
    отличной от валюты аккаунта. FX НЕ делаем — суммы считаются в валюте аккаунта (golden rule #4:
    «было→станет» обязан быть правдивым). None — расхождения нет / неприменимо.

    - Только update_budget/update_bid; процентный режим — без валюты (None).
    - currency не указана или 'percent' → трактуем как валюту аккаунта (расхождения нет).
    - валюта аккаунта неизвестна (read не удался, '') → не блокируем (деградация, не показываем чужую).
    """
    if operation not in ("update_budget", "update_bid"):
        return None
    if params.get("mode") == "increase_by_percent":
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


def compute_new_micros(current_micros: int, mode: str, value: float) -> int:
    """Пересчёт бюджета/ставки в micros по режиму команды. Валюта не конвертируется (значение в
    валюте аккаунта).

    Результат ОКРУГЛЯЕТСЯ до кратного BILLING_UNIT_MICROS (core.limits.round_micros): суммы, не
    кратные минимальной биллинг-единице, Google Ads отклоняет (NON_MULTIPLE_OF_MINIMUM_CURRENCY_UNIT),
    а increase_by_percent почти всегда даёт не-кратное значение. Округление ЗДЕСЬ — единый источник:
    превью «станет» (ads.service._read_before) считается тем же вызовом, что и применяемое значение
    (execute_confirmed), поэтому карточка «было→станет» == реально применённому (как в create-путях)."""
    if mode == "increase_by_percent":
        raw = int(round(current_micros * (1 + value / 100)))
    elif mode == "increase_by_amount":
        raw = int(round(current_micros + value * 1_000_000))
    elif mode == "set_to":
        raw = int(round(value * 1_000_000))
    else:
        raise ValueError(f"неизвестный mode бюджета: {mode}")
    return round_micros(raw)
