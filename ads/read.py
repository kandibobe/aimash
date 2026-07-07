"""Чтение Google Ads через GAQL (GoogleAdsService.Search). READ-ONLY — ничего не меняет.

Сейчас используем ПАГИНИРОВАННЫЙ Search (`ga.search(...)`): объёмы тест-аккаунта малы, дневная
квота не является ограничением. SearchStream (страница до 10k строк = 1 операция против квоты) —
осознанно ОТЛОЖЕН как оптимизация под высокообъёмные боевые аккаунты: у него другая форма ответа
(батчи с `.results`), переключение ~15 точек чтения несёт риск регрессии и делается отдельно,
когда появятся реальные объёмы. См. docs/REPORTS.md (раздел «Квота/SearchStream»).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from google.ads.googleads.client import GoogleAdsClient

from ads.client import ensure_manager_allowed, ensure_read_allowed
from ads.resolve import gaql_escape
from core.limits import round_micros


@dataclass
class ChildAccount:
    id: str
    name: str
    currency: str
    manager: bool
    level: int
    status: str


@dataclass
class AccountStats:
    impressions: int
    clicks: int
    cost: float  # в валюте аккаунта (из micros)
    conversions: float
    conv_value: float


@dataclass
class Audience:
    resource_name: str
    name: str
    size: int  # размер аудитории для показа (user_list.size_for_display), 0 если неизвестно


def list_child_accounts(client: GoogleAdsClient, manager_id: str) -> list[ChildAccount]:
    """Обход иерархии MCC через customer_client. Чокпойнт: только настроенный MCC (fail-closed) —
    перечисление дочерних аккаунтов чужого менеджера запрещено (golden rule #9)."""
    ensure_manager_allowed(manager_id)
    ga = client.get_service("GoogleAdsService")
    q = (
        "SELECT customer_client.id, customer_client.descriptive_name, "
        "customer_client.currency_code, customer_client.manager, customer_client.level, "
        "customer_client.status FROM customer_client"
    )
    out: list[ChildAccount] = []
    for row in ga.search(customer_id=str(manager_id), query=q):
        cc = row.customer_client
        out.append(
            ChildAccount(
                id=str(cc.id),
                name=cc.descriptive_name,
                currency=cc.currency_code,
                manager=cc.manager,
                level=cc.level,
                status=cc.status.name,
            )
        )
    return out


def account_stats(
    client: GoogleAdsClient,
    customer_id: str,
    days: int = 30,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> AccountStats:
    """Сводная статистика по аккаунту за РЕАЛЬНЫЙ период N дней ИЛИ явный диапазон дат (C5:
    NL-отчёты «за вчера»/«с 1 по 15 июня» — date_from/date_to ISO побеждают days). Только для
    аккаунтов из белого списка.

    Окно строим из фактического N через `segments.date BETWEEN` (а не из пресета `LAST_N_DAYS`):
    раньше любой N кроме 7/14/30 молча схлопывался в 30, и подпись «N дн.» врала про объём данных
    (а это денежные метрики). end = вчера (как LAST_N_DAYS — без неполного «сегодня»); нормализация
    таймзон по дочерним аккаунтам — §8, отложена (один аккаунт → host-дата ок)."""
    ensure_read_allowed(customer_id)
    if date_from and date_to:
        start, end = date.fromisoformat(str(date_from)), date.fromisoformat(str(date_to))
        if end < start:
            start, end = end, start
    else:
        n = max(1, int(days))
        end = date.today() - timedelta(days=1)
        start = end - timedelta(days=n - 1)
    ga = client.get_service("GoogleAdsService")
    q = (
        "SELECT metrics.impressions, metrics.clicks, metrics.cost_micros, "
        "metrics.conversions, metrics.conversions_value "
        f"FROM customer WHERE segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"
    )
    imp = clk = cost = 0
    conv = cval = 0.0
    for row in ga.search(customer_id=str(customer_id), query=q):
        m = row.metrics
        imp += m.impressions
        clk += m.clicks
        cost += m.cost_micros
        conv += m.conversions
        cval += m.conversions_value
    return AccountStats(imp, clk, cost / 1_000_000, conv, cval)


_CURRENCY_CACHE: dict[
    str, str
] = {}  # customer_id → currency_code (валюта не меняется в рамках сессии)


def account_currency(client: GoogleAdsClient, customer_id: str) -> str:
    """Код валюты аккаунта (§9), напр. 'USD'/'UAH'. Один GAQL FROM customer, кэш по customer_id.
    Только для белого списка (замок аккаунта). '' если не удалось прочитать (вызывающий покажет
    метрики без явной валюты)."""
    ensure_read_allowed(customer_id)
    cid = str(customer_id)
    if cid in _CURRENCY_CACHE:
        return _CURRENCY_CACHE[cid]
    ga = client.get_service("GoogleAdsService")
    code = ""
    for row in ga.search(
        customer_id=cid, query="SELECT customer.currency_code FROM customer LIMIT 1"
    ):
        code = row.customer.currency_code
        break
    if code:
        _CURRENCY_CACHE[cid] = code  # кэшируем только успешное чтение
    return code


_TIMEZONE_CACHE: dict[str, str] = {}  # customer_id → IANA tz (не меняется в рамках сессии)


def account_timezone(client: GoogleAdsClient, customer_id: str) -> str:
    """§8: таймзона аккаунта (напр. 'Africa/Nairobi', 'Europe/Kyiv') — для нормализации окна
    отчёта по дочерним MCC (чтобы кросс-аккаунтный дайджест сравнивал ОДИН календарный день, а не
    смешивал неполные). Один GAQL FROM customer, кэш по customer_id. Замок чтения. '' если не
    прочитать (вызывающий откатится на host-дату)."""
    ensure_read_allowed(customer_id)
    cid = str(customer_id)
    if cid in _TIMEZONE_CACHE:
        return _TIMEZONE_CACHE[cid]
    ga = client.get_service("GoogleAdsService")
    tz = ""
    for row in ga.search(customer_id=cid, query="SELECT customer.time_zone FROM customer LIMIT 1"):
        tz = row.customer.time_zone
        break
    if tz:
        _TIMEZONE_CACHE[cid] = tz  # кэшируем только успешное чтение
    return tz


def clear_read_caches() -> None:
    """Сбросить кэши валют/таймзон аккаунтов (сессионные). Нужно, когда аккаунт стал доступен/сменил
    валюту/таймзону БЕЗ рестарта бота (команда /refresh) — иначе отдали бы устаревшее пустое значение."""
    _CURRENCY_CACHE.clear()
    _TIMEZONE_CACHE.clear()


def list_audiences(client: GoogleAdsClient, customer_id: str) -> list[Audience]:
    """Доступные аудитории аккаунта (user_list) для прикрепления к кампании (§3). Только белый
    список (замок аккаунта). Берём открытые для членства списки ремаркетинга/аудиторий."""
    ensure_read_allowed(customer_id)
    ga = client.get_service("GoogleAdsService")
    q = (
        "SELECT user_list.resource_name, user_list.name, user_list.size_for_display "
        "FROM user_list WHERE user_list.membership_status = 'OPEN' ORDER BY user_list.name"
    )
    out: list[Audience] = []
    for row in ga.search(customer_id=str(customer_id), query=q):
        ul = row.user_list
        out.append(
            Audience(
                resource_name=ul.resource_name,
                name=ul.name,
                size=int(getattr(ul, "size_for_display", 0) or 0),
            )
        )
    return out


def list_attached_audiences(
    client: GoogleAdsClient, customer_id: str, campaign_id: str
) -> list[Audience]:
    """C7: аудитории (user_list), УЖЕ прикреплённые к кампании, — для показа с кнопкой
    открепления (🗑 → черновик detach_audience за confirm-гейтом). READ-ONLY. Имена
    подтягиваем из list_audiences; нет в карте (напр. закрытый список) — честно показываем id."""
    ensure_read_allowed(customer_id)
    ga = client.get_service("GoogleAdsService")
    q = (
        "SELECT campaign_criterion.user_list.user_list FROM campaign_criterion "
        f"WHERE campaign.id = {int(campaign_id)} AND campaign_criterion.type = 'USER_LIST' "
        "AND campaign_criterion.status != 'REMOVED'"
    )
    rns: list[str] = []
    for row in ga.search(customer_id=str(customer_id), query=q):
        rn = str(row.campaign_criterion.user_list.user_list or "")
        if rn:
            rns.append(rn)
    if not rns:
        return []
    by_rn = {a.resource_name: a for a in list_audiences(client, customer_id)}
    return [
        by_rn.get(rn) or Audience(resource_name=rn, name=f"user_list …{rn[-6:]}", size=0)
        for rn in rns
    ]


def list_campaigns(
    client: GoogleAdsClient, customer_id: str, *, channel_type: str | None = None
) -> list[dict]:
    """Список кампаний аккаунта (id, имя, статус). Только для белого списка.

    channel_type (напр. 'SEARCH') — фильтр по типу канала: RSA-флоу показывает ТОЛЬКО Search-
    кампании (B2), иначе объявление уходило в не-Search группу → «operation not allowed for the
    given context» на создании. Пусто (дефолт) — все кампании (пикеры отчётов/экспорта).

    Порядок (B2/D3): активные (ENABLED) первыми, затем по имени — предсказуемо для человека
    (раньше ORDER BY campaign.id давал «случайный» для оператора порядок)."""
    ensure_read_allowed(customer_id)
    ga = client.get_service("GoogleAdsService")
    # 2.9: REMOVED-кампании — вон из WHERE (а не только в хвост сортировки): они засоряли пикеры
    # /report/RSA и «клонировать как в X» мёртвыми строками (как в read_campaign_config q2/q3).
    where = " WHERE campaign.status != 'REMOVED'"
    if channel_type:
        where += f" AND campaign.advertising_channel_type = '{channel_type}'"
    q = f"SELECT campaign.id, campaign.name, campaign.status FROM campaign{where}"
    rows = [
        {"id": str(r.campaign.id), "name": r.campaign.name, "status": r.campaign.status.name}
        for r in ga.search(customer_id=str(customer_id), query=q)
    ]
    return sorted(
        rows, key=lambda c: (_campaign_status_rank(c["status"]), (c["name"] or "").casefold())
    )


# Порядок статусов для пикеров: активные → приостановленные → прочие (REMOVED/UNKNOWN).
_CAMPAIGN_STATUS_ORDER = {"ENABLED": 0, "PAUSED": 1}


def _campaign_status_rank(status: str) -> int:
    return _CAMPAIGN_STATUS_ORDER.get((status or "").upper(), 2)


# ── §2A: чтение полного конфига кампании для клонирования («как в кампании X») ────────
@dataclass
class KeywordSeed:
    text: str
    match_type: str  # broad | phrase | exact (lower-case для схем create_search_campaign)


@dataclass
class AdGroupConfig:
    id: str
    name: str
    cpc_bid_micros: int
    keywords: list[KeywordSeed]
    headlines: list[str]
    descriptions: list[str]
    final_url: str
    path1: str
    path2: str


@dataclass
class CampaignConfig:
    id: str
    name: str
    status: str
    channel_type: str  # SEARCH | DISPLAY | … (клон-в-create_search_campaign только для SEARCH)
    budget_micros: int
    ad_groups: list[AdGroupConfig]


_MATCH_TYPE_MAP = {"BROAD": "broad", "PHRASE": "phrase", "EXACT": "exact"}


@dataclass
class CampaignTargeting:
    """Текущий таргетинг кампании для показа (§3 «чтение … ГЕО»). Пустые списки = «все регионы/
    языки» (кампания без критериев показывается всем) — это НЕ ошибка."""

    locations: list[str]  # позитивные LOCATION → человекочитаемые имена geo_target_constant
    negative_locations: list[str]  # исключённые LOCATION
    proximity: list[str]  # «Kyiv (UA), 30 км» / «lat,lng, 30 км»
    languages: list[str]  # имена language_constant


def read_campaign_targeting(
    client: GoogleAdsClient, customer_id: str, campaign_id: str | int
) -> CampaignTargeting:
    """Текущее ГЕО (локации/радиусы) и языки кампании (§3: чтение гео — раньше нигде не
    показывалось, только писалось). READ-ONLY, замок чтения. GAQL-веер: campaign_criterion →
    резолв имён geo_target_constant/language_constant (resource_name'ы приходят из API, но
    эскейпим как литералы — defense-in-depth). Вызывающий оборачивает в run_ads_read_call."""
    ensure_read_allowed(customer_id)
    cid = str(customer_id)
    ga = client.get_service("GoogleAdsService")
    q = (
        "SELECT campaign_criterion.type, campaign_criterion.negative, "
        "campaign_criterion.location.geo_target_constant, "
        "campaign_criterion.proximity.radius, campaign_criterion.proximity.radius_units, "
        "campaign_criterion.proximity.address.city_name, "
        "campaign_criterion.proximity.address.country_code, "
        "campaign_criterion.proximity.geo_point.latitude_in_micro_degrees, "
        "campaign_criterion.proximity.geo_point.longitude_in_micro_degrees, "
        "campaign_criterion.language.language_constant "
        "FROM campaign_criterion "
        f"WHERE campaign.id = {int(campaign_id)} "
        "AND campaign_criterion.type IN ('LOCATION', 'PROXIMITY', 'LANGUAGE') "
        "AND campaign_criterion.status != 'REMOVED'"
    )
    loc_rns: list[tuple[str, bool]] = []  # (resource_name, negative)
    lang_rns: list[str] = []
    proximity: list[str] = []
    for row in ga.search(customer_id=cid, query=q):
        crit = row.campaign_criterion
        tname = getattr(getattr(crit, "type_", None), "name", "") or str(getattr(crit, "type_", ""))
        if tname == "LOCATION":
            rn = str(getattr(crit.location, "geo_target_constant", "") or "")
            if rn:
                loc_rns.append((rn, bool(getattr(crit, "negative", False))))
        elif tname == "PROXIMITY":
            prox = crit.proximity
            radius = getattr(prox, "radius", 0)
            units = getattr(getattr(prox, "radius_units", None), "name", "") or ""
            unit_h = "км" if units == "KILOMETERS" else ("миль" if units == "MILES" else units)
            addr = getattr(prox, "address", None)
            city = str(getattr(addr, "city_name", "") or "") if addr is not None else ""
            country = str(getattr(addr, "country_code", "") or "") if addr is not None else ""
            gp = getattr(prox, "geo_point", None)
            if city:
                where = f"{city} ({country})" if country else city
            elif gp is not None and getattr(gp, "latitude_in_micro_degrees", 0):
                lat = int(gp.latitude_in_micro_degrees) / 1_000_000
                lng = int(gp.longitude_in_micro_degrees) / 1_000_000
                where = f"{lat:.4f},{lng:.4f}"
            else:
                where = "?"
            proximity.append(f"{where}, {radius:g} {unit_h}".strip())
        elif tname == "LANGUAGE":
            rn = str(getattr(crit.language, "language_constant", "") or "")
            if rn:
                lang_rns.append(rn)

    def _resolve_names(resource: str, field: str, rns: list[str]) -> dict[str, str]:
        """resource_name → человекочитаемое имя (одним IN-запросом; пусто → {})."""
        if not rns:
            return {}
        vals = ", ".join(f"'{gaql_escape(rn)}'" for rn in sorted(set(rns)))
        qq = (
            f"SELECT {resource}.resource_name, {resource}.name FROM {resource} "
            f"WHERE {resource}.resource_name IN ({vals})"
        )
        out: dict[str, str] = {}
        for row in ga.search(customer_id=cid, query=qq):
            node = getattr(row, field)
            out[str(node.resource_name)] = str(node.name)
        return out

    geo_names = _resolve_names(
        "geo_target_constant", "geo_target_constant", [r for r, _ in loc_rns]
    )
    lang_names = _resolve_names("language_constant", "language_constant", lang_rns)
    return CampaignTargeting(
        locations=[geo_names.get(rn, rn) for rn, neg in loc_rns if not neg],
        negative_locations=[geo_names.get(rn, rn) for rn, neg in loc_rns if neg],
        proximity=proximity,
        languages=[lang_names.get(rn, rn) for rn in lang_rns],
    )


def read_campaign_config(
    client: GoogleAdsClient, customer_id: str, campaign_name: str
) -> CampaignConfig | None:
    """Полный (клонируемый) конфиг кампании по ИМЕНИ. READ-ONLY, замок чтения (ensure_read_allowed).
    None — кампания не найдена. Google не отдаёт всё одной строкой → GAQL-веер: кампания+бюджет+
    канал; группы (имя/cpc); позитивные ключи по группам (текст+тип); RSA-тексты по группам
    (headlines/descriptions/final_url/path). Гео/минус-слова/стратегия/аудитории в КЛОН не
    переносятся (вызывающий честно сообщает); ПОКАЗ текущего гео/языков есть отдельно —
    read_campaign_targeting (§3)."""
    ensure_read_allowed(customer_id)
    cid = str(customer_id)
    ga = client.get_service("GoogleAdsService")
    safe = gaql_escape(campaign_name)

    # 1) база кампании + бюджет + тип канала
    base = None
    q1 = (
        "SELECT campaign.id, campaign.name, campaign.status, "
        "campaign.advertising_channel_type, campaign.campaign_budget, "
        "campaign_budget.amount_micros FROM campaign "
        f"WHERE campaign.name = '{safe}' LIMIT 1"
    )
    for row in ga.search(customer_id=cid, query=q1):
        base = row
        break
    if base is None:
        return None
    camp_id = int(base.campaign.id)

    # 2) группы объявлений (имя + cpc), без REMOVED
    groups: dict[str, AdGroupConfig] = {}
    order: list[str] = []
    q2 = (
        "SELECT ad_group.id, ad_group.name, ad_group.cpc_bid_micros FROM ad_group "
        f"WHERE campaign.id = {camp_id} AND ad_group.status != 'REMOVED' ORDER BY ad_group.id"
    )
    for row in ga.search(customer_id=cid, query=q2):
        ag_id = str(row.ad_group.id)
        groups[ag_id] = AdGroupConfig(
            id=ag_id,
            name=row.ad_group.name,
            cpc_bid_micros=int(row.ad_group.cpc_bid_micros or 0),
            keywords=[],
            headlines=[],
            descriptions=[],
            final_url="",
            path1="",
            path2="",
        )
        order.append(ag_id)

    # 3) позитивные ключи по группам (текст + тип соответствия)
    q3 = (
        "SELECT ad_group.id, ad_group_criterion.keyword.text, "
        "ad_group_criterion.keyword.match_type FROM ad_group_criterion "
        f"WHERE campaign.id = {camp_id} AND ad_group_criterion.type = 'KEYWORD' "
        "AND ad_group_criterion.negative = FALSE AND ad_group_criterion.status != 'REMOVED'"
    )
    for row in ga.search(customer_id=cid, query=q3):
        ag = groups.get(str(row.ad_group.id))
        if ag is None:
            continue
        mt = _MATCH_TYPE_MAP.get(row.ad_group_criterion.keyword.match_type.name, "phrase")
        ag.keywords.append(KeywordSeed(text=row.ad_group_criterion.keyword.text, match_type=mt))

    # 4) RSA-тексты по группам (первое RSA на группу — источник заголовков/описаний/URL)
    q4 = (
        "SELECT ad_group.id, ad_group_ad.ad.final_urls, "
        "ad_group_ad.ad.responsive_search_ad.headlines, "
        "ad_group_ad.ad.responsive_search_ad.descriptions, "
        "ad_group_ad.ad.responsive_search_ad.path1, "
        "ad_group_ad.ad.responsive_search_ad.path2 FROM ad_group_ad "
        f"WHERE campaign.id = {camp_id} AND ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD' "
        "AND ad_group_ad.status != 'REMOVED'"
    )
    for row in ga.search(customer_id=cid, query=q4):
        ag = groups.get(str(row.ad_group.id))
        if ag is None or ag.headlines:  # первый RSA на группу — остальные пропускаем
            continue
        rsa = row.ad_group_ad.ad.responsive_search_ad
        ag.headlines = [a.text for a in rsa.headlines if getattr(a, "text", "")]
        ag.descriptions = [a.text for a in rsa.descriptions if getattr(a, "text", "")]
        ag.path1 = getattr(rsa, "path1", "") or ""
        ag.path2 = getattr(rsa, "path2", "") or ""
        urls = list(row.ad_group_ad.ad.final_urls or [])
        ag.final_url = urls[0] if urls else ""

    return CampaignConfig(
        id=str(camp_id),
        name=base.campaign.name,
        status=base.campaign.status.name,
        channel_type=base.campaign.advertising_channel_type.name,
        budget_micros=int(base.campaign_budget.amount_micros or 0),
        ad_groups=[groups[a] for a in order],
    )


# ── §3-assets: список ассетов-расширений кампании (для показа/открепления) ────────────
@dataclass
class CampaignAssetRow:
    link_resource_name: str  # campaign_asset.resource_name — его и удаляют при откреплении
    field_type: str  # SITELINK | CALLOUT | STRUCTURED_SNIPPET | …
    label: str  # человекочитаемая подпись (link_text / callout_text / header+values / имя)


def list_campaign_assets(
    client: GoogleAdsClient, customer_id: str, campaign_id: str
) -> list[CampaignAssetRow]:
    """Ассеты-расширения кампании (campaign_asset + поля связанного asset). READ-ONLY, замок чтения.
    label собираем по field_type (sitelink link_text / callout_text / snippet header+values / имя)."""
    ensure_read_allowed(customer_id)
    cid = str(customer_id)
    ga = client.get_service("GoogleAdsService")
    q = (
        "SELECT campaign_asset.resource_name, campaign_asset.field_type, campaign_asset.asset, "
        "asset.type, asset.name, asset.sitelink_asset.link_text, asset.callout_asset.callout_text, "
        "asset.structured_snippet_asset.header, asset.structured_snippet_asset.values "
        f"FROM campaign_asset WHERE campaign.id = {int(campaign_id)} "
        "AND campaign_asset.status != 'REMOVED'"
    )
    out: list[CampaignAssetRow] = []
    for row in ga.search(customer_id=cid, query=q):
        ca = row.campaign_asset
        a = row.asset
        ft = ca.field_type.name
        if ft == "SITELINK":
            label = getattr(a.sitelink_asset, "link_text", "") or a.name
        elif ft == "CALLOUT":
            label = getattr(a.callout_asset, "callout_text", "") or a.name
        elif ft == "STRUCTURED_SNIPPET":
            ss = a.structured_snippet_asset
            vals = ", ".join(list(ss.values)[:5])
            label = f"{ss.header}: {vals}" if getattr(ss, "header", "") else (a.name or "")
        else:
            label = a.name or ft
        out.append(
            CampaignAssetRow(link_resource_name=ca.resource_name, field_type=ft, label=label)
        )
    return out


# ── §19.3: медианы прошлых Search-кампаний для «по аналогии» (Этап 1) ──────────────────
@dataclass
class AccountMedians:
    """Репрезентативные значения аккаунта для заполнения пропусков настроек «по аналогии».
    None ⇒ данных нет/чтение не удалось (вызывающий откатится на дефолты)."""

    median_daily_budget_micros: int | None
    avg_cpc_micros: int | None
    common_match_type: str | None  # exact|phrase|broad — самый частый позитивный тип (по кликам)


# Единый источник округления до биллинг-единицы — core.limits.round_micros (алиас сохраняет
# прежнее имя для call-sites и тестов). Медиана/среднее из истории (cost/clicks) обычно не кратны
# 10 000 micros — API отклонит такой бид/бюджет; округляем, чтобы превью «по аналогии» совпало
# с реально созданным.
_round_micros = round_micros


def _median_int(values: list[int]) -> int | None:
    vals = sorted(int(v) for v in values if v and int(v) > 0)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else int((vals[mid - 1] + vals[mid]) / 2)


def search_campaign_medians(
    client: GoogleAdsClient, customer_id: str, *, days: int = 90
) -> AccountMedians:
    """Медианы активных Search-кампаний аккаунта (§19.3 fallback «по аналогии»). READ-ONLY, замок
    чтения. Каждый под-запрос изолирован try/except: частичный сбой даёт None этого поля, не роняя
    остальные. Деньги — в micros (КОД). Пустой аккаунт (свежий тест-MCC) → все поля None."""
    ensure_read_allowed(customer_id)
    cid = str(customer_id)
    ga = client.get_service("GoogleAdsService")
    n = max(1, int(days))
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=n - 1)

    median_budget: int | None = None
    try:  # 1) бюджеты ENABLED Search-кампаний → медиана
        q = (
            "SELECT campaign_budget.amount_micros FROM campaign "
            "WHERE campaign.advertising_channel_type = 'SEARCH' "
            "AND campaign.status = 'ENABLED'"
        )
        amounts = [
            int(row.campaign_budget.amount_micros or 0)
            for row in ga.search(customer_id=cid, query=q)
        ]
        m = _median_int(amounts)
        median_budget = _round_micros(m) if m else m  # кратно биллинг-единице (превью = создание)
    except Exception:  # noqa: BLE001 — медианы advisory, поле остаётся None
        pass

    avg_cpc: int | None = None
    try:  # 2) суммарный CPC по Search-кампаниям за окно = cost/clicks
        q = (
            "SELECT metrics.cost_micros, metrics.clicks FROM campaign "
            "WHERE campaign.advertising_channel_type = 'SEARCH' "
            f"AND segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"
        )
        cost = clicks = 0
        for row in ga.search(customer_id=cid, query=q):
            cost += int(row.metrics.cost_micros or 0)
            clicks += int(row.metrics.clicks or 0)
        if clicks > 0:
            avg_cpc = _round_micros(
                int(cost / clicks)
            )  # кратно биллинг-единице (превью = создание)
    except Exception:  # noqa: BLE001
        pass

    match_type: str | None = None
    try:  # 3) самый частый позитивный тип соответствия (по ЧИСЛУ ключей).
        # ВАЖНО (v24): metrics.* НЕЛЬЗЯ селектить из ad_group_criterion (INCOMPATIBLE) — поэтому
        # без метрик, взвешиваем просто по количеству ключей каждого типа (проверено live на Draft).
        q = (
            "SELECT ad_group_criterion.keyword.match_type "
            "FROM ad_group_criterion "
            "WHERE ad_group_criterion.type = 'KEYWORD' "
            "AND ad_group_criterion.negative = FALSE"
        )
        weight: dict[str, int] = {}
        for row in ga.search(customer_id=cid, query=q):
            mt = row.ad_group_criterion.keyword.match_type.name
            if mt and mt not in ("UNSPECIFIED", "UNKNOWN"):
                weight[mt] = weight.get(mt, 0) + 1
        if weight:
            match_type = max(weight, key=lambda k: weight[k]).lower()
    except Exception:  # noqa: BLE001
        pass

    return AccountMedians(
        median_daily_budget_micros=median_budget,
        avg_cpc_micros=avg_cpc,
        common_match_type=match_type,
    )


# ── §19.7: переиспользуемые ассеты АККАУНТА (для «использовать текущие ассеты») ────────
@dataclass
class ReusableAsset:
    asset_resource_name: str  # сам ASSET (его линкуем к новой кампании, не link rn)
    field_type: str  # SITELINK | CALLOUT | STRUCTURED_SNIPPET | CALL | PRICE | PROMOTION | …
    label: str  # человекочитаемая подпись


def list_account_assets(client: GoogleAdsClient, customer_id: str) -> list[ReusableAsset]:
    """Существующие ассеты аккаунта, пригодные к переиспользованию в новой кампании (§19.7). READ-ONLY.

    Объединяем campaign_asset + customer_asset (поиск показывает на уровне кампаний и аккаунта),
    дедуп по (asset, field_type). Тянем поля популярных типов для подписи. Сбой одного источника не
    роняет другой (best-effort)."""
    ensure_read_allowed(customer_id)
    cid = str(customer_id)
    ga = client.get_service("GoogleAdsService")
    fields = (
        "asset.resource_name, asset.type, asset.name, "
        "asset.sitelink_asset.link_text, asset.callout_asset.callout_text, "
        "asset.structured_snippet_asset.header, asset.structured_snippet_asset.values, "
        "asset.call_asset.phone_number"
    )
    out: list[ReusableAsset] = []
    seen: set[tuple[str, str]] = set()

    def _ingest(rows, ft_getter) -> None:
        for row in rows:
            a = row.asset
            ft = ft_getter(row)
            if ft in ("UNSPECIFIED", "UNKNOWN", ""):
                continue
            key = (a.resource_name, ft)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                ReusableAsset(
                    asset_resource_name=a.resource_name, field_type=ft, label=_asset_label(ft, a)
                )
            )

    for src, ft_field in (
        ("campaign_asset", "campaign_asset"),
        ("customer_asset", "customer_asset"),
    ):
        try:
            q = f"SELECT {src}.field_type, {fields} FROM {src} WHERE {src}.status != 'REMOVED'"
            _ingest(
                ga.search(customer_id=cid, query=q),
                lambda row, _f=ft_field: getattr(getattr(row, _f), "field_type").name,
            )
        except Exception:  # noqa: BLE001 — один источник может быть недоступен; берём что есть
            continue
    return out


def _asset_label(field_type: str, a) -> str:
    """Подпись ассета по field_type (зеркалит list_campaign_assets)."""
    if field_type == "SITELINK":
        return getattr(a.sitelink_asset, "link_text", "") or a.name or "Sitelink"
    if field_type == "CALLOUT":
        return getattr(a.callout_asset, "callout_text", "") or a.name or "Callout"
    if field_type == "STRUCTURED_SNIPPET":
        ss = a.structured_snippet_asset
        vals = ", ".join(list(ss.values)[:5])
        return f"{ss.header}: {vals}" if getattr(ss, "header", "") else (a.name or "Snippet")
    if field_type == "CALL":
        return getattr(a.call_asset, "phone_number", "") or a.name or "Call"
    return a.name or field_type
