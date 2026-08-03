"""Remove handoff test data from an Aimash database.

The scrubber deletes only an explicit allowlist of tables. It never uses TRUNCATE CASCADE,
so Alembic state and unrelated operational metadata cannot be removed implicitly.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import MetaData, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

ROOT = Path(__file__).resolve().parents[1]

# Child/value-dependent rows precede their parents. The live schema currently uses soft references
# for these relations, while the graph calculation below also honors real FKs added in the future.
TARGET_TABLES: tuple[str, ...] = (
    "client_dossiers",
    "client_site_pages",
    "client_profiles",
    "audit_log",
    "proposals",
)


def _deletion_order(foreign_keys: Mapping[str, Sequence[dict]]) -> list[str]:
    """Topologically order target children before target parents."""
    position = {name: index for index, name in enumerate(TARGET_TABLES)}
    edges = {name: set() for name in TARGET_TABLES}
    indegree = {name: 0 for name in TARGET_TABLES}
    for child, keys in foreign_keys.items():
        for key in keys:
            parent = key.get("referred_table")
            if child in edges and parent in edges and parent not in edges[child]:
                edges[child].add(parent)
                indegree[parent] += 1

    ready = sorted((name for name, degree in indegree.items() if degree == 0), key=position.get)
    order: list[str] = []
    while ready:
        child = ready.pop(0)
        order.append(child)
        for parent in sorted(edges[child], key=position.get):
            indegree[parent] -= 1
            if indegree[parent] == 0:
                ready.append(parent)
                ready.sort(key=position.get)
    if len(order) != len(TARGET_TABLES):
        raise RuntimeError("cyclic foreign keys exist between scrubber target tables")
    return order


def _inspect_schema(sync_connection) -> tuple[set[str], dict[str, list[dict]]]:
    inspector = inspect(sync_connection)
    tables = set(inspector.get_table_names())
    return tables, {name: inspector.get_foreign_keys(name) for name in tables}


def _find_external_references(
    foreign_keys: Mapping[str, Sequence[dict]],
) -> list[str]:
    references: list[str] = []
    targets = set(TARGET_TABLES)
    for child, keys in foreign_keys.items():
        if child in targets:
            continue
        for key in keys:
            parent = key.get("referred_table")
            if parent in targets:
                references.append(f"{child} -> {parent}")
    return sorted(references)


async def _reflect_targets(connection: AsyncConnection) -> dict[str, object]:
    metadata = MetaData()
    await connection.run_sync(
        lambda sync_connection: metadata.reflect(
            bind=sync_connection,
            only=list(TARGET_TABLES),
        )
    )
    return {name: metadata.tables[name] for name in TARGET_TABLES}


async def scrub_database(database_url: str, *, confirm: bool) -> dict[str, int]:
    url = make_url(database_url)
    safe_url = url.render_as_string(hide_password=True)
    print(f"Database: {safe_url}")
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            existing, foreign_keys = await connection.run_sync(_inspect_schema)
            missing = sorted(set(TARGET_TABLES) - existing)
            if missing:
                raise RuntimeError(f"required tables are missing: {', '.join(missing)}")
            external = _find_external_references(foreign_keys)
            if external:
                raise RuntimeError(
                    "non-target tables reference scrub targets; refusing implicit data loss: "
                    + ", ".join(external)
                )
            order = _deletion_order(foreign_keys)
            tables = await _reflect_targets(connection)

            if confirm and connection.dialect.name == "postgresql":
                await connection.execute(text("SET LOCAL lock_timeout = '5s'"))
                quote = connection.dialect.identifier_preparer.quote
                names = ", ".join(quote(name) for name in order)
                await connection.execute(text(f"LOCK TABLE {names} IN ACCESS EXCLUSIVE MODE"))

            counts = {
                name: int(
                    (
                        await connection.execute(select(func.count()).select_from(tables[name]))
                    ).scalar_one()
                )
                for name in order
            }
            print("Rows selected for deletion:")
            for name in order:
                print(f"  {name}: {counts[name]}")

            if not confirm:
                print("Dry run only; no rows deleted. Re-run with --confirm to execute.")
                return counts

            for name in order:
                await connection.execute(tables[name].delete())
            remaining = {
                name: int(
                    (
                        await connection.execute(select(func.count()).select_from(tables[name]))
                    ).scalar_one()
                )
                for name in order
            }
            if any(remaining.values()):
                raise RuntimeError(f"post-delete verification failed: {remaining}")
            print(f"Deleted {sum(counts.values())} rows in one transaction.")
            print("alembic_version and all non-target tables were preserved.")
            return counts
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="execute deletion; without it the script only prints row counts",
    )
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", override=False)
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        parser.error("DATABASE_URL is not set")
    asyncio.run(scrub_database(database_url, confirm=args.confirm))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
