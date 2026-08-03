"""Bounded arXiv metadata ingestion and local text search."""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import and_, or_, select

from db.models import ResearchSource
from db.session import Session

ARXIV_API = "https://export.arxiv.org/api/query"
MAX_RESPONSE_BYTES = 2_000_000
MAX_QUERY_CHARS = 500
MAX_RESULTS = 25
_ATOM = "{http://www.w3.org/2005/Atom}"
_ID_RE = re.compile(r"/abs/([^?#]+?)(v\d+)?$")


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_arxiv_feed(raw: bytes) -> list[dict[str, Any]]:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("arXiv response exceeds size limit")
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("arXiv response contains forbidden XML declarations")
    root = ET.fromstring(raw)
    rows: list[dict[str, Any]] = []
    for entry in root.findall(f"{_ATOM}entry"):
        canonical_url = _clean(entry.findtext(f"{_ATOM}id"))
        match = _ID_RE.search(canonical_url)
        if not match:
            continue
        external_id, version = match.group(1), match.group(2) or "v1"
        links = {
            link.attrib.get("title") or link.attrib.get("rel"): link.attrib.get("href")
            for link in entry.findall(f"{_ATOM}link")
        }
        row = {
            "source_type": "arxiv",
            "external_id": external_id,
            "version": version,
            "title": _clean(entry.findtext(f"{_ATOM}title")),
            "abstract": _clean(entry.findtext(f"{_ATOM}summary")),
            "authors": [
                _clean(author.findtext(f"{_ATOM}name"))
                for author in entry.findall(f"{_ATOM}author")
            ],
            "categories": [
                category.attrib.get("term", "")
                for category in entry.findall(f"{_ATOM}category")
                if category.attrib.get("term")
            ],
            "canonical_url": canonical_url,
            "pdf_url": links.get("pdf"),
            "published_at": _parse_dt(entry.findtext(f"{_ATOM}published")),
            "source_updated_at": _parse_dt(entry.findtext(f"{_ATOM}updated")),
        }
        digest_payload = {key: value for key, value in row.items() if key != "source_updated_at"}
        row["content_digest"] = hashlib.sha256(
            json.dumps(digest_payload, default=str, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        rows.append(row)
    return rows


async def fetch_arxiv(query: str, limit: int) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0), follow_redirects=False) as client:
        async with client.stream(
            "GET",
            ARXIV_API,
            params={
                "search_query": query,
                "start": 0,
                "max_results": limit,
                "sortBy": "relevance",
                "sortOrder": "descending",
            },
            headers={"Accept": "application/atom+xml", "User-Agent": "Aimash/1.0 research-archive"},
        ) as response:
            response.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ValueError("arXiv response exceeds size limit")
                chunks.append(chunk)
        return parse_arxiv_feed(b"".join(chunks))


def _validate(query: str, limit: int) -> tuple[str, int]:
    query = _clean(query)
    limit = int(limit)
    if not query or len(query) > MAX_QUERY_CHARS:
        raise ValueError("query must contain 1..500 characters")
    if not 1 <= limit <= MAX_RESULTS:
        raise ValueError("limit must be between 1 and 25")
    return query, limit


async def import_arxiv(query: str, limit: int = 10) -> tuple[list[dict[str, Any]], int]:
    query, limit = _validate(query, limit)
    fetched = await fetch_arxiv(query, limit)
    fetched = list({row["content_digest"]: row for row in fetched}.values())
    async with Session() as session:
        if not fetched:
            return fetched, 0
        dialect = session.bind.dialect.name
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        else:  # pragma: no cover - production and tests use the branches above
            raise RuntimeError(f"unsupported research archive dialect: {dialect}")
        statement = (
            insert(ResearchSource)
            .values(fetched)
            .on_conflict_do_nothing(index_elements=["content_digest"])
            .returning(ResearchSource.content_digest)
        )
        inserted = len((await session.execute(statement)).scalars().all())
        await session.commit()
    return fetched, inserted


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def search(query: str, limit: int = 10) -> list[dict[str, Any]]:
    query, limit = _validate(query, limit)
    conditions = [
        or_(
            ResearchSource.title.ilike(f"%{_escape_like(term)}%", escape="\\"),
            ResearchSource.abstract.ilike(f"%{_escape_like(term)}%", escape="\\"),
        )
        for term in query.split()[:10]
    ]
    stmt = (
        select(ResearchSource)
        .where(and_(*conditions))
        .order_by(ResearchSource.published_at.desc(), ResearchSource.id.desc())
        .limit(limit)
    )
    async with Session() as session:
        sources = (await session.execute(stmt)).scalars().all()
    return [_public_row(source) for source in sources]


def _public_row(source: ResearchSource | dict[str, Any]) -> dict[str, Any]:
    value = source if isinstance(source, dict) else source.__dict__
    return {
        "source_type": value["source_type"],
        "external_id": value["external_id"],
        "version": value["version"],
        "title": value["title"],
        "abstract": value["abstract"],
        "authors": value["authors"],
        "categories": value["categories"],
        "canonical_url": value["canonical_url"],
        "pdf_url": value["pdf_url"],
        "published_at": value["published_at"].isoformat() if value["published_at"] else None,
    }


def public_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_public_row(row) for row in rows]
