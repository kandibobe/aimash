"""Search-term mining 2.0: harvesting, conflicts, cannibalization and MCC themes."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from audit.terms import harvest, norm, tokens, waste_ngrams


@dataclass(frozen=True)
class KeywordRef:
    customer_id: str
    campaign: str
    ad_group: str
    text: str
    match_type: str


@dataclass(frozen=True)
class NegativeRef:
    customer_id: str
    scope: str
    scope_name: str
    text: str
    match_type: str


@dataclass(frozen=True)
class Conflict:
    keyword: KeywordRef
    negative: NegativeRef
    reason: str


@dataclass(frozen=True)
class Cannibalization:
    normalized_keyword: str
    placements: tuple[KeywordRef, ...]


def split_brand_terms(
    terms: Iterable[object], brand_terms: set[str]
) -> tuple[list[object], list[object]]:
    protected = {norm(x) for x in brand_terms if norm(x)}
    branded: list[object] = []
    non_branded: list[object] = []
    for row in terms:
        text = norm(getattr(row, "search_term", "") or "")
        target = branded if any(b in text for b in protected) else non_branded
        target.append(row)
    return branded, non_branded


def _blocks(keyword: str, negative: str, match_type: str) -> bool:
    kt, nt = tokens(keyword), tokens(negative)
    if not kt or not nt:
        return False
    match = match_type.upper()
    if match == "EXACT":
        return kt == nt
    if match == "PHRASE":
        size = len(nt)
        return any(kt[i : i + size] == nt for i in range(len(kt) - size + 1))
    if match == "BROAD":
        return set(nt) <= set(kt)
    return False


def detect_negative_conflicts(
    keywords: Iterable[KeywordRef], negatives: Iterable[NegativeRef]
) -> list[Conflict]:
    out: list[Conflict] = []
    negs = list(negatives)
    for keyword in keywords:
        for negative in negs:
            if keyword.customer_id != negative.customer_id:
                continue
            scope = negative.scope.lower()
            if scope == "campaign" and negative.scope_name != keyword.campaign:
                continue
            if scope == "ad_group" and negative.scope_name != keyword.ad_group:
                continue
            if _blocks(keyword.text, negative.text, negative.match_type):
                out.append(
                    Conflict(
                        keyword=keyword,
                        negative=negative,
                        reason=(
                            f"{negative.match_type.upper()} negative '{negative.text}' blocks "
                            f"active keyword '{keyword.text}'"
                        ),
                    )
                )
    return out


def detect_cannibalization(keywords: Iterable[KeywordRef]) -> list[Cannibalization]:
    groups: dict[tuple[str, str], list[KeywordRef]] = defaultdict(list)
    for item in keywords:
        normalized = norm(item.text)
        if normalized:
            groups[(item.customer_id, normalized)].append(item)
    out = []
    for (_customer, normalized), rows in groups.items():
        placements = {(row.campaign, row.ad_group) for row in rows}
        if len(placements) > 1:
            out.append(Cannibalization(normalized, tuple(rows)))
    return sorted(out, key=lambda item: (-len(item.placements), item.normalized_keyword))


def mine_account(
    search_terms: list,
    keywords: Iterable[KeywordRef],
    *,
    min_waste_cost: float = 10.0,
    min_conversions: float = 1.0,
) -> dict:
    refs = list(keywords)
    keyword_texts = [row.text for row in refs]
    return {
        "harvest": harvest(search_terms, keyword_texts, min_conv=min_conversions, top_n=100),
        "waste_themes": waste_ngrams(
            search_terms,
            keyword_texts,
            min_cost=min_waste_cost,
            min_terms=2,
            top_n=100,
        ),
        "cannibalization": detect_cannibalization(refs),
    }


def shared_mcc_waste_themes(
    by_customer: dict[str, list],
    keywords_by_customer: dict[str, list[str]],
    *,
    min_accounts: int = 2,
    min_cost_per_account: float = 10.0,
) -> list[dict]:
    if min_accounts < 2:
        raise ValueError("shared MCC themes require at least two accounts")
    merged: dict[str, dict] = {}
    for customer_id, rows in by_customer.items():
        themes = waste_ngrams(
            rows,
            keywords_by_customer.get(customer_id, []),
            min_cost=min_cost_per_account,
            min_terms=2,
            top_n=100,
        )
        for theme in themes:
            item = merged.setdefault(
                theme.text,
                {"text": theme.text, "accounts": set(), "cost": 0.0, "terms": 0},
            )
            item["accounts"].add(customer_id)
            item["cost"] += theme.cost
            item["terms"] += theme.terms
    out = []
    for item in merged.values():
        if len(item["accounts"]) >= min_accounts:
            out.append(
                {
                    **item,
                    "accounts": sorted(item["accounts"]),
                    "account_count": len(item["accounts"]),
                    "cost": round(item["cost"], 2),
                }
            )
    return sorted(out, key=lambda item: (-item["account_count"], -item["cost"]))
