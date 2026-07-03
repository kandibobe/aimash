"""Резолв кампании по имени → id/budget + пересчёт суммы бюджета. READ-ONLY (для оркестрации).

Модель даёт ИМЯ кампании, а SDK-мутации нужен id (и для процентов — текущий бюджет).
Здесь только чтение; запись — в ads/mutations за confirm-гейтом.
"""

from __future__ import annotations

from dataclasses import dataclass

from google.ads.googleads.client import GoogleAdsClient

from ads.client import ensure_allowed


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


_CURRENCY_HUMAN = {"USD": "USD", "UAH": "грн", "EUR": "EUR"}


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
            f"Сумма указана в {claimed}, а аккаунт ведётся в {human}. "
            f"Конвертацию валют не делаю — переформулируй сумму в {human}."
        )
    return None


def compute_new_micros(current_micros: int, mode: str, value: float) -> int:
    """Пересчёт бюджета в micros по режиму команды. Валюта не конвертируется (значение в валюте аккаунта)."""
    if mode == "increase_by_percent":
        return int(round(current_micros * (1 + value / 100)))
    if mode == "increase_by_amount":
        return int(round(current_micros + value * 1_000_000))
    if mode == "set_to":
        return int(round(value * 1_000_000))
    raise ValueError(f"неизвестный mode бюджета: {mode}")
