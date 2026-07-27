"""Поиск ключевых слов, которые БЛОКИРУЮТСЯ минус-словами (пересечения негативов и активных ключей).

Логика:
  - Читает ВСЕ минус-слова (уровни: кампания, группа, shared) через GAQL
  - Читает ВСЕ активные ключевые слова (ad_group_criterion с negative=FALSE, status!=REMOVED)
  - Находит пересечения: если минус-слово блокирует точное/фразовое совпадение активного ключа
  - Возвращает список dict'ов с risk-оценкой

Функция — чистая READ-логика (вызывается через run_ads_read_call).
"""

from __future__ import annotations

from typing import Any

from ads.client import ensure_read_allowed
from reports.queries import _search, _enum_name


def _keyword_text(key) -> str:
    """Извлечь текст ключевого слова из protobuf-объекта критерия."""
    return str(getattr(key, "text", "") or "")


def _keyword_match_type(key) -> str:
    """Извлечь тип соответствия как lower-case строку."""
    return (_enum_name(getattr(key, "match_type", None)) or "unknown").lower()


def _normalise(kw: str) -> str:
    """Нормализация минус-слова: убираем кавычки и операторы модификатора широкого.

    Google Ads хранит:
      - точное:  [some keyword]  → some keyword
      - фразовое: "some keyword"  → some keyword
      - широкое:  some keyword    → some keyword
      - модификатор широкого: +some +keyword  → some keyword

    В shared-списках минус-слова хранятся БЕЗ скобок, match_type отдельно.
    Однако на уровне ad_group_criterion.match_type уже корректный.
    Эта функция нормализует текст на всякий случай (удаляем синтаксические артефакты).
    """
    text = kw.strip()
    # Удаляем квадратные скобки [keyword]
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    # Удаляем кавычки "keyword"
    if (text.startswith('"') and text.endswith('"')) or (
        text.startswith("\u201c") and text.endswith("\u201d")
    ):
        text = text[1:-1]
    # Удаляем модификаторы широкого (+)
    words = text.split()
    words = [w.lstrip("+") for w in words]
    return " ".join(words)


def _overlaps(negative_text: str, negative_type: str, kw_text: str, kw_type: str) -> bool:
    """Проверяет, блокирует ли минус-слово активный ключ.

    Правила Google Ads:
      - Точное минус-слово → блокирует точное + фразовое совпадение с тем же текстом
      - Фразовое минус-слово → блокирует точное + фразовое + широкое, если минус-слово
        содержится как фраза в ключе
      - Широкое минус-слово → блокирует любой тип, если минус-слово содержится как
        компонент (семантически — но для простоты проверяем по подстроке/словам)

    Возвращает True если есть блокировка.
    """
    neg = _normalise(negative_text).casefold()
    kw = _normalise(kw_text).casefold()

    if not neg or not kw:
        return False

    if negative_type == "exact":
        # Точное минус-слово: блокирует ТОЛЬКО точное и фразовое совпадение ТОГО ЖЕ текста
        if kw == neg:
            return True
        # Для фразовых ключей: если ключ в кавычках — он фразовый, текст может совпадать
        if kw_type == "phrase" and kw == neg:
            return True
        return False

    if negative_type == "phrase":
        # Фразовое минус-слово: блокирует любой ключ, содержащий эту фразу
        # (кроме случаев, когда минус-слово — часть другого слова)
        # Для точности: проверяем вхождение neg как целой фразы (через split + поиск)
        kw_words = kw.split()
        neg_words = neg.split()
        if len(neg_words) > len(kw_words):
            return False
        # Ищем neg как последовательность в kw
        for i in range(len(kw_words) - len(neg_words) + 1):
            if kw_words[i : i + len(neg_words)] == neg_words:
                return True
        return False

    # Широкое/unknown: семантическое совпадение — сложно проверить без NLP.
    # Для простоты: проверяем что ВСЕ слова минус-слова присутствуют в ключе
    # (грубое приближение — не 100% точное, но safe side: скорее пере-блокируем)
    neg_words = set(neg.split())
    kw_words = set(kw.split())
    if neg_words and neg_words.issubset(kw_words):
        return True
    return False


def _risk_level(
    negative_scope: str,
    negative_match_type: str,
    blocked_match_type: str,
) -> str:
    """Оценка риска блокировки:
      - high: shared + блокирует точное совпадение
      - medium: shared + блокирует фразовое (или группа/кампания + точное)
      - low: всё остальное
    """
    is_shared = negative_scope == "shared"
    if is_shared and blocked_match_type == "exact":
        return "high"
    if is_shared and blocked_match_type == "phrase":
        return "medium"
    if not is_shared and blocked_match_type == "exact":
        return "medium"
    return "low"


def find_keyword_blockers(client, customer_id: str) -> list[dict[str, Any]]:
    """Найти активные ключи, которые БЛОКИРУЮТСЯ минус-словами аккаунта.

    Читает все минус-слова (кампания/группа/shared) + все активные ключи,
    находит пересечения и оценивает риск. READ-ONLY, замок чтения.

    Возвращает список dict'ов с полями:
      campaign, ad_group, negative_keyword, negative_match_type, negative_scope,
      blocked_keyword, blocked_match_type, risk
    """
    ensure_read_allowed(customer_id)
    cid = str(customer_id)

    # ── 1) Минус-слова уровня кампании ──────────────────────────────────────
    neg_campaign: list[dict[str, Any]] = []
    q_camp = (
        "SELECT campaign.name, campaign_criterion.negative, campaign_criterion.keyword.text, "
        "campaign_criterion.keyword.match_type FROM campaign_criterion "
        "WHERE campaign_criterion.type = 'KEYWORD' AND campaign_criterion.status != 'REMOVED' "
        "AND campaign_criterion.negative = TRUE"
    )
    for r in _search(client, cid, q_camp):
        neg_campaign.append({
            "scope": "campaign",
            "campaign": str(r.campaign.name),
            "text": _keyword_text(r.campaign_criterion.keyword),
            "match_type": _keyword_match_type(r.campaign_criterion.keyword),
        })

    # ── 2) Минус-слова уровня группы ────────────────────────────────────────
    neg_adgroup: list[dict[str, Any]] = []
    q_ag = (
        "SELECT campaign.name, ad_group.name, ad_group_criterion.keyword.text, "
        "ad_group_criterion.keyword.match_type FROM ad_group_criterion "
        "WHERE ad_group_criterion.type = 'KEYWORD' AND ad_group_criterion.status != 'REMOVED' "
        "AND ad_group_criterion.negative = TRUE"
    )
    for r in _search(client, cid, q_ag):
        neg_adgroup.append({
            "scope": "ad_group",
            "campaign": str(r.campaign.name),
            "ad_group": str(r.ad_group.name),
            "text": _keyword_text(r.ad_group_criterion.keyword),
            "match_type": _keyword_match_type(r.ad_group_criterion.keyword),
        })

    # ── 3) Минус-слова из shared-списков ────────────────────────────────────
    neg_shared: list[dict[str, Any]] = []
    q_shared = (
        "SELECT shared_set.name, shared_criterion.keyword.text, "
        "shared_criterion.keyword.match_type FROM shared_criterion "
        "WHERE shared_criterion.type = 'KEYWORD'"
    )
    for r in _search(client, cid, q_shared):
        neg_shared.append({
            "scope": "shared",
            "list_name": str(r.shared_set.name),
            "text": _keyword_text(r.shared_criterion.keyword),
            "match_type": _keyword_match_type(r.shared_criterion.keyword),
        })

    # Привязка shared-списков к кампаниям
    shared_to_campaigns: dict[str, set[str]] = {}
    q_att = (
        "SELECT campaign.name, shared_set.name, shared_set.type FROM campaign_shared_set "
        "WHERE campaign_shared_set.status = 'ENABLED'"
    )
    for r in _search(client, cid, q_att):
        if _enum_name(r.shared_set.type_) == "NEGATIVE_KEYWORDS":
            shared_to_campaigns.setdefault(str(r.shared_set.name), set()).add(
                str(r.campaign.name)
            )

    # ── 4) Активные ключевые слова ──────────────────────────────────────────
    active_keywords: list[dict[str, Any]] = []
    q_kw = (
        "SELECT campaign.name, ad_group.name, ad_group_criterion.keyword.text, "
        "ad_group_criterion.keyword.match_type FROM ad_group_criterion "
        "WHERE ad_group_criterion.type = 'KEYWORD' AND ad_group_criterion.status != 'REMOVED' "
        "AND ad_group_criterion.negative = FALSE"
    )
    for r in _search(client, cid, q_kw):
        active_keywords.append({
            "campaign": str(r.campaign.name),
            "ad_group": str(r.ad_group.name),
            "text": _keyword_text(r.ad_group_criterion.keyword),
            "match_type": _keyword_match_type(r.ad_group_criterion.keyword),
        })

    # ── 5) Поиск пересечений ────────────────────────────────────────────────
    blockers: list[dict[str, Any]] = []

    # 5а) Минус-слова кампании против ключей в той же кампании
    for neg in neg_campaign:
        for kw in active_keywords:
            if kw["campaign"] == neg["campaign"] and _overlaps(
                neg["text"], neg["match_type"], kw["text"], kw["match_type"]
            ):
                blockers.append({
                    "campaign": neg["campaign"],
                    "ad_group": kw["ad_group"],
                    "negative_keyword": neg["text"],
                    "negative_match_type": neg["match_type"],
                    "negative_scope": neg["scope"],
                    "blocked_keyword": kw["text"],
                    "blocked_match_type": kw["match_type"],
                    "risk": _risk_level(neg["scope"], neg["match_type"], kw["match_type"]),
                })

    # 5б) Минус-слова группы против ключей в той же группе
    for neg in neg_adgroup:
        for kw in active_keywords:
            if (
                kw["campaign"] == neg["campaign"]
                and kw["ad_group"] == neg["ad_group"]
                and _overlaps(neg["text"], neg["match_type"], kw["text"], kw["match_type"])
            ):
                blockers.append({
                    "campaign": neg["campaign"],
                    "ad_group": neg["ad_group"],
                    "negative_keyword": neg["text"],
                    "negative_match_type": neg["match_type"],
                    "negative_scope": neg["scope"],
                    "blocked_keyword": kw["text"],
                    "blocked_match_type": kw["match_type"],
                    "risk": _risk_level(neg["scope"], neg["match_type"], kw["match_type"]),
                })

    # 5в) Минус-слова shared-списков против ключей в привязанных кампаниях
    for neg in neg_shared:
        campaigns_with_list = shared_to_campaigns.get(neg["list_name"], set())
        for kw in active_keywords:
            if kw["campaign"] in campaigns_with_list and _overlaps(
                neg["text"], neg["match_type"], kw["text"], kw["match_type"]
            ):
                blockers.append({
                    "campaign": kw["campaign"],
                    "ad_group": kw["ad_group"],
                    "negative_keyword": neg["text"],
                    "negative_match_type": neg["match_type"],
                    "negative_scope": neg["scope"],
                    "blocked_keyword": kw["text"],
                    "blocked_match_type": kw["match_type"],
                    "risk": _risk_level(neg["scope"], neg["match_type"], kw["match_type"]),
                })

    return blockers